"""Prospective, deterministic paper comparisons; this module cannot place orders.

An experiment starts with two empty portfolios and consumes only observations
received after its creation. Candle exits and funding are conservative estimates,
not exchange fills. A recommendation is evidence for the coordinator, never an
instruction to mutate configuration or weaken hard risk limits.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from .config import Config
from .models import Advice, Snapshot
from .risk import RiskAgent, new_state
from .technical import LiquidityAgent, _closed_candles, profile_weights


DAY = 86400
COLOMBIA = timezone(timedelta(hours=-5))
PATCH_FIELDS = {
    "trend": "min_trend_score", "momentum": "min_momentum_score",
    "liquidity": "max_spread_bps", "market_context": "require_fresh_news",
    "risk": "risk_per_trade",
}


def _number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("Booleano en dato numerico")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("Dato no finito o no positivo")
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _decimal(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError("Filtro numerico invalido")
        return result
    except InvalidOperation as exc:
        raise ValueError("Filtro numerico invalido") from exc


def _quantity(info, symbol, proposed, price):
    """Apply the intersection of LOT_SIZE/MARKET_LOT_SIZE, rounding down only."""
    if not isinstance(info, dict) or info.get("symbol", symbol) != symbol:
        raise ValueError("Faltan filtros del simbolo")
    if info.get("status", "TRADING") != "TRADING":
        raise ValueError("Simbolo no negociable")
    filters = info["filters"]
    lots = [f for f in filters if f["filterType"] in ("LOT_SIZE", "MARKET_LOT_SIZE")]
    steps = [_decimal(f["stepSize"]) for f in lots]
    steps = [s for s in steps if s > 0]
    if not steps:
        raise ValueError("Falta stepSize positivo")
    scale = Decimal(10) ** min(s.as_tuple().exponent for s in steps)
    step = Decimal(math.lcm(*(int(s / scale) for s in steps))) * scale
    quantity = _decimal(proposed)
    limits = [_decimal(f["maxQty"]) for f in lots if _decimal(f["maxQty"]) > 0]
    if limits:
        quantity = min(quantity, *limits)
    quantity = (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if quantity <= 0 or quantity < max(_decimal(f["minQty"]) for f in lots):
        raise ValueError("Cantidad inferior al minimo")
    notional = quantity * _decimal(price)
    for item in filters:
        if item["filterType"] == "MIN_NOTIONAL":
            if notional < _decimal(item.get("notional", item.get("minNotional", 0))):
                raise ValueError("Nocional inferior al minimo")
        elif item["filterType"] == "NOTIONAL":
            if item.get("applyMinToMarket", True) and notional < _decimal(item["minNotional"]):
                raise ValueError("Nocional inferior al minimo")
            if item.get("applyMaxToMarket", True) and notional > _decimal(item["maxNotional"]):
                raise ValueError("Nocional superior al maximo")
    if quantity > _decimal(proposed):
        raise ValueError("Cantidad excede presupuesto")
    return float(quantity)


class ShadowLab:
    """One frozen challenger at a time, with separate baseline/candidate ledgers."""

    def __init__(self, config):
        self.config = config

    def start(self, state: dict, proposal: dict, now: float) -> bool:
        experiments = state.setdefault("experiments", {})
        if any(e.get("status") == "running" for e in experiments.values()):
            return False
        try:
            now = _number(now, True)
            identity, agent, patch = proposal["id"], proposal["agent"], proposal["patch"]
            if not isinstance(identity, str) or not identity or identity in experiments:
                return False
            field = PATCH_FIELDS[agent]
            if not isinstance(patch, dict) or set(patch) != {field}:
                return False
            before, after = getattr(self.config, field), patch[field]
            if field == "require_fresh_news":
                valid = before is False and after is True
            else:
                after = _number(after, True)
                valid = after > before if field.startswith("min_") else after < before
                if field.startswith("min_"):
                    valid = valid and after <= .95
                elif field == "max_spread_bps":
                    valid = valid and after >= 1
                elif field == "risk_per_trade":
                    valid = valid and after >= self.config.risk_per_trade * .25
            if not valid:
                return False
            candidate = replace(self.config, **patch)
            if self.config.fee_rate <= 0 or self.config.slippage_bps <= 0:
                return False  # A fee-less experiment cannot support promotion.
            adaptation_day = datetime.fromtimestamp(now, COLOMBIA).date().isoformat()
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
            return False
        portfolios = {}
        for name, config in (("baseline", self.config), ("candidate", candidate)):
            portfolio = new_state(config)
            portfolio.update(created_at=now, observed_peak=config.initial_equity,
                             max_observed_drawdown=0.0, limits_breached=False,
                             adaptation_day=adaptation_day,
                             observations=0, last_update=0, last_entry_rejection=None)
            portfolios[name] = portfolio
        experiments[identity] = dict(
            id=identity, agent=agent, patch=copy.deepcopy(patch),
            proposal=copy.deepcopy(proposal), status="running", started_at=now,
            frozen_config=asdict(self.config), candidate_config=asdict(candidate),
            marks={}, observations=0, coverage_seconds=0.0, coverage_checkpoint=None,
            evidence_hash=_digest({"proposal": proposal, "config": asdict(self.config), "start": now}),
            model="Prospective paper; observed bid/ask, configured fees/slippage; funding estimated",
            **portfolios)
        return True

    def _config(self, experiment, candidate=False):
        return Config(**experiment["candidate_config" if candidate else "frozen_config"])

    @staticmethod
    def _fresh_marks(experiment, symbols, now):
        return all(s in experiment["marks"] and
                   0 <= now - experiment["marks"][s]["time"] <= 90 for s in symbols)

    def _value(self, experiment, portfolio, config, now):
        if not self._fresh_marks(experiment, portfolio["positions"], now):
            return False
        day = datetime.fromtimestamp(now, COLOMBIA).date().isoformat()
        if portfolio["adaptation_day"] != day:
            cutoff = datetime.fromtimestamp(now, COLOMBIA).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            historical = copy.deepcopy(portfolio)
            historical["trades"] = [t for t in portfolio["trades"] if t["closed_at"] < cutoff]
            if historical["adaptation_count"] <= len(historical["trades"]):
                RiskAgent(config).adapt(historical)
                for key in ("risk_multiplier", "adaptation_count", "adaptations"):
                    portfolio[key] = historical[key]
                if historical["halt_reason"]:
                    portfolio["halt_reason"] = historical["halt_reason"]
            portfolio["adaptation_day"] = day
        equity = portfolio["balance"]
        for symbol, position in portfolio["positions"].items():
            quote = experiment["marks"][symbol]
            mark = quote["mark"]
            direction, quantity = position["direction"], position["quantity"]
            equity += direction * quantity * (mark - position["entry_price"])
            costs = mark * (2 * config.fee_rate + 2 * config.slippage_bps / 10000 + abs(quote["rate"]))
            position["risk_usdt"] = max(position["risk_usdt"], quantity *
                                        (max(0, direction * (mark - position["stop"])) + costs))
        RiskAgent(config).update_limits(portfolio, equity, now)
        # Drawdown comparison uses an equity estimate net of immediate exit costs.
        liquidation = equity
        for symbol, position in portfolio["positions"].items():
            quote = experiment["marks"][symbol]
            direction, quantity = position["direction"], position["quantity"]
            exit_price = quote["bid"] if direction == 1 else quote["ask"]
            exit_price *= 1 - direction * config.slippage_bps / 10000
            liquidation += direction * quantity * (exit_price - quote["mark"]) - quantity * exit_price * config.fee_rate
        portfolio["liquidation_equity"] = liquidation
        peak = portfolio["observed_peak"] = max(portfolio["observed_peak"], liquidation)
        portfolio["max_observed_drawdown"] = max(portfolio["max_observed_drawdown"], (peak - liquidation) / peak)
        portfolio["limits_breached"] |= bool(portfolio["halt_reason"] or portfolio["daily_paused"])
        return True

    @staticmethod
    def _exit(portfolio, config, symbol, price, reason, closed_at):
        position = portfolio["positions"].pop(symbol)
        fee = position["quantity"] * price * config.fee_rate
        gross = position["direction"] * position["quantity"] * (price - position["entry_price"])
        portfolio["balance"] += gross - fee
        portfolio["trades"].append(dict(
            **position, exit_price=price, closed_at=closed_at, reason=reason,
            gross_pnl=gross, fees=position["entry_fee"] + fee,
            net_pnl=gross - position["entry_fee"] - fee + position["funding"],
            mode="shadow_paper", funding_estimated=True))

    @staticmethod
    def _fund(portfolio, position, premium, mark, now, cutoff):
        checkpoint = position["next_funding_time"]
        if checkpoint and checkpoint <= cutoff * 1000 and checkpoint > position["funding_checkpoint"]:
            if now * 1000 - checkpoint > 8 * 3600 * 1000:
                portfolio["halt_reason"] = "Interrupcion: financiacion incompleta en experimento"
            amount = -position["direction"] * position["quantity"] * mark * position["funding_rate"]
            portfolio["balance"] += amount
            position["funding"] += amount
            position["funding_checkpoint"] = checkpoint
        next_time = int(_number(premium["nextFundingTime"]))
        # Never rewind a paid funding checkpoint when the provider repeats a row.
        if next_time > max(now * 1000, position["funding_checkpoint"]):
            position["next_funding_time"] = next_time
        elif checkpoint <= now * 1000:
            position["next_funding_time"] = 0
        position["funding_rate"] = _number(premium["lastFundingRate"])

    def _manage(self, experiment, portfolio, config, snapshot, premium, now):
        symbol = snapshot.symbol
        position = portfolio["positions"].get(symbol)
        if not position:
            return
        direction = position["direction"]
        mark = _number(premium["markPrice"], True)
        price, reason, closed_at = None, None, now
        # Replay exclusively fully closed candles that began after this fill.
        for candle in snapshot.candles:
            if candle.open_time < position["opened_at"] * 1000 or candle.close_time <= position["checked_candle"]:
                continue
            position["checked_candle"] = candle.close_time
            stopped = candle.low <= position["stop"] if direction == 1 else candle.high >= position["stop"]
            targeted = candle.high >= position["target"] if direction == 1 else candle.low <= position["target"]
            if stopped or targeted:
                level = position["stop"] if stopped else position["target"]
                if stopped:
                    level = min(level, candle.open) if direction == 1 else max(level, candle.open)
                # No historical book is available: charge the observed half-spread
                # in addition to slippage, and favor the stop on ambiguous candles.
                price = (level - direction * (snapshot.ask - snapshot.bid) / 2) * (1 - direction * config.slippage_bps / 10000)
                reason, closed_at = ("stop_loss" if stopped else "take_profit"), candle.close_time / 1000
                break
        executable = snapshot.bid if direction == 1 else snapshot.ask
        if reason is None:
            adverse_price = min(mark, executable) if direction == 1 else max(mark, executable)
            if direction * (adverse_price - position["stop"]) <= 0:
                reason = "stop_loss"
            elif direction * (mark - position["target"]) >= 0:
                reason = "take_profit"
            elif now - position["opened_at"] >= config.max_holding_hours * 3600:
                reason = "time_exit"
            elif portfolio["halt_reason"] or portfolio["daily_paused"]:
                reason = "risk_stop"
            if reason:
                price = executable * (1 - direction * config.slippage_bps / 10000)
        self._fund(portfolio, position, premium, mark, now, closed_at)
        if reason is None and (portfolio["halt_reason"] or portfolio["daily_paused"]):
            reason = "risk_stop"
            price = executable * (1 - direction * config.slippage_bps / 10000)
        if reason:
            self._exit(portfolio, config, symbol, _number(price, True), reason, closed_at)
            portfolio["last_candles"][symbol] = snapshot.candles[-1].close_time

    def _entry(self, experiment, portfolio, config, snapshot, premium, advice, now):
        symbol, candle = snapshot.symbol, snapshot.candles[-1].close_time
        if premium.get("_entries_blocked"):
            portfolio["last_entry_rejection"] = "Entrada bloqueada por estado de datos del coordinador"
            return
        if symbol in portfolio["positions"] or portfolio["last_candles"].get(symbol, -1) >= candle:
            return
        if not self._value(experiment, portfolio, config, now):
            portfolio["last_entry_rejection"] = "Faltan marcas actuales de posiciones abiertas"
            return
        portfolio["last_candles"][symbol] = candle
        reports = {item.agent: item for item in advice}
        if not {"trend", "momentum", "liquidity", "market_context"} <= reports.keys():
            portfolio["last_entry_rejection"] = "Faltan especialistas"
            return
        trend, momentum, context = (reports[k] for k in ("trend", "momentum", "market_context"))
        if any(not -1 <= _number(item.score) <= 1 for item in (trend, momentum, context)):
            raise ValueError("Puntuacion fuera del rango [-1, 1]")
        weights = profile_weights(config.strategy_profile)
        score = weights[0] * _number(trend.score) + weights[1] * _number(momentum.score)
        direction = 1 if score > 0 else -1 if score < 0 else 0
        # Risk depends on this portfolio; the production risk advice is not reused.
        risk = RiskAgent(config).analyze(snapshot, portfolio, direction, momentum.details.get("atr", 0),
                                         _number(premium["lastFundingRate"]))
        liquidity = LiquidityAgent(config.max_spread_bps, config.min_quote_volume).analyze(snapshot)
        allowed = ((direction == 1 or (direction == -1 and config.allow_shorts))
                   and not any(a.veto for a in (trend, momentum, context, liquidity, risk))
                   and (not config.require_fresh_news or context.details.get("fresh") is True)
                   and abs(score) >= config.entry_threshold
                   and trend.score * direction > 0 and momentum.score * direction >= 0
                   and abs(trend.score) >= getattr(config, "min_trend_score", 0)
                   and abs(momentum.score) >= getattr(config, "min_momentum_score", 0))
        if not allowed:
            portfolio["last_entry_rejection"] = "Filtros o veto de riesgo"
            return
        if now - snapshot.fetched_at > config.poll_seconds:
            portfolio["last_entry_rejection"] = "Cotizacion envejecida"
            return
        try:
            price = (snapshot.ask if direction == 1 else snapshot.bid) * (1 + direction * config.slippage_bps / 10000)
            unit_risk = risk.details["stop_distance"] + price * (2 * config.fee_rate +
                           2 * config.slippage_bps / 10000 + abs(_number(premium["lastFundingRate"])))
            maximum = min(risk.details["quantity"], risk.details["risk_budget"] / unit_risk,
                          risk.details["margin"] * config.leverage / price)
            quantity = _quantity(premium.get("_symbol_filters"), symbol, maximum, price)
        except (ValueError, TypeError, KeyError, InvalidOperation):
            portfolio["last_entry_rejection"] = "Filtros de Binance ausentes o minimos incompatibles con riesgo"
            return
        fee = quantity * price * config.fee_rate
        distance = risk.details["stop_distance"]
        initial_risk = quantity * unit_risk
        portfolio["balance"] -= fee
        next_time = int(_number(premium["nextFundingTime"]))
        portfolio["positions"][symbol] = dict(
            symbol=symbol, direction=direction, quantity=quantity, entry_price=price,
            entry_fee=fee, opened_at=now, stop=price-direction*distance,
            target=price+direction*distance*config.reward_risk_ratio,
            risk_usdt=initial_risk, initial_risk=initial_risk, funding=0.0,
            funding_rate=_number(premium["lastFundingRate"]), funding_checkpoint=0,
            next_funding_time=next_time if next_time > now * 1000 else 0,
            checked_candle=candle, signal_score=score)
        portfolio["last_entry_rejection"] = None

    def update(self, state: dict, snapshot: Snapshot, premium: dict, advice: list[Advice], now: float) -> None:
        for experiment in state.get("experiments", {}).values():
            if experiment.get("status") != "running":
                continue
            source = experiment
            experiment = copy.deepcopy(source)
            try:
                now = _number(now, True)
                config = self._config(experiment)
                if now < experiment["started_at"] or snapshot.symbol not in config.symbols:
                    continue
                if not 0 <= now - _number(snapshot.fetched_at, True) <= 90:
                    raise ValueError("Cotizacion futura o desactualizada")
                previous = experiment["marks"].get(snapshot.symbol)
                if previous and snapshot.fetched_at <= previous["time"]:
                    continue
                _closed_candles(snapshot, 1)
                seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}[config.interval]
                if not 0 <= now * 1000 - snapshot.candles[-1].close_time <= (seconds + 90) * 1000:
                    raise ValueError("Velas desactualizadas")
                bid, ask = _number(snapshot.bid, True), _number(snapshot.ask, True)
                if bid > ask or premium.get("symbol", snapshot.symbol) != snapshot.symbol:
                    raise ValueError("Cotizacion o simbolo incorrectos")
                if "time" not in premium or not 0 <= now - _number(premium["time"]) / 1000 <= 90:
                    raise ValueError("Marca sin fecha verificable o desactualizada")
                mark = _number(premium["markPrice"], True)
                rate = _number(premium["lastFundingRate"])
                if _number(premium["nextFundingTime"]) <= now * 1000:
                    raise ValueError("Proximo funding ausente o desactualizado")
                experiment["marks"][snapshot.symbol] = dict(time=snapshot.fetched_at, mark=mark, bid=bid, ask=ask, rate=rate)
                observation = dict(now=now, snapshot=asdict(snapshot), premium=premium,
                                   advice=[asdict(a) for a in advice], previous=experiment["evidence_hash"])
                experiment["evidence_hash"] = _digest(observation)
                experiment["observations"] += 1
                if self._fresh_marks(experiment, config.symbols, now):
                    previous_time = experiment["coverage_checkpoint"]
                    if previous_time is not None and 0 <= now - previous_time <= 90:
                        experiment["coverage_seconds"] += now - previous_time
                    experiment["coverage_checkpoint"] = now
                else:
                    experiment["coverage_checkpoint"] = None
                for name, candidate in (("baseline", False), ("candidate", True)):
                    portfolio, variant = experiment[name], self._config(experiment, candidate)
                    # Settle already closed candle exits before valuing at a later
                    # quote; otherwise an exited trade can create an impossible peak.
                    self._manage(experiment, portfolio, variant, snapshot, premium, now)
                    self._value(experiment, portfolio, variant, now)
                    if portfolio["halt_reason"] or portfolio["daily_paused"]:
                        self._manage(experiment, portfolio, variant, snapshot, premium, now)
                    self._entry(experiment, portfolio, variant, snapshot, premium, advice, now)
                    self._value(experiment, portfolio, variant, now)
                    portfolio["observations"] += 1
                    portfolio["last_update"] = now
                experiment["last_update"] = now
                experiment["last_error"] = None
                source.clear()
                source.update(experiment)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                # Invalid evidence never becomes an entry or a validation pass.
                source["last_error"] = str(exc)
                source["coverage_checkpoint"] = None

    @staticmethod
    def _metrics(portfolio, initial):
        trades = portfolio["trades"]
        pnl = [_number(t["net_pnl"]) for t in trades]
        gains, losses = math.fsum(max(0, p) for p in pnl), -math.fsum(min(0, p) for p in pnl)
        expectancy = math.fsum(p / _number(t["initial_risk"], True) for p, t in zip(pnl, trades)) / len(trades) if trades else 0.0
        return dict(closed_trades=len(trades), realized_pnl=math.fsum(pnl),
                    net_pnl=portfolio.get("liquidation_equity", portfolio["equity"]) - initial,
                    profit_factor=gains / losses if losses else None,
                    no_losing_trades=losses == 0 and gains > 0,
                    expectancy_r=expectancy, max_drawdown=portfolio["max_observed_drawdown"],
                    limits_breached=bool(portfolio["limits_breached"] or portfolio["halt_reason"]),
                    open_positions=len(portfolio["positions"]))

    def evaluate(self, state: dict, now: float) -> list[dict]:
        now = _number(now, True)
        decisions = []
        for experiment in state.get("experiments", {}).values():
            if experiment.get("status") != "running":
                continue
            elapsed = now - experiment["started_at"]
            if elapsed < 7 * DAY:
                continue
            config = self._config(experiment)
            baseline = self._metrics(experiment["baseline"], config.initial_equity)
            candidate = self._metrics(experiment["candidate"], config.initial_equity)
            current_config = _digest(asdict(self.config)) == _digest(experiment["frozen_config"])
            enough = baseline["closed_trades"] >= 30 and candidate["closed_trades"] >= 30
            fresh = (self._fresh_marks(experiment, config.symbols, now)
                     and 0 <= now - experiment.get("last_update", 0) <= 90
                     and not experiment.get("last_error"))
            covered = experiment["coverage_seconds"] >= 7 * DAY
            factor_ok = candidate["no_losing_trades"] or (candidate["profit_factor"] is not None and candidate["profit_factor"] >= 1.2)
            performance_ok = (candidate["net_pnl"] > 0 and candidate["realized_pnl"] > 0 and factor_ok
                              and candidate["net_pnl"] >= baseline["net_pnl"]
                              and candidate["expectancy_r"] >= baseline["expectancy_r"]
                              and candidate["max_drawdown"] <= baseline["max_drawdown"]
                              and not candidate["limits_breached"] and not baseline["limits_breached"])
            expired = elapsed >= 30 * DAY
            passed = not expired and enough and fresh and covered and current_config and performance_ok
            failed = elapsed >= 14 * DAY and enough and covered and fresh and not performance_ok
            if not (passed or expired or failed or not current_config):
                continue
            recommendation = "promote" if passed else "reject"
            reason = ("Prueba prospectiva supera criterios fijos; requiere aprobacion del coordinador" if passed else
                      "Configuracion base cambio durante la prueba" if not current_config else
                      "Prueba caducada sin evidencia suficiente para promocion" if expired else
                      "El candidato no supera los criterios de beneficio y riesgo")
            result = dict(id=experiment["id"], agent=experiment["agent"], patch=copy.deepcopy(experiment["patch"]),
                          recommendation=recommendation, reason=reason, evaluated_at=now,
                          baseline=baseline, candidate=candidate, coverage_seconds=experiment["coverage_seconds"],
                          elapsed_days=elapsed / DAY, enough_trades=enough, fresh_marks=fresh,
                          evidence_hash=experiment["evidence_hash"],
                          config_hash=_digest(experiment["frozen_config"]),
                          policy="Fixed comparison criteria, not probabilities of future profitability")
            experiment.update(status=recommendation, decision=copy.deepcopy(result), finished_at=now)
            decisions.append(result)
        return decisions
