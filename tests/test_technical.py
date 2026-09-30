import math
import unittest
from dataclasses import replace

from bot.models import Candle, Snapshot
from bot.technical import (LiquidityAgent, MomentumAgent, ScalpingMomentumAgent,
                           ScalpingTrendAgent, SwingMomentumAgent, TrendAgent,
                           atr, ema, profile_weights, rsi, strategy_agents)


def snapshot(prices, *, quote_volume=20_000_000, spread=0.02):
    bars = [
        Candle(index * 60_000, (index + 1) * 60_000 - 1, price, price + 1, price - 1, price, 100)
        for index, price in enumerate(prices)
    ]
    last = prices[-1] if prices else 100.0
    return Snapshot("BTCUSDT", bars, last - spread / 2, last + spread / 2, quote_volume, max(1, len(bars) * 60))


class IndicatorsTest(unittest.TestCase):
    def test_ema_uses_sma_seed_and_recursive_update(self):
        self.assertEqual(ema([1, 2, 3], 3), 2)
        self.assertEqual(ema([1, 2, 3, 8], 3), 5)
        self.assertEqual(ema([1, 2, 3], 1), 3)

    def test_rsi_wilder_known_reference(self):
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00]
        self.assertAlmostEqual(rsi(closes[:15]), 70.4641350211, places=8)
        self.assertAlmostEqual(rsi(closes), 66.2496185536, places=8)

    def test_rsi_flat_rising_falling(self):
        self.assertEqual(rsi([10] * 20), 50)
        self.assertEqual(rsi(range(1, 21)), 100)
        self.assertEqual(rsi(range(20, 0, -1)), 0)

    def test_atr_includes_price_gaps_and_wilder_smoothing(self):
        bars = [
            Candle(0, 0, 10, 11, 9, 10, 1),
            Candle(1, 1, 14, 15, 13, 14, 1),  # TR 5 from previous close.
            Candle(2, 2, 13, 14, 12, 13, 1),  # TR 2; initial ATR is 3.5.
            Candle(3, 3, 13, 16, 12, 13, 1),  # TR 4; smoothed ATR is 3.75.
        ]
        self.assertEqual(atr(bars[:3], 2), 3.5)
        self.assertEqual(atr(bars, 2), 3.75)

    def test_helpers_reject_insufficient_or_invalid_inputs(self):
        for compute in (lambda: ema([], 2), lambda: ema([1, math.nan], 2), lambda: ema([1], 0), lambda: rsi([1] * 14), lambda: rsi([1] * 14 + [math.inf]), lambda: atr([], 14)):
            with self.subTest(compute=compute):
                with self.assertRaises(ValueError):
                    compute()


class TechnicalAgentsTest(unittest.TestCase):
    def test_named_profiles_use_distinct_agents_and_fixed_weights(self):
        trend = strategy_agents("trend")
        swing = strategy_agents("swing")
        scalping = strategy_agents("scalping")
        self.assertIsInstance(trend[0], TrendAgent)
        self.assertIsInstance(trend[1], MomentumAgent)
        self.assertIsInstance(swing[1], SwingMomentumAgent)
        self.assertIsInstance(scalping[0], ScalpingTrendAgent)
        self.assertIsInstance(scalping[1], ScalpingMomentumAgent)
        for profile in ("trend", "swing", "scalping"):
            self.assertAlmostEqual(sum(profile_weights(profile)), 1)
        with self.assertRaises(ValueError):
            strategy_agents("unknown")

    def test_scalping_agents_confirm_a_clean_short_horizon_trend(self):
        market = snapshot([100 + index * .1 for index in range(240)])
        trend = ScalpingTrendAgent().analyze(market)
        momentum = ScalpingMomentumAgent().analyze(market)
        self.assertFalse(trend.veto)
        self.assertFalse(momentum.veto)
        self.assertGreater(trend.score, 0)
        self.assertGreater(momentum.score, 0)

    def test_swing_agent_requires_a_moderate_pullback(self):
        market = snapshot([100 + index * .1 for index in range(240)])
        advice = SwingMomentumAgent().analyze(market)
        self.assertFalse(advice.veto)
        self.assertEqual(advice.score, 0)
        self.assertIn("pullback_atr", advice.details)

    def test_long_and_short_alignment(self):
        for direction in (1, -1):
            market = snapshot([500 + direction * index for index in range(240)])
            trend = TrendAgent().analyze(market)
            momentum = MomentumAgent().analyze(market)
            self.assertFalse(trend.veto)
            self.assertFalse(momentum.veto)
            self.assertGreater(direction * trend.score, 0.5)
            self.assertGreater(direction * momentum.score, 0)
            self.assertEqual(trend.details["regime"], "bullish" if direction == 1 else "bearish")
            self.assertTrue(trend.details["long_eligible" if direction == 1 else "short_eligible"])
            self.assertEqual(momentum.details["atr"], 2)
            # A linear series has a constant MACD of +/- 7 after the SMA seed.
            self.assertAlmostEqual(momentum.details["macd"], direction * 7, places=8)
            self.assertAlmostEqual(momentum.details["macd_histogram"], 0, places=8)

    def test_flat_market_has_no_direction(self):
        market = snapshot([100] * 240)
        self.assertEqual(TrendAgent().analyze(market).score, 0)
        self.assertEqual(MomentumAgent().analyze(market).score, 0)

    def test_insufficient_history_vetoes(self):
        for adviser, length in ((TrendAgent(), 199), (MomentumAgent(), 34)):
            self.assertTrue(adviser.analyze(snapshot([100] * length)).veto)
            self.assertTrue(adviser.analyze(snapshot([])).veto)

    def test_invalid_candles_and_time_series_veto(self):
        market = snapshot([500 + index for index in range(240)])
        mutations = []
        for replacement in (
            replace(market.candles[20], close=math.nan),
            replace(market.candles[20], high=1),
            replace(market.candles[20], volume=-1),
            market.candles[19],
        ):
            bars = market.candles.copy()
            bars[20] = replacement
            mutations.append(replace(market, candles=bars))
        mutations.append(replace(market, candles=market.candles[:20] + market.candles[21:]))
        mutations.append(replace(market, fetched_at=market.fetched_at - 10))
        mutations.append(replace(market, candles=list(reversed(market.candles))))
        for invalid in mutations:
            for adviser in (TrendAgent(), MomentumAgent()):
                with self.subTest(adviser=type(adviser).__name__, invalid=invalid.candles[20]):
                    advice = adviser.analyze(invalid)
                    self.assertTrue(advice.veto)
                    self.assertEqual(advice.score, 0)

    def test_zero_atr_veto_keeps_atr_available(self):
        market = snapshot([100] * 240)
        market = replace(market, candles=[replace(bar, high=100, low=100) for bar in market.candles])
        advice = MomentumAgent().analyze(market)
        self.assertTrue(advice.veto)
        self.assertEqual(advice.details["atr"], 0)

    def test_indicator_only_uses_provided_history(self):
        market = snapshot([500 + index for index in range(240)])
        before = TrendAgent().analyze(market)
        future = snapshot([500 + index for index in range(240)] + [100] * 50)
        self.assertNotEqual(before.score, TrendAgent().analyze(future).score)
        self.assertEqual(before, TrendAgent().analyze(market))


class LiquidityTest(unittest.TestCase):
    def test_liquid_market_passes_without_direction(self):
        advice = LiquidityAgent().analyze(snapshot([100] * 200))
        self.assertFalse(advice.veto)
        self.assertEqual(advice.score, 0)
        self.assertAlmostEqual(advice.details["spread_bps"], 2)

    def test_spread_and_low_volume_veto_independently(self):
        self.assertTrue(LiquidityAgent().analyze(snapshot([100] * 200, spread=0.1)).veto)
        self.assertTrue(LiquidityAgent().analyze(snapshot([100] * 200, quote_volume=9_999_999)).veto)
        self.assertFalse(LiquidityAgent().analyze(snapshot([100] * 200, quote_volume=10_000_000)).veto)

    def test_invalid_market_values_veto(self):
        market = snapshot([100] * 200)
        for invalid in (replace(market, bid=0), replace(market, ask=99), replace(market, bid=math.nan), replace(market, ask=math.inf), replace(market, quote_volume=-1), replace(market, quote_volume=math.nan)):
            self.assertTrue(LiquidityAgent().analyze(invalid).veto)

    def test_bad_configuration_is_rejected(self):
        for kwargs in ({"max_spread_bps": 0}, {"max_spread_bps": math.nan}, {"min_quote_volume": -1}):
            with self.assertRaises(ValueError):
                LiquidityAgent(**kwargs)


if __name__ == "__main__":
    unittest.main()
