"""Execution and crash recovery tests with durable SQLite and a simulated venue."""
import copy
import tempfile
import time
import unittest
from pathlib import Path

from bot.config import Config
from bot.engine import Coordinator
from bot.exchange import ExchangeError, UnknownExecution
from bot.models import Advice, Candle, Snapshot
from bot.storage import Store


class NeutralContext:
    def analyze(self, snapshot):
        return Advice("context", 0, False, "test context")


def snapshot(price=100):
    now = time.time()
    end = int(now // 300) * 300000
    candles = [Candle(end-(240-i)*300000, end-(239-i)*300000-1,
                      price-0.1, price+0.3, price-0.3, price, 10000) for i in range(240)]
    return Snapshot("ETHUSDT", candles, price, price, 1_000_000_000, now)


class FakeExchange:
    def __init__(self):
        self.price = 100.0
        self.markets, self.cancelled_stops, self.cancelled_orders = [], [], []
        self.orders, self.fills, self.stops = {}, {}, {}
        self.amount, self.wallet = 0.0, 1000.0
        self.entry_price = 0
        self.unknown_after_fill = False
        self.stop_failure = False
        self.partial = False
        self.cancel_exit = False
        self.income_rows, self.income_starts = [], []
        self.before_write = None

    def _fill(self, side, quantity, cid, reduce_only=False, status="FILLED"):
        order_id = len(self.orders) + 1
        actual_qty = quantity / 2 if self.partial and not reduce_only else quantity
        if status == "CANCELED":
            actual_qty = 0
        if actual_qty:
            change = actual_qty * (1 if side == "BUY" else -1)
            if reduce_only:
                if self.amount * change >= 0 or actual_qty > abs(self.amount) + 1e-9:
                    raise ExchangeError("reduceOnly rejected", -2022)
                self.wallet += actual_qty * (self.price-self.entry_price) * (1 if self.amount > 0 else -1)
            else:
                self.entry_price = self.price
            self.amount += change
        fee = actual_qty * self.price * .0005
        self.wallet -= fee
        result = dict(symbol="ETHUSDT", clientOrderId=cid, orderId=order_id,
                      status="PARTIALLY_FILLED" if self.partial and not reduce_only else status,
                      executedQty=str(actual_qty), avgPrice=str(self.price))
        self.orders[cid] = result
        self.fills[order_id] = [dict(orderId=order_id, qty=str(actual_qty), price=str(self.price),
                                    commission=str(fee), commissionAsset="USDT")] if actual_qty else []
        return result

    def place_market(self, symbol, side, quantity, client_order_id, reduce_only=False):
        if self.before_write:
            self.before_write(client_order_id)
        self.markets.append(dict(symbol=symbol, side=side, quantity=quantity,
                                 client_order_id=client_order_id, reduce_only=reduce_only))
        result = self._fill(side, quantity, client_order_id, reduce_only,
                            "CANCELED" if reduce_only and self.cancel_exit else "FILLED")
        if self.unknown_after_fill:
            self.unknown_after_fill = False
            raise UnknownExecution("timeout after accepted order")
        return result

    def get_order(self, symbol, cid):
        return self.orders.get(cid)

    def trades(self, symbol, order_id):
        return self.fills[int(order_id)]

    def protection_stop(self, symbol, side, stop_price, client_order_id):
        if self.stop_failure:
            raise ExchangeError("Stop refused", -2021)
        algo = dict(symbol=symbol, algoId=len(self.stops)+1, clientAlgoId=client_order_id,
                    algoStatus="NEW", side=side, triggerPrice=str(stop_price), actualOrderId="0")
        self.stops[algo["algoId"]] = algo
        return algo

    def cancel_stop(self, symbol, algo_id):
        self.cancelled_stops.append(algo_id)
        self.stops[algo_id]["algoStatus"] = "CANCELED"

    def get_stop(self, symbol, algo_id):
        return self.stops[algo_id]

    def get_stop_by_client_id(self, symbol, cid):
        return next((s for s in self.stops.values() if s["clientAlgoId"] == cid), None)

    def open_stops(self, symbol):
        return [s for s in self.stops.values() if s["algoStatus"] == "NEW"]

    def cancel_order(self, symbol, cid):
        self.cancelled_orders.append(cid)
        self.orders[cid]["status"] = "CANCELED"
        return self.orders[cid]

    def account(self):
        unrealized = self.amount * (self.price-self.entry_price)
        return dict(totalWalletBalance=str(self.wallet), availableBalance=str(self.wallet),
                    totalMarginBalance=str(self.wallet+unrealized))

    def positions(self, symbol=None):
        return [dict(symbol="ETHUSDT", positionAmt=str(self.amount), positionSide="BOTH")] if self.amount else []

    def income(self, start):
        self.income_starts.append(start)
        return [r for r in self.income_rows if r["time"] >= start]

    def snapshot(self, symbol, interval="5m", limit=240):
        return snapshot(self.price)

    def premium_index(self, symbol):
        return dict(markPrice=str(self.price), lastFundingRate="0", nextFundingTime=int((time.time()+3600)*1000))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"state.sqlite3")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.client = FakeExchange()

    def coordinator(self, mode="demo"):
        config = Config(mode=mode, symbols=("ETHUSDT",), news_enabled=False)
        return Coordinator(config, self.store, self.client, NeutralContext())

    def entry(self, quantity=1, side="BUY"):
        return dict(role="entry", symbol="ETHUSDT", side=side, quantity=quantity,
                    plan=dict(stop_distance=2, risk_usdt=.5))

    def open(self, engine, side="BUY"):
        self.assertTrue(engine._submit(self.entry(side=side), snapshot()))
        engine._protect("ETHUSDT", snapshot())

    def test_uncertain_order_is_journaled_before_send_and_restart_never_duplicates(self):
        engine = self.coordinator()
        self.client.before_write = lambda cid: self.assertIn(cid, self.store.load()["pending"])
        self.client.unknown_after_fill = True
        self.assertFalse(engine._submit(self.entry(), snapshot()))
        self.assertEqual(len(engine.state["pending"]), 1)
        recovered = self.coordinator()
        recovered.reconcile_pending()
        recovered._protect("ETHUSDT", snapshot())
        self.assertEqual(len(self.client.markets), 1)
        self.assertEqual(recovered.state["positions"]["ETHUSDT"]["quantity"], 1)
        self.assertFalse(recovered.state["pending"])
        self.coordinator().reconcile_pending()
        self.assertEqual(len(self.client.markets), 1)
        self.assertEqual(len(self.client.stops), 1)

    def test_unlocated_uncertain_order_stays_pending_without_resubmit(self):
        engine = self.coordinator()
        self.client.unknown_after_fill = True
        engine._submit(self.entry(), snapshot())
        self.client.orders.clear()
        self.coordinator().reconcile_pending()
        self.assertEqual(len(self.client.markets), 1)
        self.assertEqual(len(self.store.load()["pending"]), 1)

    def test_stop_failure_emergency_exit_is_reduce_only(self):
        engine = self.coordinator()
        self.client.stop_failure = True
        engine._submit(self.entry(), snapshot())
        engine._protect("ETHUSDT", snapshot())
        self.assertFalse(engine.state["positions"])
        self.assertEqual(len(self.client.markets), 2)
        self.assertEqual(self.client.markets[-1]["side"], "SELL")
        self.assertTrue(self.client.markets[-1]["reduce_only"])
        self.assertAlmostEqual(engine.state["balance"], 99.9)
        self.assertIsNotNone(engine.state["halt_reason"])

    def test_short_exit_buys_same_contract_quantity_reduce_only(self):
        engine = self.coordinator()
        self.open(engine, "SELL")
        engine.exit_position("ETHUSDT", snapshot(), "test")
        self.assertEqual(self.client.markets[-1]["side"], "BUY")
        self.assertEqual(self.client.markets[-1]["quantity"], 1)
        self.assertTrue(self.client.markets[-1]["reduce_only"])
        self.assertEqual(self.client.cancelled_stops, [1])

    def test_canceled_zero_fill_exit_preserves_position_stop(self):
        engine = self.coordinator()
        self.open(engine)
        self.client.cancel_exit = True
        engine.exit_position("ETHUSDT", snapshot(), "test")
        self.assertIn("ETHUSDT", engine.state["positions"])
        self.assertEqual(self.client.cancelled_stops, [])
        self.assertEqual(self.client.stops[1]["algoStatus"], "NEW")

    def test_partial_entry_is_protected_and_final_consumption_is_incremental(self):
        engine = self.coordinator()
        self.client.partial = True
        self.assertFalse(engine._submit(self.entry(), snapshot()))
        cid = next(iter(engine.state["pending"]))
        self.assertEqual(engine.state["positions"]["ETHUSDT"]["quantity"], .5)
        self.assertEqual(len(self.client.stops), 1)
        recovered = self.coordinator()
        recovered.reconcile_pending()
        self.assertAlmostEqual(recovered.state["balance"], 99.975)
        self.assertEqual(len(self.client.stops), 1)
        order = self.client.orders[cid]
        order.update(status="FILLED", executedQty="1")
        self.client.fills[order["orderId"]] = [dict(qty="1", price="100", commission=".05", commissionAsset="USDT")]
        self.client.amount = 1
        recovered.reconcile_pending()
        self.assertEqual(recovered.state["positions"]["ETHUSDT"]["quantity"], 1)
        self.assertAlmostEqual(recovered.state["balance"], 99.95)
        self.assertEqual(len(self.client.markets), 1)
        self.assertEqual(len(self.client.stops), 1)
        self.assertFalse(recovered.state["pending"])

    def test_partial_entry_stop_failure_cancels_remainder_then_flattens(self):
        engine = self.coordinator()
        self.client.partial = self.client.stop_failure = True
        engine._submit(self.entry(), snapshot())
        self.assertEqual(len(self.client.cancelled_orders), 1)
        self.assertFalse(engine.state["positions"])
        self.assertFalse(engine.state["pending"])
        self.assertEqual(self.client.markets[-1]["quantity"], .5)
        self.assertTrue(self.client.markets[-1]["reduce_only"])

    def test_filled_unknown_stop_recovered_by_client_id_after_crash(self):
        engine = self.coordinator()
        engine.state["demo_wallet_offset"] = 900
        self.open(engine)
        stop = self.client.stops[1]
        engine.state["positions"]["ETHUSDT"]["stop_id"] = None
        engine.save()
        self.client.price = 98
        result = self.client._fill("SELL", 1, "exchange-stop", True)
        stop.update(algoStatus="FINISHED", actualOrderId=str(result["orderId"]))
        recovered = self.coordinator()
        recovered._sync_demo()
        self.assertFalse(recovered.state["positions"])
        self.assertEqual(len(self.client.markets), 1)
        self.assertEqual(recovered.state["trades"][0]["reason"], "exchange_stop")
        self.assertAlmostEqual(recovered.state["balance"], 97.901)

    def test_dedicated_baseline_rejects_preexisting_position(self):
        engine = self.coordinator()
        self.client.amount = 1
        with self.assertRaises(ExchangeError):
            engine._sync_demo()
        self.assertIsNone(engine.state["demo_wallet_offset"])

    def test_position_mismatch_blocks_writes(self):
        engine = self.coordinator()
        engine.state["demo_wallet_offset"] = 900
        self.open(engine)
        self.client.amount = -.5
        engine._sync_demo()
        engine.exit_position("ETHUSDT", snapshot(), "risk_stop")
        self.assertEqual(len(self.client.markets), 1)
        self.assertTrue(engine.state["positions"]["ETHUSDT"]["sync_mismatch"])

    def test_late_funding_is_assigned_to_closed_trade_once_across_restart(self):
        engine = self.coordinator()
        self.open(engine)
        p = engine.state["positions"]["ETHUSDT"]
        p["opened_at"] -= 10
        funding_time = int((time.time()-5)*1000)
        engine.state["created_at"] -= 20
        engine.exit_position("ETHUSDT", snapshot(), "target")
        before = engine.state["trades"][0]["net_pnl"]
        self.client.income_rows = [dict(tranId=7, incomeType="COMMISSION", income="-.05", asset="USDT", time=funding_time+1000)]
        engine._sync_income()
        self.client.income_rows.append(dict(tranId=8, incomeType="FUNDING_FEE", income="-.01", asset="USDT", symbol="ETHUSDT", time=funding_time))
        engine._sync_income()
        self.assertAlmostEqual(engine.state["trades"][0]["funding"], -.01)
        self.assertAlmostEqual(engine.state["trades"][0]["net_pnl"], before-.01)
        recovered = self.coordinator()
        recovered._sync_income()
        self.assertAlmostEqual(recovered.state["actual_funding_total"], -.01)
        self.assertAlmostEqual(recovered.state["trades"][0]["net_pnl"], before-.01)

    def test_bad_later_income_row_does_not_partially_commit_funding(self):
        engine = self.coordinator()
        self.open(engine)
        engine.state["created_at"] -= 20
        engine.state["positions"]["ETHUSDT"]["opened_at"] -= 10
        stamp = int((time.time()-5)*1000)
        self.client.income_rows = [dict(tranId=1, incomeType="FUNDING_FEE", income="-.01", asset="USDT", symbol="ETHUSDT", time=stamp),
                                   dict(tranId=2, incomeType="FUNDING_FEE", income="NaN", asset="USDT", symbol="ETHUSDT", time=stamp+1)]
        with self.assertRaises(ValueError):
            engine._sync_income()
        self.assertEqual(engine.state["positions"]["ETHUSDT"]["funding"], 0)
        self.assertNotIn("actual_funding_total", engine.state)
        # The runner saves state on errors; that save must not make the first row double-count.
        engine.save("cycle_error", {})
        recovered = self.coordinator()
        self.client.income_rows.pop()
        recovered._sync_income()
        self.assertAlmostEqual(recovered.state["actual_funding_total"], -.01)

    def test_crash_after_partial_stop_intent_cancels_remainder_and_flattens(self):
        engine = self.coordinator()
        self.client.partial = True
        engine._submit(self.entry(), snapshot())
        self.client.stops.clear()
        engine.state["positions"]["ETHUSDT"]["stop_id"] = None
        engine.save()
        recovered = self.coordinator()
        recovered._protect("ETHUSDT", snapshot())
        self.assertFalse(recovered.state["positions"])
        self.assertFalse(recovered.state["pending"])
        self.assertEqual(len(self.client.cancelled_orders), 1)
        self.assertTrue(self.client.markets[-1]["reduce_only"])

    def test_paper_round_trip_accounts_spread_slippage_fees_and_funding(self):
        engine = self.coordinator("paper")
        engine._submit(self.entry(), snapshot())
        p = engine.state["positions"]["ETHUSDT"]
        entry_price = 100 * 1.0005
        self.assertAlmostEqual(p["entry_price"], entry_price)
        p["funding"] = -.01
        engine.state["balance"] -= .01
        engine.exit_position("ETHUSDT", snapshot(110), "target")
        exit_price = 110 * .9995
        fees = (entry_price+exit_price) * .0005
        expected = exit_price-entry_price-fees-.01
        self.assertAlmostEqual(engine.state["trades"][0]["net_pnl"], expected)
        self.assertAlmostEqual(engine.state["balance"], 100+expected)
        self.assertFalse(self.client.markets)

    def test_paper_cycle_uses_closed_240_candles_and_halted_state_never_enters(self):
        engine = self.coordinator("paper")
        engine.state["halt_reason"] = "test pause"
        self.assertEqual(len(snapshot().candles), 240)
        result = engine.cycle()
        self.assertFalse(result["positions"])
        self.assertFalse(self.client.markets)
        self.assertGreater(result["heartbeat"], 0)


if __name__ == "__main__":
    unittest.main()
