"""The local dashboard is an observer, never a second trading worker."""
import base64
import hashlib
import http.client
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.dashboard import AGENTS, DashboardServer, dashboard_snapshot
from bot.risk import new_state
from bot.storage import Store


NOW = 1_800_000_001
HTML = b"<!doctype html><html><head><style>body{color:#fff}</style></head><body><main>Panel local</main><script>\n'use strict';\ndocument.title='Final Boss';\n</script><script type=\"application/javascript\">window.ready=true;</script></body></html>"


def telemetry(now=NOW):
    opening = int(now//300*300-300)*1000
    return dict(schema_version=1, updated_at=now, pid=987,
                process_instance_id="test-instance", last_cycle_heartbeat=now,
                agent_activity={"trend": dict(status="running", task="Analizar EMA", updated_at=now)},
                agent_events=[dict(id="one", agent="trend", status="running", task="Analizar EMA", ts=now)],
                market_data={"DOGEUSDT": dict(price=.1, bid=.0999, ask=.1001, fetched_at=now,
                    mark_price=.1, mark_fetched_at=now, candles=[dict(open_time=opening,
                    close_time=opening+299999, open=.1, high=.101, low=.099, close=.1, volume=1000)])})


class DashboardServerTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state_path = self.root / "paper.sqlite3"
        self.activity_path = self.state_path.with_suffix(".activity.json")
        self.html_path = self.root / "dashboard.html"
        self.html_path.write_bytes(HTML)
        (self.root / ".env").write_text("BINANCE_DEMO_API_SECRET=never-expose-this", encoding="utf-8")
        self.state = new_state(Config())
        self.state.update(heartbeat=NOW, healthy=True, created_at=NOW-100,
                          daily_schedule={"next_review_at": "2027-01-16T05:05:00+00:00"})
        self.save_state()
        self.server = DashboardServer(self.state_path, mode="paper", port=0, html_path=self.html_path)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.clock = patch("bot.dashboard.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def save_state(self):
        store = Store(self.state_path)
        try:
            store.save(self.state, "fixture", {"ok": True})
        finally:
            store.close()

    def activity(self, value=None):
        self.activity_path.write_text(json.dumps(value if value is not None else telemetry(), allow_nan=False), encoding="utf-8")

    def request(self, target="/api/dashboard", method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, target, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def test_api_returns_financial_state_and_actual_observation_contract(self):
        self.activity()
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report["mode"], "paper")
        self.assertEqual(report["equity"], 100)
        self.assertEqual(report["closed_trades"], 0)
        self.assertEqual(set(report["agent_activity"]), set(AGENTS))
        self.assertEqual(report["agent_activity"]["trend"]["status"], "running")
        self.assertEqual(report["agent_events"][0]["id"], "one")
        self.assertTrue(report["runtime"]["cycle_fresh"])
        self.assertTrue(report["runtime"]["telemetry_fresh"])
        self.assertTrue(report["market_data"]["DOGEUSDT"]["fresh"])
        self.assertEqual(report["agent_activity"]["research_validation"]["next_run_at"], self.state["daily_schedule"]["next_review_at"])
        self.assertIn("application/json", headers["Content-Type"])

    def test_local_routes_are_fixed_and_never_serve_workspace_files(self):
        for route in ("/.env", "/dashboard.html", "/paper.sqlite3", "/api/../.env", "/../.env",
                      "/%2e%2e/.env", "/%2eenv", "/D:/BotTradingFinalBoss/.env", "/api/dashboard/extra"):
            with self.subTest(route=route):
                status, _, body = self.request(route)
                self.assertEqual(status, 404)
                self.assertNotIn(b"never-expose", body)
                self.assertNotIn(str(self.root).encode(), body)
        self.assertEqual(self.request("/health")[0], 200)
        self.assertEqual(self.request("/favicon.ico")[0], 204)

    def test_script_csp_uses_html_normalized_windows_line_endings(self):
        self.html_path.write_bytes(HTML.replace(b"\n", b"\r\n"))
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        expected = base64.b64encode(hashlib.sha256(b"\n'use strict';\ndocument.title='Final Boss';\n").digest()).decode()
        wrong = base64.b64encode(hashlib.sha256(b"\r\n'use strict';\r\ndocument.title='Final Boss';\r\n").digest()).decode()
        self.assertIn("'sha256-"+expected+"'", headers["Content-Security-Policy"])
        self.assertNotIn(wrong, headers["Content-Security-Policy"])
        self.assertIn(b"\r\n", body)

    def test_host_origin_and_fetch_site_checks(self):
        for headers in ({"Host": "attacker.example"}, {"Host": "127.0.0.1:1"},
                        {"Origin": "https://attacker.example"}, {"Origin": "null"},
                        {"Origin": "http://127.0.0.1:1"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                status, _, body = self.request(headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn(b"initial_equity", body)
        local = f"http://127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request(headers={"Origin": local, "Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(self.request(headers={"Host": f"localhost:{self.server.server_port}"})[0], 200)

    def test_http_mutations_are_rejected(self):
        before = self.state_path.read_bytes()
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method=method)[0], 405)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_security_headers_and_inline_script_csp_match_served_html(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, HTML)
        scripts = [b"\n'use strict';\ndocument.title='Final Boss';\n", b"window.ready=true;"]
        for script in scripts:
            digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
            self.assertIn("'sha256-"+digest+"'", headers["Content-Security-Policy"])
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("script-src 'unsafe-inline'", headers["Content-Security-Policy"])
        for route in ("/", "/api/dashboard", "/health", "/missing"):
            _, headers, _ = self.request(route)
            self.assertIn("no-store", headers["Cache-Control"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_head_has_no_response_body(self):
        status, headers, body = self.request("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["Content-Length"]), len(HTML))

    def test_missing_telemetry_preserves_financial_report_without_fabricated_charts(self):
        report = dashboard_snapshot(self.state_path, now=NOW)
        self.assertEqual(report["equity"], 100)
        self.assertFalse(report["telemetry_available"])
        self.assertFalse(report["market_data"])
        self.assertTrue(all(a["status"] == "waiting" for a in report["agent_activity"].values()))

    def test_malformed_telemetry_preserves_financial_api(self):
        for content in (b"{broken", b"[]", b'{"schema_version":2}',
                        b'{"schema_version":1,"updated_at":NaN}',
                        b'{"schema_version":1,"agent_activity":{"trend":{"last_result":1e309}}}',
                        b'{"schema_version":1,"agent_activity":{"trend":{"last_result":'+b'['*500+b'0'+b']'*500+b'}}}',
                        b'{"schema_version":1,"agent_activity":{"trend":{"last_result":'+b'['*1100+b'0'+b']'*1100+b'}}}',
                        b" " * 4_000_001):
            with self.subTest(content=content[:70]):
                self.activity_path.write_bytes(content)
                status, _, body = self.request()
                self.assertEqual(status, 200)
                report = json.loads(body)
                self.assertEqual(report["equity"], 100)
                self.assertFalse(report["telemetry_available"])
                self.assertTrue(report["telemetry_error"])

    def test_stale_telemetry_or_stale_cycle_never_claim_running(self):
        for activity_age, cycle_age in ((91, 0), (0, 181), (91, 181)):
            with self.subTest(activity_age=activity_age, cycle_age=cycle_age):
                self.activity(dict(telemetry(), updated_at=NOW-activity_age))
                self.state["heartbeat"] = NOW-cycle_age
                self.save_state()
                report = dashboard_snapshot(self.state_path, now=NOW)
                self.assertNotEqual(report["agent_activity"]["trend"]["status"], "running")
                self.assertEqual(report["agent_activity"]["trend"]["recorded_status"], "running")

    def test_impossible_future_telemetry_never_claims_running(self):
        self.activity(dict(telemetry(), updated_at=NOW+100))
        report = dashboard_snapshot(self.state_path, now=NOW)
        self.assertFalse(report["runtime"]["telemetry_fresh"])
        self.assertNotEqual(report["agent_activity"]["trend"]["status"], "running")

    def test_research_schedule_falls_back_when_recorder_has_explicit_null(self):
        payload = telemetry()
        payload["agent_activity"]["research_validation"] = dict(status="waiting", next_run_at=None)
        self.activity(payload)
        report = dashboard_snapshot(self.state_path, now=NOW)
        self.assertEqual(report["agent_activity"]["research_validation"]["next_run_at"], self.state["daily_schedule"]["next_review_at"])

    def test_old_or_future_closed_candles_are_not_fresh_when_fetch_timestamp_is_new(self):
        for shift in (-86400, 600):
            with self.subTest(shift=shift):
                payload = telemetry()
                candle = payload["market_data"]["DOGEUSDT"]["candles"][0]
                candle["open_time"] += shift*1000
                candle["close_time"] += shift*1000
                self.activity(payload)
                report = dashboard_snapshot(self.state_path, now=NOW)
                self.assertFalse(report["market_data"]["DOGEUSDT"]["fresh"])
                self.assertEqual(report["equity"], 100)

    def test_candle_freshness_uses_configured_interval(self):
        payload = telemetry()
        candle = payload["market_data"]["DOGEUSDT"]["candles"][0]
        candle.update(close_time=(NOW-600)*1000, open_time=(NOW-1500)*1000+1)
        self.activity(payload)
        five = dashboard_snapshot(self.state_path, now=NOW, interval="5m")
        fifteen = dashboard_snapshot(self.state_path, now=NOW, interval="15m")
        self.assertFalse(five["market_data"]["DOGEUSDT"]["fresh"])
        self.assertTrue(fifteen["market_data"]["DOGEUSDT"]["fresh"])
        self.assertEqual(fifteen["interval"], "15m")

    def test_quote_mark_and_candle_freshness_are_separate(self):
        payload = telemetry()
        market = payload["market_data"]["DOGEUSDT"]
        market["mark_fetched_at"] = NOW-91
        self.activity(payload)
        row = dashboard_snapshot(self.state_path, now=NOW)["market_data"]["DOGEUSDT"]
        self.assertFalse(row["mark_fresh"])
        self.assertTrue(row["price_fresh"])
        self.assertTrue(row["candles_fresh"])
        market["fetched_at"] = NOW-91
        self.activity(payload)
        row = dashboard_snapshot(self.state_path, now=NOW)["market_data"]["DOGEUSDT"]
        self.assertFalse(row["price_fresh"])
        self.assertFalse(row["fresh"])
        self.assertTrue(row["candles_fresh"])

    def test_closed_candle_cannot_use_clock_drift_grace_to_include_the_future(self):
        payload = telemetry()
        payload["market_data"]["DOGEUSDT"]["candles"][0]["close_time"] = (NOW+1)*1000
        self.activity(payload)
        row = dashboard_snapshot(self.state_path, now=NOW)["market_data"]["DOGEUSDT"]
        self.assertTrue(row["price_fresh"])
        self.assertFalse(row["candles_fresh"])

    def test_dashboard_only_emits_configured_symbols_and_keeps_bounded_events(self):
        payload = telemetry()
        payload["market_data"]["NOTCONFIGUREDUSDT"] = dict(price=999, fetched_at=NOW)
        payload["agent_events"] = [dict(id=str(i)) for i in range(500)]
        self.activity(payload)
        report = dashboard_snapshot(self.state_path, now=NOW)
        self.assertNotIn("NOTCONFIGUREDUSDT", report["market_data"])
        self.assertEqual(len(report["agent_events"]), 200)
        self.assertEqual(report["agent_events"][0]["id"], "300")

    def test_read_requests_do_not_change_state_activity_html_or_secrets(self):
        self.activity()
        sources = [self.state_path, self.activity_path, self.html_path, self.root / ".env"]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sources}
        for route in ("/", "/api/dashboard", "/health", "/.env"):
            self.request(route)
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sources}
        self.assertEqual(after, before)
        connection = sqlite3.connect(self.state_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        finally:
            connection.close()

    def test_invalid_state_or_mode_returns_generic_503_without_paths(self):
        for mode, content in (("paper", b"not sqlite"), ("demo", None)):
            with self.subTest(mode=mode):
                if content is not None:
                    self.state_path.write_bytes(content)
                else:
                    self.state_path.unlink()
                    self.save_state()
                self.server.mode = mode
                status, _, body = self.request()
                self.assertEqual(status, 503)
                result = json.loads(body)
                self.assertEqual(result["retry_seconds"], 2)
                self.assertNotIn(str(self.root).encode(), body)
                self.assertNotIn(b"sqlite", body.lower())
                self.assertNotIn(b"traceback", body.lower())

    def test_loopback_bind_and_import_have_no_exchange_dependency(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        script = "import sys; import bot.dashboard; assert 'bot.exchange' not in sys.modules; assert 'bot.engine' not in sys.modules"
        process = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                 capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
