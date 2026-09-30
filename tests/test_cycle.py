import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from bot.config import Config
from bot.engine import Coordinator
from bot.exchange import ExchangeError
from bot.models import Advice, Candle, Snapshot
from bot.storage import Store


NOW = (1_788_739_200 // 300) * 300 + 1


def snapshot(symbol="DOGEUSDT", price=100, *, direction=0):
    boundary = int(NOW - 1) * 1000
    bars = []
    for index in range(240):
        close = price + direction * (index - 239) * .1
        start = boundary - (240 - index) * 300_000
        bars.append(Candle(start, start + 299_999, close, close + .5, close - .5, close, 1000))
    return Snapshot(symbol, bars, price, price, 20_000_000, NOW)


class FakeClient:
    def __init__(self, symbols=("DOGEUSDT",)):
        self.snapshots = {symbol: snapshot(symbol) for symbol in symbols}
        self.premiums = {symbol: {"symbol": symbol, "markPrice": "100", "lastFundingRate": "0", "nextFundingTime": int((NOW+3600)*1000), "time": int(NOW*1000)} for symbol in symbols}
        self.quantity_factor = 1.0
        self.calls = []

    def snapshot(self, symbol, interval, limit):
        self.calls.append(("snapshot", symbol))
        result = self.snapshots[symbol]
        if isinstance(result, Exception):
            raise result
        return result

    def premium_index(self, symbol):
        self.calls.append(("premium", symbol))
        result = self.premiums[symbol]
        if isinstance(result, Exception):
            raise result
        return result

    def normalize_quantity(self, symbol, quantity, price):
        return quantity * self.quantity_factor


class CycleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = patch("bot.engine.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.stores = []
        self.addCleanup(lambda: [store.close() for store in self.stores])

    def coordinator(self, symbols=("DOGEUSDT",), *, mode="paper", name="state", direction=1, **config_changes):
        config = Config(mode=mode, symbols=symbols, fee_rate=0, slippage_bps=0,
                        news_enabled=False, **config_changes)
        store = Store(Path(self.directory.name) / (name + ".sqlite3"))
        self.stores.append(store)
        client = FakeClient(symbols)
        context = Mock()
        context.analyze.return_value = Advice("market_context", 0)
        bot = Coordinator(config, store, client, context)
        bot.trend.analyze = Mock(return_value=Advice("trend", direction))
        bot.momentum.analyze = Mock(return_value=Advice("momentum", direction, details={"atr": 1}))
        return bot

    def hold(self, bot, symbol="DOGEUSDT", direction=1, quantity=.1, opened_at=NOW-400):
        bot._record_entry({"symbol": symbol, "side": "BUY" if direction == 1 else "SELL", "created_at": opened_at, "plan": {"stop_distance": 2, "risk_usdt": quantity*2}}, quantity, 100, 0)
        bot.save()
        return bot.state["positions"][symbol]

    def last_bar(self, bot, *, symbol="DOGEUSDT", **changes):
        snap = bot.client.snapshots[symbol]
        bot.client.snapshots[symbol] = replace(snap, candles=snap.candles[:-1] + [replace(snap.candles[-1], **changes)])

    def test_paper_entry_both_directions_and_exactly_five_reports(self):
        for direction in (1, -1):
            bot = self.coordinator(direction=direction, name=str(direction))
            bot.cycle()
            self.assertEqual(bot.state["positions"]["DOGEUSDT"]["direction"], direction)
            report = next(event["data"] for event in bot.store.events() if event["kind"] == "analysis")
            self.assertEqual([item["agent"] for item in report["agents"]], ["trend", "momentum", "liquidity", "market_context", "risk"])
            self.assertTrue(report["allowed"])
            self.assertEqual(report["strategy_profile"], "trend")
            self.assertEqual(report["buy_decision"], "COMPRAR" if direction == 1 else "NO_COMPRAR")
            self.assertEqual(bot.state["last_analysis"]["DOGEUSDT"]["execution"]["status"], "filled")
            self.assertTrue(bot.state["healthy"])
            self.assertEqual(bot.state["positions"]["DOGEUSDT"]["next_funding_time"], int((NOW+3600)*1000))

    def test_long_only_profile_blocks_short_entry(self):
        bot = self.coordinator(direction=-1, allow_shorts=False)
        bot.cycle()
        self.assertFalse(bot.state["positions"])
        report = next(event["data"] for event in bot.store.events() if event["kind"] == "analysis")
        self.assertFalse(report["allowed"])
        self.assertEqual(report["execution"]["reason"], "Shorts deshabilitados para este perfil")
        self.assertEqual(report["buy_decision"], "NO_COMPRAR")

    def test_disagreeing_momentum_blocks_entry(self):
        bot = self.coordinator()
        bot.momentum.analyze.return_value = Advice("momentum", -.1, details={"atr": 1})
        bot.cycle()
        self.assertFalse(bot.state["positions"])

    def test_same_or_older_candle_is_not_reprocessed_after_restart(self):
        bot = self.coordinator(direction=0)
        bot.cycle()
        restarted = Coordinator(bot.c, bot.store, bot.client, bot.context)
        restarted.trend.analyze = Mock(side_effect=AssertionError("Duplicate candle analyzed"))
        restarted.cycle()
        self.assertEqual(len([event for event in bot.store.events(100) if event["kind"] == "analysis"]), 1)

    def test_stop_wins_when_both_stop_and_target_occur_in_same_bar(self):
        for direction in (1, -1):
            bot = self.coordinator(direction=direction, name="both"+str(direction))
            self.hold(bot, direction=direction)
            self.last_bar(bot, high=105, low=95)
            bot.cycle()
            self.assertFalse(bot.state["positions"])
            self.assertEqual(len(bot.state["trades"]), 1)
            self.assertEqual(bot.state["trades"][0]["reason"], "stop_loss")
            self.assertEqual(bot.state["trades"][0]["exit_price"], 100-direction*2)
            # Exiting a candle also durably consumes it, avoiding instant reentry.
            restarted = Coordinator(bot.c, bot.store, bot.client, bot.context)
            restarted.cycle()
            self.assertFalse(restarted.state["positions"])

    def test_stop_gap_fills_at_worse_bar_open_for_long_and_short(self):
        for direction, opening in ((1, 97), (-1, 103)):
            bot = self.coordinator(direction=direction, name="gap"+str(direction))
            self.hold(bot, direction=direction)
            self.last_bar(bot, open=opening, high=opening+1, low=opening-1, close=opening)
            bot.cycle()
            self.assertEqual(bot.state["trades"][0]["exit_price"], opening)
            self.assertLess(bot.state["trades"][0]["net_pnl"], -.2)

    def test_target_only_bar_records_take_profit(self):
        bot = self.coordinator()
        self.hold(bot)
        self.last_bar(bot, low=99, high=105)
        bot.cycle()
        self.assertEqual(bot.state["trades"][0]["reason"], "take_profit")
        self.assertEqual(bot.state["trades"][0]["exit_price"], 104)

    def test_candle_that_started_before_entry_is_not_replayed(self):
        bot = self.coordinator()
        self.hold(bot, opened_at=NOW-100)
        self.last_bar(bot, high=105, low=95)
        bot.cycle()
        self.assertTrue(bot.state["positions"])
        self.assertFalse(bot.state["trades"])

    def test_mark_triggers_stop_and_gap_uses_current_quote(self):
        bot = self.coordinator()
        self.hold(bot)
        bot.client.premiums["DOGEUSDT"]["markPrice"] = "97"
        bot.client.snapshots["DOGEUSDT"] = replace(bot.client.snapshots["DOGEUSDT"], bid=96.9, ask=97)
        bot.cycle()
        self.assertEqual(bot.state["trades"][0]["exit_price"], 96.9)

    def test_stale_nonfinite_and_future_data_prevent_entries(self):
        for index, alteration in enumerate(({"fetched_at": NOW-91}, {"fetched_at": math.nan}, {"fetched_at": NOW+20}, {"bid": 0})):
            bot = self.coordinator(name="stale"+str(index))
            bot.client.snapshots["DOGEUSDT"] = replace(bot.client.snapshots["DOGEUSDT"], **alteration)
            bot.cycle()
            self.assertFalse(bot.state["positions"])
            self.assertFalse(bot.state["healthy"])
            self.assertTrue(bot.state["market_errors"])

    def test_old_candle_and_stale_mark_prevent_entries(self):
        for index, kind in enumerate(("candle", "mark")):
            bot = self.coordinator(name="age"+str(index))
            if kind == "candle":
                snap = bot.client.snapshots["DOGEUSDT"]
                bot.client.snapshots["DOGEUSDT"] = replace(snap, candles=snap.candles[:-2])
            else:
                bot.client.premiums["DOGEUSDT"]["time"] = int((NOW-91)*1000)
            bot.cycle()
            self.assertFalse(bot.state["positions"])
            self.assertFalse(bot.state["healthy"])

    def test_error_in_other_symbol_does_not_prevent_a_paper_stop(self):
        bot = self.coordinator(symbols=("SOLUSDT", "DOGEUSDT"))
        self.hold(bot)
        self.last_bar(bot, low=97)
        bot.client.snapshots["SOLUSDT"] = ExchangeError("Offline symbol")
        bot.client.premiums["SOLUSDT"] = ExchangeError("Offline mark")
        bot.cycle()
        self.assertFalse(bot.state["positions"])
        self.assertEqual(bot.state["trades"][0]["reason"], "stop_loss")
        self.assertFalse(bot.state["healthy"])

    def test_demo_protection_runs_even_when_another_symbol_fails(self):
        bot = self.coordinator(symbols=("SOLUSDT", "DOGEUSDT"), mode="demo")
        position = self.hold(bot)
        bot._sync_demo = Mock()
        def protect(symbol, snap):
            position["stop_id"] = 17
            bot.client.calls.append(("protect", symbol))
        bot._protect = Mock(side_effect=protect)
        bot.client.open_stops = Mock(return_value=[{"algoId": 17}])
        bot.client.snapshots["SOLUSDT"] = ExchangeError("Unrelated symbol unavailable")
        bot.cycle()
        bot._protect.assert_called_once_with("DOGEUSDT", None)
        self.assertEqual(bot.client.calls[0], ("protect", "DOGEUSDT"))
        self.assertFalse(bot.state["healthy"])

    def test_mark_price_drives_equity_and_reserved_open_risk(self):
        bot = self.coordinator(symbols=("DOGEUSDT", "SOLUSDT"))
        position = self.hold(bot, quantity=1)
        position["target"] = 200
        bot.client.premiums["DOGEUSDT"]["markPrice"] = "110"
        bot.context.analyze.return_value = Advice("market_context", 0, veto=True)
        bot.cycle()
        self.assertEqual(bot.state["equity"], 110)
        self.assertEqual(position["risk_usdt"], 12)
        report = next(event["data"] for event in bot.store.events() if event["kind"] == "analysis")
        risk = next(item for item in report["agents"] if item["agent"] == "risk")
        self.assertTrue(risk["veto"])
        self.assertIn("presupuesto", risk["reason"])

    def test_equity_rejects_missing_or_nonfinite_marks(self):
        bot = self.coordinator()
        self.hold(bot)
        with self.assertRaises(KeyError):
            bot._equity({})
        with self.assertRaises(ValueError):
            bot._equity({"DOGEUSDT": math.nan})

    def test_quantity_rounding_never_increases_planned_risk(self):
        bot = self.coordinator()
        bot.client.quantity_factor = 1.1
        with self.assertRaisesRegex(ValueError, "presupuesto"):
            bot.cycle()
        self.assertFalse(bot.state["positions"])

    def test_unavailable_contract_information_waits_without_opening(self):
        bot = self.coordinator()
        bot.client.normalize_quantity = Mock(side_effect=ExchangeError("unavailable"))
        bot.cycle()
        self.assertFalse(bot.state["positions"])
        analysis = bot.state["last_analysis"]["DOGEUSDT"]
        self.assertEqual(analysis["buy_decision"], "ESPERAR")
        self.assertEqual(analysis["execution"]["status"], "skipped")

    def test_stop_file_exits_existing_positions_and_blocks_entries(self):
        bot = self.coordinator()
        self.hold(bot)
        bot.store.path.with_suffix(".STOP").touch()
        bot.cycle()
        self.assertFalse(bot.state["positions"])
        self.assertEqual(bot.state["trades"][0]["reason"], "risk_stop")

    def test_funding_payment_and_cursor_are_atomic_across_restart(self):
        for direction in (1, -1):
            bot = self.coordinator(direction=direction, name="funding"+str(direction))
            position = self.hold(bot, direction=direction, quantity=1)
            position.update(next_funding_time=int((NOW-1)*1000), funding_rate=.001)
            premium = bot.client.premiums["DOGEUSDT"]
            premium["lastFundingRate"] = ".001"
            bot._fund_paper("DOGEUSDT", premium, NOW)
            self.assertAlmostEqual(bot.state["balance"], 100-direction*.1)
            restarted = Coordinator(bot.c, bot.store, bot.client, bot.context)
            restarted._fund_paper("DOGEUSDT", premium, NOW)
            self.assertAlmostEqual(restarted.state["balance"], 100-direction*.1)
            self.assertEqual(len([event for event in bot.store.events(100) if event["kind"] == "paper_funding_estimate"]), 1)


if __name__ == "__main__":
    unittest.main()
