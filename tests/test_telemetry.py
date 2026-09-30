import copy
import json
import math
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from bot.config import Config
from bot.engine import Coordinator
from bot.models import Advice, Candle, Snapshot
from bot.storage import Store
from bot.telemetry import AGENTS, ActivityRecorder, MAX_EVENTS
from tests.test_cycle import FakeClient, NOW, snapshot


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "paper.activity.json"
        self.clock = patch("bot.telemetry.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.recorder = ActivityRecorder(self.path)

    def persisted(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_initial_state_reports_unknown_activity_and_no_cycle_heartbeat(self):
        value = self.persisted()
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(set(value["agent_activity"]), set(AGENTS))
        self.assertTrue(all(row["status"] == "waiting" for row in value["agent_activity"].values()))
        self.assertTrue(all(row["started_at"] is None for row in value["agent_activity"].values()))
        self.assertIsNone(value["last_cycle_heartbeat"])
        self.assertEqual(value["agent_events"], [])

    def test_start_is_visible_before_actual_work_finishes_and_return_is_preserved(self):
        advice = Advice("trend", .7, False, "Tendencia alcista", {"regime": "bullish"})
        marker = []

        def function():
            value = self.persisted()
            self.assertEqual(value["agent_activity"]["trend"]["status"], "running")
            self.assertEqual(value["agent_events"][-1]["status"], "running")
            marker.append(True)
            return advice

        returned = self.recorder.run("trend", "Analizar", "DOGEUSDT", function)
        self.assertIs(returned, advice)
        self.assertEqual(marker, [True])
        value = self.persisted()
        self.assertEqual([event["status"] for event in value["agent_events"]], ["running", "completed"])
        status = value["agent_activity"]["trend"]
        self.assertEqual(status["status"], "waiting")
        self.assertEqual(status["last_result"]["score"], .7)
        self.assertGreaterEqual(status["duration_ms"], 0)
        self.assertIsNone(value["last_cycle_heartbeat"])

    def test_original_exception_identity_and_type_are_preserved_without_secret_text(self):
        error = RuntimeError("api_key=VERY_SECRET password=private https://x.invalid/?token=private")

        def failing():
            raise error

        with self.assertRaises(RuntimeError) as raised:
            self.recorder.run("market_context", "Consultar", "ETHUSDT", failing)
        self.assertIs(raised.exception, error)
        value = self.persisted()
        self.assertEqual(value["agent_activity"]["market_context"]["status"], "error")
        self.assertEqual(value["agent_events"][-1]["result"], {"error_type": "RuntimeError"})
        serialized = json.dumps(value)
        self.assertNotIn("VERY_SECRET", serialized)
        self.assertNotIn("private", serialized)
        self.assertNotIn("x.invalid", serialized)

    def test_write_failure_cannot_change_return_or_original_exception(self):
        result = object()
        with patch.object(Path, "write_text", side_effect=PermissionError("permission secret")):
            self.assertIs(self.recorder.run("risk", "Riesgo", None, lambda: result), result)
            original = ValueError("original")
            with self.assertRaises(ValueError) as raised:
                self.recorder.run("risk", "Riesgo", None, Mock(side_effect=original))
            self.assertIs(raised.exception, original)
        self.assertEqual(self.recorder.snapshot()["agent_activity"]["risk"]["status"], "error")

    def test_unserializable_result_cannot_leave_false_running_status(self):
        class OpaqueResult:
            @property
            def score(self):
                raise ValueError("private object implementation")

        original = OpaqueResult()
        self.assertIs(self.recorder.run("risk", "Evaluar", None, lambda: original), original)
        value = self.recorder.snapshot()
        self.assertEqual(value["agent_activity"]["risk"]["status"], "waiting")
        self.assertEqual(value["agent_events"][-1]["result"], {"result_type": "OpaqueResult"})

    def test_atomic_replace_failure_leaves_a_readable_previous_file(self):
        previous = self.persisted()
        with patch.object(Path, "replace", side_effect=PermissionError("busy")):
            self.recorder.run("trend", "Analizar", None, lambda: Advice("trend", .5))
        self.assertEqual(self.persisted(), previous)
        self.recorder.idle("trend", "Esperando vela")
        self.assertEqual(self.persisted()["agent_events"][-1]["status"], "completed")

    def test_reason_redaction_and_detail_allowlist(self):
        value = Advice("market_context", 0, True,
                       'Authorization: Bearer secretbearer; "api_key": "abc secret"; password=private; https://x.invalid/?a=secret',
                       {"available": False, "fresh": False, "api_key": "private", "headers": {"Authorization": "private"},
                        "headlines": ["private headline"], "grok_status": "missing_configuration"})
        self.recorder.run("market_context", "Contexto", None, lambda: value)
        result = self.persisted()["agent_events"][-1]["result"]
        serialized = json.dumps(result)
        for secret in ("secretbearer", "abc secret", "private", "x.invalid"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result["details"], {"available": False, "fresh": False, "grok_status": "missing_configuration"})

    def test_event_ring_is_bounded_and_snapshot_is_not_mutable_shared_state(self):
        with patch.object(self.recorder, "_publish"):
            for _ in range(120):
                self.recorder.run("risk", "Evaluar", "DOGEUSDT", lambda: None)
        self.recorder.idle("risk", "Esperando")
        value = self.recorder.snapshot()
        self.assertEqual(len(value["agent_events"]), MAX_EVENTS)
        value["agent_events"].clear()
        self.assertEqual(len(self.recorder.snapshot()["agent_events"]), MAX_EVENTS)

    def test_restart_preserves_prior_events_but_never_resumes_old_running_tasks(self):
        self.recorder.run("trend", "Analizar", "SOLUSDT", lambda: Advice("trend", -.7))
        self.recorder.start("research_validation", "Revisión diaria")
        self.recorder.heartbeat()
        previous = self.persisted()
        restarted = ActivityRecorder(self.path).snapshot()
        self.assertNotEqual(restarted["process_instance_id"], previous["process_instance_id"])
        self.assertEqual(len(restarted["agent_events"]), len(previous["agent_events"]))
        self.assertEqual(restarted["agent_activity"]["research_validation"]["status"], "waiting")
        self.assertIsNone(restarted["agent_activity"]["research_validation"]["started_at"])
        self.assertIsNone(restarted["last_cycle_heartbeat"])

    def test_bad_or_other_schema_history_is_ignored(self):
        for content in ('not-json', '{"schema_version": 9, "agent_events": []}', '{"schema_version": 1, "agent_events": NaN}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                recorder = ActivityRecorder(self.path)
                self.assertEqual(recorder.snapshot()["agent_events"], [])
                self.assertEqual(self.persisted()["schema_version"], 1)

    def test_idle_does_not_invent_work_events_or_refresh_agent_work_timestamp(self):
        self.recorder.run("trend", "Analizar", None, lambda: None)
        old = self.recorder.snapshot()["agent_activity"]["trend"]
        self.recorder.idle("trend", "Esperando vela", NOW + 30)
        with patch.object(self.recorder, "_publish") as publish:
            self.recorder.idle("trend", "Esperando vela", NOW + 30)
            publish.assert_not_called()
        current = self.recorder.snapshot()["agent_activity"]["trend"]
        self.assertEqual(current["started_at"], old["started_at"])
        self.assertEqual(current["updated_at"], old["updated_at"])
        self.assertEqual(current["next_run_at"], NOW + 30)
        self.assertEqual(len(self.recorder.snapshot()["agent_events"]), 2)

    def test_nested_work_completion_does_not_end_another_running_span(self):
        outer = self.recorder.start("research_validation", "Revisión diaria")
        inner = self.recorder.start("research_validation", "Validación breve")
        self.recorder.finish("research_validation", inner, result={"status": "done"})
        current = self.recorder.snapshot()["agent_activity"]["research_validation"]
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["task"], "Revisión diaria")
        self.recorder.idle("research_validation", "Esperando")
        self.assertEqual(self.recorder.snapshot()["agent_activity"]["research_validation"]["status"], "running")
        self.recorder.finish("research_validation", outer)
        self.assertEqual(self.recorder.snapshot()["agent_activity"]["research_validation"]["status"], "waiting")

    def test_parallel_calls_are_thread_safe_and_keep_complete_event_pairs(self):
        with patch.object(self.recorder, "_publish"):
            threads = [threading.Thread(target=lambda: self.recorder.run("risk", "Evaluar", None, lambda: None)) for _ in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        value = self.recorder.snapshot()
        self.assertEqual(len(value["agent_events"]), 40)
        self.assertEqual(sum(e["status"] == "completed" for e in value["agent_events"]), 20)
        self.assertEqual(value["agent_activity"]["risk"]["status"], "waiting")

    def test_only_explicit_heartbeat_refreshes_cycle_freshness(self):
        self.recorder.run("trend", "Analizar", None, lambda: None)
        self.recorder.publish_market({"DOGEUSDT": snapshot()})
        self.assertIsNone(self.recorder.snapshot()["last_cycle_heartbeat"])
        self.recorder.heartbeat(NOW)
        self.assertEqual(self.recorder.snapshot()["last_cycle_heartbeat"], NOW)
        for invalid in (NOW - 1, NOW + 10, math.nan, -1):
            self.recorder.heartbeat(invalid)
        self.assertEqual(self.recorder.snapshot()["last_cycle_heartbeat"], NOW)

    def test_market_uses_actual_closed_candles_and_never_fetches_data(self):
        snap = snapshot()
        future = replace(snap.candles[-1], open_time=int(NOW * 1000), close_time=int((NOW + 300) * 1000))
        snap = replace(snap, candles=snap.candles + [future])
        premium = {"markPrice": "100.1", "lastFundingRate": ".001", "time": NOW * 1000}
        self.recorder.publish_market({"DOGEUSDT": snap}, {"DOGEUSDT": premium})
        row = self.persisted()["market_data"]["DOGEUSDT"]
        self.assertEqual(row["price"], 100)
        self.assertEqual(row["mark_price"], 100.1)
        self.assertEqual(row["funding_rate"], .001)
        self.assertEqual(row["quote_volume"], 20_000_000)
        self.assertEqual(len(row["candles"]), 120)
        self.assertLessEqual(row["candles"][-1]["close_time"], NOW * 1000)
        self.assertEqual(set(row["candles"][0]), {"open_time", "close_time", "open", "high", "low", "close", "volume"})

    def test_partial_market_update_preserves_older_symbol_and_field_timestamps(self):
        old = snapshot()
        self.recorder.publish_market({"DOGEUSDT": old, "SOLUSDT": replace(old, symbol="SOLUSDT")},
                                     {"DOGEUSDT": {"markPrice": "100", "lastFundingRate": "0", "time": NOW * 1000}})
        original = copy.deepcopy(self.recorder.snapshot()["market_data"])
        with patch("bot.telemetry.time.time", return_value=NOW + 30):
            self.recorder.publish_market({"DOGEUSDT": replace(old, bid=101, ask=101, fetched_at=NOW + 30)})
        updated = self.recorder.snapshot()["market_data"]
        self.assertEqual(updated["SOLUSDT"], original["SOLUSDT"])
        self.assertEqual(updated["DOGEUSDT"]["fetched_at"], NOW + 30)
        self.assertEqual(updated["DOGEUSDT"]["mark_fetched_at"], NOW)
        self.assertEqual(updated["DOGEUSDT"]["mark_price"], 100)
        self.recorder.publish_market({"DOGEUSDT": old})
        self.assertEqual(self.recorder.snapshot()["market_data"]["DOGEUSDT"]["fetched_at"], NOW + 30)

    def test_nonfinite_market_and_advice_data_never_write_invalid_json(self):
        self.recorder.run("trend", "Analizar", None, lambda: Advice("trend", math.nan, details={"atr": math.inf}))
        self.recorder.publish_market({"DOGEUSDT": replace(snapshot(), bid=math.nan)},
                                     {"DOGEUSDT": {"markPrice": "NaN", "lastFundingRate": 0}})
        json.dumps(self.persisted(), allow_nan=False)
        self.assertEqual(self.persisted()["market_data"], {})

    def test_engine_records_only_real_analyze_calls_and_keeps_sqlite_event_count(self):
        store = Store(Path(self.temp.name) / "paper.sqlite3")
        self.addCleanup(store.close)
        config = Config(symbols=("DOGEUSDT",), news_enabled=False)
        context = Mock()
        context.analyze.return_value = Advice("market_context", 0)
        bot = Coordinator(config, store, FakeClient(), context)
        bot.trend.analyze = Mock(return_value=Advice("trend", 0))
        bot.momentum.analyze = Mock(return_value=Advice("momentum", 0, details={"atr": 1}))
        bot.cycle()
        bot.cycle()
        events = bot.activity.snapshot()["agent_events"]
        counts = {agent: sum(e["agent"] == agent and e["status"] == "completed" for e in events) for agent in AGENTS}
        self.assertEqual(counts, {"coordinator": 0, "trend": 1, "momentum": 1, "market_context": 1,
                                  "liquidity": 2, "risk": 2, "research_validation": 0})
        bot.trend.analyze.assert_called_once()
        bot.context.analyze.assert_called_once()
        self.assertEqual(store.db.execute("SELECT count(*) FROM events").fetchone()[0], 4)
        self.assertEqual(bot.state["balance"], 100)
        self.assertEqual(bot.state["positions"], {})
        self.assertEqual(bot.state["trades"], [])


if __name__ == "__main__":
    unittest.main()
