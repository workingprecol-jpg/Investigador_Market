"""Deterministic, read-only daily research; a proposal is never an approval.

Only closed outcomes known before the review cutoff are used. Older records
without decision IDs remain useful for accounting, never causal attribution.
No exchange, filesystem, network, or configuration writes happen here.
"""
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone


BOGOTA = timezone(timedelta(hours=-5), name="America/Bogota")
SPECIALISTS = (
    ("trend", "Tendencia"),
    ("momentum", "Indicadores y momentum"),
    ("liquidity", "Liquidez y ejecución"),
    ("market_context", "Noticias y contexto"),
    ("risk", "Riesgo y rendimiento"),
    ("research_validation", "Investigación y validación"),
)
MIN_POSITIONS = 30


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _sum(values):
    try:
        value = math.fsum(values)
        return value if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _mean(values):
    total = _sum(values)
    return total / len(values) if values and total is not None else None


def _identifier(value):
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) else None


def _direction(value):
    if isinstance(value, bool):
        return "unknown"
    if value == 1 or value in ("long", "LONG"):
        return "long"
    if value == -1 or value in ("short", "SHORT"):
        return "short"
    return "unknown"


def _consistent(rows, key):
    values = {str(row[key]) for row in rows if row.get(key) is not None}
    return next(iter(values)) if len(values) == 1 else None


def _metrics(positions):
    profits = [p["net_pnl"] for p in positions]
    gains = _sum(x for x in profits if x > 0)
    losses = _sum(-x for x in profits if x < 0)
    missing = {key: sum(p.get(key) is None for p in positions)
               for key in ("fees", "funding", "gross_pnl", "decision_id", "position_id", "regime", "symbol")}
    missing["direction"] = sum(p["direction"] == "unknown" for p in positions)
    totals = {key: _sum(p[key] for p in positions) if not missing[key] else None
              for key in ("fees", "funding", "gross_pnl")}
    return {
        "count": len(positions), "closed_positions": len(positions),
        "verified_closed_positions": sum(p["completion_known"] for p in positions),
        "exit_records": sum(p["exit_records"] for p in positions),
        "legacy_record_count": sum(p["position_id"] is None for p in positions),
        "net_pnl": _sum(profits), **totals,
        "known_fees": _sum(p["fees"] for p in positions if p["fees"] is not None),
        "known_funding": _sum(p["funding"] for p in positions if p["funding"] is not None),
        "wins": sum(x > 0 for x in profits), "losses": sum(x < 0 for x in profits),
        "win_rate_pct": 100 * sum(x > 0 for x in profits) / len(profits) if profits else None,
        "profit_factor": _number(gains / losses) if gains is not None and losses else None,
        "profit_factor_note": "Sin pérdidas: razón indefinida" if profits and losses == 0 else None,
        "expectancy_usdt": _mean(profits), "missing_metadata": missing,
    }


def _period(positions, start, end):
    sample = [p for p in positions if (start is None or p["closed_at"] >= start) and p["closed_at"] < end]
    result = {"start_utc": _iso(start) if start is not None else None,
              "end_utc_exclusive": _iso(end), **_metrics(sample)}
    for key in ("symbol", "direction", "regime"):
        groups = {}
        for position in sample:
            groups.setdefault(position.get(key) or "unknown", []).append(position)
        result["by_" + key] = {name: _metrics(rows) for name, rows in sorted(groups.items())}
    return result


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _positions(state, cutoff):
    groups, seen = {}, set()
    quality = dict(invalid_trade_records=0, duplicate_trade_records=0,
                   excluded_partial_positions=0, unknown_completion_positions=0,
                   future_trade_records_excluded=0, inconsistent_position_metadata=0)
    rows = state.get("trades", [])
    if not isinstance(rows, list):
        rows = []
        quality["invalid_trade_records"] += 1
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or _number(row.get("closed_at")) is None or _number(row.get("net_pnl")) is None:
            quality["invalid_trade_records"] += 1
            continue
        if row["closed_at"] >= cutoff:
            quality["future_trade_records_excluded"] += 1
            continue
        # Do not collapse two economically identical fills without a durable ID.
        fill_id = _identifier(row.get("trade_id")) or _identifier(row.get("execution_id"))
        if fill_id is not None:
            unique = (row.get("symbol"), fill_id)
            if unique in seen:
                quality["duplicate_trade_records"] += 1
                continue
            seen.add(unique)
        position_id = _identifier(row.get("position_id"))
        key = ("position", position_id) if position_id else ("legacy", index)
        groups.setdefault(key, []).append(row)
    positions = []
    for key, rows in groups.items():
        rows = sorted(rows, key=lambda r: r["closed_at"])
        latest = rows[-1]
        completion = [r.get("position_closed") for r in rows if isinstance(r.get("position_closed"), bool)]
        remaining = _number(latest.get("remaining_quantity"))
        complete = latest.get("position_closed") is True or remaining == 0
        explicitly_open = (latest.get("position_closed") is False
                           or (latest.get("position_closed") is not True and remaining is not None and remaining > 0))
        if explicitly_open or (completion and not complete):
            quality["excluded_partial_positions"] += 1
            continue
        if key[0] == "position" and not complete:
            quality["unknown_completion_positions"] += 1
        totals = {name: _sum(r[name] for r in rows) if all(_number(r.get(name)) is not None for r in rows) else None
                  for name in ("net_pnl", "fees", "funding", "gross_pnl")}
        if totals["net_pnl"] is None:
            quality["invalid_trade_records"] += len(rows)
            continue
        directions = {_direction(r.get("direction")) for r in rows}
        symbol = _consistent(rows, "symbol")
        decision_id = _consistent(rows, "decision_id")
        if len(directions) > 1 or symbol is None or len({r.get("decision_id") for r in rows if r.get("decision_id")}) > 1:
            quality["inconsistent_position_metadata"] += 1
            decision_id = None
        opened = [_number(r.get("opened_at")) for r in rows]
        position = {
            **totals, "symbol": symbol, "direction": next(iter(directions)) if len(directions) == 1 else "unknown",
            "regime": _consistent(rows, "regime") or _consistent(
                [r["entry_context"] for r in rows if isinstance(r.get("entry_context"), dict)], "regime"),
            "decision_id": decision_id,
            "position_id": key[1] if key[0] == "position" else None,
            "opened_at": min(opened) if all(t is not None for t in opened) else None,
            "closed_at": latest["closed_at"], "exit_records": len(rows),
            "completion_known": complete,
        }
        positions.append(position)
    return positions, quality


def _analyses(events, cutoff):
    analyses, seen = [], set()
    quality = dict(invalid_analysis_records=0, duplicate_analysis_records=0,
                   legacy_analysis_records=0, ambiguous_decision_ids=0)
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "analysis":
            continue
        timestamp = _number(event.get("ts"))
        payload = event.get("data")
        if timestamp is None or not isinstance(payload, dict) or not isinstance(payload.get("agents"), list):
            quality["invalid_analysis_records"] += 1
            continue
        if timestamp >= cutoff:
            continue
        # Legacy event copies can be deduplicated exactly, never by score/symbol alone.
        fingerprint = json.dumps({"ts": timestamp, "data": payload}, sort_keys=True, default=str)
        event_id = _identifier(event.get("id"))
        key = ("id", event_id) if event_id else ("legacy", fingerprint)
        if key in seen:
            quality["duplicate_analysis_records"] += 1
            continue
        seen.add(key)
        decision_id = _identifier(payload.get("decision_id"))
        quality["legacy_analysis_records"] += decision_id is None
        analyses.append({"ts": timestamp, "decision_id": decision_id,
                         "symbol": payload.get("symbol"), "allowed": payload.get("allowed"),
                         "agents": [a for a in payload["agents"] if isinstance(a, dict)]})
    decisions, ambiguous = {}, set()
    for entry in analyses:
        key = entry["decision_id"]
        if key is None:
            continue
        if key in decisions:
            ambiguous.add(key)
        decisions[key] = entry
    for key in ambiguous:
        decisions.pop(key, None)
    quality["ambiguous_decision_ids"] = len(ambiguous)
    return analyses, decisions, quality


def _feature(agent, advice):
    details = advice.get("details") if isinstance(advice.get("details"), dict) else {}
    if agent in ("trend", "momentum"):
        return _number(advice.get("score"))
    if agent == "liquidity":
        return _number(details.get("spread_bps"))
    if agent == "market_context":
        return details.get("fresh") if isinstance(details.get("fresh"), bool) else None
    return _number(details.get("risk_usdt"))


def _evidence(agent, analyses, positions, decisions):
    observations, linked = [], []
    for analysis in analyses:
        matching = [a for a in analysis["agents"] if a.get("agent") == agent]
        if len(matching) == 1:
            observations.append(matching[0])
    for position in positions:
        analysis = decisions.get(position["decision_id"])
        if not analysis or analysis["symbol"] != position["symbol"]:
            continue
        # A decision recorded after entry cannot explain the entry.
        if position["opened_at"] is None or analysis["ts"] > position["opened_at"] or analysis["allowed"] is not True:
            continue
        matching = [a for a in analysis["agents"] if a.get("agent") == agent]
        if len(matching) == 1:
            linked.append((position, matching[0]))
    scores = [_number(a.get("score")) for a in observations]
    scores = [x for x in scores if x is not None]
    available = [a.get("details", {}).get("available") for a in observations if isinstance(a.get("details"), dict)]
    fresh = [a.get("details", {}).get("fresh") for a in observations if isinstance(a.get("details"), dict)]
    evidence = {
        "observations": len(observations), "veto_count": sum(a.get("veto") is True for a in observations),
        "available_count": sum(x is True for x in available),
        "unavailable_count": sum(x is False for x in available),
        "unknown_availability_count": len(observations) - sum(isinstance(x, bool) for x in available),
        "fresh_count": sum(x is True for x in fresh), "stale_count": sum(x is False for x in fresh),
        "score_count": len(scores), "score_average": _mean(scores),
        "invalid_or_missing_scores": len(observations) - len(scores),
        "feature_observations": sum(_feature(agent, a) is not None for a in observations),
        "attributed_positions": len(linked),
        "attributed_net_pnl": _sum(p["net_pnl"] for p, _ in linked),
        "linked_feature_positions": sum(_feature(agent, a) is not None for _, a in linked),
        "attribution_method": "decision_id exacto, símbolo coincidente y decisión anterior a entrada",
        "causal_effect_estimated": False,
    }
    return evidence, linked


class ResearchValidationAgent:
    """Sixth specialist: assess evidence and propose bounded, unapplied trials."""

    def __init__(self, config):
        self.config = config

    def _value(self, name, default):
        return self.config.get(name, default) if isinstance(self.config, dict) else getattr(self.config, name, default)

    def _proposal(self, agent, evidence, linked, all_metrics):
        choices = {
            "trend": ("min_trend_score", .65, "Evaluar si exigir más fuerza de tendencia evita entradas débiles."),
            "momentum": ("min_momentum_score", .1, "Evaluar si una confirmación mínima de momentum mejora entradas."),
            "liquidity": ("max_spread_bps", None, "Evaluar un spread máximo menor para reducir costes de ejecución."),
            "market_context": ("require_fresh_news", True, "Evaluar el bloqueo de nuevas entradas sin noticias recientes."),
            "risk": ("risk_per_trade", None, "Evaluar menor tamaño para limitar pérdidas; no implica mayor rentabilidad."),
        }
        field, target, hypothesis = choices[agent]
        defaults = dict(min_trend_score=0.0, min_momentum_score=0.0, max_spread_bps=12.0,
                        require_fresh_news=False, risk_per_trade=.005)
        current = self._value(field, defaults[field])
        valid = isinstance(current, bool) if field == "require_fresh_news" else _number(current) is not None
        if field == "max_spread_bps" and valid:
            target = min(current, max(1.0, current * .75))
        if field == "risk_per_trade" and valid:
            target = current * .75
        if not valid or (field != "require_fresh_news" and current < 0):
            target = None
        unchanged = target is None or target == current
        if field in ("min_trend_score", "min_momentum_score") and valid and target is not None and current >= target:
            unchanged = True
            target = current
        patch = {field: target} if target is not None else {}
        key = hashlib.sha256(json.dumps({"agent": agent, "patch": patch}, sort_keys=True).encode()).hexdigest()[:16]
        affected = []
        for position, advice in linked:
            feature = _feature(agent, advice)
            if feature is None or target is None:
                continue
            applies = ((agent in ("trend", "momentum") and abs(feature) < target)
                       or (agent == "liquidity" and feature > target)
                       or (agent == "market_context" and feature is False))
            if applies:
                affected.append(position)
        sample_ok = all_metrics["verified_closed_positions"] >= MIN_POSITIONS
        if agent == "risk":
            feature_ok = sample_ok
            hypothesis_supported = all_metrics["net_pnl"] is not None and all_metrics["net_pnl"] < 0
        else:
            feature_ok = evidence["linked_feature_positions"] >= MIN_POSITIONS
            affected_pnl = _sum(p["net_pnl"] for p in affected)
            hypothesis_supported = len(affected) >= 5 and affected_pnl is not None and affected_pnl < 0
        status = ("no_change" if unchanged else "insufficient_data" if not (sample_ok and feature_ok)
                  else "proposed" if hypothesis_supported else "no_change")
        return {
            "id": "proposal-" + key, "agent": agent, "hypothesis": hypothesis, "patch": patch,
            "current": {field: current if valid else None}, "status": status,
            "evidence": {"closed_positions": all_metrics["count"],
                         "verified_closed_positions": all_metrics["verified_closed_positions"], "minimum_positions": MIN_POSITIONS,
                         "linked_feature_positions": evidence["linked_feature_positions"],
                         "affected_historical_positions": len(affected),
                         "affected_historical_net_pnl": _sum(p["net_pnl"] for p in affected),
                         "hypothesis_supported_for_testing": hypothesis_supported},
            "applied": False, "approval": "requires_forward_paper_validation",
        }

    def review(self, state: dict, events: list[dict], review_date: str, cutoff: float) -> dict:
        if _number(cutoff) is None:
            raise ValueError("El corte temporal debe ser finito")
        review_day = date.fromisoformat(review_date)
        day_start = datetime.combine(review_day, time(), BOGOTA).timestamp()
        day_end = day_start + 86400
        effective_end = min(cutoff, day_end)
        if effective_end <= day_start:
            raise ValueError("El corte debe ser posterior al inicio del día revisado")
        positions, quality = _positions(state, effective_end)
        analyses, decisions, event_quality = _analyses(events, effective_end)
        quality.update(event_quality)
        # Regime may be recovered only through the exact historical entry decision.
        for position in positions:
            analysis = decisions.get(position["decision_id"])
            if (not position["regime"] and analysis and analysis["symbol"] == position["symbol"]
                    and position["opened_at"] is not None and analysis["ts"] <= position["opened_at"]):
                trend = [a for a in analysis["agents"] if a.get("agent") == "trend"]
                if len(trend) == 1 and isinstance(trend[0].get("details"), dict):
                    regime = trend[0]["details"].get("regime")
                    if isinstance(regime, str) and regime:
                        position["regime"] = regime
        periods = {name: _period(positions, start, effective_end) for name, start in (
            ("day", day_start), ("days7", day_start - 6 * 86400),
            ("days30", day_start - 29 * 86400), ("all", None))}
        specialists, proposals = [], []
        for agent, name in SPECIALISTS[:-1]:
            evidence, linked = _evidence(agent, analyses, positions, decisions)
            proposal = self._proposal(agent, evidence, linked, periods["all"])
            proposals.append(proposal)
            findings = [f'{evidence["observations"]} evaluaciones, {evidence["veto_count"]} vetos; '
                        f'{evidence["attributed_positions"]} posiciones enlazadas exactamente.']
            if proposal["status"] == "insufficient_data":
                findings.append("Muestra insuficiente para recomendar un experimento de este especialista.")
            elif proposal["status"] == "no_change":
                findings.append("Conservar configuración: no hay evidencia suficiente a favor del cambio propuesto.")
            else:
                findings.append("Hay evidencia para probar una hipótesis; todavía no está validada ni aplicada.")
            specialists.append({"id": agent, "name": name, "status": proposal["status"],
                                "findings": findings, "evidence": evidence, "proposal_ids": [proposal["id"]]})
        eligible = [p for p in proposals if p["status"] == "proposed"]
        specialists.append({
            "id": "research_validation", "name": "Investigación y validación",
            "status": "evaluate_candidates" if eligible else "collect_data",
            "findings": ["Revisión determinista del historial conocido al corte; sin optimización con datos futuros.",
                         "Cada candidato modifica un único campo y exige evaluación prospectiva en paper trading.",
                         "La atribución observacional no demuestra el efecto causal de un especialista."],
            "evidence": {**quality, "closed_positions": len(positions), "analysis_records": len(analyses),
                         "eligible_proposals": len(eligible), "proposals_applied": 0},
            "proposal_ids": [],
        })
        limitations = [
            "Las propuestas son hipótesis: no garantizan beneficio ni autorizan cambios en producción.",
            "Los resultados de posiciones se asignan a su cierre final e incluyen sus salidas parciales conocidas.",
            "Sin position_id, cada registro legado se cuenta por separado; no se puede reconstruir un cierre parcial con certeza.",
            "No se estima el resultado de entradas vetadas sin una simulación prospectiva independiente.",
            "El historial usado para proponer cambios no sirve como validación independiente de esos mismos cambios.",
            "Las observaciones y los vínculos describen asociaciones; no acreditan contribuciones causales ni confianza predictiva.",
            "Funding y costes reflejan el registro disponible; cargos recibidos después del informe requieren revisión posterior.",
        ]
        if quality["legacy_analysis_records"] or periods["all"]["missing_metadata"]["decision_id"]:
            limitations.append("Existen registros legados sin decision_id: no se atribuyen resultados a agentes por aproximación temporal.")
        if quality["unknown_completion_positions"]:
            limitations.append("Hay posiciones agrupadas sin indicador de cierre completo; su finalización no está acreditada.")
        if any(quality[k] for k in ("invalid_trade_records", "invalid_analysis_records", "inconsistent_position_metadata")):
            limitations.append("Se detectaron registros inválidos o inconsistentes; consulte la evidencia del especialista de validación.")
        return {
            "schema_version": 1, "date": review_date, "timezone": "America/Bogota",
            "cutoff_utc": _iso(effective_end), "requested_cutoff_utc": _iso(cutoff),
            "day_complete": cutoff >= day_end, "periods": periods, "specialists": specialists,
            "proposals": proposals, "decision": "evaluate_candidates" if eligible else "collect_data",
            "limitations": limitations,
        }
