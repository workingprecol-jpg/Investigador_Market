import copy
import json
import math
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from bot.config import Config
from bot.daily import (DailyReviewRunner, ReviewStore, day_bounds, next_review_at,
                       read_history, reviews_path, run_daily_review)
from bot.risk import new_state
from bot.storage import ProcessLock, Store


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


NOW = ts("2026-09-08T05:10:00")
END = ts("2026-09-08T05:00:00")


class DailyScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "paper.sqlite3"
        self.output = Path(self.tmp.name) / "reports"
        self.config = Config()
        self.state = new_state(self.config)
        self.state["created_at"] = ts("2026-09-07T05:00:00")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.store.save(self.state)

    def run_review(self, **kwargs):
        return run_daily_review(self.config, self.path, now=NOW, output_dir=self.output, **kwargs)

    def test_midnight_schedule_uses_fixed_bogota_offset(self):
        self.assertEqual(day_bounds("2026-09-07"), (ts("2026-09-07T05:00:00"), END))
        self.assertEqual(next_review_at(NOW - 1), "2026-09-08T00:10:00-05:00")
        self.assertEqual(next_review_at(NOW), "2026-09-09T00:10:00-05:00")

    def test_default_reviews_previous_complete_day_without_mutating_runtime(self):
        original = self.store.load()
        report = self.run_review()
        self.assertEqual(report["date"], "2026-09-07")
        self.assertEqual(report["source_version"], 1)
        self.assertEqual(report["cutoff_utc"], "2026-09-08T05:00:00+00:00")
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["day_complete"])
        self.assertEqual(self.store.load(), original)
        self.assertTrue((self.output / "2026-09-07.json").exists())

    def test_preview_does_not_complete_or_overwrite_daily_run(self):
        report = self.run_review(preview=True)
        self.assertEqual(report["date"], "2026-09-08")
        self.assertTrue(report["preview"])
        self.assertFalse(report["day_complete"])
        db = ReviewStore(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.completed(), set())
        self.assertIsNone(db.latest())
        self.assertTrue((self.output / "2026-09-08-preview.json").exists())

    def test_completed_day_is_idempotent_even_if_accounting_changes(self):
        original = self.run_review()
        self.state["balance"] = 99
        self.store.save(self.state)
        with patch("bot.daily.read_history", side_effect=AssertionError("must not reread")):
            repeated = self.run_review()
        self.assertEqual(original, repeated)

    def test_failed_and_crashed_running_days_are_retryable(self):
        db = ReviewStore(self.path)
        db.put("2026-09-07", "running")
        db.close()
        with patch("bot.daily.read_history", side_effect=sqlite3.OperationalError("temporary")):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_review()
        db = ReviewStore(self.path)
        self.assertEqual(db.get("2026-09-07")["status"], "failed")
        db.close()
        self.assertEqual(self.run_review()["status"], "completed")

    def test_worker_lock_prevents_concurrent_review(self):
        with ProcessLock(reviews_path(self.path).with_suffix(".lock")):
            with self.assertRaises(RuntimeError):
                self.run_review()

    def test_future_and_incomplete_day_not_accepted_as_complete(self):
        for day in ("2026-09-08", "2026-09-09"):
            with self.subTest(day=day), self.assertRaises(ValueError):
                self.run_review(review_date=day)
        with self.assertRaises(ValueError):
            self.run_review(review_date="2026-09-09", preview=True)

    def test_read_history_is_consistent_during_concurrent_wal_write(self):
        with patch("bot.storage.time.time", return_value=END - 10):
            self.store.save(self.state, "old", {"value": 1})
        real_loads = json.loads
        triggered = []

        def concurrent_loads(payload, **kwargs):
            if not triggered:
                triggered.append(True)
                changed = copy.deepcopy(self.state)
                changed["balance"] = 90
                with patch("bot.storage.time.time", return_value=END - 5):
                    self.store.save(changed, "new", {"value": 2})
            return real_loads(payload, **kwargs)

        with patch("bot.daily.json.loads", side_effect=concurrent_loads):
            snapshot, events = read_history(self.path, END)
        self.assertEqual(snapshot["balance"], 100)
        self.assertEqual([e["kind"] for e in events], ["old"])
        self.assertEqual(self.store.load()["balance"], 90)

    def test_read_history_excludes_events_at_cutoff(self):
        with patch("bot.storage.time.time", return_value=END):
            self.store.save(self.state, "future", {})
        self.assertEqual(read_history(self.path, END)[1], [])
        for cutoff in (math.nan, math.inf, True):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                read_history(self.path, cutoff)

    def test_corrupt_json_is_rejected(self):
        self.store.db.execute("UPDATE state SET data=? WHERE id=1", ('{"balance": NaN}',))
        self.store.db.commit()
        with self.assertRaises(ValueError):
            read_history(self.path, END)

    def test_scheduler_waits_until_0010_and_spawns_only_once(self):
        runner = DailyReviewRunner(self.config, self.path)
        worker = Mock(returncode=None)
        worker.poll.return_value = None
        with patch("bot.daily.subprocess.Popen", return_value=worker) as launch:
            runner.poll(self.state, NOW - 1)
            launch.assert_not_called()
            runner.poll(self.state, NOW)
            runner.poll(self.state, NOW + 30)
            launch.assert_called_once()
            self.assertIn("2026-09-07", launch.call_args.args[0])

    def test_two_runners_use_atomic_dispatch_reservation(self):
        left, right = DailyReviewRunner(self.config, self.path), DailyReviewRunner(self.config, self.path)
        worker = Mock(returncode=None)
        worker.poll.return_value = None
        with patch("bot.daily.subprocess.Popen", return_value=worker) as launch:
            left.poll(self.state, NOW)
            right.poll(self.state, NOW)
            launch.assert_called_once()

    def test_worker_unavailable_does_not_interrupt_trade_loop_or_busy_retry(self):
        runner = DailyReviewRunner(self.config, self.path)
        with patch("bot.daily.subprocess.Popen", side_effect=OSError("missing runtime")) as launch:
            self.assertIsNone(runner.poll(self.state, NOW))
            self.assertEqual(runner.last_error, "review_worker_unavailable")
            runner.poll(self.state, NOW + 30)
            launch.assert_called_once()
            runner.poll(self.state, NOW + 300)
            self.assertEqual(launch.call_count, 2)

    def test_storage_unavailable_does_not_interrupt_trade_loop(self):
        runner = DailyReviewRunner(self.config, self.path)
        with patch("bot.daily.ReviewStore", side_effect=sqlite3.OperationalError("busy")):
            self.assertIsNone(runner.poll(self.state, NOW))
        self.assertEqual(runner.last_error, "review_storage_unavailable")

    def test_stale_dispatch_can_retry_after_crash(self):
        db = ReviewStore(self.path)
        self.assertTrue(db.claim("2026-09-07", NOW - 700))
        db.close()
        with patch("bot.daily.subprocess.Popen") as launch:
            DailyReviewRunner(self.config, self.path).poll(self.state, NOW)
            launch.assert_called_once()

    def test_timeout_terminates_without_waiting_then_kills_unresponsive_worker(self):
        runner = DailyReviewRunner(self.config, self.path)
        worker = Mock(returncode=None)
        worker.poll.return_value = None
        runner.process, runner.process_started = worker, NOW - 601
        runner.poll(self.state, NOW)
        runner.poll(self.state, NOW + 1)
        worker.terminate.assert_called_once()
        runner.poll(self.state, NOW + 30)
        worker.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
