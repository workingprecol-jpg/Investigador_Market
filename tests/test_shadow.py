import copy
import json
import unittest
from dataclasses import replace

from bot.config import Config
from bot.models import Advice, Candle, Snapshot
from bot.shadow import DAY, ShadowLab


NOW = 1_800_000_001


def snapshot(now=NOW, symbol="DOGEUSDT", price=100.0, spread=4.0, bar=None):
    start = int(now // 300 * 300 - 300) * 1000
    candle = bar or Candle(start, start+299999, price, price+.2, price-.2, price, 1000)
    half = price * spread / 20000
    return Snapshot(symbol, [candle], price-half, price+half, 20_000_000, now)


def premium(now=NOW, symbol="DOGEUSDT", price=100.0, rate=0.0, next_time=None):
    return dict(symbol=symbol, time=int(now*1000), markPrice=str(price), lastFundingRate=str(rate),
                nextFundingTime=int((next_time or now+3600)*1000),
                _symbol_filters={"symbol": symbol, "status": "TRADING", "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100000", "stepSize": "0.001"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100000", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]})


def reports(direction=1, fresh=True, parent_risk_veto=False):
    return [Advice("trend", direction*.8), Advice("momentum", direction*.8, details={"atr": 1}),
            Advice("liquidity", 0), Advice("market_context", 0, details={"fresh": fresh}),
            Advice("risk", 0, veto=parent_risk_veto)]


class ShadowLabTest(unittest.TestCase):
    def setUp(self):
        self.config = Config(symbols=("DOGEUSDT",))
        self.lab = ShadowLab(self.config)
        self.state = {}
        self.proposal = dict(id="proposal-1", agent="liquidity", patch={"max_spread_bps": 10.0},
                             hypothesis="Avoid more costly entries")
        self.assertTrue(self.lab.start(self.state, self.proposal, NOW))

    @property
    def experiment(self):
        return self.state["experiments"]["proposal-1"]

    def feed(self, now=NOW, symbol="DOGEUSDT", price=100.0, *, spread=4, bar=None, data=None, advice=None):
        self.lab.update(self.state, snapshot(now, symbol, price, spread, bar),
                        data or premium(now, symbol, price), advice or reports(), now)

    def test_empty_portfolios_and_frozen_config_are_independent(self):
        self.assertEqual(self.experiment["baseline"]["balance"], 100)
        self.assertFalse(self.experiment["baseline"]["positions"])
        self.experiment["baseline"]["trades"].append({"example": True})
        self.assertFalse(self.experiment["candidate"]["trades"])
        self.proposal["patch"]["max_spread_bps"] = 1
        self.assertEqual(self.experiment["patch"], {"max_spread_bps": 10.0})
        self.assertFalse(self.lab.start(self.state, dict(self.proposal, id="second"), NOW))

    def test_patch_allowlist_and_nonzero_costs(self):
        invalid = [dict(agent="liquidity", patch={"max_spread_bps": 15}),
                   dict(agent="risk", patch={"risk_per_trade": .009}),
                   dict(agent="risk", patch={"leverage": 10}),
                   dict(agent="liquidity", patch={"max_spread_bps": 9, "max_positions": 1}),
                   dict(agent="market_context", patch={"require_fresh_news": False})]
        for item in invalid:
            self.assertFalse(self.lab.start({}, dict(id="bad", **item), NOW))
        self.assertFalse(ShadowLab(replace(self.config, fee_rate=0)).start({}, self.proposal, NOW))
        self.assertFalse(ShadowLab(replace(self.config, slippage_bps=0)).start({}, self.proposal, NOW))
        self.assertFalse(self.lab.start({}, self.proposal, float("nan")))

    def test_identical_fills_and_fees_when_filter_does_not_differ(self):
        self.feed(advice=reports(parent_risk_veto=True))
        left, right = self.experiment["baseline"], self.experiment["candidate"]
        self.assertEqual(left["positions"], right["positions"])
        position = left["positions"]["DOGEUSDT"]
        self.assertGreater(position["entry_price"], snapshot().ask)
        self.assertLess(left["balance"], 100)
        self.assertGreater(position["initial_risk"], 0)
        self.assertLessEqual(position["initial_risk"], .5)
        self.assertLessEqual(position["quantity"]*position["entry_price"]/5, 10)
        self.assertLess(left["liquidation_equity"], left["balance"])

    def test_challenger_recomputes_tighter_liquidity(self):
        self.feed(spread=11)
        self.assertIn("DOGEUSDT", self.experiment["baseline"]["positions"])
        self.assertFalse(self.experiment["candidate"]["positions"])

    def test_news_patch_requires_fresh_news_without_inventing_context(self):
        state = {}
        proposal = dict(id="news", agent="market_context", patch={"require_fresh_news": True})
        self.assertTrue(self.lab.start(state, proposal, NOW))
        self.lab.update(state, snapshot(), premium(), reports(fresh=False), NOW)
        self.assertTrue(state["experiments"]["news"]["baseline"]["positions"])
        self.assertFalse(state["experiments"]["news"]["candidate"]["positions"])

    def test_directional_thresholds_are_independent_filters(self):
        for agent, field in (("trend", "min_trend_score"), ("momentum", "min_momentum_score")):
            state = {}
            self.assertTrue(self.lab.start(state, dict(id=agent, agent=agent, patch={field: .85}), NOW))
            self.lab.update(state, snapshot(), premium(), reports(), NOW)
            self.assertTrue(state["experiments"][agent]["baseline"]["positions"])
            self.assertFalse(state["experiments"][agent]["candidate"]["positions"])

    def test_risk_challenger_has_lower_size_not_larger_leverage(self):
        state = {}
        self.assertTrue(self.lab.start(state, dict(id="risk", agent="risk", patch={"risk_per_trade": .0025}), NOW))
        self.lab.update(state, snapshot(), premium(), reports(), NOW)
        baseline = state["experiments"]["risk"]["baseline"]["positions"]["DOGEUSDT"]
        candidate = state["experiments"]["risk"]["candidate"]["positions"]["DOGEUSDT"]
        self.assertLess(candidate["quantity"], baseline["quantity"])
        self.assertEqual(candidate["stop"], baseline["stop"])
        self.assertEqual(state["experiments"]["risk"]["candidate"]["leverage"], 5)

    def test_short_fills_and_stops_include_adverse_costs(self):
        self.feed(advice=reports(-1))
        position = self.experiment["baseline"]["positions"]["DOGEUSDT"]
        self.assertLess(position["entry_price"], snapshot().bid)
        self.feed(NOW+30, price=103, advice=reports(-1))
        trade = self.experiment["baseline"]["trades"][0]
        self.assertEqual(trade["reason"], "stop_loss")
        self.assertGreater(trade["exit_price"], snapshot(NOW+30, price=103).ask)
        self.assertLess(trade["net_pnl"], 0)

    def test_aggregate_portfolio_limit_is_two_positions(self):
        self.config = replace(self.config, symbols=("DOGEUSDT", "SOLUSDT", "ETHUSDT", "1000PEPEUSDT"))
        self.lab, self.state = ShadowLab(self.config), {}
        self.assertTrue(self.lab.start(self.state, self.proposal, NOW))
        for symbol in self.config.symbols:
            self.feed(symbol=symbol)
        self.assertEqual(len(self.experiment["baseline"]["positions"]), 2)
        self.assertEqual(len(self.experiment["candidate"]["positions"]), 2)

    def test_shared_news_risk_veto_is_preserved(self):
        advice = reports()
        advice[3] = Advice("market_context", 0, True, details={"fresh": True})
        self.feed(advice=advice)
        self.assertFalse(self.experiment["baseline"]["positions"])
        self.assertFalse(self.experiment["candidate"]["positions"])

    def test_coordinator_data_veto_blocks_entries_but_keeps_exit_management(self):
        self.feed(data=dict(premium(), _entries_blocked=True))
        self.assertFalse(self.experiment["baseline"]["positions"])
        self.feed(NOW+1)
        self.assertTrue(self.experiment["baseline"]["positions"])
        self.feed(NOW+30, price=97, data=dict(premium(NOW+30, price=97), _entries_blocked=True))
        self.assertFalse(self.experiment["baseline"]["positions"])
        self.assertEqual(len(self.experiment["baseline"]["trades"]), 1)

    def test_absent_exchange_filters_never_create_fills(self):
        data = premium()
        data.pop("_symbol_filters")
        self.feed(data=data)
        self.assertFalse(self.experiment["baseline"]["positions"])
        self.assertIn("Filtros", self.experiment["baseline"]["last_entry_rejection"])

    def test_quantity_minimum_is_never_rounded_up(self):
        data = premium()
        data["_symbol_filters"]["filters"][0]["minQty"] = "1"
        self.feed(data=data)
        self.assertFalse(self.experiment["baseline"]["positions"])

    def test_both_quantity_steps_are_enforced(self):
        data = premium()
        data["_symbol_filters"]["filters"][0]["stepSize"] = "0.003"
        data["_symbol_filters"]["filters"][1]["stepSize"] = "0.002"
        self.feed(data=data)
        quantity = self.experiment["baseline"]["positions"]["DOGEUSDT"]["quantity"]
        self.assertAlmostEqual(quantity/.006, round(quantity/.006))

    def test_stop_first_when_both_levels_cross_and_gap_fill_is_adverse(self):
        self.feed()
        now = NOW+600
        start = int(now//300*300-300)*1000
        bar = Candle(start, start+299999, 95, 110, 90, 100, 1000)
        self.feed(now, bar=bar)
        portfolio = self.experiment["baseline"]
        self.assertFalse(portfolio["positions"])
        self.assertEqual(len(portfolio["trades"]), 1)
        trade = portfolio["trades"][0]
        self.assertEqual(trade["reason"], "stop_loss")
        self.assertLess(trade["exit_price"], 95)
        self.assertAlmostEqual(portfolio["balance"]-100, trade["net_pnl"])
        self.assertGreater(trade["fees"], 0)
        self.feed(now+1, bar=bar)
        self.assertEqual(len(self.experiment["baseline"]["trades"]), 1)
        self.assertFalse(self.experiment["baseline"]["positions"])

    def test_quote_after_historical_stop_cannot_create_phantom_equity_peak(self):
        self.feed()
        now = NOW+600
        start = int(now//300*300-300)*1000
        stopped_bar = Candle(start, start+299999, 100, 100.5, 97, 100, 1000)
        self.feed(now, price=110, bar=stopped_bar)
        portfolio = self.experiment["baseline"]
        self.assertFalse(portfolio["positions"])
        self.assertEqual(portfolio["observed_peak"], 100)
        self.assertAlmostEqual(portfolio["max_observed_drawdown"], (100-portfolio["balance"])/100)

    def test_preentry_candle_and_duplicate_quote_cannot_create_historical_profit(self):
        first = snapshot()
        old = replace(first.candles[0], high=110, low=90)
        self.feed(bar=old)
        self.feed(NOW+1, bar=old)
        self.assertFalse(self.experiment["baseline"]["trades"])
        before = copy.deepcopy(self.experiment)
        self.feed(NOW+1, bar=old)
        self.assertEqual(before, self.experiment)

    def test_future_or_unclosed_data_is_not_consumed(self):
        fresh = snapshot()
        future = replace(fresh, fetched_at=NOW+1)
        self.lab.update(self.state, future, premium(), reports(), NOW)
        self.assertEqual(self.experiment["observations"], 0)
        unclosed = replace(fresh, candles=[replace(fresh.candles[0], close_time=int(NOW*1000+1))])
        self.lab.update(self.state, unclosed, premium(), reports(), NOW)
        self.assertEqual(self.experiment["observations"], 0)
        self.assertFalse(self.experiment["baseline"]["positions"])

    def test_invalid_advice_is_transactional_between_portfolios(self):
        advice = reports()
        advice[0] = Advice("trend", float("nan"))
        before = copy.deepcopy(self.experiment["baseline"])
        self.feed(advice=advice)
        self.assertEqual(before, self.experiment["baseline"])
        self.assertEqual(self.experiment["observations"], 0)

    def test_funding_checkpoint_survives_serialization_without_double_charge(self):
        self.feed(data=premium(next_time=NOW+30, rate=.001))
        self.feed(NOW+31, data=premium(NOW+31, next_time=NOW+3600, rate=.001))
        funded = self.experiment["baseline"]["positions"]["DOGEUSDT"]["funding"]
        self.assertLess(funded, 0)
        self.state = json.loads(json.dumps(self.state))
        self.lab = ShadowLab(self.config)
        self.feed(NOW+32, data=premium(NOW+32, next_time=NOW+3600, rate=.001))
        self.assertEqual(self.experiment["baseline"]["positions"]["DOGEUSDT"]["funding"], funded)

    def test_long_interruption_disqualifies_funding_sample(self):
        self.feed(data=premium(next_time=NOW+30, rate=.0001))
        self.feed(NOW+9*3600, data=premium(NOW+9*3600, rate=.0001))
        self.assertIn("financiacion incompleta", self.experiment["baseline"]["halt_reason"])
        self.assertTrue(self.experiment["baseline"]["limits_breached"])

    def test_missing_mark_for_held_symbol_blocks_new_symbol(self):
        self.config = replace(self.config, symbols=("DOGEUSDT", "SOLUSDT"))
        self.lab, self.state = ShadowLab(self.config), {}
        self.assertTrue(self.lab.start(self.state, self.proposal, NOW))
        self.feed()
        self.feed(NOW+120, "SOLUSDT")
        self.assertEqual(set(self.experiment["baseline"]["positions"]), {"DOGEUSDT"})
        self.feed(NOW+120)
        self.feed(NOW+121, "SOLUSDT")
        self.assertEqual(len(self.experiment["baseline"]["positions"]), 2)

    def evidence(self, now, candidate_pnl=.3, baseline_pnl=.2):
        experiment = self.experiment
        experiment.update(coverage_seconds=7*DAY, last_update=now, last_error=None)
        experiment["marks"] = {"DOGEUSDT": dict(time=now, mark=100, bid=100, ask=100, rate=0)}
        for name, profit in (("baseline", baseline_pnl), ("candidate", candidate_pnl)):
            portfolio = experiment[name]
            portfolio["trades"] = [dict(net_pnl=profit, initial_risk=.5) for _ in range(30)]
            portfolio.update(equity=100+profit*30, liquidation_equity=100+profit*30,
                             max_observed_drawdown=.01, last_update=now)

    def test_elapsed_clock_without_observed_coverage_never_promotes(self):
        now = NOW+8*DAY
        self.evidence(now)
        self.experiment["coverage_seconds"] = 0
        self.assertEqual(self.lab.evaluate(self.state, now), [])
        self.assertEqual(self.experiment["status"], "running")

    def test_promotion_requires_sample_and_positive_net_then_reports_once(self):
        now = NOW+8*DAY
        self.evidence(now)
        self.experiment["candidate"]["trades"].pop()
        self.assertFalse(self.lab.evaluate(self.state, now))
        self.experiment["candidate"]["trades"].append(dict(net_pnl=.3, initial_risk=.5))
        decisions = self.lab.evaluate(self.state, now)
        self.assertEqual(decisions[0]["recommendation"], "promote")
        self.assertEqual(decisions[0]["patch"], self.proposal["patch"])
        self.assertEqual(len(decisions[0]["evidence_hash"]), 64)
        self.assertEqual(self.config.max_spread_bps, 12)
        self.assertEqual(self.lab.evaluate(self.state, now), [])

    def test_inferior_profit_or_drawdown_or_risk_halt_rejects_at_14_days(self):
        for change in ("profit", "drawdown", "halt", "expectancy", "factor"):
            with self.subTest(change=change):
                self.setUp()
                now = NOW+14*DAY
                self.evidence(now)
                candidate = self.experiment["candidate"]
                if change == "profit":
                    candidate["liquidation_equity"] = 101
                elif change == "drawdown":
                    candidate["max_observed_drawdown"] = .02
                elif change == "halt":
                    candidate["limits_breached"] = True
                elif change == "expectancy":
                    for trade in candidate["trades"]:
                        trade["initial_risk"] = 1
                elif change == "factor":
                    candidate["trades"] = [dict(net_pnl=1.1 if i % 2 else -1, initial_risk=.5) for i in range(30)]
                self.assertEqual(self.lab.evaluate(self.state, now)[0]["recommendation"], "reject")

    def test_insufficient_sample_keeps_collecting_then_expires(self):
        self.assertEqual(self.lab.evaluate(self.state, NOW+14*DAY), [])
        self.assertEqual(self.lab.evaluate(self.state, NOW+30*DAY)[0]["recommendation"], "reject")

    def test_expiration_is_a_fixed_deadline_even_when_final_metrics_look_good(self):
        now = NOW+30*DAY
        self.evidence(now)
        self.assertEqual(self.lab.evaluate(self.state, now)[0]["recommendation"], "reject")

    def test_stale_marks_and_changed_config_cannot_promote(self):
        now = NOW+8*DAY
        self.evidence(now)
        self.experiment["marks"]["DOGEUSDT"]["time"] = now-91
        self.assertEqual(self.lab.evaluate(self.state, now), [])
        self.evidence(now)
        self.lab = ShadowLab(replace(self.config, risk_per_trade=.004))
        self.assertEqual(self.lab.evaluate(self.state, now)[0]["recommendation"], "reject")

    def test_coverage_excludes_market_outage(self):
        self.feed()
        self.feed(NOW+30)
        self.assertEqual(self.experiment["coverage_seconds"], 30)
        self.feed(NOW+300)
        self.assertEqual(self.experiment["coverage_seconds"], 30)


if __name__ == "__main__":
    unittest.main()
