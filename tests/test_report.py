import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Config
from bot.report import build_report, write_report
from bot.risk import new_state


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.state = new_state(Config())
        self.state.update(heartbeat=time.time()-20, healthy=True)

    def render(self, state=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"report.html"
            write_report(build_report(state or self.state), path)
            return path.read_text(encoding="utf-8")

    def test_legacy_state_preserves_metrics_and_does_not_invent_history(self):
        report = build_report(self.state)
        self.assertEqual(report["equity"], 100)
        self.assertEqual(report["closed_trades"], 0)
        self.assertIsNone(report["profit_factor"])
        self.assertEqual(report["equity_history"], [])
        page = self.render()
        self.assertIn("No se reconstruye un historial ficticio", page)
        self.assertNotIn("<polyline", page)
        self.assertIn("no verifica que el proceso siga activo", page)
        self.assertIn("No hay experimentos registrados", page)
        self.assertEqual(page.count('<article class="specialist">'), 6)

    def test_report_contains_full_daily_contract_without_mutating_state(self):
        self.state.update(
            daily_review={"date": "2026-09-06", "periods": {"day": {"closed_trades": 2}},
                          "specialists": [{"agent": "trend", "assessment": "Mantener reglas"}],
                          "decision": "collect_more_data", "next_review_at": "2026-09-08T00:10:00-05:00"},
            strategy={"version": 2, "patch": {"score_threshold": .7}, "history": []},
            experiments={"exp-1": {"status": "running", "baseline": {"balance": 100}}},
            decisions_audit=[{"ts": 1000, "symbol": "ETHUSDT", "status": "vetoed", "reason": "Spread alto"}],
        )
        before = copy.deepcopy(self.state)
        report = build_report(self.state)
        self.assertEqual(report["daily_review"], self.state["daily_review"])
        self.assertEqual(report["strategy"], self.state["strategy"])
        self.assertEqual(report["decisions_audit"], self.state["decisions_audit"])
        page = self.render()
        for expected in ("2026-09-06", "2026-09-08T00:10:00-05:00", "Mantener reglas", "exp-1", "Spread alto"):
            self.assertIn(expected, page)
        self.assertEqual(self.state, before)

    def test_chart_uses_only_recorded_finite_equity_points(self):
        self.state["equity_history"] = [{"ts": 1, "equity": 100}, {"ts": 2, "equity": 101},
                                        {"ts": 3, "equity": None}, {"ts": 4, "equity": float("nan")},
                                        {"ts": True, "equity": 99}]
        page = self.render()
        self.assertIn('aria-label="Equidad observada en USDT"', page)
        self.assertIn("2 observaciones registradas", page)
        self.assertNotRegex(page.lower(), r"\bnan\b")
        self.assertIn("<polyline", page)

    def test_every_external_text_is_escaped(self):
        payload = '<script src="https://example.com/steal">attack()</script>'
        self.state.update(halt_reason=payload, daily_review={"decision": payload, "specialists": {"trend": payload}},
                          experiments={"test": payload}, strategy={"version": payload},
                          decisions_audit=[{"symbol": payload, "reason": payload, "status": payload}],
                          last_analysis={payload: {"agents": [{"agent": "trend", "reason": payload}]}})
        page = self.render()
        self.assertEqual(page.count("<script"), 1)
        self.assertIn('<script id="report-browser-controls">', page)
        self.assertNotIn(payload, page)
        self.assertNotIn('src="https://example.com/steal"', page)
        self.assertIn("&lt;script", page)
        self.assertNotIn('<iframe', page)

    def test_trade_costs_and_profit_factor_are_actual_values(self):
        self.state["trades"] = [dict(symbol="ETHUSDT", direction=1, gross_pnl=2, fees=.2, funding=-.1,
                                     net_pnl=1.7, reason="target", closed_at=1000),
                                 dict(symbol="DOGEUSDT", direction=-1, gross_pnl=-1, fees=.1, funding=.1,
                                      net_pnl=-1, reason="stop", closed_at=2000)]
        self.state.update(balance=100.7, equity=100.8, peak_equity=101)
        report = build_report(self.state)
        self.assertAlmostEqual(report["profit_factor"], 1.7)
        self.assertAlmostEqual(report["expectancy_usdt"], .35)
        self.assertEqual(report["win_rate_pct"], 50)
        page = self.render()
        self.assertIn("+1.7000", page)
        self.assertIn("0.2000", page)
        self.assertIn("-0.1000", page)
        self.assertIn("Neto = bruto − comisiones + financiación", page)

    def test_json_retains_new_structured_fields(self):
        self.state["daily_review"] = {"decision": "Mantener"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"report.json"
            write_report(build_report(self.state), path)
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["daily_review"]["decision"], "Mantener")
        self.assertIn("cycle_age_seconds", stored)
        self.assertIn("decisions_audit", stored)

    def test_stale_snapshot_is_not_presented_as_live(self):
        self.state["heartbeat"] = time.time()-10000
        report = build_report(self.state)
        self.assertGreater(report["cycle_age_seconds"], 9999)
        page = self.render()
        self.assertNotIn("LIVE", page)
        self.assertIn("antigüedad del ciclo", page)
        self.assertIn("Informe estático", page)

    def test_scheduled_time_and_all_daily_specialist_ids_are_rendered(self):
        self.state["daily_schedule"] = {"next_review_at": "2026-09-09T00:10:00-05:00", "running": True}
        self.state["daily_review"] = {"next_review_at": "2026-09-08T00:10:00-05:00", "specialists": [
            {"id": "market_context", "findings": ["Revisar cobertura de fuentes"]},
            {"id": "research_validation", "findings": ["Muestra aún insuficiente"]}]}
        page = self.render()
        self.assertIn("Revisar cobertura de fuentes", page)
        self.assertIn("Muestra aún insuficiente", page)
        self.assertIn('<span>Próxima revisión</span>2026-09-09T00:10:00-05:00', page)

    def test_browser_controls_use_escaped_timestamp_and_clear_local_mode(self):
        self.state["heartbeat"] = 1788815700
        report = build_report(self.state)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"report.html"
            write_report(report, path)
            page = path.read_text(encoding="utf-8")
        self.assertIn('datetime="'+report["last_cycle"]+'"', page)
        self.assertIn('id="refresh-now"', page)
        self.assertIn('id="refresh-auto"', page)
        self.assertIn("PAPER · simulación local", page)
        self.assertIn("Colombia (UTC−5)", page)
        self.assertIn("Sin actualización reciente", page)
        self.assertIn("reports/paper.html", page)

    def test_missing_cycle_and_demo_mode_are_explicit(self):
        self.state.update(heartbeat=0, mode="demo")
        page = self.render()
        self.assertIn('id="recency-status">Sin ciclo registrado', page)
        self.assertIn('id="last-cycle" datetime=""', page)
        self.assertIn("DEMO · órdenes virtuales Binance", page)

    def test_failed_atomic_publication_keeps_previous_complete_report(self):
        for extension in ("html", "json"):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/("report."+extension)
                path.write_text("previous complete report", encoding="utf-8")
                with patch("bot.report.Path.replace", side_effect=PermissionError("reader holds file")):
                    with self.assertRaises(PermissionError):
                        write_report(build_report(self.state), path)
                self.assertEqual(path.read_text(encoding="utf-8"), "previous complete report")
                self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
