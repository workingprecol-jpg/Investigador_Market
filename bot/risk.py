import math
from datetime import datetime, timezone
from .models import Advice


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def new_state(config):
    return dict(version=1, mode=config.mode, initial_equity=config.initial_equity,
                symbols=list(config.symbols), leverage=config.leverage,
                strategy_profile=config.strategy_profile, balance=config.initial_equity,
                equity=config.initial_equity, peak_equity=config.initial_equity,
                day="", day_start_equity=config.initial_equity, positions={}, pending={},
                trades=[], last_candles={}, halt_reason=None, daily_paused=False,
                risk_multiplier=1.0, adaptation_count=0, adaptations=[], heartbeat=0,
                healthy=False, demo_wallet_offset=None, created_at=datetime.now(timezone.utc).timestamp())


class RiskAgent:
    """Fifth specialist. Risk constraints have final authority over all signals."""
    def __init__(self, config):
        self.config = config

    def update_limits(self, state, equity, now):
        c = self.config
        if not _finite(equity):
            state["halt_reason"] = "Equidad invalida"
            return
        try:
            if not _finite(now) or now < 0:
                raise ValueError("Timestamp invalido")
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            if any(not _finite(state[name]) or state[name] <= 0 for name in ("peak_equity", "day_start_equity")):
                raise ValueError("Referencias de equidad invalidas")
        except (ValueError, OverflowError, OSError, KeyError) as exc:
            state["halt_reason"] = str(exc)
            return
        if state["day"] != day:
            state.update(day=day, day_start_equity=equity, daily_paused=False)
        state["equity"] = equity
        state["peak_equity"] = max(state["peak_equity"], equity)
        if equity <= c.initial_equity - c.max_total_loss_usdt:
            state["halt_reason"] = "Limite de perdida total alcanzado"
        elif equity <= state["peak_equity"] * (1 - c.max_drawdown_pct):
            state["halt_reason"] = "Limite de drawdown alcanzado"
        if equity <= state["day_start_equity"] * (1 - c.max_daily_loss_pct):
            state["daily_paused"] = True

    def analyze(self, snapshot, state, direction, atr, funding_rate=0.0):
        try:
            return self._analyze(snapshot, state, direction, atr, funding_rate)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, ZeroDivisionError) as exc:
            return Advice("risk", 0, True, f"Datos de riesgo invalidos: {exc}")

    def _analyze(self, snapshot, state, direction, atr, funding_rate):
        c = self.config
        reasons = []
        if state["halt_reason"] or state["daily_paused"]:
            reasons.append(state["halt_reason"] or "Limite diario alcanzado")
        if state["pending"]:
            reasons.append("Orden pendiente de conciliacion")
        if not isinstance(state["positions"], dict):
            raise ValueError("Posiciones invalidas")
        if snapshot.symbol not in c.symbols:
            reasons.append("Simbolo no configurado")
        if snapshot.symbol in state["positions"] or len(state["positions"]) >= c.max_positions:
            reasons.append("Limite de posiciones")
        if isinstance(direction, bool) or direction not in (-1, 1) or not _finite(atr) or atr <= 0:
            reasons.append("ATR/direccion invalido")
        if not _finite(funding_rate) or abs(funding_rate) > c.max_funding_rate:
            reasons.append("Financiacion demasiado alta o desconocida")
        if any(not _finite(state[name]) or state[name] <= 0 for name in ("equity", "peak_equity", "day_start_equity", "risk_multiplier")):
            reasons.append("Equidad o multiplicador de riesgo invalido")
        elif state["risk_multiplier"] > 1:
            reasons.append("No se permite aumentar el riesgo base")
        if not all(_finite(value) and value > 0 for value in (snapshot.ask, snapshot.bid)) or snapshot.ask < snapshot.bid:
            reasons.append("Cotizacion invalida")
        if reasons:
            return Advice("risk", 0, True, "; ".join(reasons))
        for position in state["positions"].values():
            if any(not _finite(position[name]) or position[name] < 0 for name in ("risk_usdt", "quantity", "entry_price")):
                raise ValueError("Riesgo o margen de una posicion invalido")
            if position["quantity"] <= 0 or position["entry_price"] <= 0:
                raise ValueError("Cantidad o precio de posicion invalido")
        price = snapshot.ask if direction == 1 else snapshot.bid
        stop_distance = max(atr * c.stop_atr_multiple, price * .003)
        # Avoid liquidation territory at 5x even before exchange-specific maintenance margin.
        if not _finite(stop_distance) or stop_distance / price > .08:
            return Advice("risk", 0, True, "Stop demasiado amplio para 5x")
        equity = state["equity"]
        remaining_total = equity - (c.initial_equity - c.max_total_loss_usdt)
        remaining_day = equity - state["day_start_equity"] * (1 - c.max_daily_loss_pct)
        remaining_drawdown = equity - state["peak_equity"] * (1 - c.max_drawdown_pct)
        existing_risk = math.fsum(p["risk_usdt"] for p in state["positions"].values())
        budget = min(equity * c.risk_per_trade * state["risk_multiplier"],
                     remaining_total - existing_risk, remaining_day - existing_risk,
                     remaining_drawdown - existing_risk)
        if budget <= 0 or not _finite(budget):
            return Advice("risk", 0, True, "Sin presupuesto de riesgo disponible")
        margin_used = math.fsum(p["quantity"] * p["entry_price"] / c.leverage for p in state["positions"].values())
        margin = min(equity * c.max_margin_fraction, max(0, equity * .4 - margin_used))
        cost_per_unit = stop_distance + price * (2*c.fee_rate + 2*c.slippage_bps/10000 + abs(funding_rate))
        quantity = min(budget / cost_per_unit, margin * c.leverage / price)
        stop, target = price-direction*stop_distance, price+direction*stop_distance*c.reward_risk_ratio
        if quantity <= 0 or not _finite(quantity) or not _finite(cost_per_unit):
            return Advice("risk", 0, True, "Sin margen disponible")
        if not all(_finite(value) and value > 0 for value in (stop, target)):
            return Advice("risk", 0, True, "Stop u objetivo invalido")
        return Advice("risk", direction, False, "Riesgo aprobado", {
            "quantity": quantity, "stop": stop,
            "target": target,
            "risk_usdt": quantity*cost_per_unit, "stop_distance": stop_distance,
            "risk_budget": budget, "margin": quantity*price/c.leverage})

    def adapt(self, state):
        """Evaluate disjoint completed batches, reducing exposure but never increasing it."""
        n = self.config.adaptation_min_trades
        try:
            count = len(state["trades"])
            processed = state["adaptation_count"]
            if isinstance(processed, bool) or not isinstance(processed, int) or not 0 <= processed <= count:
                raise ValueError("Contador de adaptacion invalido")
            before = state["risk_multiplier"]
            if not _finite(before) or not 0 < before <= 1:
                raise ValueError("Multiplicador de riesgo invalido")
            while count - processed >= n:
                sample = state["trades"][processed:processed+n]
                pnl = [trade["net_pnl"] for trade in sample]
                if not all(_finite(value) for value in pnl):
                    raise ValueError("PnL invalido en historial de adaptacion")
                net = math.fsum(pnl)
                gains = math.fsum(max(0, value) for value in pnl)
                losses = -math.fsum(min(0, value) for value in pnl)
                factor = gains / losses if losses else None
                if factor is not None and not _finite(factor):
                    raise ValueError("Factor de beneficio fuera de rango")
                before = state["risk_multiplier"]
                if net <= 0 or (factor is not None and factor < 1):
                    state["risk_multiplier"] = min(before, max(.25, before * .5))
                processed += n
                state["adaptation_count"] = processed
                state["adaptations"].append(dict(trades=processed, net_pnl=net, profit_factor=factor,
                                                 before=before, after=state["risk_multiplier"],
                                                 reason="Reducir exposicion si el bloque no es rentable; sin aumentar riesgo"))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            state["halt_reason"] = f"Adaptacion detenida: {exc}"
