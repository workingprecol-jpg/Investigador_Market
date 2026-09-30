import copy
import json
import math
import unittest
from datetime import datetime, timezone

from bot.config import Config
from bot.daily_analysis import ResearchValidationAgent


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


START = ts("2026-09-07T05:00:00")
END = ts("2026-09-08T05:00:00")


def trade(index=0, **changes):
    return dict({"trade_id": f"fill-{index}", "position_id": f"p-{index}",
                 "decision_id": f"d-{index}", "position_closed": True,
                 "symbol": "DOGEUSDT", "direction": 1, "opened_at": START + index * 10 + 1,
                 "closed_at": START + index * 10 + 5, "fees": .02,
                 "funding": -.01, "gross_pnl": -.97, "net_pnl": -1.0}, **changes)


def event(index=0, **changes):
    payload = {"decision_id": f"d-{index}", "symbol": "DOGEUSDT", "allowed": True, "agents": [
        {"agent": "trend", "score": .4, "veto": False, "details": {"regime": "bullish"}},
        {"agent": "momentum", "score": .05, "veto": False, "details": {"atr": .01}},
        {"agent": "liquidity", "score": 0, "veto": False, "details": {"spread_bps": 11}},
        {"agent": "market_context", "score": 0, "veto": False, "details": {"fresh": False, "available": False}},
        {"agent": "risk", "score": 1, "veto": False, "details": {"risk_usdt": .5}},
    ]}
    return dict({"id": index + 1, "ts": START + index * 10, "kind": "analysis", "data": payload}, **changes)


def review(trades=None, events=None, config=None, **kwargs):
    return ResearchValidationAgent(config or Config()).review(
        {"trades": trades or []}, events or [], "2026-09-07", kwargs.get("cutoff", END))


def specialist(report, name):
    return next(s for s in report["specialists"] if s["id"] == name)


class DailyAnalysisTest(unittest.TestCase):
    def test_empty_history_has_six_specialists_without_fabricated_metrics(self):
        result = review()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(len(result["specialists"]), 6)
        self.assertEqual(result["decision"], "collect_data")
        self.assertEqual(result["periods"]["all"]["count"], 0)
        self.assertIsNone(result["periods"]["all"]["win_rate_pct"])
        self.assertIsNone(result["periods"]["all"]["profit_factor"])
        self.assertTrue(all(p["status"] == "insufficient_data" for p in result["proposals"]))

    def test_day_is_bogota_and_cutoff_is_exclusive(self):
        rows = [trade(0, closed_at=START - 1), trade(1, closed_at=START),
                trade(2, closed_at=END - 1), trade(3, closed_at=END)]
        result = review(rows)
        self.assertEqual(result["periods"]["day"]["count"], 2)
        self.assertEqual(result["periods"]["all"]["count"], 3)
        self.assertEqual(result["periods"]["day"]["start_utc"], "2026-09-07T05:00:00+00:00")

    def test_complete_calendar_windows_include_review_day(self):
        times = [START - 30 * 86400, START - 29 * 86400, START - 7 * 86400,
                 START - 6 * 86400, START - 1, START]
        result = review([trade(i, closed_at=at) for i, at in enumerate(times)])
        self.assertEqual([result["periods"][key]["count"] for key in ("day", "days7", "days30", "all")], [1, 3, 5, 6])

    def test_future_cutoff_cannot_leak_next_days_records(self):
        result = review([trade(), trade(1, closed_at=END + 100)], [event(), event(1, ts=END + 100)], cutoff=END + 86400)
        self.assertEqual(result["periods"]["all"]["count"], 1)
        self.assertEqual(specialist(result, "trend")["evidence"]["observations"], 1)
        self.assertEqual(result["cutoff_utc"], "2026-09-08T05:00:00+00:00")

    def test_partial_day_is_marked_and_filtered(self):
        result = review([trade(), trade(1)], cutoff=START + 10)
        self.assertFalse(result["day_complete"])
        self.assertEqual(result["periods"]["day"]["count"], 1)

    def test_invalid_cutoff_rejected(self):
        for value in (math.nan, math.inf, -math.inf, START, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                review(cutoff=value)

    def test_partial_exits_group_at_final_close_with_all_costs(self):
        first = trade(0, closed_at=START - 1, position_closed=False, net_pnl=-.4)
        final = trade(1, position_id="p-0", decision_id="d-0", net_pnl=1.4)
        result = review([first, final])
        daily = result["periods"]["day"]
        self.assertEqual(daily["count"], 1)
        self.assertEqual(daily["exit_records"], 2)
        self.assertAlmostEqual(daily["net_pnl"], 1)
        self.assertAlmostEqual(daily["fees"], .04)
        self.assertAlmostEqual(daily["funding"], -.02)

    def test_partial_only_position_not_counted_before_final_exit(self):
        rows = [trade(position_closed=False), trade(1, position_id="p-0", closed_at=END + 1)]
        result = review(rows)
        self.assertEqual(result["periods"]["all"]["count"], 0)
        self.assertEqual(specialist(result, "research_validation")["evidence"]["excluded_partial_positions"], 1)

    def test_remaining_quantity_without_completion_flag_marks_partial(self):
        row = trade(remaining_quantity=.5)
        row.pop("position_closed")
        self.assertEqual(review([row])["periods"]["all"]["count"], 0)
        row["remaining_quantity"] = 0
        self.assertEqual(review([row])["periods"]["all"]["verified_closed_positions"], 1)

    def test_final_flag_respects_engine_rounding_tolerance_and_entry_regime(self):
        row = trade(position_closed=True, remaining_quantity=1e-10, entry_context={"regime": "range"})
        metrics = review([row])["periods"]["all"]
        self.assertEqual(metrics["verified_closed_positions"], 1)
        self.assertEqual(metrics["by_regime"]["range"]["count"], 1)

    def test_duplicate_fills_are_deduped_by_durable_identity_only(self):
        first = trade()
        copy_fill = copy.deepcopy(first)
        distinct = trade(1, opened_at=first["opened_at"], closed_at=first["closed_at"])
        result = review([first, copy_fill, distinct])
        self.assertEqual(result["periods"]["all"]["count"], 2)
        self.assertEqual(specialist(result, "research_validation")["evidence"]["duplicate_trade_records"], 1)

    def test_legacy_rows_remain_independent_and_not_attributed(self):
        row = {key: value for key, value in trade().items() if key not in ("trade_id", "position_id", "decision_id", "position_closed")}
        old_event = event()
        old_event.pop("id")
        old_event["data"].pop("decision_id")
        result = review([row, copy.deepcopy(row)], [old_event, copy.deepcopy(old_event)])
        self.assertEqual(result["periods"]["all"]["count"], 2)
        self.assertEqual(result["periods"]["all"]["legacy_record_count"], 2)
        self.assertEqual(specialist(result, "trend")["evidence"]["attributed_positions"], 0)
        quality = specialist(result, "research_validation")["evidence"]
        self.assertEqual(quality["duplicate_analysis_records"], 1)
        self.assertEqual(quality["legacy_analysis_records"], 1)

    def test_exact_decision_link_and_regime_recovery(self):
        result = review([trade()], [event()])
        self.assertEqual(specialist(result, "trend")["evidence"]["attributed_positions"], 1)
        self.assertEqual(result["periods"]["all"]["by_regime"]["bullish"]["count"], 1)

    def test_no_time_matching_or_decision_after_entry_attribution(self):
        for mutation in ({"decision_id": "unrelated"}, {"opened_at": START - 1}, {"symbol": "SOLUSDT"}):
            with self.subTest(mutation=mutation):
                result = review([trade(**mutation)], [event()])
                self.assertEqual(specialist(result, "trend")["evidence"]["attributed_positions"], 0)

    def test_ambiguous_decision_id_is_not_attributed(self):
        a, b = event(), event(1)
        b["data"]["decision_id"] = "d-0"
        result = review([trade()], [a, b])
        self.assertEqual(specialist(result, "trend")["evidence"]["attributed_positions"], 0)
        self.assertEqual(specialist(result, "research_validation")["evidence"]["ambiguous_decision_ids"], 1)

    def test_attribution_does_not_assign_profit_to_blocked_signal(self):
        a = event()
        a["data"]["allowed"] = False
        a["data"]["agents"][0]["veto"] = True
        result = review([trade()], [a])
        evidence = specialist(result, "trend")["evidence"]
        self.assertEqual(evidence["veto_count"], 1)
        self.assertEqual(evidence["attributed_positions"], 0)
        self.assertFalse(evidence["causal_effect_estimated"])

    def test_safe_proposals_need_sample_and_do_not_mutate_inputs(self):
        rows, events = [trade(i) for i in range(30)], [event(i) for i in range(30)]
        original = copy.deepcopy((rows, events))
        result = review(rows, events)
        self.assertEqual((rows, events), original)
        self.assertEqual(result["decision"], "evaluate_candidates")
        patches = {p["agent"]: p["patch"] for p in result["proposals"]}
        self.assertEqual(patches, {"trend": {"min_trend_score": .65}, "momentum": {"min_momentum_score": .1},
                                  "liquidity": {"max_spread_bps": 9.0}, "market_context": {"require_fresh_news": True},
                                  "risk": {"risk_per_trade": .00375}})
        self.assertTrue(all(p["status"] == "proposed" and not p["applied"] for p in result["proposals"]))
        self.assertTrue(all(len(p["patch"]) == 1 for p in result["proposals"]))
        repeated = review(rows, events)
        self.assertEqual([p["id"] for p in result["proposals"]], [p["id"] for p in repeated["proposals"]])

    def test_twenty_nine_positions_cannot_propose_changes(self):
        result = review([trade(i) for i in range(29)], [event(i) for i in range(29)])
        self.assertEqual(result["decision"], "collect_data")
        self.assertTrue(all(p["status"] == "insufficient_data" for p in result["proposals"]))

    def test_many_observations_without_attribution_cannot_support_agent_filter(self):
        rows = [trade(i, decision_id=None) for i in range(30)]
        result = review(rows, [event(i) for i in range(100)])
        for proposal in result["proposals"]:
            self.assertEqual(proposal["status"], "proposed" if proposal["agent"] == "risk" else "insufficient_data")

    def test_no_change_when_history_does_not_support_hypothesis(self):
        result = review([trade(i, net_pnl=1) for i in range(30)], [event(i) for i in range(30)])
        self.assertTrue(all(p["status"] == "no_change" for p in result["proposals"]))

    def test_existing_tighter_limits_are_not_relaxed(self):
        config = {"min_trend_score": .8, "min_momentum_score": .2,
                  "max_spread_bps": .5, "require_fresh_news": True, "risk_per_trade": .001}
        result = review([trade(i) for i in range(30)], [event(i) for i in range(30)], config=config)
        for proposal in result["proposals"]:
            if proposal["agent"] != "risk":
                self.assertEqual(proposal["status"], "no_change")
        self.assertEqual(result["proposals"][-1]["patch"], {"risk_per_trade": .00075})

    def test_missing_costs_and_metadata_are_explicit(self):
        result = review([trade(fees=None, funding=None, direction=None, symbol=None, regime=None, decision_id=None)])
        metrics = result["periods"]["all"]
        self.assertIsNone(metrics["fees"])
        self.assertIsNone(metrics["funding"])
        self.assertEqual(metrics["missing_metadata"]["fees"], 1)
        self.assertIn("unknown", metrics["by_symbol"])
        self.assertIn("unknown", metrics["by_direction"])
        self.assertIn("unknown", metrics["by_regime"])

    def test_nonfinite_records_are_excluded_and_json_remains_strict(self):
        rows = [trade(), trade(1, net_pnl=math.nan), trade(2, closed_at=math.inf),
                trade(3, fees=math.nan), trade(4, funding=math.inf)]
        a = event()
        a["data"]["agents"][0]["score"] = math.nan
        result = review(rows, [a, event(1, ts=math.nan)])
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["periods"]["all"]["count"], 3)
        self.assertEqual(specialist(result, "research_validation")["evidence"]["invalid_trade_records"], 2)
        self.assertEqual(specialist(result, "trend")["evidence"]["invalid_or_missing_scores"], 1)

    def test_numeric_overflow_is_not_reported_as_infinite_profit_factor(self):
        result = review([trade(0, net_pnl=1e308), trade(1, net_pnl=-1e-300)])
        self.assertIsNone(result["periods"]["all"]["profit_factor"])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
