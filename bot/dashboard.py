"""Read-only dashboard on loopback; it never creates an exchange client."""
import base64
import copy
import hashlib
import json
import math
import re
import socket
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .report import build_report, read_state
from .storage import _invalid_constant


AGENTS = ("coordinator", "trend", "momentum", "liquidity", "market_context", "risk", "research_validation")


def _age(value, now):
    try:
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0 or value > now+5:
            return None
    except OverflowError:
        return None
    return max(0, now-value)


def _json_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Numero de actividad no finito")
    return result


def _validate_telemetry_shape(value):
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if depth > 20 or count > 50000:
            raise ValueError("Estructura de actividad fuera de limites")
        if isinstance(item, dict):
            stack.extend((child, depth+1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth+1) for child in item)


def dashboard_snapshot(state_path, mode="paper", now=None, interval="5m"):
    """Project persisted trading state and optional telemetry, without writes."""
    now = time.time() if now is None else now
    state_path = Path(state_path)
    state = read_state(state_path)
    if state.get("mode") != mode:
        raise ValueError("El modo del estado no corresponde al panel")
    report = build_report(state)
    heartbeat_age = _age(state.get("heartbeat"), now)
    cycle_fresh = heartbeat_age is not None and heartbeat_age <= 180
    telemetry = {}
    telemetry_error = None
    try:
        path = state_path.with_suffix(".activity.json")
        with path.open("rb") as stream:
            raw = stream.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("Registro de actividad demasiado grande")
        candidate = json.loads(raw, parse_constant=_invalid_constant, parse_float=_json_float)
        if not isinstance(candidate, dict) or candidate.get("schema_version") != 1:
            raise ValueError("Formato de actividad no compatible")
        _validate_telemetry_shape(candidate)
        telemetry = candidate
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, RecursionError):
        telemetry_error = "Actividad temporalmente no disponible"
    telemetry_age = _age(telemetry.get("updated_at"), now)
    telemetry_fresh = telemetry_age is not None and telemetry_age <= 90
    activity = {}
    source = telemetry.get("agent_activity", {})
    source = source if isinstance(source, dict) else {}
    for agent in AGENTS:
        item = copy.deepcopy(source.get(agent, {}))
        if not isinstance(item, dict):
            item = {}
        item.setdefault("status", "waiting")
        item.setdefault("task", "Esperando actividad registrada")
        item.setdefault("last_result", None)
        # A live HTTP server is not proof that the trading worker is running.
        if item["status"] == "running" and (not telemetry_fresh or not cycle_fresh):
            item["recorded_status"] = "running"
            item["status"] = "stale"
            item["task"] = "Sin confirmación reciente del proceso"
        if agent == "research_validation":
            if item.get("next_run_at") is None:
                item["next_run_at"] = state.get("daily_schedule", {}).get("next_review_at")
        activity[agent] = item
    events = telemetry.get("agent_events", [])
    events = [event for event in events[-200:] if isinstance(event, dict)] if isinstance(events, list) else []
    markets = telemetry.get("market_data", {})
    markets = markets if isinstance(markets, dict) else {}
    # Never backfill an absent price series using positions or fictional bars.
    clean_markets = {}
    for symbol in state.get("symbols", []):
        market = markets.get(symbol)
        if not isinstance(market, dict):
            continue
        market = copy.deepcopy(market)
        market["age_seconds"] = _age(market.get("fetched_at"), now)
        market["price_fresh"] = market["age_seconds"] is not None and market["age_seconds"] <= 90
        mark_age = _age(market.get("mark_fetched_at"), now)
        market["mark_fresh"] = mark_age is not None and mark_age <= 90
        bars = market.get("candles", [])
        close_time = bars[-1].get("close_time") if isinstance(bars, list) and bars and isinstance(bars[-1], dict) else None
        candle_age = _age(close_time/1000, now) if type(close_time) in (int, float) and abs(close_time) < 1e20 else None
        max_candle_age = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}[interval]+90
        market["candles_fresh"] = candle_age is not None and close_time/1000 <= now and candle_age <= max_candle_age
        market["fresh"] = market["price_fresh"] and market["candles_fresh"]
        clean_markets[symbol] = market
    report.update(server_time=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                  telemetry_available=bool(telemetry), telemetry_error=telemetry_error,
                  agent_activity=activity, agent_events=events, market_data=clean_markets,
                  symbols=state.get("symbols", []), interval=interval,
                  runtime=dict(heartbeat_age_seconds=heartbeat_age, cycle_fresh=cycle_fresh,
                               telemetry_age_seconds=telemetry_age, telemetry_fresh=telemetry_fresh,
                               process_id=telemetry.get("pid"), instance_id=telemetry.get("process_instance_id")))
    return report


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, state_path, mode="paper", port=8765, html_path=None, interval="5m"):
        self.state_path = Path(state_path).resolve()
        self.mode = mode
        self.interval = interval
        self.html_path = Path(html_path) if html_path else Path(__file__).with_name("dashboard.html")
        super().__init__(("127.0.0.1", port), DashboardHandler)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "FinalBossDashboard"
    sys_version = ""

    def log_message(self, *_):
        pass

    def _authorized(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "").lower() not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in {f"http://{host}" for host in hosts}:
            return False
        return self.headers.get("Sec-Fetch-Site", "") not in ("cross-site", "same-site")

    def _respond(self, status, body, content_type="application/json; charset=utf-8", csp=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp or "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status, value):
        self._respond(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def do_GET(self):
        if not self._authorized():
            return self._json(403, {"error": "Acceso local requerido"})
        route = urlsplit(self.path).path
        if route == "/":
            try:
                content = self.server.html_path.read_bytes()
                # HTML parsing normalizes CRLF and CR before CSP hashes are checked.
                normalized = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                scripts = re.findall(rb"<script(?:\s[^>]*)?>(.*?)</script>", normalized, re.DOTALL)
                hashes = " ".join("'sha256-"+base64.b64encode(hashlib.sha256(script).digest()).decode()+"'" for script in scripts)
                csp = "default-src 'none'; script-src "+(hashes or "'none'")+"; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                return self._respond(200, content, "text/html; charset=utf-8", csp)
            except OSError:
                return self._json(503, {"error": "Panel temporalmente no disponible"})
        if route == "/api/dashboard":
            try:
                if self.server.mode == "analysis":
                    from .scanner import scanner_snapshot
                    return self._json(200, scanner_snapshot(self.server.state_path))
                return self._json(200, dashboard_snapshot(self.server.state_path, self.server.mode, interval=self.server.interval))
            except Exception:
                # Paths, SQL details and arbitrary exception text never leave the server.
                return self._json(503, {"error": "El estado del bot no está disponible", "retry_seconds": 2})
        if route == "/health":
            return self._json(200, {"dashboard": "available", "mode": self.server.mode})
        if route == "/favicon.ico":
            return self._respond(204, b"", "image/x-icon")
        return self._json(404, {"error": "Ruta no disponible"})

    do_HEAD = do_GET

    def do_POST(self):
        self._json(405, {"error": "Panel de solo lectura"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST


def serve_dashboard(state_path, mode="paper", port=8765, interval="5m"):
    server = DashboardServer(state_path, mode, port, interval=interval)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
