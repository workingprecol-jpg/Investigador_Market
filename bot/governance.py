"""Bounded strategy versions. Validators cannot rewrite capital or risk limits."""
import hashlib
import json
import math
import copy
from dataclasses import asdict, replace
from datetime import datetime

ALLOWED_FIELDS = {"min_trend_score", "min_momentum_score", "max_spread_bps", "require_fresh_news", "risk_per_trade"}
OWNER = {"min_trend_score": "trend", "min_momentum_score": "momentum", "max_spread_bps": "liquidity",
         "require_fresh_news": "market_context", "risk_per_trade": "risk"}


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Se requiere zona horaria explícita")
    return parsed.timestamp()


def _completed_report(report, now):
    try:
        if (not isinstance(report, dict) or report.get("preview") is not False
                or report.get("status") != "completed" or report.get("day_complete") is not True
                or report.get("schema_version") != 1 or not _finite(now)):
            return False
        from .daily import day_bounds
        _, end = day_bounds(report["date"])
        cutoff, generated = _timestamp(report["cutoff_utc"]), _timestamp(report["generated_at"])
        return cutoff == end and cutoff <= generated <= now
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def fingerprint(config):
    values = asdict(config)
    for field in ("daily_review_enabled", "daily_review_time"):
        values.pop(field, None)
    # Preserve the fingerprint of states created before named profiles existed.
    # Non-default values remain part of the fingerprint and cannot be changed
    # silently on an existing ledger.
    if values.get("strategy_profile") == "trend":
        values.pop("strategy_profile")
    if values.get("allow_shorts") is True:
        values.pop("allow_shorts")
    return hashlib.sha256(json.dumps(values, sort_keys=True, allow_nan=False).encode()).hexdigest()


def ensure_strategy(state, config):
    if "strategy" not in state:
        state["strategy"] = dict(version=1, patch={}, history=[], base_config_fingerprint=fingerprint(config))
    strategy = state["strategy"]
    if strategy["base_config_fingerprint"] != fingerprint(config):
        raise ValueError("Configuracion base modificada: revisar versiones y experimentos antes de reiniciar")
    if type(strategy["version"]) is not int or strategy["version"] < 1:
        raise ValueError("Version de estrategia invalida")
    validate_patch(config, strategy["patch"], multiple=True)
    state.setdefault("experiments", {})
    state.setdefault("pending_promotions", [])
    state.setdefault("proposal_queue", [])
    state.setdefault("decisions_audit", [])
    state.setdefault("equity_history", [])
    state.setdefault("last_observations", {})
    return strategy


def validate_patch(config, patch, *, multiple=False, current=None, agent=None):
    if not isinstance(patch, dict) or (not multiple and len(patch) != 1) or set(patch) - ALLOWED_FIELDS:
        raise ValueError("Solo se admite un cambio del especialista, dentro de la lista permitida")
    current = current or config
    for key, value in patch.items():
        if agent is not None and OWNER[key] != agent:
            raise ValueError("El cambio no pertenece a este especialista")
        if key == "require_fresh_news":
            if type(value) is not bool or (getattr(config, key) and not value) or (getattr(current, key) and not value):
                raise ValueError("No se puede relajar el requisito de noticias")
        else:
            if not _finite(value):
                raise ValueError("Parametro no finito")
            if key.startswith("min_") and not max(getattr(config, key), getattr(current, key)) <= value <= .95:
                raise ValueError("Solo se permiten umbrales mas exigentes")
            if key == "max_spread_bps" and not 1 <= value <= min(config.max_spread_bps, current.max_spread_bps):
                raise ValueError("No se permite relajar el limite de spread")
            if key == "risk_per_trade" and not config.risk_per_trade*.25 <= value <= min(config.risk_per_trade, current.risk_per_trade):
                raise ValueError("No se permite elevar el riesgo o reducirlo fuera del rango validado")
    replace(config, **patch)


def effective_config(config, state):
    strategy = ensure_strategy(state, config)
    return replace(config, **strategy["patch"])


def queue_review(state, report, config, now):
    """A report is diagnostic. Only safe proposals can enter forward testing."""
    if not _completed_report(report, now):
        return False
    current = effective_config(config, state)
    if report.get("mode") != state["mode"]:
        return False
    if state.get("daily_review", {}).get("date", "") > report["date"]:
        return False
    state["daily_review"] = copy.deepcopy(report)
    if type(report.get("source_version")) is not int or report["source_version"] != state["strategy"]["version"]:
        state["daily_review"]["governance_status"] = "stale_source_version"
        return False
    state["daily_review"]["governance_status"] = "accepted"
    queued = {p["id"] for p in state["proposal_queue"]}
    for proposal in report.get("proposals", []):
        if proposal.get("status") != "proposed" or proposal.get("id") in queued or proposal.get("id") in state["experiments"]:
            continue
        try:
            validate_patch(config, proposal["patch"], current=current, agent=proposal["agent"])
        except (KeyError, ValueError, TypeError):
            continue
        if all(getattr(current, key) == value for key, value in proposal["patch"].items()):
            continue
        state["proposal_queue"].append({**copy.deepcopy(proposal), "review_date": report["date"], "queued_at": now,
                                        "base_version": state["strategy"]["version"]})
        queued.add(proposal["id"])
    # Decisions are admitted on a daily review, never directly by a news/model reply.
    for decision in state.get("pending_promotions", []):
        evaluated = decision.get("evaluated_at")
        if _finite(evaluated) and evaluated <= _timestamp(report["cutoff_utc"]):
            decision.update(daily_approved_at=now, daily_review_date=report["date"],
                            daily_review_cutoff=report["cutoff_utc"], daily_source_version=report["source_version"])
    return True


def _validated_experiment(experiment, decision, current, now):
    """Recompute the terminal comparison instead of trusting its promote label."""
    from .shadow import ShadowLab, DAY
    try:
        saved = experiment["decision"]
        core = ("id", "agent", "patch", "recommendation", "evaluated_at", "baseline", "candidate",
                "coverage_seconds", "elapsed_days", "enough_trades", "fresh_marks", "evidence_hash", "config_hash")
        if any(decision[key] != saved[key] for key in core):
            return False
        if (decision["id"] != experiment["id"] or decision["agent"] != experiment["agent"]
                or decision["patch"] != experiment["patch"] or experiment.get("last_error")
                or not isinstance(experiment.get("observations"), int) or experiment["observations"] <= 0):
            return False
        baseline_config = asdict(current)
        if (_digest(experiment["frozen_config"]) != _digest(baseline_config)
                or decision["config_hash"] != _digest(baseline_config)
                or _digest(experiment["candidate_config"]) != _digest(asdict(replace(current, **decision["patch"])) )
                or not isinstance(decision["evidence_hash"], str) or len(decision["evidence_hash"]) != 64
                or decision["evidence_hash"] != experiment["evidence_hash"]):
            return False
        started, evaluated, approved = experiment["started_at"], decision["evaluated_at"], decision["daily_approved_at"]
        if not all(_finite(v) for v in (started, evaluated, approved, now, experiment["coverage_seconds"])):
            return False
        if not started + 7 * DAY <= evaluated <= approved <= now:
            return False
        if evaluated > _timestamp(decision["daily_review_cutoff"]):
            return False
        if not 7 * DAY <= experiment["coverage_seconds"] <= evaluated - started:
            return False
        if decision["coverage_seconds"] != experiment["coverage_seconds"] or decision["elapsed_days"] != (evaluated-started)/DAY:
            return False
        if (decision["fresh_marks"] is not True or decision["enough_trades"] is not True
                or not ShadowLab._fresh_marks(experiment, current.symbols, evaluated)
                or not 0 <= evaluated - experiment["last_update"] <= 90):
            return False
        metrics = {}
        for name in ("baseline", "candidate"):
            portfolio = experiment[name]
            for trade in portfolio["trades"]:
                if (not _finite(trade.get("opened_at")) or not _finite(trade.get("closed_at"))
                        or not started <= trade["opened_at"] <= trade["closed_at"] <= evaluated):
                    return False
            metric = ShadowLab._metrics(portfolio, current.initial_equity)
            json.dumps(metric, allow_nan=False)
            if (metric != decision[name] or metric["closed_trades"] < 30 or metric["limits_breached"]
                    or not 0 <= metric["max_drawdown"] <= current.max_drawdown_pct):
                return False
            if not portfolio["positions"] and not math.isclose(metric["net_pnl"], metric["realized_pnl"], rel_tol=1e-8, abs_tol=1e-8):
                return False
            metrics[name] = metric
        b, c = metrics["baseline"], metrics["candidate"]
        factor = c["no_losing_trades"] or (c["profit_factor"] is not None and c["profit_factor"] >= 1.2)
        return (current.fee_rate > 0 and current.slippage_bps > 0 and factor
                and c["net_pnl"] > 0 and c["realized_pnl"] > 0 and c["net_pnl"] >= b["net_pnl"]
                and c["expectancy_r"] >= b["expectancy_r"] and c["max_drawdown"] <= b["max_drawdown"])
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, ZeroDivisionError):
        return False


def apply_ready_promotion(state, config, now):
    """Apply one validated revision only when the original portfolio is flat."""
    if state["positions"] or state["pending"] or state["halt_reason"] or state["daily_paused"]:
        return False
    current = effective_config(config, state)
    for decision in list(state["pending_promotions"]):
        if not decision.get("daily_approved_at"):
            continue
        experiment = state["experiments"].get(decision.get("id"), {})
        proposal = experiment.get("proposal", {})
        if (experiment.get("status") != "promote" or decision.get("recommendation") != "promote"
                or proposal.get("patch") != decision.get("patch")
                or experiment.get("source_version") != state["strategy"]["version"]
                or decision.get("daily_source_version") != state["strategy"]["version"]
                or state.get("daily_review", {}).get("governance_status") != "accepted"
                or decision.get("daily_review_date") != state["daily_review"].get("date")
                or decision.get("daily_review_cutoff") != state["daily_review"].get("cutoff_utc")
                or not _completed_report(state["daily_review"], now)
                or not _validated_experiment(experiment, decision, current, now)):
            state["pending_promotions"].remove(decision)
            continue
        try:
            validate_patch(config, decision["patch"], current=current, agent=decision["agent"])
        except (ValueError, KeyError, TypeError):
            state["pending_promotions"].remove(decision)
            continue
        strategy = state["strategy"]
        before = dict(strategy["patch"])
        strategy["patch"].update(decision["patch"])
        strategy["version"] += 1
        strategy["history"].append(dict(at=now, version=strategy["version"], proposal_id=decision["id"],
            agent=decision["agent"], action="promote", patch=decision["patch"], previous_patch=before,
            evidence={"baseline": copy.deepcopy(decision["baseline"]), "candidate": copy.deepcopy(decision["candidate"]),
                      "coverage_seconds": decision["coverage_seconds"], "elapsed_days": decision["elapsed_days"]},
            evidence_hash=decision.get("evidence_hash"),
            promoted_trade_count=len(state["trades"]), promoted_equity=state["equity"]))
        experiment["applied_at"] = now
        state["pending_promotions"].remove(decision)
        # Pending alternatives were compared with the old champion: re-review instead.
        state["proposal_queue"] = []
        return True
    return False


def rollback(state, config, now, reason="manual"):
    if state["positions"] or state["pending"]:
        raise ValueError("Reversion exige cartera sin posiciones ni ordenes pendientes")
    strategy = ensure_strategy(state, config)
    last = next((h for h in reversed(strategy["history"]) if h["action"] == "promote"), None)
    if not last or any(h["action"] == "rollback" and h.get("reverts_version") == last["version"] for h in strategy["history"]):
        raise ValueError("No hay version promovida pendiente de reversion")
    # Return to a previously authorized patch; this does not relax original hard limits.
    previous = dict(last["previous_patch"])
    # Reconstruct the saved predecessor, rather than accept an arbitrary backup.
    expected = {}
    for entry in strategy["history"]:
        if entry is last:
            break
        if entry["action"] == "promote":
            if entry["previous_patch"] != expected:
                raise ValueError("Historial de versiones inconsistente")
            validate_patch(config, entry["patch"], current=replace(config, **expected), agent=entry["agent"])
            expected.update(entry["patch"])
        elif entry["action"] == "rollback":
            target = next((h for h in strategy["history"] if h.get("version") == entry.get("reverts_version") and h.get("action") == "promote"), None)
            if target is None or entry["patch"] != target["previous_patch"]:
                raise ValueError("Reversion histórica sin predecesor acreditado")
            expected = dict(entry["patch"])
        else:
            raise ValueError("Accion de version desconocida")
    if previous != expected or strategy["patch"] != {**previous, **last["patch"]}:
        raise ValueError("La reversion no coincide con la version previamente autorizada")
    validate_patch(config, previous, multiple=True)
    strategy["patch"] = previous
    strategy["version"] += 1
    strategy["history"].append(dict(at=now, version=strategy["version"], action="rollback", patch=previous,
                                    reason=reason, reverts_version=last["version"]))
    state["pending_promotions"] = []
    state["proposal_queue"] = []
    return True
