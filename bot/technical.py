"""Deterministic technical advisers that only consume closed market candles.

Timestamps follow Binance's convention (milliseconds); ``fetched_at`` is Unix
seconds. Missing, duplicate, overlapping or unfinished candles veto a decision.
Indicator helpers return their final value and raise ``ValueError`` when their
input cannot support a calculation. No indicator reads beyond its input window.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Iterable

from .models import Advice, Candle, Snapshot


def _period(period: int) -> None:
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("El período debe ser un entero positivo")


def _numbers(values: Iterable[float], minimum: int) -> list[float]:
    try:
        result = [float(value) for value in values]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("La serie contiene valores inválidos") from exc
    if len(result) < minimum:
        raise ValueError(f"Se necesitan al menos {minimum} observaciones")
    if not all(math.isfinite(value) for value in result):
        raise ValueError("La serie contiene valores no finitos")
    return result


def _ema_series(values: Iterable[float], period: int) -> list[float]:
    _period(period)
    numbers = _numbers(values, period)
    previous = math.fsum(value / period for value in numbers[:period])
    result = [previous]
    alpha = 2.0 / (period + 1)
    for value in numbers[period:]:
        previous = alpha * value + (1.0 - alpha) * previous
        result.append(previous)
    return result


def ema(values: Iterable[float], period: int) -> float:
    """Latest exponential average, initialized with the first period's SMA."""
    return _ema_series(values, period)[-1]


def rsi(values: Iterable[float], period: int = 14) -> float:
    """Wilder RSI; a flat series is 50, a rise-only series is 100."""
    _period(period)
    numbers = _numbers(values, period + 1)
    changes = [current - previous for previous, current in zip(numbers, numbers[1:])]
    if not all(math.isfinite(change) for change in changes):
        raise ValueError("La variación de precios excede el rango numérico")
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    gain = math.fsum(value / period for value in gains[:period])
    loss = math.fsum(value / period for value in losses[:period])
    for next_gain, next_loss in zip(gains[period:], losses[period:]):
        gain = gain * ((period - 1) / period) + next_gain / period
        loss = loss * ((period - 1) / period) + next_loss / period
    if gain == 0 and loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    if gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def _check_ohlcv(candle: Candle) -> None:
    try:
        prices = (candle.open, candle.high, candle.low, candle.close)
        if not all(math.isfinite(value) and value > 0 for value in prices):
            raise ValueError("OHLC debe contener precios positivos y finitos")
        if candle.high < max(candle.open, candle.close, candle.low):
            raise ValueError("El máximo OHLC es inconsistente")
        if candle.low > min(candle.open, candle.close, candle.high):
            raise ValueError("El mínimo OHLC es inconsistente")
        if not math.isfinite(candle.volume) or candle.volume < 0:
            raise ValueError("El volumen de vela es inválido")
    except (AttributeError, TypeError, OverflowError) as exc:
        raise ValueError("Formato de vela inválido") from exc


def atr(candles: Iterable[Candle], period: int = 14) -> float:
    """Wilder ATR using a preceding close for every true-range observation."""
    _period(period)
    bars = list(candles)
    if len(bars) < period + 1:
        raise ValueError(f"Se necesitan al menos {period + 1} velas para ATR")
    for bar in bars:
        _check_ohlcv(bar)
    ranges = [
        max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
        for previous, bar in zip(bars, bars[1:])
    ]
    result = math.fsum(value / period for value in ranges[:period])
    for value in ranges[period:]:
        result = result * ((period - 1) / period) + value / period
    return result


def _closed_candles(snapshot: Snapshot, minimum: int) -> list[Candle]:
    bars = snapshot.candles
    if len(bars) < minimum:
        raise ValueError(f"Se necesitan al menos {minimum} velas cerradas")
    if not math.isfinite(snapshot.fetched_at) or snapshot.fetched_at <= 0:
        raise ValueError("Fecha de captura inválida")
    intervals = []
    for index, bar in enumerate(bars):
        _check_ohlcv(bar)
        if (
            not isinstance(bar.open_time, int)
            or not isinstance(bar.close_time, int)
            or bar.open_time < 0
            or bar.close_time < bar.open_time
        ):
            raise ValueError("Timestamps de vela inválidos")
        if bar.close_time > snapshot.fetched_at * 1000:
            raise ValueError("Se recibió una vela que todavía no ha cerrado")
        if index:
            previous = bars[index - 1]
            difference = bar.open_time - previous.open_time
            if difference <= 0 or previous.close_time >= bar.open_time:
                raise ValueError("Velas duplicadas, solapadas o fuera de orden")
            intervals.append(difference)
    if intervals:
        interval = median(intervals)
        if any(value != interval for value in intervals):
            raise ValueError("Hay huecos o intervalos inconsistentes entre las velas")
        if any(bar.close_time - bar.open_time + 1 != interval for bar in bars):
            raise ValueError("La duración de las velas no coincide con su intervalo")
    return bars


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


class TrendAgent:
    """EMA20/50/200 alignment with a price and fast-average slope filter."""

    def analyze(self, snapshot: Snapshot) -> Advice:
        try:
            bars = _closed_candles(snapshot, 200)
            closes = [bar.close for bar in bars]
            fast_series = _ema_series(closes, 20)
            fast, middle, slow = fast_series[-1], ema(closes, 50), ema(closes, 200)
            slope = fast_series[-1] - fast_series[-2]
            last = closes[-1]
            tolerance = last * 1e-10
            bullish = fast > middle + tolerance and middle > slow + tolerance
            bearish = fast < middle - tolerance and middle < slow - tolerance
            regime = "bullish" if bullish else "bearish" if bearish else "sideways"
            long_eligible = bullish and last >= middle and slope > tolerance
            short_eligible = bearish and last <= middle and slope < -tolerance
            strength = min(1.0, abs(fast - middle) / middle * 10000 / 80)
            score = 0.0
            if long_eligible:
                score = (0.55 + 0.40 * strength) if last >= fast else (0.40 + 0.30 * strength)
            elif short_eligible:
                score = -((0.55 + 0.40 * strength) if last <= fast else (0.40 + 0.30 * strength))
            return Advice(
                "trend", score,
                reason=f"Régimen {regime}; alineación EMA20/50/200 y pendiente de EMA20",
                details={
                    "regime": regime, "ema20": fast, "ema50": middle, "ema200": slow,
                    "ema20_slope": slope, "strength": strength,
                    "long_eligible": long_eligible, "short_eligible": short_eligible,
                },
            )
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            return Advice("trend", 0.0, veto=True, reason=str(exc), details={"regime": "unknown"})


class MomentumAgent:
    """Direction from RSI/MACD; extreme RSI reduces chasing strength."""

    def analyze(self, snapshot: Snapshot) -> Advice:
        try:
            bars = _closed_candles(snapshot, 35)
            closes = [bar.close for bar in bars]
            relative_strength = rsi(closes, 14)
            average_range = atr(bars, 14)
            if average_range <= 0:
                return Advice("momentum", 0.0, veto=True, reason="ATR nulo: no se puede dimensionar el riesgo", details={"atr": 0.0})
            fast = _ema_series(closes, 12)
            slow = _ema_series(closes, 26)
            macd_series = [left - right for left, right in zip(fast[14:], slow)]
            macd = macd_series[-1]
            signal = ema(macd_series, 9)
            histogram = macd - signal
            score = (
                0.45 * _clamp(macd / average_range)
                + 0.30 * _clamp(histogram / (average_range * 0.15))
                + 0.25 * _clamp((relative_strength - 50) / 25)
            )
            overextended = relative_strength >= 75 or relative_strength <= 25
            if (relative_strength >= 75 and score > 0) or (relative_strength <= 25 and score < 0):
                score *= 0.65
            return Advice(
                "momentum", _clamp(score),
                reason="RSI14 y MACD12/26/9 normalizados por ATR14",
                details={
                    "atr": average_range, "rsi": relative_strength, "macd": macd,
                    "macd_signal": signal, "macd_histogram": histogram,
                    "overextended": overextended,
                },
            )
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            return Advice("momentum", 0.0, veto=True, reason=str(exc), details={"atr": 0.0})


class SwingMomentumAgent:
    """Hourly pullback continuation inside an established EMA20/50 trend."""

    def analyze(self, snapshot: Snapshot) -> Advice:
        try:
            bars = _closed_candles(snapshot, 55)
            closes = [bar.close for bar in bars]
            average_range = atr(bars, 14)
            if average_range <= 0:
                return Advice("momentum", 0.0, veto=True,
                              reason="ATR nulo: no se puede dimensionar el riesgo", details={"atr": 0.0})
            fast, slow = ema(closes, 20), ema(closes, 50)
            relative_strength = rsi(closes, 14)
            last = closes[-1]
            pullback_atr = (last - fast) / average_range
            separation = min(1.0, abs(fast - slow) / average_range)
            long_eligible = fast > slow and last > slow and -0.75 <= pullback_atr <= 1.0 and 45 <= relative_strength <= 68
            short_eligible = fast < slow and last < slow and -1.0 <= pullback_atr <= 0.75 and 32 <= relative_strength <= 55
            score = 0.0
            if long_eligible or short_eligible:
                direction = 1 if long_eligible else -1
                pullback_quality = max(0.0, 1.0 - abs(pullback_atr) / 1.25)
                score = direction * (0.50 + 0.30 * separation + 0.20 * pullback_quality)
            return Advice(
                "momentum", _clamp(score),
                reason="Continuación swing EMA20/50 con retroceso y RSI14 moderado",
                details={"atr": average_range, "rsi": relative_strength, "ema20": fast,
                         "ema50": slow, "pullback_atr": pullback_atr,
                         "long_eligible": long_eligible, "short_eligible": short_eligible},
            )
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            return Advice("momentum", 0.0, veto=True, reason=str(exc), details={"atr": 0.0})


class ScalpingTrendAgent:
    """Short-horizon EMA9/21/50 alignment for one-minute profiles."""

    def analyze(self, snapshot: Snapshot) -> Advice:
        try:
            bars = _closed_candles(snapshot, 50)
            closes = [bar.close for bar in bars]
            fast_series = _ema_series(closes, 9)
            fast, middle, slow = fast_series[-1], ema(closes, 21), ema(closes, 50)
            average_range = atr(bars, 7)
            if average_range <= 0:
                return Advice("trend", 0.0, veto=True,
                              reason="ATR nulo: no se puede medir la tendencia corta", details={"atr": 0.0})
            slope = fast_series[-1] - fast_series[-3]
            last = closes[-1]
            bullish = fast > middle > slow and last >= fast and slope > 0
            bearish = fast < middle < slow and last <= fast and slope < 0
            strength = min(1.0, abs(fast - middle) / average_range)
            direction = 1 if bullish else -1 if bearish else 0
            score = direction * (0.55 + 0.35 * strength) if direction else 0.0
            return Advice(
                "trend", _clamp(score),
                reason="Alineación scalping EMA9/21/50 con pendiente de EMA9",
                details={"regime": "bullish" if bullish else "bearish" if bearish else "sideways",
                         "ema9": fast, "ema21": middle, "ema50": slow, "ema9_slope": slope,
                         "atr": average_range, "strength": strength,
                         "long_eligible": bullish, "short_eligible": bearish},
            )
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            return Advice("trend", 0.0, veto=True, reason=str(exc), details={"regime": "unknown", "atr": 0.0})


class ScalpingMomentumAgent:
    """RSI7 and MACD5/13/4 confirmation for the one-minute profile."""

    def analyze(self, snapshot: Snapshot) -> Advice:
        try:
            bars = _closed_candles(snapshot, 20)
            closes = [bar.close for bar in bars]
            average_range = atr(bars, 7)
            if average_range <= 0:
                return Advice("momentum", 0.0, veto=True,
                              reason="ATR nulo: no se puede dimensionar el riesgo", details={"atr": 0.0})
            relative_strength = rsi(closes, 7)
            fast, slow = _ema_series(closes, 5), _ema_series(closes, 13)
            macd_series = [left - right for left, right in zip(fast[8:], slow)]
            macd = macd_series[-1]
            signal = ema(macd_series, 4)
            histogram = macd - signal
            score = (0.40 * _clamp(macd / average_range)
                     + 0.35 * _clamp(histogram / (average_range * 0.12))
                     + 0.25 * _clamp((relative_strength - 50) / 20))
            overextended = relative_strength >= 72 or relative_strength <= 28
            if (relative_strength >= 72 and score > 0) or (relative_strength <= 28 and score < 0):
                score *= 0.45
            return Advice(
                "momentum", _clamp(score),
                reason="Confirmación scalping RSI7 y MACD5/13/4 normalizados por ATR7",
                details={"atr": average_range, "rsi": relative_strength, "macd": macd,
                         "macd_signal": signal, "macd_histogram": histogram,
                         "overextended": overextended},
            )
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            return Advice("momentum", 0.0, veto=True, reason=str(exc), details={"atr": 0.0})


def profile_weights(profile: str) -> tuple[float, float]:
    weights = {"trend": (0.65, 0.35), "swing": (0.70, 0.30), "scalping": (0.55, 0.45)}
    try:
        return weights[profile]
    except (KeyError, TypeError) as exc:
        raise ValueError("Perfil de estrategia no soportado") from exc


def strategy_agents(profile: str):
    if profile == "trend":
        agents = TrendAgent(), MomentumAgent()
    elif profile == "swing":
        agents = TrendAgent(), SwingMomentumAgent()
    elif profile == "scalping":
        agents = ScalpingTrendAgent(), ScalpingMomentumAgent()
    else:
        raise ValueError("Perfil de estrategia no soportado")
    return (*agents, profile_weights(profile))


class LiquidityAgent:
    """A nondirectional gate on top-of-book spread and 24-hour quote volume."""

    def __init__(self, max_spread_bps: float = 8, min_quote_volume: float = 10_000_000):
        if not math.isfinite(max_spread_bps) or max_spread_bps <= 0:
            raise ValueError("El límite de spread debe ser positivo y finito")
        if not math.isfinite(min_quote_volume) or min_quote_volume < 0:
            raise ValueError("El volumen mínimo debe ser no negativo y finito")
        self.max_spread_bps = max_spread_bps
        self.min_quote_volume = min_quote_volume

    def analyze(self, snapshot: Snapshot) -> Advice:
        try:
            bid, ask, volume = snapshot.bid, snapshot.ask, snapshot.quote_volume
            if not all(math.isfinite(value) for value in (bid, ask, volume)):
                raise ValueError("Cotización o volumen no finitos")
            if bid <= 0 or ask <= 0 or ask < bid or volume < 0:
                raise ValueError("Cotización o volumen inválidos")
            midpoint = bid / 2 + ask / 2
            spread = (ask - bid) / midpoint * 10000
            details = {"spread_bps": spread, "quote_volume": volume}
            if spread > self.max_spread_bps:
                return Advice("liquidity", 0.0, veto=True, reason="Spread superior al límite", details=details)
            if volume < self.min_quote_volume:
                return Advice("liquidity", 0.0, veto=True, reason="Volumen cotizado de 24 h insuficiente", details=details)
            return Advice("liquidity", 0.0, reason="Spread y volumen dentro de los límites", details=details)
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            return Advice("liquidity", 0.0, veto=True, reason=str(exc))
