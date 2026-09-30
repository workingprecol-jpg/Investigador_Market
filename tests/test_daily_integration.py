import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bot.config import Config
from bot.daily_analysis import ResearchValidationAgent
from bot.engine import Coordinator
from bot.models import Advice
from bot.storage import Store
from tests.test_cycle import FakeClient, NOW


class DailyEngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name)/"paper.sqlite3")
        self.addCleanup(self.store.close)
        self.config = Config(symbols=("DOGEUSDT",), news_enabled=False)
        self.client = FakeClient()
        context = Mock()
        context.analyze.return_value = Advice("market_context", 0, details={"fresh": False})
        self.bot = Coordinator(self.config, self.store, self.client, context)

    def entry(self):
        self.bot._record_entry(dict(symbol="DOGEUSDT", side="BUY", created_at=NOW-600,
            plan={"stop_distance": 2, "risk_usdt": .5}, decision_id="d1",
            position_id="p1", entry_context={"regime":"bullish", "version":1}), .2, 100, .01)

    def test_exits_preserve_position_identity_and_do_not_adapt_per_trade(self):
        self.entry()
        self.bot.risk.adapt = Mock()
        with patch("bot.engine.time.time", return_value=NOW):
            self.bot._record_exit({"symbol":"DOGEUSDT"}, .1, 99, .005)
            self.bot._record_exit({"symbol":"DOGEUSDT"}, .1, 98, .005)
        a, b = self.bot.state["trades"]
        self.assertEqual(a["position_id"], b["position_id"])
        self.assertEqual(b["decision_id"], "d1")
        self.assertFalse(a["position_closed"])
        self.assertTrue(b["position_closed"])
        self.assertEqual(b["remaining_quantity"], 0)
        self.bot.risk.adapt.assert_not_called()

    def test_legacy_open_positions_survive_upgrade_without_changing_protection(self):
        self.entry()
        legacy = copy.deepcopy(self.bot.state)
        legacy.pop("strategy")
        for key in ("position_id", "decision_id", "entry_context"):
            legacy["positions"]["DOGEUSDT"].pop(key)
        before = copy.deepcopy(legacy["positions"]["DOGEUSDT"])
        self.store.save(legacy)
        upgraded = Coordinator(self.config, self.store, self.client)
        position = upgraded.state["positions"]["DOGEUSDT"]
        self.assertEqual({key:position[key] for key in before}, before)
        self.assertTrue(position["entry_context"]["legacy"])
        self.assertEqual(upgraded.state["balance"], legacy["balance"])

    def test_daily_reduction_uses_completed_day_and_grouped_partial_exits(self):
        tz = timezone(timedelta(hours=-5))
        end = datetime(2026, 9, 8, tzinfo=tz).timestamp()
        trades = []
        for i in range(30):
            for part in (0, 1):
                trades.append(dict(symbol="DOGEUSDT", direction=1, position_id=f"p{i}", position_closed=part==1,
                    remaining_quantity=0 if part else .1, opened_at=end-4000+i*20,
                    closed_at=end-3990+i*20+part, net_pnl=-.01, fees=.002, funding=0))
        # Today's losses must wait for the next review.
        trades.append(dict(symbol="DOGEUSDT", direction=1, position_id="today", position_closed=True,
            remaining_quantity=0, opened_at=end+1, closed_at=end+2, net_pnl=-50, fees=0, funding=0))
        self.bot.state["trades"] = trades
        report = ResearchValidationAgent(self.config).review(self.bot.state, [], "2026-09-07", end)
        report.update(preview=False, status="completed", generated_at=datetime.fromtimestamp(end+600,timezone.utc).isoformat(),
                      mode="paper", source_version=1)
        report["proposals"] = []
        with patch("bot.engine.time.time", return_value=end+601):
            self.bot.daily_maintenance(report)
        self.assertEqual(self.bot.state["risk_multiplier"], .5)
        self.assertEqual(self.bot.state["adaptation_count"], 30)
        self.assertEqual(len(self.bot.state["trades"]), 61)
        with patch("bot.engine.time.time", return_value=end+602):
            self.bot.daily_maintenance(report)
        self.assertEqual(self.bot.state["risk_multiplier"], .5)

    def test_preview_never_changes_parameters_or_starts_experiments(self):
        before = copy.deepcopy(self.bot.state)
        self.bot.daily_maintenance({"preview": True, "date":"2026-09-07", "proposals":[{"patch":{"risk_per_trade":.01}}]})
        self.assertEqual(self.bot.state, before)

    def test_new_decisions_are_linked_to_trade_and_record_rejections(self):
        self.bot.trend.analyze = Mock(return_value=Advice("trend", 1, details={"regime":"bullish"}))
        self.bot.momentum.analyze = Mock(return_value=Advice("momentum", 1, details={"atr":1}))
        with patch("bot.engine.time.time", return_value=NOW):
            self.bot.cycle()
        position = self.bot.state["positions"]["DOGEUSDT"]
        analysis = self.bot.state["last_analysis"]["DOGEUSDT"]
        self.assertEqual(position["decision_id"], analysis["decision_id"])
        self.assertEqual(position["entry_context"]["regime"], "bullish")
        self.assertEqual(len(position["entry_context"]["agents"]), 5)
        self.assertEqual(self.bot.state["decisions_audit"][-1]["status"], "filled")
        self.assertEqual(len(self.bot.state["equity_history"]), 1)


if __name__ == "__main__":
    unittest.main()
