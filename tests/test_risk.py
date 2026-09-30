import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from bot.config import Config, load_config
from bot.models import Snapshot
from bot.risk import RiskAgent, new_state
from bot.storage import ProcessLock, Store


def market(**changes):
    return replace(Snapshot("DOGEUSDT", [], 100.0, 100.0, 20_000_000, 1_788_739_200), **changes)


def timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class ConfigTest(unittest.TestCase):
    def test_defaults_match_paper_futures_100_usdt_5x(self):
        config = Config()
        self.assertEqual(config.mode, "paper")
        self.assertEqual(config.initial_equity, 100)
        self.assertEqual(config.leverage, 5)
        self.assertEqual(config.max_total_loss_usdt, 30)
        self.assertEqual(config.max_daily_loss_pct, .05)
        self.assertEqual(config.max_drawdown_pct, .10)
        self.assertEqual(config.reward_risk_ratio, 2)
        self.assertEqual(config.strategy_profile, "trend")
        self.assertTrue(config.allow_shorts)

    def test_production_and_other_leverage_are_rejected(self):
        for kwargs in ({"mode": "live"}, {"mode": "production"}, {"leverage": 20}, {"leverage": 5.0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)

    def test_strategy_profiles_and_direction_gate_are_validated(self):
        for profile in ("trend", "swing", "scalping"):
            self.assertEqual(Config(strategy_profile=profile).strategy_profile, profile)
        for kwargs in ({"strategy_profile": "martingale"}, {"allow_shorts": "false"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)

    def test_nonfinite_negative_and_wrong_types_are_rejected(self):
        numeric = ("initial_equity", "risk_per_trade", "max_daily_loss_pct", "max_total_loss_usdt", "max_drawdown_pct", "max_margin_fraction", "stop_atr_multiple", "reward_risk_ratio", "fee_rate", "slippage_bps", "entry_threshold", "max_spread_bps", "min_quote_volume", "max_funding_rate", "max_holding_hours")
        for name in numeric:
            for invalid in (math.nan, math.inf, -1, True, "0.5"):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(ValueError):
                    Config(**{name: invalid})
        for name in ("poll_seconds", "max_positions", "news_refresh_seconds", "adaptation_min_trades"):
            for invalid in (True, math.nan, 30.5):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(ValueError):
                    Config(**{name: invalid})
        for name in ("news_enabled", "require_fresh_news", "grok_enabled"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Config(**{name: "false"})

    def test_limits_cannot_exceed_configured_safety_bounds(self):
        for kwargs in ({"risk_per_trade": .011}, {"max_daily_loss_pct": .051}, {"max_total_loss_usdt": 31}, {"initial_equity": 30}, {"max_margin_fraction": .21}, {"max_positions": 3}, {"adaptation_min_trades": 29}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)

    def test_symbol_collection_is_validated_and_immutable(self):
        symbols = ["DOGEUSDT"]
        config = Config(symbols=symbols)
        symbols.append("SOLUSDT")
        self.assertEqual(config.symbols, ("DOGEUSDT",))
        for invalid in ("DOGEUSDT", [], ["DOGEUSDT", "DOGEUSDT"], [None], ["dogeusdt"]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Config(symbols=invalid)

    def test_load_rejects_unknown_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('unknown_option = 5\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "desconocidas"):
                load_config(path)


class RiskSizingTest(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.agent = RiskAgent(self.config)
        self.state = new_state(self.config)

    def test_long_and_short_have_correct_stops_and_2r_targets(self):
        for direction in (1, -1):
            advice = self.agent.analyze(market(), self.state, direction, atr=1)
            self.assertFalse(advice.veto, advice.reason)
            self.assertEqual(advice.score, direction)
            self.assertAlmostEqual(advice.details["quantity"], .5 / 2.2)
            self.assertAlmostEqual(advice.details["risk_usdt"], .5)
            self.assertEqual(advice.details["stop"], 100 - direction * 2)
            self.assertEqual(advice.details["target"], 100 + direction * 4)
            self.assertAlmostEqual(advice.details["margin"], advice.details["quantity"] * 100 / 5)

    def test_direction_uses_executable_side_of_book(self):
        spread_market = market(bid=99, ask=101)
        long = self.agent.analyze(spread_market, self.state, 1, 1)
        short = self.agent.analyze(spread_market, self.state, -1, 1)
        self.assertEqual(long.details["stop"], 99)
        self.assertEqual(short.details["stop"], 101)

    def test_low_atr_has_minimum_stop_and_per_position_margin_cap(self):
        advice = self.agent.analyze(market(), self.state, 1, .001)
        self.assertFalse(advice.veto)
        self.assertEqual(advice.details["stop_distance"], .3)
        self.assertEqual(advice.details["margin"], 10)
        self.assertEqual(advice.details["quantity"], .5)
        self.assertLessEqual(advice.details["risk_usdt"], advice.details["risk_budget"])

    def test_existing_risk_is_reserved_from_remaining_daily_budget(self):
        self.state["equity"] = 95.15
        self.state["positions"] = {"SOLUSDT": {"risk_usdt": .10, "quantity": .01, "entry_price": 100}}
        advice = self.agent.analyze(market(), self.state, 1, 1)
        self.assertFalse(advice.veto)
        self.assertAlmostEqual(advice.details["risk_budget"], .05)
        self.assertAlmostEqual(advice.details["risk_usdt"], .05)
        self.state["positions"]["SOLUSDT"]["risk_usdt"] = .20
        self.assertTrue(self.agent.analyze(market(), self.state, 1, 1).veto)

    def test_drawdown_budget_can_be_tighter_than_daily_budget(self):
        self.state.update(equity=90.05, day_start_equity=92)
        advice = self.agent.analyze(market(), self.state, -1, 1)
        self.assertFalse(advice.veto)
        self.assertAlmostEqual(advice.details["risk_budget"], .05)

    def test_portfolio_margin_cap_and_exhaustion(self):
        self.state["positions"] = {"SOLUSDT": {"risk_usdt": .1, "quantity": 1.9, "entry_price": 100}}
        advice = self.agent.analyze(market(), self.state, 1, .01)
        self.assertFalse(advice.veto)
        self.assertAlmostEqual(advice.details["margin"], 2)
        self.state["positions"]["SOLUSDT"]["quantity"] = 2
        self.assertTrue(self.agent.analyze(market(), self.state, 1, .01).veto)

    def test_limits_pending_and_existing_symbol_veto(self):
        for changes in ({"halt_reason": "manual"}, {"daily_paused": True}, {"pending": {"id": 1}}, {"positions": {"DOGEUSDT": {}}}, {"positions": {"SOLUSDT": {}, "ETHUSDT": {}}}):
            state = {**self.state, **changes}
            self.assertTrue(self.agent.analyze(market(), state, 1, 1).veto)

    def test_invalid_atr_direction_funding_and_too_wide_stops_veto(self):
        for direction, average_range, funding in ((0, 1, 0), (True, 1, 0), (1, math.nan, 0), (1, 0, 0), (1, -1, 0), (1, 1, math.nan), (1, 1, .0011), (-1, 1, -.0011), (1, 4.01, 0)):
            with self.subTest(direction=direction, atr=average_range, funding=funding):
                self.assertTrue(self.agent.analyze(market(), self.state, direction, average_range, funding).veto)

    def test_funding_cost_reduces_quantity(self):
        baseline = self.agent.analyze(market(), self.state, 1, 1)
        funded = self.agent.analyze(market(), self.state, 1, 1, .001)
        self.assertFalse(funded.veto)
        self.assertLess(funded.details["quantity"], baseline.details["quantity"])

    def test_invalid_quotes_and_nonfinite_state_fail_closed(self):
        for invalid in (market(ask=math.nan), market(bid=0), market(ask=99), market(bid=math.inf), market(symbol="BTCUSDT")):
            self.assertTrue(self.agent.analyze(invalid, self.state, 1, 1).veto)
        for name in ("equity", "peak_equity", "day_start_equity", "risk_multiplier"):
            for value in (math.nan, math.inf, -1):
                with self.subTest(name=name, value=value):
                    self.assertTrue(self.agent.analyze(market(), {**self.state, name: value}, 1, 1).veto)
        self.assertTrue(self.agent.analyze(market(), {**self.state, "risk_multiplier": 2}, 1, 1).veto)
        self.state["positions"] = {"SOLUSDT": {"risk_usdt": math.nan, "quantity": 1, "entry_price": 100}}
        self.assertTrue(self.agent.analyze(market(), self.state, 1, 1).veto)


class RiskLimitsTest(unittest.TestCase):
    def setUp(self):
        self.agent = RiskAgent(Config())
        self.state = new_state(Config())
        self.now = timestamp("2026-09-07T12:00:00")
        self.agent.update_limits(self.state, 100, self.now)

    def test_total_loss_30_usdt_is_a_sticky_halt(self):
        self.agent.update_limits(self.state, 70, self.now + 1)
        self.assertIn("perdida total", self.state["halt_reason"])
        self.agent.update_limits(self.state, 110, self.now + 2)
        self.assertIsNotNone(self.state["halt_reason"])

    def test_drawdown_10_percent_tracks_peak_and_sticks(self):
        self.agent.update_limits(self.state, 120, self.now + 1)
        self.agent.update_limits(self.state, 108, self.now + 2)
        self.assertEqual(self.state["peak_equity"], 120)
        self.assertIn("drawdown", self.state["halt_reason"])
        self.agent.update_limits(self.state, 130, self.now + 86400)
        self.assertIsNotNone(self.state["halt_reason"])

    def test_daily_loss_stays_paused_until_utc_midnight(self):
        before_midnight = timestamp("2026-09-07T23:59:59")
        self.agent.update_limits(self.state, 95, before_midnight)
        self.assertTrue(self.state["daily_paused"])
        self.assertIsNone(self.state["halt_reason"])
        self.agent.update_limits(self.state, 99, before_midnight + .5)
        self.assertTrue(self.state["daily_paused"])
        self.agent.update_limits(self.state, 99, before_midnight + 1)
        self.assertEqual(self.state["day"], "2026-09-08")
        self.assertFalse(self.state["daily_paused"])
        self.assertEqual(self.state["day_start_equity"], 99)

    def test_invalid_equity_or_time_halts_without_storing_nan(self):
        for equity, moment in ((math.nan, self.now), (math.inf, self.now), (100, math.nan), (100, math.inf), (100, -1)):
            state = new_state(Config())
            self.agent.update_limits(state, equity, moment)
            self.assertIsNotNone(state["halt_reason"])
            json.dumps(state, allow_nan=False)


class AdaptationTest(unittest.TestCase):
    def setUp(self):
        self.agent = RiskAgent(Config())
        self.state = new_state(Config())

    def test_only_complete_disjoint_batches_of_30_reduce_risk(self):
        self.state["trades"] = [{"net_pnl": -1}] * 29
        self.agent.adapt(self.state)
        self.assertEqual(self.state["risk_multiplier"], 1)
        self.state["trades"].append({"net_pnl": -1})
        self.agent.adapt(self.state)
        self.assertEqual(self.state["risk_multiplier"], .5)
        self.assertEqual(self.state["adaptation_count"], 30)
        self.agent.adapt(self.state)
        self.assertEqual(len(self.state["adaptations"]), 1)
        self.state["trades"] += [{"net_pnl": -1}] * 30
        self.agent.adapt(self.state)
        self.assertEqual(self.state["risk_multiplier"], .25)

    def test_profitable_batches_never_restore_or_raise_risk(self):
        self.state["risk_multiplier"] = .5
        self.state["trades"] = [{"net_pnl": 1}] * 30
        self.agent.adapt(self.state)
        self.assertEqual(self.state["risk_multiplier"], .5)
        self.assertIsNone(self.state["adaptations"][0]["profit_factor"])

    def test_restart_backlog_processes_all_complete_batches_in_order(self):
        self.state["trades"] = [{"net_pnl": -1}] * 30 + [{"net_pnl": 2}] * 30 + [{"net_pnl": -1}] * 35
        self.agent.adapt(self.state)
        self.assertEqual(self.state["risk_multiplier"], .25)
        self.assertEqual(self.state["adaptation_count"], 90)
        self.assertEqual([item["trades"] for item in self.state["adaptations"]], [30, 60, 90])
        self.assertEqual([item["net_pnl"] for item in self.state["adaptations"]], [-30, 60, -30])

    def test_reduced_manual_multiplier_cannot_increase_to_floor(self):
        self.state["risk_multiplier"] = .1
        self.state["trades"] = [{"net_pnl": -1}] * 30
        self.agent.adapt(self.state)
        self.assertLessEqual(self.state["risk_multiplier"], .1)

    def test_nonfinite_pnl_halts_adaptation(self):
        self.state["trades"] = [{"net_pnl": -1}] * 29 + [{"net_pnl": math.nan}]
        self.agent.adapt(self.state)
        self.assertIsNotNone(self.state["halt_reason"])
        self.assertEqual(self.state["risk_multiplier"], 1)
        self.assertEqual(self.state["adaptation_count"], 0)


class PersistenceTest(unittest.TestCase):
    def child(self, code, *arguments):
        return subprocess.run([sys.executable, "-c", code, *map(str, arguments)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def test_pending_orders_limits_and_adaptation_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = new_state(Config())
            state.update(pending={"DOGEUSDT": {"client_order_id": "persist-before-send"}}, halt_reason="manual", risk_multiplier=.5, adaptation_count=30)
            store = Store(path)
            store.save(state, "pending_order", {"id": "persist-before-send"})
            store.close()
            reopened = Store(path)
            try:
                self.assertEqual(reopened.load(), state)
                self.assertEqual(reopened.events()[0]["data"]["id"], "persist-before-send")
            finally:
                reopened.close()

    def test_invalid_event_rolls_back_state_and_event_together(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            try:
                store.save({"version": 1}, "first", {})
                with self.assertRaises(ValueError):
                    store.save({"version": 2}, "bad", {"nan": math.nan})
                self.assertEqual(store.load(), {"version": 1})
                self.assertEqual(len(store.events()), 1)
                with self.assertRaises(ValueError):
                    store.save({"version": math.nan})
                self.assertEqual(store.load(), {"version": 1})
            finally:
                store.close()

    def test_corrupted_nonfinite_json_is_rejected_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            try:
                store.db.execute("INSERT INTO state VALUES(1, ?)", ('{"equity": NaN}',))
                store.db.commit()
                with self.assertRaises(ValueError):
                    store.load()
            finally:
                store.close()

    def test_process_lock_rejects_another_process_and_reentrant_use(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.lock"
            lock = ProcessLock(path)
            with lock:
                with self.assertRaises(RuntimeError):
                    lock.__enter__()
                result = self.child("from bot.storage import ProcessLock\nimport sys\ntry:\n    with ProcessLock(sys.argv[1]):\n        sys.exit(2)\nexcept RuntimeError:\n    sys.exit(0)\n", path)
                self.assertEqual(result.returncode, 0, result.stderr)
            with ProcessLock(path):
                pass
            self.assertTrue(path.exists())

    def test_crash_releases_lock_and_preserves_only_committed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            lock_path = Path(directory) / "bot.lock"
            result = self.child("from bot.storage import Store, ProcessLock\nimport sys, os\nwith ProcessLock(sys.argv[2]):\n    store = Store(sys.argv[1])\n    store.save({'pending': {'id': 'order-before-crash'}})\n    store.db.execute('UPDATE state SET data=? WHERE id=1', ('{}',))\n    os._exit(23)\n", path, lock_path)
            self.assertEqual(result.returncode, 23, result.stderr)
            with ProcessLock(lock_path):
                store = Store(path)
                try:
                    self.assertEqual(store.load(), {"pending": {"id": "order-before-crash"}})
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
