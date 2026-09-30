"""Read-only chronological research; one train/holdout split, no promotion.

This is a preliminary technical-strategy experiment, not a reconstruction of
the full live engine: historical news, order books, funding and fills are absent.
Every symbol and every segment receives its own isolated initial capital.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math

from .exchange import ExchangeError
from .models import Candle, Snapshot
from .risk import RiskAgent, new_state
from .technical import MomentumAgent, TrendAgent, _closed_candles


HISTORY_LIMIT = 1499  # Client requests one extra kline to exclude the open bar.
WARMUP = 200
THRESHOLDS = (0.4, 0.5, 0.6)
STOP_MULTIPLES = (1.5, 2.0, 2.5)
FUNDING_PERIOD_MS = 8 * 3600 * 1000


@dataclass(frozen=True)
class Signal:
    score: float
    atr: float
    eligible: bool
    based_on_close_time: int


def _iso(milliseconds: int | float) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec="seconds")


def _signals(symbol: str, bars: list[Candle]) -> list[Signal | None]:
    """Signal at index i uses only candles [max(0, i-240):i]."""
    trend_agent, momentum_agent = TrendAgent(), MomentumAgent()
    result: list[Signal | None] = [None] * len(bars)
    for i in range(WARMUP, len(bars)):
        history = bars[max(0, i - 240):i]
        previous = history[-1]
        snapshot = Snapshot(symbol, history, previous.close, previous.close, 0,
                            previous.close_time / 1000)
        trend = trend_agent.analyze(snapshot)
        momentum = momentum_agent.analyze(snapshot)
        score = 0.65 * trend.score + 0.35 * momentum.score
        direction = 1 if score > 0 else -1
        result[i] = Signal(
            score, momentum.details.get("atr", 0),
            not (trend.veto or momentum.veto) and trend.score * direction > 0
            and momentum.score * direction >= 0,
            previous.close_time,
        )
    return result


def _funding(position: dict, at: int, rate: float) -> float:
    elapsed = max(0, at - position["entry_time"])
    # Conservative scenario: debit both directions; never invent funding income.
    return position["quantity"] * position["entry_price"] * abs(rate) * elapsed / FUNDING_PERIOD_MS


def _exit_trigger(position: dict, bar: Candle, max_holding_hours: float,
                  last_bar: bool) -> tuple[float, int, str] | None:
    """Known opening gaps precede OHLC ambiguity; otherwise stop wins ties."""
    direction, stop, target = position["direction"], position["stop"], position["target"]
    if (direction == 1 and bar.open <= stop) or (direction == -1 and bar.open >= stop):
        return bar.open, bar.open_time, "stop_gap"
    if (direction == 1 and bar.open >= target) or (direction == -1 and bar.open <= target):
        return target, bar.open_time, "target_gap"  # Do not assume price improvement.
    if bar.open_time - position["entry_time"] >= max_holding_hours * 3600 * 1000:
        return bar.open, bar.open_time, "max_holding"
    if (direction == 1 and bar.low <= stop) or (direction == -1 and bar.high >= stop):
        return stop, bar.close_time, "stop"
    if (direction == 1 and bar.high >= target) or (direction == -1 and bar.low <= target):
        return target, bar.close_time, "target"
    if last_bar:
        return bar.close, bar.close_time, "segment_end"
    return None


def _metrics(trades: list[dict], initial: float, balance: float,
             max_drawdown: float, max_drawdown_usdt: float, halted: str | None,
             skipped: dict) -> dict:
    profits = [trade["net_pnl"] for trade in trades]
    gains = math.fsum(max(0, value) for value in profits)
    losses = -math.fsum(min(0, value) for value in profits)
    return {
        "initial_equity": initial, "final_equity": balance,
        "net_pnl": balance - initial,
        "return_pct": (balance / initial - 1) * 100,
        "trade_count": len(trades),
        "wins": sum(value > 0 for value in profits),
        "losses": sum(value < 0 for value in profits),
        "win_rate": sum(value > 0 for value in profits) / len(profits) if profits else None,
        "profit_factor": gains / losses if losses > 0 else None,
        "profit_factor_note": "undefined_without_losses" if losses == 0 else None,
        "expectancy_usdt": math.fsum(profits) / len(profits) if profits else None,
        "max_drawdown_pct": max_drawdown * 100,
        "max_drawdown_usdt": max_drawdown_usdt,
        "fees_usdt": math.fsum(t["entry_fee"] + t["exit_fee"] for t in trades),
        "funding_estimate_usdt": math.fsum(t["funding_estimate"] for t in trades),
        "halt_reason": halted, "skipped_entries": skipped,
    }


def _simulate(config, symbol: str, bars: list[Candle], signals: list[Signal | None],
              start: int, end: int, normalize, funding_rate: float) -> dict:
    """Simulate [start, end), entering at the next open after each signal."""
    if not WARMUP <= start < end <= len(bars) or len(signals) != len(bars):
        raise ValueError("Invalid research segment")
    state = new_state(config)
    risk = RiskAgent(config)
    balance = config.initial_equity
    peak, max_drawdown, max_drawdown_usdt = balance, 0.0, 0.0
    position = None
    trades = []
    skipped = {"risk": 0, "exchange_filters": 0}
    slip = config.slippage_bps / 10000

    def record_equity(value: float) -> None:
        nonlocal peak, max_drawdown, max_drawdown_usdt
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak)
        max_drawdown_usdt = max(max_drawdown_usdt, peak - value)

    for i in range(start, end):
        bar = bars[i]
        had_position = position is not None
        opening_equity = balance
        if position:
            opening_equity += position["direction"] * position["quantity"] * (bar.open - position["entry_price"])
            opening_equity -= _funding(position, bar.open_time, funding_rate)
        risk.update_limits(state, opening_equity, bar.open_time / 1000)
        record_equity(opening_equity)
        signal = signals[i]
        if (not had_position and not state["halt_reason"] and not state["daily_paused"]
                and signal and signal.eligible and abs(signal.score) >= config.entry_threshold):
            if signal.based_on_close_time >= bar.open_time:
                raise ValueError("Signal must precede entry candle")
            direction = 1 if signal.score > 0 else -1
            entry = bar.open * (1 + direction * slip)
            entry_snapshot = Snapshot(symbol, bars[max(0, i - 240):i], entry, entry, 0,
                                      bar.open_time / 1000)
            approval = risk.analyze(entry_snapshot, state, direction, signal.atr, funding_rate)
            if approval.veto:
                skipped["risk"] += 1
            else:
                approved = approval.details
                # Reserve up to maximum holding time in the same funding scenario.
                funding_reserve = abs(funding_rate) * max(1, config.max_holding_hours / 8)
                unit_cost = (approved["stop_distance"] + entry *
                             (2 * config.fee_rate + 2 * slip + funding_reserve))
                requested = min(approved["quantity"], approved["risk_budget"] / unit_cost)
                try:
                    quantity = normalize(symbol, requested, entry)
                    if (type(quantity) not in (int, float) or not math.isfinite(quantity)
                            or quantity <= 0 or quantity > requested * (1 + 1e-12)
                            or quantity * entry / config.leverage > approved["margin"] * (1 + 1e-12)):
                        raise ValueError("Normalization increased exposure")
                except ValueError:
                    skipped["exchange_filters"] += 1
                else:
                    entry_fee = quantity * entry * config.fee_rate
                    balance -= entry_fee
                    position = {
                        "direction": direction, "entry_index": i,
                        "entry_time": bar.open_time,
                        "signal_close_time": signal.based_on_close_time,
                        "entry_price": entry, "quantity": quantity,
                        "entry_fee": entry_fee,
                        "stop": approved["stop"], "target": approved["target"],
                        "risk_usdt": quantity * unit_cost,
                        "margin": quantity * entry / config.leverage,
                        "entry_equity": opening_equity,
                    }
        if position:
            trigger = _exit_trigger(position, bar, config.max_holding_hours, i == end - 1)
            if state["halt_reason"] or state["daily_paused"]:
                trigger = (bar.open, bar.open_time, "risk_limit")
            if trigger:
                raw_exit, exit_time, reason = trigger
                exit_price = raw_exit * (1 - position["direction"] * slip)
                exit_fee = position["quantity"] * exit_price * config.fee_rate
                funding = _funding(position, exit_time, funding_rate)
                gross = position["direction"] * position["quantity"] * (exit_price - position["entry_price"])
                net = gross - position["entry_fee"] - exit_fee - funding
                # Stop-first paths permit a conservative known adverse level.
                # For a target, do not use prices that may have occurred after exit.
                adverse = raw_exit if reason in {"stop", "stop_gap", "risk_limit"} else min(
                    bar.open, raw_exit) if position["direction"] == 1 else max(bar.open, raw_exit)
                if reason in {"segment_end", "max_holding"} and exit_time != bar.open_time:
                    adverse = bar.low if position["direction"] == 1 else bar.high
                record_equity(balance + position["direction"] * position["quantity"] *
                              (adverse - position["entry_price"]) - funding - exit_fee)
                balance += gross - exit_fee - funding
                trades.append({**position, "exit_index": i, "exit_time": exit_time,
                               "exit_price": exit_price, "exit_fee": exit_fee,
                               "funding_estimate": funding, "gross_pnl": gross,
                               "net_pnl": net, "reason": reason})
                position = None
                record_equity(balance)
            else:
                # OHLC adverse excursion is conservative; no favorable intrabar
                # peak is assumed because its ordering is not observable.
                adverse = bar.low if position["direction"] == 1 else bar.high
                funding = _funding(position, bar.close_time, funding_rate)
                adverse_equity = balance + position["direction"] * position["quantity"] * (adverse - position["entry_price"]) - funding
                record_equity(adverse_equity)
        closing_equity = balance
        if position:
            closing_equity += position["direction"] * position["quantity"] * (bar.close - position["entry_price"])
            closing_equity -= _funding(position, bar.close_time, funding_rate)
        record_equity(closing_equity)
        risk.update_limits(state, closing_equity, bar.close_time / 1000)
    return {
        "start_index": start, "end_index_exclusive": end,
        "start_utc": _iso(bars[start].open_time), "end_utc": _iso(bars[end - 1].close_time),
        "metrics": _metrics(trades, config.initial_equity, balance, max_drawdown,
                            max_drawdown_usdt, state["halt_reason"], skipped),
        "trades": trades,
    }


def _assessment(train: dict, holdout: dict, config) -> dict:
    tm, hm = train["metrics"], holdout["metrics"]
    count = tm["trade_count"] + hm["trade_count"]
    checks = {
        "at_least_100_trades": count >= 100,
        "at_least_30_holdout_trades": hm["trade_count"] >= 30,
        "positive_training_net_pnl": tm["net_pnl"] > 0,
        "positive_holdout_net_pnl": hm["net_pnl"] > 0,
        "holdout_profit_factor_above_1_2": hm["profit_factor"] is not None and hm["profit_factor"] > 1.2,
        "holdout_drawdown_within_limit": hm["max_drawdown_pct"] <= config.max_drawdown_pct * 100,
        "no_holdout_halt": hm["halt_reason"] is None,
    }
    sufficient = checks["at_least_100_trades"] and checks["at_least_30_holdout_trades"]
    return {
        "sample_sufficient": sufficient,
        "passes_preliminary_checks": all(checks.values()),
        "checks": checks,
        "status": "insufficient_sample" if not sufficient else "preliminary_only",
        "auto_promote": False,
        "note": "Filtros heurísticos de investigación; no acreditan rentabilidad ni autorizan operar en real.",
    }


def run_research(config, client) -> dict:
    """Fetch public Demo data, train nine candidates, test only train winner."""
    if config.leverage != 5 or config.mode not in {"paper", "demo"}:
        raise ValueError("Research is limited to paper/demo at 5x")
    result = {
        "kind": "chronological_train_holdout_research",
        "data_environment": "binance_futures_demo_public",
        "history_limit": HISTORY_LIMIT,
        "train_fraction": 0.6, "holdout_fraction": 0.4,
        "warmup_candles": WARMUP,
        "selection": "Maximum training net PnL minus training drawdown USDT; holdout excluded from selection",
        "grid": {"entry_threshold": list(THRESHOLDS), "stop_atr_multiple": list(STOP_MULTIPLES)},
        "capital_per_symbol_per_segment": config.initial_equity,
        "portfolio_aggregation": False, "auto_promote": False,
        "assumptions": [
            "Cada símbolo y tramo es una simulación aislada; no sumar capitales o resultados como cartera.",
            "Señal con velas previas cerradas; entrada a la siguiente apertura con slippage adverso.",
            "Si stop y objetivo se tocan en una vela sin orden observable, se ejecuta primero el stop.",
            "Se cierra toda posición al final de cada tramo y se reinicia el capital para holdout.",
            "Comisiones/slippage configurados; no se reconstruyen spread, liquidez ni noticias históricas.",
            "Filtros actuales de Binance se aplican hacia atrás; pueden diferir de los filtros históricos.",
            "Financiación es un escenario de coste con abs(tasa actual), prorrateada cada 8h; NO es financiación histórica real.",
            "El drawdown usa marcas observables y excursiones adversas conservadoras; no reconstruye una trayectoria tick a tick.",
            "No modela liquidaciones exactas, mantenimiento de margen, profundidad ni latencia; los gaps pueden superar el riesgo estimado.",
            "Una única división temporal es una evaluación preliminar, no múltiples ventanas walk-forward ni prueba estadística.",
        ],
        "symbols": [],
    }
    for symbol in config.symbols:
        try:
            snapshot = client.snapshot(symbol, config.interval, HISTORY_LIMIT)
            bars = _closed_candles(snapshot, WARMUP + 100)
            split = int(len(bars) * 0.6)
            if split <= WARMUP or len(bars) - split < 50:
                raise ValueError("Insufficient chronological history")
            premium = client.premium_index(symbol)
            funding_rate = float(premium["lastFundingRate"])
            if not math.isfinite(funding_rate):
                raise ValueError("Invalid funding estimate")
            # Cache/validate public symbol filters once before simulation.
            client.exchange_info(symbol)
            signals = _signals(symbol, bars)
            candidates = []
            for threshold in THRESHOLDS:
                for multiple in STOP_MULTIPLES:
                    candidate_config = replace(config, entry_threshold=threshold, stop_atr_multiple=multiple)
                    train = _simulate(candidate_config, symbol, bars, signals, WARMUP, split,
                                      client.normalize_quantity, funding_rate)
                    metrics = train["metrics"]
                    candidates.append({
                        "entry_threshold": threshold, "stop_atr_multiple": multiple,
                        "training_objective": metrics["net_pnl"] - metrics["max_drawdown_usdt"],
                        "train": train,
                    })
            # Stable grid order breaks ties; no holdout data ranks candidates.
            selected = max(candidates, key=lambda item: item["training_objective"])
            selected_config = replace(config, entry_threshold=selected["entry_threshold"],
                                      stop_atr_multiple=selected["stop_atr_multiple"])
            holdout = _simulate(selected_config, symbol, bars, signals, split, len(bars),
                                client.normalize_quantity, funding_rate)
            result["symbols"].append({
                "symbol": symbol, "status": "ok", "candles": len(bars),
                "fetched_at": _iso(snapshot.fetched_at * 1000),
                "funding_rate_current": funding_rate,
                "funding_method": "absolute_current_rate_prorated_8h_not_historical",
                "candidate": {"entry_threshold": selected["entry_threshold"],
                              "stop_atr_multiple": selected["stop_atr_multiple"]},
                "training_candidates": [
                    {key: value for key, value in candidate.items() if key != "train"}
                    | {"metrics": candidate["train"]["metrics"]} for candidate in candidates
                ],
                "train": selected["train"], "holdout": holdout,
                "assessment": _assessment(selected["train"], holdout, config),
            })
        except (ExchangeError, ValueError, TypeError, KeyError, OverflowError, OSError):
            # Do not expose arbitrary client exception text or URLs/credentials.
            result["symbols"].append({"symbol": symbol, "status": "unavailable_or_invalid_data",
                                      "auto_promote": False})
    result["status"] = "completed" if all(s["status"] == "ok" for s in result["symbols"]) else "partial_or_unavailable"
    return result
