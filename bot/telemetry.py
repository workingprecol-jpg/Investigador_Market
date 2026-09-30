"""Best-effort observation of actual work, isolated from trading state and SQLite.

The sidecar is an atomic, bounded snapshot, not evidence that trading is healthy.
Only an explicit heartbeat call records a completed coordinator cycle.
"""
import copy
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


AGENTS = ("coordinator", "trend", "momentum", "liquidity", "market_context", "risk", "research_validation")
MAX_EVENTS = 200
MAX_CANDLES = 120
DETAIL_FIELDS = {
    "regime", "ema20", "ema50", "ema200", "ema20_slope", "strength", "long_eligible", "short_eligible",
    "atr", "rsi", "macd", "macd_signal", "macd_histogram", "overextended", "spread_bps", "quote_volume",
    "available", "fresh", "status", "grok_status", "checked_at", "fetched_at", "latest_published_at",
    "max_age_seconds", "explicit_risk_headline_ids", "risk_usdt", "quantity", "stop", "target",
    "stop_distance", "margin", "notional", "leverage", "entry", "score", "risk_budget", "portfolio_risk",
    "decision", "date", "closed_positions", "eligible_proposals", "proposals_applied", "observations",
}
SECRET_FIELD = re.compile(r'''(?i)(api[_-]?key|secret|authorization|password|token|signature|x-mbx-apikey)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''')
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value, limit=300):
    if not isinstance(value, str):
        return None
    value = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", value)
    value = SECRET_FIELD.sub(r"\1=[redacted]", value)
    value = URL.sub("[url omitted]", value)
    value = LONG_TOKEN.sub("[redacted]", value)
    return " ".join(value.split())[:limit]


def _safe_value(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (float, int)):
        return _number(value)
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value[:10] if isinstance(item, (float, int, str, bool)) or item is None]
    return None


def _result(value):
    """Allowlist result metadata; never serialize arbitrary objects or exceptions."""
    if isinstance(value, BaseException):
        return {"error_type": type(value).__name__[:80]}
    source = value if isinstance(value, dict) else {
        name: getattr(value, name, None) for name in ("score", "veto", "reason", "details")
    }
    result = {}
    for field in ("score", "veto", "reason", "status", "decision", "date", "closed_positions", "eligible_proposals", "observations", "error_type"):
        if field in source and source[field] is not None:
            result[field] = _safe_value(source[field])
    details = source.get("details")
    if isinstance(details, dict):
        result["details"] = {key: _safe_value(value) for key, value in details.items() if key in DETAIL_FIELDS}
    return result


def _next_time(value):
    number = _number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp.timestamp() if stamp.tzinfo is not None else None
        except (ValueError, OverflowError):
            pass
    return None


class ActivityRecorder:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._spans = {}
        now = time.time()
        self._data = {
            "schema_version": 1, "process_instance_id": uuid.uuid4().hex, "pid": os.getpid(),
            "started_at": now, "updated_at": now, "last_cycle_heartbeat": None,
            "agent_activity": {agent: {
                "status": "waiting", "task": "Sin actividad observada en este proceso", "symbol": None,
                "started_at": None, "finished_at": None, "updated_at": now,
                "duration_ms": None, "last_result": None, "next_run_at": None,
            } for agent in AGENTS},
            "agent_events": [], "market_data": {},
        }
        self._write_error = None
        try:
            self._restore_history()
        except Exception as exc:
            self._write_error = type(exc).__name__
        try:
            self._publish(now)
        except Exception as exc:
            self._write_error = type(exc).__name__

    def _restore_history(self):
        if not self.path.exists() or self.path.stat().st_size > 2_000_000:
            return
        previous = json.loads(self.path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if not isinstance(previous, dict) or previous.get("schema_version") != 1:
            return
        events = previous.get("agent_events", [])
        if isinstance(events, list):
            for event in events[-MAX_EVENTS:]:
                if (not isinstance(event, dict) or event.get("agent") not in AGENTS
                        or event.get("status") not in ("running", "completed", "error", "waiting")
                        or _number(event.get("ts")) is None or not isinstance(event.get("id"), str)):
                    continue
                self._data["agent_events"].append({
                    "id": event["id"][:100], "ts": _number(event["ts"]), "agent": event["agent"],
                    "status": event["status"], "task": _text(event.get("task")), "symbol": _text(event.get("symbol"), 24),
                    "result": _result(event.get("result", {})), "duration_ms": _number(event.get("duration_ms")),
                    "process_instance_id": event.get("process_instance_id") if isinstance(event.get("process_instance_id"), str) else None,
                })
        market = previous.get("market_data", {})
        if isinstance(market, dict):
            for symbol, row in market.items():
                if isinstance(symbol, str) and re.fullmatch(r"[A-Z0-9]{3,24}", symbol) and isinstance(row, dict):
                    clean = self._clean_market_row(row)
                    if clean:
                        self._data["market_data"][symbol] = clean

    def _publish(self, now):
        self._data["updated_at"] = now
        self._data["agent_events"] = self._data["agent_events"][-MAX_EVENTS:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + self._data["process_instance_id"] + ".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        temporary.replace(self.path)
        self._write_error = None

    def _event(self, span, status, now, result=None, duration=None):
        self._data["agent_events"].append({
            "id": uuid.uuid4().hex, "ts": now, "agent": span["agent"], "status": status,
            "task": span["task"], "symbol": span["symbol"], "result": result,
            "duration_ms": duration, "process_instance_id": self._data["process_instance_id"],
        })

    def start(self, agent, task, symbol=None):
        token = None
        try:
            if agent not in AGENTS:
                return None
            with self._lock:
                now, token = time.time(), uuid.uuid4().hex
                span = {"agent": agent, "task": _text(task), "symbol": _text(symbol, 24),
                        "started_at": now, "monotonic": time.perf_counter(), "token": token}
                self._spans[token] = span
                self._data["agent_activity"][agent].update(
                    status="running", task=span["task"], symbol=span["symbol"], started_at=now,
                    finished_at=None, updated_at=now, duration_ms=None, next_run_at=None)
                self._event(span, "running", now)
                self._publish(now)
        except Exception as exc:
            self._write_error = type(exc).__name__
        return token

    def finish(self, agent, token=None, result=None, error=None):
        try:
            if agent not in AGENTS:
                return
            with self._lock:
                if token is None:
                    token = next((key for key, span in reversed(self._spans.items()) if span["agent"] == agent), None)
                span = self._spans.get(token)
                if not span or span["agent"] != agent:
                    return
                self._spans.pop(token)
                now = time.time()
                duration = max(0.0, (time.perf_counter() - span["monotonic"]) * 1000)
                try:
                    safe = {"error_type": type(error).__name__[:80]} if error is not None else _result(result)
                except Exception:
                    safe = {"result_type": type(result).__name__[:80]}
                self._event(span, "error" if error is not None else "completed", now, safe, duration)
                other = next((entry for entry in reversed(self._spans.values()) if entry["agent"] == agent), None)
                latest = self._data["agent_activity"][agent]
                latest.update(last_result=safe, duration_ms=duration, finished_at=now, updated_at=now)
                if other is None:
                    latest.update(status="error" if error is not None else "waiting", task=span["task"],
                                  symbol=span["symbol"], started_at=span["started_at"])
                else:
                    latest.update(status="running", task=other["task"], symbol=other["symbol"],
                                  started_at=other["started_at"], finished_at=None, duration_ms=None)
                self._publish(now)
        except Exception as exc:
            self._write_error = type(exc).__name__

    def run(self, agent, task, symbol, function, *args, **kwargs):
        token = self.start(agent, task, symbol)
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:
            self.finish(agent, token, error=exc)
            raise
        self.finish(agent, token, result=result)
        return result

    def idle(self, agent, task="Esperando", next_run_at=None):
        try:
            if agent not in AGENTS:
                return
            with self._lock:
                if any(span["agent"] == agent for span in self._spans.values()):
                    return
                latest = self._data["agent_activity"][agent]
                task, next_run_at = _text(task), _next_time(next_run_at)
                if latest["status"] == "waiting" and latest["task"] == task and latest["next_run_at"] == next_run_at:
                    return
                latest.update(status="waiting", task=task, next_run_at=next_run_at)
                # A wait update is scheduling information, not a new work event.
                self._publish(time.time())
        except Exception as exc:
            self._write_error = type(exc).__name__

    def heartbeat(self, timestamp=None):
        try:
            now = time.time()
            stamp = now if timestamp is None else _number(timestamp)
            if stamp is None or stamp <= 0 or stamp > now + 5:
                return
            with self._lock:
                previous = self._data["last_cycle_heartbeat"]
                if previous is not None and stamp <= previous:
                    return
                self._data["last_cycle_heartbeat"] = stamp
                self._publish(now)
        except Exception as exc:
            self._write_error = type(exc).__name__

    @staticmethod
    def _clean_market_row(row):
        fields = ("price", "bid", "ask", "mark_price", "funding_rate", "quote_volume", "fetched_at", "mark_fetched_at")
        clean = {key: _number(row[key]) for key in fields if key in row and _number(row[key]) is not None}
        bars = row.get("candles", [])
        clean["candles"] = []
        if isinstance(bars, list):
            for candle in bars[-MAX_CANDLES:]:
                if not isinstance(candle, dict):
                    continue
                parsed = {key: _number(candle.get(key)) for key in ("open_time", "close_time", "open", "high", "low", "close", "volume")}
                if all(value is not None for value in parsed.values()):
                    clean["candles"].append(parsed)
        return clean

    def publish_market(self, snapshots, premiums=None):
        """Publish actual inputs; absent symbols or fields keep their old timestamps."""
        try:
            with self._lock:
                now, changed = time.time(), False
                premiums = premiums or {}
                for symbol in set(snapshots) | set(premiums):
                    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{3,24}", symbol):
                        continue
                    row = copy.deepcopy(self._data["market_data"].get(symbol, {}))
                    snap = snapshots.get(symbol)
                    if snap is not None:
                        fetched = _number(getattr(snap, "fetched_at", None))
                        bid, ask = _number(getattr(snap, "bid", None)), _number(getattr(snap, "ask", None))
                        if (fetched is not None and fetched <= now + 10 and fetched >= row.get("fetched_at", 0)
                                and bid is not None and ask is not None and 0 < bid <= ask):
                            row.update(price=bid / 2 + ask / 2, bid=bid, ask=ask, fetched_at=fetched,
                                       quote_volume=_number(getattr(snap, "quote_volume", None)))
                            candles = []
                            for candle in getattr(snap, "candles", []):
                                values = {key: _number(getattr(candle, key, None)) for key in (
                                    "open_time", "close_time", "open", "high", "low", "close", "volume")}
                                if (all(value is not None for value in values.values())
                                        and values["open_time"] <= values["close_time"] <= fetched * 1000
                                        and 0 < values["low"] <= min(values["open"], values["close"])
                                        and values["high"] >= max(values["open"], values["close"]) and values["volume"] >= 0):
                                    candles.append(values)
                            row["candles"] = sorted(candles, key=lambda c: c["close_time"])[-MAX_CANDLES:]
                            changed = True
                    premium = premiums.get(symbol)
                    if isinstance(premium, dict):
                        mark, funding = _number(premium.get("markPrice")), _number(premium.get("lastFundingRate"))
                        stamp = _number(premium.get("time"))
                        stamp = stamp / 1000 if stamp is not None else now
                        if (mark is not None and mark > 0 and funding is not None
                                and row.get("mark_fetched_at", 0) <= stamp <= now + 10):
                            row.update(mark_price=mark, funding_rate=funding, mark_fetched_at=stamp)
                            changed = True
                    if row:
                        self._data["market_data"][symbol] = row
                if changed:
                    self._publish(now)
        except Exception as exc:
            self._write_error = type(exc).__name__

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)
