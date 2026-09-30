from dataclasses import replace
import json
import math
import unittest
from unittest.mock import patch

from bot.config import Config
from bot.models import Candle, Snapshot
from bot.research import Signal, WARMUP, _assessment, _exit_trigger, _signals, _simulate, run_research


INTERVAL = 300_000


def bar(index, opening=100.0, high=101.0, low=99.0, close=100.0):
    return Candle(index * INTERVAL, (index + 1) * INTERVAL - 1,
                  opening, high, low, close, 1000.0)


def market_bars(count=600):
    result = []
    for i in range(count):
        opening = 100 + i * .025 + 1.5 * math.sin(i / 13)
        close = 100 + (i + 1) * .025 + 1.5 * math.sin((i + 1) / 13)
        result.append(bar(i, opening, max(opening, close) + .3,
                          min(opening, close) - .3, close))
    return result


def forced_signals(bars, score=.9, atr=1):
    return [None if i < WARMUP else Signal(score, atr, True, bars[i - 1].close_time)
            for i in range(len(bars))]


def floor_quantity(symbol, quantity, price):
    rounded = math.floor(quantity * 10000) / 10000
    if rounded <= 0:
        raise ValueError("below step")
    return rounded


class FakeClient:
    def __init__(self, bars=None):
        self.bars = bars or market_bars()
        self.calls = []

    def snapshot(self, symbol, interval, limit):
        self.calls.append(("snapshot", symbol, interval, limit))
        return Snapshot(symbol, self.bars[:], 100, 100.1, 100_000_000,
                        (self.bars[-1].close_time + 1) / 1000)

    def premium_index(self, symbol):
        self.calls.append(("premium_index", symbol))
        return {"symbol": symbol, "lastFundingRate": "0.0001"}

    def exchange_info(self, symbol):
        self.calls.append(("exchange_info", symbol))
        return {"symbol": symbol, "filters": []}

    def normalize_quantity(self, symbol, quantity, price):
        self.calls.append(("normalize_quantity", symbol))
        return floor_quantity(symbol, quantity, price)

    def account(self):
        raise AssertionError("Research must not access private endpoints")

    def place_market(self, *args, **kwargs):
        raise AssertionError("Research must never submit an order")


class ChronologyTests(unittest.TestCase):
    def test_signals_do_not_change_when_future_bars_change(self):
        bars = market_bars(350)
        prefix_signals = _signals("ETHUSDT", bars)
        mutated = bars[:260] + [bar(i, 10000, 10100, 9900, 10001) for i in range(260, 350)]
        changed_signals = _signals("ETHUSDT", mutated)
        self.assertEqual(prefix_signals[:261], changed_signals[:261])
        self.assertEqual(prefix_signals[260].based_on_close_time, bars[259].close_time)

    def test_entry_is_next_open_not_signal_close(self):
        bars = [bar(i) for i in range(203)]
        bars[200] = bar(200, 110, 110.3, 109.7, 110.1)
        bars[201] = bar(201, 110.1, 110.3, 109.8, 110)
        bars[202] = bar(202, 110, 110.2, 109.8, 110)
        config = Config(symbols=("ETHUSDT",))
        result = _simulate(config, "ETHUSDT", bars, forced_signals(bars), 200, 203, floor_quantity, 0)
        trade = result["trades"][0]
        self.assertAlmostEqual(trade["entry_price"], 110 * (1 + config.slippage_bps / 10000))
        self.assertLess(trade["signal_close_time"], trade["entry_time"])
        self.assertEqual(trade["reason"], "segment_end")
        self.assertEqual(trade["exit_index"], 202)

    def test_future_dated_signal_is_rejected(self):
        bars = [bar(i) for i in range(202)]
        signals = forced_signals(bars)
        signals[200] = Signal(.9, 1, True, bars[200].close_time)
        with self.assertRaises(ValueError):
            _simulate(Config(symbols=("ETHUSDT",)), "ETHUSDT", bars, signals,
                      200, 202, floor_quantity, 0)

    def test_training_candidate_does_not_use_holdout_for_selection(self):
        config = Config(symbols=("ETHUSDT",))
        original = market_bars(400)
        split = int(len(original) * .6)
        mutated = original[:split] + [bar(i, 1000, 1050, 950, 1005) for i in range(split, len(original))]
        first = run_research(config, FakeClient(original))["symbols"][0]
        second = run_research(config, FakeClient(mutated))["symbols"][0]
        self.assertEqual(first["candidate"], second["candidate"])
        self.assertEqual(first["training_candidates"], second["training_candidates"])
        self.assertEqual(first["train"], second["train"])
        self.assertEqual(first["holdout"]["start_index"], split)
        self.assertTrue(all(t["entry_index"] >= split for t in first["holdout"]["trades"]))


class FillAndRiskTests(unittest.TestCase):
    def test_stop_wins_when_both_levels_touched(self):
        bars = [bar(i) for i in range(202)]
        bars[200] = bar(200, 100, 110, 90, 101)
        result = _simulate(Config(symbols=("ETHUSDT",)), "ETHUSDT", bars,
                           forced_signals(bars), 200, 202, floor_quantity, 0)
        self.assertEqual(result["trades"][0]["reason"], "stop")
        self.assertLess(result["trades"][0]["net_pnl"], 0)

    def test_gap_fills_at_worse_open_for_long_and_short(self):
        long = {"direction": 1, "entry_time": 0, "stop": 98, "target": 104}
        short = {"direction": -1, "entry_time": 0, "stop": 102, "target": 96}
        self.assertEqual(_exit_trigger(long, bar(1, 90, 95, 85, 91), 12, False),
                         (90, INTERVAL, "stop_gap"))
        self.assertEqual(_exit_trigger(short, bar(1, 110, 115, 109, 111), 12, False),
                         (110, INTERVAL, "stop_gap"))

    def test_fees_slippage_and_funding_reduce_net_result(self):
        bars = [bar(i, 100, 100.1, 99.9, 100) for i in range(203)]
        config = Config(symbols=("ETHUSDT",))
        report = _simulate(config, "ETHUSDT", bars, forced_signals(bars), 200, 203, floor_quantity, -.0001)
        trade = report["trades"][0]
        self.assertGreater(trade["entry_price"], 100)
        self.assertLess(trade["exit_price"], 100)
        self.assertGreater(trade["funding_estimate"], 0)
        self.assertAlmostEqual(trade["net_pnl"], trade["gross_pnl"] - trade["entry_fee"]
                               - trade["exit_fee"] - trade["funding_estimate"])
        self.assertAlmostEqual(report["metrics"]["net_pnl"], sum(t["net_pnl"] for t in report["trades"]))

    def test_quantity_never_rounded_up_and_margin_capped(self):
        bars = [bar(i) for i in range(202)]
        config = Config(symbols=("ETHUSDT",))
        result = _simulate(config, "ETHUSDT", bars, forced_signals(bars), 200, 202,
                           floor_quantity, 0)
        for trade in result["trades"]:
            self.assertLessEqual(trade["margin"], trade["entry_equity"] * config.max_margin_fraction)
            self.assertLessEqual(trade["risk_usdt"], trade["entry_equity"] * config.risk_per_trade + 1e-10)
        rejected = _simulate(config, "ETHUSDT", bars, forced_signals(bars), 200, 202,
                             lambda symbol, quantity, price: quantity + 1, 0)
        self.assertEqual(rejected["metrics"]["trade_count"], 0)
        self.assertGreater(rejected["metrics"]["skipped_entries"]["exchange_filters"], 0)

    def test_minimum_filter_failure_skips_instead_of_increasing_risk(self):
        bars = [bar(i) for i in range(202)]
        def reject(symbol, quantity, price):
            raise ValueError("minimum notional")
        result = _simulate(Config(symbols=("ETHUSDT",)), "ETHUSDT", bars,
                           forced_signals(bars), 200, 202, reject, 0)
        self.assertEqual(result["metrics"]["trade_count"], 0)
        self.assertEqual(result["metrics"]["skipped_entries"]["exchange_filters"], 2)

    def test_loss_limit_stops_new_exposure(self):
        bars = [bar(i, 100, 110, 90, 100) for i in range(240)]
        config = Config(symbols=("ETHUSDT",), max_total_loss_usdt=1.0)
        result = _simulate(config, "ETHUSDT", bars, forced_signals(bars), 200, 240, floor_quantity, 0)
        self.assertGreaterEqual(result["metrics"]["net_pnl"], -1.0001)
        self.assertLess(result["metrics"]["trade_count"], 40)


class ResearchReportTests(unittest.TestCase):
    def test_public_only_isolated_capitals_and_no_promotion(self):
        config = Config(symbols=("ETHUSDT", "SOLUSDT"))
        client = FakeClient(market_bars(400))
        report = run_research(config, client)
        self.assertEqual(report["status"], "completed")
        self.assertFalse(report["portfolio_aggregation"])
        self.assertFalse(report["auto_promote"])
        self.assertEqual(len(report["symbols"]), 2)
        self.assertEqual(config.entry_threshold, .45)
        self.assertEqual(config.stop_atr_multiple, 2)
        for item in report["symbols"]:
            self.assertEqual(len(item["training_candidates"]), 9)
            self.assertEqual(item["train"]["metrics"]["initial_equity"], 100)
            self.assertEqual(item["holdout"]["metrics"]["initial_equity"], 100)
            self.assertFalse(item["assessment"]["sample_sufficient"])
            self.assertFalse(item["assessment"]["auto_promote"])
            self.assertIn("not_historical", item["funding_method"])
        self.assertTrue(all(call[0] in {"snapshot", "premium_index", "exchange_info", "normalize_quantity"}
                            for call in client.calls))
        self.assertTrue(all(call[3] == 1499 for call in client.calls if call[0] == "snapshot"))
        json.dumps(report, allow_nan=False)

    def test_short_history_or_client_error_reports_unavailable(self):
        config = Config(symbols=("ETHUSDT",))
        for client in (FakeClient(market_bars(210)), FakeClient()):
            if len(client.bars) > 210:
                client.snapshot = lambda *args: (_ for _ in ()).throw(ValueError("do not expose secret"))
            report = run_research(config, client)
            self.assertEqual(report["status"], "partial_or_unavailable")
            self.assertEqual(report["symbols"][0]["status"], "unavailable_or_invalid_data")
            self.assertNotIn("secret", repr(report))

    def test_assessment_requires_sample_and_quality_and_never_promotes(self):
        metrics = {"trade_count": 70, "net_pnl": 2, "profit_factor": 1.5,
                   "max_drawdown_pct": 2, "halt_reason": None}
        train = {"metrics": metrics}
        holdout = {"metrics": {**metrics, "trade_count": 30}}
        assessment = _assessment(train, holdout, Config())
        self.assertTrue(assessment["passes_preliminary_checks"])
        self.assertFalse(assessment["auto_promote"])
        holdout["metrics"]["profit_factor"] = .9
        self.assertFalse(_assessment(train, holdout, Config())["passes_preliminary_checks"])


if __name__ == "__main__":
    unittest.main()
