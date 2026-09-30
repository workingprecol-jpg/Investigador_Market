"""Small USD-M Futures REST client restricted to Binance Demo.

Conditional stops use the Algo Service introduced in 2025. No write is retried:
an uncertain write must be reconciled using its client ID before doing anything
else. This module never stores credentials, logs request URLs or follows HTTP
redirects. Official contracts checked 2026-09-07:
https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import Candle, Snapshot


DEMO_HOST = "https://demo-fapi.binance.com"
_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}


class ExchangeError(RuntimeError):
    """Sanitized error. API messages and request URLs are deliberately omitted."""

    def __init__(self, message: str, code: int | None = None, status: int | None = None, retry_after: float = 0):
        self.code = code
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"{message} (code={code}, http={status})")


class UnknownExecution(ExchangeError):
    """A write may have executed; never submit it again without reconciliation."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _decimal(value, label="number", *, positive=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid {label}") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"Invalid {label}")
    return result


def _symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9_]{3,30}", symbol):
        raise ValueError("Invalid futures symbol")
    return symbol


def _client_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[.A-Za-z0-9_:/-]{1,36}", value):
        raise ValueError("Invalid client order ID")
    return value


def _side(value: str) -> str:
    if value not in {"BUY", "SELL"}:
        raise ValueError("Order side must be BUY or SELL")
    return value


class BinanceDemoClient:
    def __init__(self, api_key: str = "", api_secret: str = "", timeout: float = 15):
        self._api_key = api_key or os.environ.get("BINANCE_DEMO_API_KEY", "")
        self._api_secret = api_secret or os.environ.get("BINANCE_DEMO_API_SECRET", "")
        self.timeout = float(_decimal(timeout, "timeout", positive=True))
        self._opener = build_opener(_NoRedirect())
        self._time_offset_ms = 0.0
        self._synced_at = None
        self._exchange_cache: dict[str, dict] = {}
        self._exchange_cached_at = 0.0
        self._blocked_until = 0.0
        self._blocked_status = None
        self._blocked_code = None

    def sync_time(self) -> int:
        start_ms = time.time() * 1000
        result = self._request("GET", "/fapi/v1/time")
        end_ms = time.time() * 1000
        try:
            server_ms = int(result["serverTime"])
        except (KeyError, TypeError, ValueError):
            raise ExchangeError("Invalid server time response") from None
        self._time_offset_ms = server_ms - (start_ms + end_ms) / 2
        self._synced_at = time.monotonic()
        return server_ms

    def _request(self, method: str, path: str, params: dict | None = None, *, signed=False):
        if method not in {"GET", "POST", "DELETE"} or not re.fullmatch(r"/fapi/v[1-3]/[A-Za-z/0-9]+", path):
            raise ValueError("Only fixed Binance Demo futures REST paths are permitted")
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise ExchangeError("Binance Demo cooldown active; no request sent", self._blocked_code,
                                self._blocked_status, retry_after=remaining)
        if signed and (not self._api_key or not self._api_secret):
            raise ExchangeError("Set BINANCE_DEMO_API_KEY and BINANCE_DEMO_API_SECRET")
        if signed and (self._synced_at is None or time.monotonic() - self._synced_at > 30):
            self.sync_time()
        is_write = method != "GET"
        attempts = 1 if is_write else 3
        for attempt in range(attempts):
            data = dict(params or {})
            headers = {"Accept": "application/json", "User-Agent": "FinalBossDemo/1.0"}
            if signed:
                data["recvWindow"] = 5000
                data["timestamp"] = int(time.time() * 1000 + self._time_offset_ms)
                headers["X-MBX-APIKEY"] = self._api_key
            encoded = urlencode(data)
            if signed:
                signature = hmac.new(self._api_secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
                encoded += "&signature=" + signature
            # The destination is intentionally not configurable, including by env.
            url = DEMO_HOST + path
            body = None
            if method == "POST":
                body = encoded.encode("ascii")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif encoded:
                url += "?" + encoded
            request = Request(url, data=body, headers=headers, method=method)
            status, retry_after = 200, 0.0
            transport_failed = False
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    status = response.status
                    raw = response.read()
            except HTTPError as exc:
                status = exc.code
                try:
                    raw = exc.read()
                except (OSError, HTTPException):
                    raw = b""
                try:
                    retry_after = max(0.0, float(exc.headers.get("Retry-After", "0")))
                except (AttributeError, ValueError):
                    retry_after = 0.0
                exc.close()
            except (URLError, OSError, HTTPException, TimeoutError):
                raw = b""
                transport_failed = True
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                result = None
            code = result.get("code") if isinstance(result, dict) else None
            try:
                code = int(code) if code is not None else None
            except (ValueError, TypeError):
                code = None
            api_error = code is not None and code < 0
            valid_json = isinstance(result, (dict, list))
            if status in {429, 418} or code == -1003:
                cooldown = retry_after if retry_after > 0 and math.isfinite(retry_after) else (900 if status == 418 else 60)
                self._blocked_until = time.monotonic() + cooldown
                self._blocked_status, self._blocked_code = status, code
                raise ExchangeError("Binance Demo rate limit; cooldown activated", code, status,
                                    retry_after=cooldown) from None
            if not transport_failed and 200 <= status < 300 and not api_error and valid_json:
                return result
            unknown = transport_failed or status >= 500 or status == 408 or code in {-1001, -1006, -1007}
            if is_write and (unknown or (200 <= status < 300 and not valid_json)):
                raise UnknownExecution("Write outcome unknown; reconcile before retrying", code, status) from None
            retryable = unknown or status == 429 or code == -1021 or (200 <= status < 300 and not valid_json)
            if not is_write and retryable and attempt + 1 < attempts and retry_after <= 30:
                if signed and code == -1021:
                    self.sync_time()
                time.sleep(max(retry_after, 0.5 * 2**attempt))
                continue
            raise ExchangeError("Binance Demo request failed", code, status) from None
        raise ExchangeError("Binance Demo read attempts exhausted")

    def snapshot(self, symbol: str, interval: str = "5m", limit: int = 240) -> Snapshot:
        started_at = time.time()
        symbol = _symbol(symbol)
        if interval not in _INTERVALS or not isinstance(limit, int) or not 2 <= limit <= 1499:
            raise ValueError("Invalid candle interval or limit")
        server_ms = self.sync_time()
        rows = self._request("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit + 1})
        book = self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        ticker = self._request("GET", "/fapi/v1/ticker/24hr", {"symbol": symbol})
        try:
            candles = []
            for row in rows:
                if int(row[6]) >= server_ms:
                    continue
                candle = Candle(open_time=int(row[0]), close_time=int(row[6]),
                                open=float(_decimal(row[1], positive=True)), high=float(_decimal(row[2], positive=True)),
                                low=float(_decimal(row[3], positive=True)), close=float(_decimal(row[4], positive=True)),
                                volume=float(_decimal(row[5])))
                if (candle.close_time <= candle.open_time or candle.low > min(candle.open, candle.close)
                        or candle.high < max(candle.open, candle.close)):
                    raise ValueError("Invalid candle")
                candles.append(candle)
            bid = float(_decimal(book["bidPrice"], positive=True))
            ask = float(_decimal(book["askPrice"], positive=True))
            quote_volume = float(_decimal(ticker["quoteVolume"]))
            if ask < bid or len(candles) < 2:
                raise ValueError("Invalid quote or insufficient closed candles")
            if any(a.open_time >= b.open_time for a, b in zip(candles, candles[1:])):
                raise ValueError("Candles not strictly ordered")
        except (KeyError, IndexError, TypeError, ValueError):
            raise ExchangeError("Invalid or insufficient Binance Demo market data") from None
        # Conservative age includes network latency and any bounded read retries.
        return Snapshot(symbol, candles[-limit:], bid, ask, quote_volume, started_at)

    def exchange_info(self, symbol: str) -> dict:
        symbol = _symbol(symbol)
        if not self._exchange_cache or time.monotonic() - self._exchange_cached_at > 300:
            result = self._request("GET", "/fapi/v1/exchangeInfo")
            try:
                self._exchange_cache = {row["symbol"]: row for row in result["symbols"]}
            except (KeyError, TypeError):
                raise ExchangeError("Invalid exchange information") from None
            self._exchange_cached_at = time.monotonic()
        info = self._exchange_cache.get(symbol)
        if not info:
            raise ExchangeError("Symbol unavailable on Binance Demo")
        if info.get("status") != "TRADING" or info.get("contractType") != "PERPETUAL" or info.get("quoteAsset") != "USDT":
            raise ExchangeError("Only trading USDT perpetual futures are supported")
        return info

    def account(self) -> dict:
        return self._request("GET", "/fapi/v3/account", signed=True)

    def premium_index(self, symbol: str) -> dict:
        """Return the exchange mark price, current funding rate and next time."""
        symbol = _symbol(symbol)
        result = self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        if not isinstance(result, dict) or result.get("symbol") != symbol:
            raise ExchangeError("Invalid mark price response")
        return result

    def income(self, start_time: int) -> list[dict]:
        """All account flows since an inclusive ms timestamp, with bounded paging.

        Callers should persist and deduplicate by (incomeType, tranId), including
        overlap on later calls. The exchange only retains three months of flows.
        """
        if not isinstance(start_time, int) or start_time < 0:
            raise ValueError("Income start time must be a nonnegative millisecond timestamp")
        end_time = self.sync_time()
        if start_time > end_time or end_time - start_time > 89 * 86_400_000:
            raise ValueError("Income range must be within the last 89 days")
        results, seen = [], set()
        for page in range(1, 21):
            rows = self._request("GET", "/fapi/v1/income", {
                "startTime": start_time, "endTime": end_time, "page": page, "limit": 1000,
            }, signed=True)
            if not isinstance(rows, list):
                raise ExchangeError("Invalid income history response")
            fresh = 0
            for row in rows:
                try:
                    key = (row["incomeType"], str(row["tranId"]))
                except (KeyError, TypeError):
                    raise ExchangeError("Invalid income history entry") from None
                if key not in seen:
                    seen.add(key)
                    results.append(row)
                    fresh += 1
            if len(rows) < 1000:
                return results
            if not fresh:
                raise ExchangeError("Income pagination did not advance")
        raise ExchangeError("Income pagination cap reached; narrow reconciliation interval")

    def positions(self, symbol: str | None = None) -> list[dict]:
        return self._request("GET", "/fapi/v3/positionRisk", {"symbol": _symbol(symbol)} if symbol else {}, signed=True)

    def open_orders(self, symbol: str) -> list[dict]:
        return self._request("GET", "/fapi/v1/openOrders", {"symbol": _symbol(symbol)}, signed=True)

    def configure_symbol(self, symbol: str, leverage: int = 5):
        symbol = _symbol(symbol)
        if leverage != 5:
            raise ValueError("This demo strategy requires exactly 5x leverage")
        self.exchange_info(symbol)
        mode = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        if mode.get("dualSidePosition") is not False:
            raise ExchangeError("One-way position mode required; hedge mode is not changed automatically")
        try:
            self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except ExchangeError as exc:
            if isinstance(exc, UnknownExecution) or exc.code != -4046:
                raise
        result = self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": 5}, signed=True)
        if result.get("leverage") != 5:
            raise ExchangeError("Exchange did not confirm 5x leverage")
        return result

    def normalize_quantity(self, symbol: str, quantity: float, price: float) -> float:
        """Round down without increasing risk; enforce BOTH lot-size filters."""
        quantity_d = _decimal(quantity, "quantity", positive=True)
        price_d = _decimal(price, "price", positive=True)
        filters = self.exchange_info(symbol)["filters"]
        lots = [f for f in filters if f["filterType"] in {"LOT_SIZE", "MARKET_LOT_SIZE"}]
        if not lots:
            raise ExchangeError("Missing quantity filters")
        steps = [_decimal(f["stepSize"], "step size") for f in lots]
        steps = [step for step in steps if step > 0]
        if not steps:
            raise ExchangeError("Missing positive quantity step")
        scale = Decimal(10) ** min(step.as_tuple().exponent for step in steps)
        step = Decimal(math.lcm(*(int(s / scale) for s in steps))) * scale
        maximums = [_decimal(f["maxQty"]) for f in lots if _decimal(f["maxQty"]) > 0]
        if maximums:
            quantity_d = min(quantity_d, *maximums)
        quantity_d = (quantity_d / step).to_integral_value(rounding=ROUND_FLOOR) * step
        minimum = max(_decimal(f["minQty"]) for f in lots)
        if quantity_d <= 0 or quantity_d < minimum:
            raise ValueError("Quantity below minimum; increase in risk is not permitted")
        for item in filters:
            kind = item["filterType"]
            if kind == "MIN_NOTIONAL":
                minimum_notional = _decimal(item.get("notional", item.get("minNotional", 0)))
                if quantity_d * price_d < minimum_notional:
                    raise ValueError("Quantity below minimum notional")
            elif kind == "NOTIONAL":
                if item.get("applyMinToMarket", True) and quantity_d * price_d < _decimal(item["minNotional"]):
                    raise ValueError("Quantity below minimum notional")
                if item.get("applyMaxToMarket", True) and quantity_d * price_d > _decimal(item["maxNotional"]):
                    raise ValueError("Quantity above maximum notional")
        return float(quantity_d)

    def place_market(self, symbol: str, side: str, quantity: float, client_order_id: str, reduce_only: bool = False) -> dict:
        if not isinstance(reduce_only, bool):
            raise ValueError("reduce_only must be boolean")
        result = self._request("POST", "/fapi/v1/order", {
            "symbol": _symbol(symbol), "side": _side(side), "positionSide": "BOTH", "type": "MARKET",
            "quantity": format(_decimal(quantity, "quantity", positive=True), "f"),
            "newClientOrderId": _client_id(client_order_id), "newOrderRespType": "RESULT",
            "reduceOnly": str(reduce_only).lower(),
        }, signed=True)
        if not isinstance(result, dict) or not result.get("orderId"):
            raise UnknownExecution("Incomplete order acknowledgement; reconcile by client ID")
        return result

    def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        return self._request("DELETE", "/fapi/v1/order", {
            "symbol": _symbol(symbol), "origClientOrderId": _client_id(client_order_id),
        }, signed=True)

    def get_order(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            return self._request("GET", "/fapi/v1/order", {
                "symbol": _symbol(symbol), "origClientOrderId": _client_id(client_order_id),
            }, signed=True)
        except ExchangeError as exc:
            if exc.code == -2013:
                return None
            raise

    def trades(self, symbol: str, order_id) -> list[dict]:
        order_id = int(order_id)
        if order_id <= 0:
            raise ValueError("Invalid order ID")
        # A full page is ambiguous: do not silently return incomplete commissions.
        result = self._request("GET", "/fapi/v1/userTrades", {
            "symbol": _symbol(symbol), "orderId": order_id, "limit": 1000,
        }, signed=True)
        if len(result) >= 1000:
            raise ExchangeError("Trade page full; manual reconciliation required")
        return result

    def protection_stop(self, symbol: str, side: str, stop_price: float, client_order_id: str) -> dict:
        symbol, side = _symbol(symbol), _side(side)
        price = _decimal(stop_price, "stop price", positive=True)
        price_filter = next((f for f in self.exchange_info(symbol)["filters"] if f["filterType"] == "PRICE_FILTER"), None)
        if price_filter is None:
            raise ExchangeError("Missing price filter for protective stop")
        tick = _decimal(price_filter["tickSize"], "tick size", positive=True)
        # Move at most one tick toward the market, never widen planned loss.
        rounding = ROUND_CEILING if side == "SELL" else ROUND_FLOOR
        price = (price / tick).to_integral_value(rounding=rounding) * tick
        minimum = _decimal(price_filter.get("minPrice", 0))
        maximum = _decimal(price_filter.get("maxPrice", 0))
        if price <= 0 or price < minimum or (maximum > 0 and price > maximum):
            raise ValueError("Stop price violates exchange price filter")
        result = self._request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "positionSide": "BOTH",
            "type": "STOP_MARKET", "triggerPrice": format(price, "f"),
            "workingType": "MARK_PRICE", "closePosition": "true", "priceProtect": "false",
            "clientAlgoId": _client_id(client_order_id),
        }, signed=True)
        if not isinstance(result, dict) or not result.get("algoId"):
            raise UnknownExecution("Incomplete stop acknowledgement; reconcile by client algo ID")
        return result

    def cancel_stop(self, symbol: str, algo_id) -> dict:
        _symbol(symbol)
        return self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": int(algo_id)}, signed=True)

    def get_stop(self, symbol: str, algo_id) -> dict:
        symbol = _symbol(symbol)
        result = self._request("GET", "/fapi/v1/algoOrder", {"algoId": int(algo_id)}, signed=True)
        if result.get("symbol") != symbol:
            raise ExchangeError("Algo response symbol does not match requested symbol")
        return result

    def open_stops(self, symbol: str) -> list[dict]:
        return self._request("GET", "/fapi/v1/openAlgoOrders", {
            "symbol": _symbol(symbol), "algoType": "CONDITIONAL",
        }, signed=True)

    def get_stop_by_client_id(self, symbol: str, client_order_id: str) -> dict | None:
        symbol = _symbol(symbol)
        try:
            result = self._request("GET", "/fapi/v1/algoOrder", {
                "clientAlgoId": _client_id(client_order_id),
            }, signed=True)
        except ExchangeError as exc:
            if exc.code == -2013:
                return None
            raise
        if not isinstance(result, dict) or result.get("symbol") != symbol:
            raise ExchangeError("Algo response symbol does not match requested symbol")
        return result
