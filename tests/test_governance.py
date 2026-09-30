import copy
import math
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timezone

from bot.config import Config
from bot.governance import (apply_ready_promotion, effective_config, ensure_strategy,
                            fingerprint, queue_review, rollback, validate_patch)
from bot.risk import new_state
from bot.shadow import DAY, ShadowLab


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


NOW = ts("2026-09-08T05:10:00")
END = ts("2026-09-08T05:00:00")


def report(**changes):
    return dict({"schema_version": 1, "status": "completed", "preview": False, "day_complete": True,
                 "mode": "paper", "date": "2026-09-07", "cutoff_utc": "2026-09-08T05:00:00+00:00",
                 "generated_at": "2026-09-08T05:10:00+00:00", "source_version": 1,
                 "proposals": [{"id": "trend-1", "agent": "trend", "patch": {"min_trend_score": .65},
                                "status": "proposed"}]}, **changes)


def validated_state():
    config = Config()
    state = new_state(config)
    ensure_strategy(state, config)
    proposal = report()["proposals"][0]
    started, evaluated = END - 8 * DAY, END - 3600
    lab = ShadowLab(config)
    assert lab.start(state, proposal, started)
    experiment = state["experiments"][proposal["id"]]
    experiment.update(source_version=1, coverage_seconds=7 * DAY, observations=21000, last_update=evaluated)
    experiment["marks"] = {symbol: {"time": evaluated} for symbol in config.symbols}
    for name, pnl, dd in (("baseline", .02, .02), ("candidate", .03, .01)):
        portfolio = experiment[name]
        portfolio["trades"] = [{"opened_at": started + i * 60 + 1, "closed_at": started + i * 60 + 10,
                                "net_pnl": pnl, "initial_risk": .1, "fees": .002, "funding": 0,
                                "gross_pnl": pnl + .002, "mode": "shadow_paper"} for i in range(30)]
        portfolio.update(equity=100 + 30 * pnl, balance=100 + 30 * pnl,
                         liquidation_equity=100 + 30 * pnl, max_observed_drawdown=dd)
    decisions = lab.evaluate(state, evaluated)
    assert len(decisions) == 1 and decisions[0]["recommendation"] == "promote"
    state["pending_promotions"] = decisions
    assert queue_review(state, report(proposals=[]), config, NOW)
    return config, state


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.state = new_state(self.config)
        ensure_strategy(self.state, self.config)

    def test_whitelist_never_changes_hard_limits(self):
        for field, value in (("leverage", 10), ("mode", "live"), ("initial_equity", 1000),
                             ("max_total_loss_usdt", 50), ("max_daily_loss_pct", .1),
                             ("max_drawdown_pct", .2), ("max_positions", 10)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_patch(self.config, {field: value})

    def test_each_proposal_is_one_field_owned_by_its_specialist(self):
        with self.assertRaises(ValueError):
            validate_patch(self.config, {"min_trend_score": .65, "min_momentum_score": .1})
        with self.assertRaises(ValueError):
            validate_patch(self.config, {"risk_per_trade": .004}, agent="trend")

    def test_absolute_and_current_risk_bounds_are_not_relaxed(self):
        for patch in ({"risk_per_trade": .006}, {"risk_per_trade": .0001}, {"max_spread_bps": .5},
                      {"max_spread_bps": 15}, {"min_trend_score": .96}, {"min_momentum_score": math.nan},
                      {"risk_per_trade": True}, {"risk_per_trade": 10**10000}):
            with self.subTest(patch=list(patch)), self.assertRaises(ValueError):
                validate_patch(self.config, patch)
        tighter = replace(self.config, risk_per_trade=.003, min_trend_score=.7, require_fresh_news=True)
        for patch in ({"risk_per_trade": .004}, {"min_trend_score": .6}, {"require_fresh_news": False}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate_patch(self.config, patch, current=tighter)

    def test_fingerprint_preserves_operational_timing_flexibility(self):
        self.assertEqual(fingerprint(self.config), fingerprint(replace(self.config, daily_review_time="01:00")))
        self.assertNotEqual(fingerprint(self.config), fingerprint(replace(self.config, fee_rate=.001)))

    def test_base_config_change_requires_review(self):
        with self.assertRaises(ValueError):
            effective_config(replace(self.config, max_spread_bps=10), self.state)

    def test_completed_current_report_queues_once_without_mutating_report(self):
        value = report()
        old = copy.deepcopy(value)
        self.assertTrue(queue_review(self.state, value, self.config, NOW))
        self.assertTrue(queue_review(self.state, value, self.config, NOW))
        self.assertEqual(len(self.state["proposal_queue"]), 1)
        self.assertEqual(self.state["proposal_queue"][0]["base_version"], 1)
        self.assertEqual(value, old)

    def test_preview_future_incomplete_and_stale_reports_never_approve(self):
        cases = [report(preview=True), report(status="preview"), report(day_complete=False),
                 report(source_version=0), report(mode="demo"), report(generated_at="2026-09-09T00:00:00+00:00"),
                 report(cutoff_utc="2026-09-08T06:00:00+00:00")]
        for value in cases:
            with self.subTest(value=value):
                state = copy.deepcopy(self.state)
                state["pending_promotions"] = [{"id": "unapproved", "evaluated_at": END - 1}]
                self.assertFalse(queue_review(state, value, self.config, NOW))
                self.assertEqual(state["proposal_queue"], [])
                self.assertNotIn("daily_approved_at", state["pending_promotions"][0])

    def test_daily_approval_requires_evaluation_inside_closed_day(self):
        self.state["pending_promotions"] = [{"id": "after-cutoff", "evaluated_at": END + 1}]
        queue_review(self.state, report(proposals=[]), self.config, NOW)
        self.assertNotIn("daily_approved_at", self.state["pending_promotions"][0])

    def test_real_validated_comparison_promotes_when_flat_and_daily_approved(self):
        config, state = validated_state()
        self.assertTrue(apply_ready_promotion(state, config, NOW))
        self.assertEqual(state["strategy"]["version"], 2)
        self.assertEqual(state["strategy"]["patch"], {"min_trend_score": .65})
        effective = effective_config(config, state)
        for key in ("leverage", "max_total_loss_usdt", "initial_equity", "max_positions", "mode"):
            self.assertEqual(getattr(effective, key), getattr(config, key))
        self.assertEqual(state["strategy"]["history"][-1]["evidence"]["candidate"]["closed_trades"], 30)
        self.assertFalse(apply_ready_promotion(state, config, NOW))

    def test_positions_pending_orders_pause_or_halt_prevent_promotion(self):
        for field, value in (("positions", {"DOGEUSDT": {}}), ("pending", {"order": {}}),
                             ("halt_reason", "halt"), ("daily_paused", True)):
            with self.subTest(field=field):
                config, state = validated_state()
                state[field] = value
                self.assertFalse(apply_ready_promotion(state, config, NOW))
                self.assertEqual(state["strategy"]["version"], 1)
                self.assertEqual(len(state["pending_promotions"]), 1)

    def test_unapproved_or_unknown_experiment_cannot_promote(self):
        config, state = validated_state()
        state["pending_promotions"][0].pop("daily_approved_at")
        self.assertFalse(apply_ready_promotion(state, config, NOW))
        config, state = validated_state()
        state["experiments"] = {}
        self.assertFalse(apply_ready_promotion(state, config, NOW))

    def test_wrong_source_version_or_field_cannot_promote(self):
        for mutation in ("source", "patch", "agent", "frozen", "candidate_config"):
            with self.subTest(mutation=mutation):
                config, state = validated_state()
                experiment = state["experiments"]["trend-1"]
                if mutation == "source":
                    experiment["source_version"] = 0
                elif mutation == "patch":
                    state["pending_promotions"][0]["patch"] = {"leverage": 10}
                elif mutation == "agent":
                    state["pending_promotions"][0]["agent"] = "risk"
                elif mutation == "frozen":
                    experiment["frozen_config"]["fee_rate"] = 0
                else:
                    experiment["candidate_config"]["risk_per_trade"] = .001
                self.assertFalse(apply_ready_promotion(state, config, NOW))

    def test_recomputed_trade_metrics_detect_forged_promote_label(self):
        for mutation in ("negative", "few", "drawdown", "limits", "coverage", "hash", "future"):
            with self.subTest(mutation=mutation):
                config, state = validated_state()
                experiment = state["experiments"]["trend-1"]
                candidate = experiment["candidate"]
                if mutation == "negative":
                    candidate["trades"][0]["net_pnl"] = -10
                elif mutation == "few":
                    candidate["trades"] = candidate["trades"][:5]
                elif mutation == "drawdown":
                    candidate["max_observed_drawdown"] = .1
                elif mutation == "limits":
                    candidate["limits_breached"] = True
                elif mutation == "coverage":
                    experiment["coverage_seconds"] = 100
                elif mutation == "hash":
                    experiment["evidence_hash"] = "x" * 64
                else:
                    candidate["trades"][0]["closed_at"] = NOW + 100
                self.assertFalse(apply_ready_promotion(state, config, NOW))
                self.assertEqual(state["strategy"]["patch"], {})

    def test_mutating_decision_metrics_is_not_an_approval(self):
        config, state = validated_state()
        state["pending_promotions"][0]["candidate"]["closed_trades"] = 10000
        self.assertFalse(apply_ready_promotion(state, config, NOW))

    def test_rollback_restores_saved_predecessor_and_versions_it(self):
        config, state = validated_state()
        self.assertTrue(apply_ready_promotion(state, config, NOW))
        self.assertTrue(rollback(state, config, NOW + 1))
        self.assertEqual(state["strategy"]["patch"], {})
        self.assertEqual(state["strategy"]["version"], 3)
        self.assertEqual(state["strategy"]["history"][-1]["reverts_version"], 2)
        with self.assertRaises(ValueError):
            rollback(state, config, NOW + 2)

    def test_rollback_rejects_arbitrary_backup_or_nonflat_portfolio(self):
        for mutation in ("backup", "current", "open"):
            with self.subTest(mutation=mutation):
                config, state = validated_state()
                apply_ready_promotion(state, config, NOW)
                if mutation == "backup":
                    state["strategy"]["history"][-1]["previous_patch"] = {"risk_per_trade": .002}
                elif mutation == "current":
                    state["strategy"]["patch"] = {"min_trend_score": .7}
                else:
                    state["positions"] = {"DOGEUSDT": {}}
                with self.assertRaises(ValueError):
                    rollback(state, config, NOW + 1)


if __name__ == "__main__":
    unittest.main()
