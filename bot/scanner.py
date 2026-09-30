"""Public market research. GET-only clients; no account, order or .env access."""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import signal
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from .models import Candle, Snapshot
from .technical import _closed_candles, ema, rsi, atr
from .storage import ProcessLock, _invalid_constant

SPOT = "https://data-api.binance.vision"
FUTURES = "https://fapi.binance.com"
DEX = "https://api.dexscreener.com"
GOPLUS = "https://api.gopluslabs.io"
MEMPOOL = "https://mempool.space"
COINGECKO = "https://api.coingecko.com"
COINGLASS = "https://open-api-v4.coinglass.com"
WHALE = "https://leviathan.whale-alert.io"
POLL_SECONDS = 300
MAX_AGE = 900
STABLES = {"USDC", "USDP", "TUSD", "FDUSD", "DAI", "USDD", "USDE", "USD1", "BUSD", "EUR", "EURI", "AEUR"}
MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
          "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT", "SUIUSDT", "TONUSDT", "PEPEUSDT")
ASSET_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "BNB": "BNB", "XRP": "XRP",
               "ADA": "Cardano", "DOGE": "Dogecoin", "AVAX": "Avalanche", "LINK": "Chainlink",
               "DOT": "Polkadot", "LTC": "Litecoin", "TRX": "TRON", "SUI": "Sui", "TON": "Toncoin", "PEPE": "Pepe"}
ENTRY_DECISIONS = {"ENTRADA_CONDICIONAL", "SPOT_CONDICIONAL", "LONG_CONDICIONAL", "SHORT_CONDICIONAL"}


def major_symbol(symbol):
    return "PEPEUSDT" if symbol == "1000PEPEUSDT" else symbol


class DataError(ValueError):
    pass


class RateLimit(DataError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise DataError("Redirección de fuente rechazada")


def number(value, *, positive=False):
    if isinstance(value, bool) or value is None:
        raise DataError("Dato numérico ausente")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise DataError("Dato numérico inválido")
    return result


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class PublicData:
    def get(self, base, path, _headers=None, **params):
        spot_ok = base == SPOT and path in ("/api/v3/exchangeInfo", "/api/v3/ticker/24hr", "/api/v3/ticker/bookTicker", "/api/v3/klines")
        futures_ok = base == FUTURES and path in ("/fapi/v1/exchangeInfo", "/fapi/v1/ticker/24hr", "/fapi/v1/ticker/bookTicker", "/fapi/v1/klines", "/fapi/v1/premiumIndex", "/fapi/v1/fundingRate", "/futures/data/openInterestHist", "/futures/data/takerlongshortRatio")
        dex_ok = base == DEX and (path in ("/token-profiles/latest/v1", "/token-profiles/recent-updates/v1")
                                 or re.fullmatch(r"/token-pairs/v1/[a-z0-9-]{1,40}/[A-Za-z0-9]{1,128}", path))
        security_ok = base == GOPLUS and (path == "/api/v1/solana/token_security" or re.fullmatch(r"/api/v1/token_security/[0-9]{1,8}", path))
        research_ok = (base == MEMPOOL and path == "/api/mempool/recent" or
                       base == COINGECKO and path == "/api/v3/coins/markets" or
                       base == COINGLASS and path == "/api/coin/unlock-list" or
                       base == WHALE and re.fullmatch(r"/(bitcoin|ethereum|solana)/(status|transactions)", path))
        if not (spot_ok or futures_ok or dex_ok or security_ok or research_ok):
            raise DataError("Solo endpoints públicos de lectura permitidos")
        if _headers and not (base == COINGECKO and set(_headers)=={"x-cg-demo-api-key"} or base==COINGLASS and set(_headers)=={"CG-API-KEY"}):
            raise DataError("Cabeceras de autenticación de trading no permitidas")
        url = base + path + ("?" + urlencode(params) if params else "")
        request = Request(url, headers={"User-Agent": "FinalBossResearch/3.0", "Accept": "application/json", **(_headers or {})}, method="GET")
        try:
            with build_opener(NoRedirect()).open(request, timeout=12) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise DataError("Respuesta demasiado grande")
            return json.loads(raw, parse_constant=_invalid_constant)
        except HTTPError as exc:
            if exc.code in (418, 429):
                raise RateLimit("Fuente limitó las consultas; esperar próximo ciclo") from None
            raise DataError(f"Fuente no disponible (HTTP {exc.code})") from None
        except (OSError, ValueError) as exc:
            if isinstance(exc, DataError):
                raise
            raise DataError("Fuente no disponible o respuesta inválida") from None


def select_universe(metadata, tickers, books, now, market="spot"):
    active = {s["symbol"]: s for s in metadata["symbols"]
              if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
              and (s.get("isSpotTradingAllowed") is True if market == "spot" else
                   s.get("contractType") == "PERPETUAL" and s.get("marginAsset") == "USDT")
              and s.get("baseAsset") not in STABLES}
    quotes = {q["symbol"]: q for q in books}
    result = []
    for ticker in tickers:
        symbol = ticker.get("symbol")
        if symbol not in active or symbol not in quotes:
            continue
        try:
            q = quotes[symbol]
            price = number(ticker["lastPrice"], positive=True)
            bid, ask = number(q["bidPrice"], positive=True), number(q["askPrice"], positive=True)
            if ask < bid or not 0 <= now - number(ticker["closeTime"])/1000 <= 180:
                continue
            volume = number(ticker["quoteVolume"])
            change = number(ticker["priceChangePercent"])
            if volume < 0:
                continue
            result.append(dict(symbol=symbol, name=active[symbol]["baseAsset"], price=price,
                               change_24h=change, volume_24h=volume,
                               spread_bps=(ask-bid)/((ask+bid)/2)*10000,
                               observed_at=now, bid=bid, ask=ask, market=market))
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return result


def bars_from(raw, symbol, interval, quote, now):
    bars = [Candle(int(b[0]), int(b[6]), *(number(b[i], positive=True) for i in (1, 2, 3, 4)), number(b[5]))
            for b in raw if number(b[6]) <= now*1000]
    snapshot = Snapshot(symbol, bars, quote["bid"], quote["ask"], quote["volume_24h"], now)
    _closed_candles(snapshot, 60)
    seconds = {"15m": 900, "1h": 3600, "4h":14400, "1d":86400}[interval]
    if any(b.close_time-b.open_time+1 != seconds*1000 for b in bars):
        raise DataError("Duración de vela inválida")
    if not 0 <= now-bars[-1].close_time/1000 <= seconds+90:
        raise DataError("Velas antiguas")
    return bars


def analyze_spot(quote, bars, hourly, news_ok):
    return analyze_market(quote, bars, hourly, news_ok, market="spot")


def analyze_market(quote, bars, hourly, news_ok, market="spot"):
    """Independent spot/derivative hypotheses with an auditable checklist."""
    closes = [b.close for b in bars]
    hours = [b.close for b in hourly]
    fast, slow = ema(closes, 20), ema(closes, 50)
    hour_fast, hour_slow = ema(hours, 20), ema(hours, 50)
    momentum, volatility = rsi(closes), atr(bars)
    if volatility <= 0:
        raise DataError("ATR nulo")
    resistance = max(b.high for b in bars[-21:-1])
    support = min(b.low for b in bars[-21:-1])
    volume_base = mean(b.volume for b in bars[-21:-1])
    relative_volume = bars[-1].volume/volume_base if volume_base > 0 else 0
    liquid = quote["volume_24h"] >= 2_000_000 and quote["spread_bps"] <= 20
    scenarios = []
    for side in (["spot"] if market == "spot" else ["long", "short"]):
        sign = -1 if side == "short" else 1
        rising = sign == 1
        entry = quote["ask"] if rising else quote["bid"]
        trigger = resistance if rising else support
        trend = fast > slow and closes[-1] > fast if rising else fast < slow and closes[-1] < fast
        hour_trend = hour_fast > hour_slow and hours[-1] > hour_fast if rising else hour_fast < hour_slow and hours[-1] < hour_fast
        breakout = closes[-1] > resistance if rising else closes[-1] < support
        held = quote["bid"] >= resistance if rising else quote["ask"] <= support
        price_near = abs(entry-closes[-1]) <= volatility*.5
        extension = sign*(entry-fast)/volatility
        rsi_ok = 50 <= momentum <= 70 if rising else 30 <= momentum <= 50
        distance = max(2*volatility, entry*.005)
        checks = []
        def check(key, label, passed, observed, required):
            checks.append(dict(key=key, label=label, passed=bool(passed), observed=observed, required=required))
        direction = "alcista" if rising else "bajista"
        check("trend_15m", "Tendencia 15m", trend, f"EMA20 {fast:.8g}; EMA50 {slow:.8g}", "Alineación "+direction+" y cierre del lado de EMA20")
        check("trend_1h", "Confirmación 1h", hour_trend, f"EMA20 {hour_fast:.8g}; EMA50 {hour_slow:.8g}", "Alineación "+direction+" en una hora")
        check("breakout", "Ruptura con vela cerrada", breakout, f"Cierre {closes[-1]:.8g}; nivel {trigger:.8g}", "Superar resistencia de 20 velas" if rising else "Perder soporte de 20 velas")
        check("volume", "Volumen relativo 15m", relative_volume >= 1.5, f"{relative_volume:.2f}×", "Al menos 1,5× la media de las 20 velas previas")
        check("current_quote", "Precio actual confirma", held and price_near, f"Bid {quote['bid']:.8g}; ask {quote['ask']:.8g}", "Mantener ruptura y estar a <=0,5 ATR del cierre")
        check("rsi", "RSI14", rsi_ok, f"{momentum:.2f}", "Entre 50 y 70" if rising else "Entre 30 y 50")
        check("extension", "Movimiento sin perseguir", extension <= 2.5 and abs(quote["change_24h"]) <= 25, f"{extension:.2f} ATR; 24h {quote['change_24h']:+.2f}%", "Extensión <=2,5 ATR y cambio absoluto 24h <=25%")
        check("liquidity", "Liquidez y spread", liquid, f"Volumen {quote['volume_24h']:,.0f} USDT; spread {quote['spread_bps']:.2f} bps", "Volumen >=2 millones USDT y spread <=20 bps")
        check("context", "Disponibilidad de contexto", news_ok, "RSS disponible" if news_ok else "RSS no disponible", "Consulta reciente exitosa; no certifica ausencia de incidentes")
        check("risk_distance", "Distancia de invalidación", distance < entry*.08 and entry-sign*distance > 0 and entry+sign*2*distance > 0, f"{distance/entry*100:.2f}%", "Menor del 8%; niveles de precio positivos")
        if market == "futures":
            funding = quote.get("funding_rate")
            funding_fresh = quote.get("funding_fresh") is True
            funding_ok = funding_fresh and funding is not None and sign*funding <= .001
            check("funding", "Funding del contrato", funding_ok, "No disponible" if funding is None else f"{funding*100:+.4f}%", "Dato reciente y funding adverso <=0,1% por evento")
        signal = {"spot": "SPOT_CONDICIONAL", "long": "LONG_CONDICIONAL", "short": "SHORT_CONDICIONAL"}[side]
        decision = signal if all(c["passed"] for c in checks) else "ESPERAR"
        if not liquid:
            decision = "DESCARTAR"
        levels = dict(reference_entry=entry, invalidation=entry-sign*distance,
                      reference_target=entry+sign*2*distance, resistance=resistance, support=support,
                      trigger=trigger, side=side, risk_distance_pct=distance/entry*100,
                      note="Referencias técnicas 2 ATR / 2R bruto; sin costes, apalancamiento ni órdenes")
        scenarios.append(dict(side=side, decision=decision, checks=checks, levels=levels,
                              passed=sum(c["passed"] for c in checks), total=len(checks)))
    # Direction is a tentative bias until every checklist condition passes.
    chosen = max(scenarios, key=lambda s: (s["decision"] in ENTRY_DECISIONS, s["passed"]))
    reasons = [c["label"]+": "+c["observed"] for c in chosen["checks"] if c["passed"]]
    risks = [c["label"]+": falta "+c["required"]+" ("+c["observed"]+")" for c in chosen["checks"] if not c["passed"]]
    if market == "futures":
        risks.append("Derivado: riesgo de liquidación y financiación; no se define apalancamiento ni tamaño")
    source = "Binance Spot" if market == "spot" else "Binance Futures"
    return {**quote, "source": source, "id": market+":"+quote["symbol"], "decision": chosen["decision"],
            "asset_name": ASSET_NAMES.get(major_symbol(quote["symbol"])[:-4], quote["name"]),
            "market": market, "bias": chosen["side"], "scenarios": scenarios, "checks": chosen["checks"],
            "score": round(chosen["passed"]/chosen["total"]*100, 1), "score_kind": "Condiciones cumplidas, no probabilidad",
            "reasons": reasons, "risks": risks, "rsi": momentum, "relative_volume": relative_volume,
            "atr_pct": volatility/quote["price"]*100, "levels": chosen["levels"], "analysis_ok": True,
            "candle_at": bars[-1].close_time/1000, "hour_candle_at": hourly[-1].close_time/1000,
            "url": ("https://www.binance.com/en/trade/"+quote["name"]+"_USDT?type=spot" if market == "spot" else
                    "https://www.binance.com/en/futures/"+quote["symbol"])}


def unavailable_major(symbol, market, now, reason):
    source = "Binance Spot" if market == "spot" else "Binance Futures"
    return dict(id=market+":"+symbol, symbol=symbol, name=symbol[:-4],
                asset_name=ASSET_NAMES.get(major_symbol(symbol)[:-4], symbol[:-4]), market=market,
                source=source, decision="ESPERAR", score=0, major=True, analysis_ok=False,
                observed_at=now, price=None, change_24h=None, volume_24h=None,
                reasons=[], risks=[reason], checks=[], scenarios=[], levels=None, news_ids=[], url=None)


def pick_candidates(universe, rotation=0, market="spot"):
    liquid = [q for q in universe if q["volume_24h"] >= 2_000_000 and q["spread_bps"] <= 20]
    winners = sorted(liquid, key=lambda q: q["change_24h"], reverse=True)
    leaders = winners[:15] if market == "spot" else winners[:8]+list(reversed(winners))[:7]
    rotating = sorted(liquid, key=lambda q: q["symbol"])
    extra = [rotating[(rotation+i) % len(rotating)] for i in range(min(15, len(rotating)))] if rotating else []
    fixed = [q for q in universe if major_symbol(q["symbol"]) in MAJORS]
    selected = {q["symbol"]:q for q in fixed+leaders+extra}
    return selected, (rotation+15) % max(1, len(rotating)), len(liquid)


def analyze_dex(pair, chain, address, now):
    base = pair.get("baseToken", {})
    if pair.get("chainId") != chain or base.get("address") != address:
        raise DataError("El par no corresponde al contrato solicitado")
    price = number(pair.get("priceUsd"), positive=True)
    liquidity = number((pair.get("liquidity") or {}).get("usd"))
    volume = number((pair.get("volume") or {}).get("h24"))
    change = number((pair.get("priceChange") or {}).get("h24"))
    hour = number((pair.get("priceChange") or {}).get("h1"))
    created = number(pair.get("pairCreatedAt"))/1000
    if liquidity < 0 or volume < 0 or not 0 < created <= now:
        raise DataError("Datos DEX inválidos")
    age_hours = (now-created)/3600
    risks = ["Contrato, concentración de holders y posibilidad de vender no auditados",
             "Listado reciente de perfiles: muestra parcial, no todas las monedas DEX"]
    if liquidity < 100_000: risks.append("Liquidez menor de 100.000 USD")
    if age_hours < 24: risks.append("Par con menos de 24 horas; riesgo elevado")
    if abs(change) > 50 or hour > 20: risks.append("Volatilidad extrema o subida ya extendida")
    score = min(100, max(0, hour)*1.5 + min(30, volume/100_000*10) + min(20, liquidity/100_000*10))
    pair_address = pair.get("pairAddress", "")
    if not re.fullmatch(r"[A-Za-z0-9]{1,128}", pair_address):
        raise DataError("Dirección del par inválida")
    return dict(id=f"dex:{chain}:{address}", symbol=str(base.get("symbol", "?"))[:24],
                name=str(base.get("name", "Token"))[:80], chain=chain, address=address,
                price=price, change_24h=change, change_1h=hour, volume_24h=volume,
                liquidity_usd=liquidity, age_hours=age_hours, observed_at=now,
                source="DEX Screener", decision="INVESTIGAR" if liquidity >= 100_000 and age_hours >= 24 else "DESCARTAR",
                score=round(score, 1), score_kind="Actividad observada, no probabilidad",
                reasons=[f"Movimiento de una hora: {hour:+.2f}%", f"Liquidez observada: {liquidity:,.0f} USD"],
                risks=risks, levels=None, url=f"https://dexscreener.com/{chain}/{pair_address}")


def write_snapshot(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",",":")), encoding="utf-8")
    os.replace(temporary, path)


def scanner_snapshot(path, now=None):
    now = time.time() if now is None else now
    path = Path(path)
    if not path.exists():
        return dict(mode="analysis", execution_enabled=False, status="starting", candidates=[], news=[], sources={}, coverage={})
    with path.open("rb") as stream:
        raw = stream.read(8_000_001)
    if len(raw) > 8_000_000:
        raise DataError("Informe fuera de límites")
    data = json.loads(raw, parse_constant=_invalid_constant)
    if data.get("mode") != "analysis" or data.get("execution_enabled") is not False:
        raise DataError("Informe incompatible con modo de análisis")
    age = now-number(data["completed_at"])
    fresh = 0 <= age <= MAX_AGE and data.get("status") == "ok"
    data["fresh"] = fresh
    data["age_seconds"] = max(0, age)
    for row in data.get("candidates", []):
        row_fresh = fresh and row.get("analysis_ok", True) and 0 <= now-number(row["observed_at"]) <= MAX_AGE
        row["fresh"] = row_fresh
        if not row_fresh and row["decision"] in ENTRY_DECISIONS:
            row["recorded_decision"] = row["decision"]
            row["decision"] = "ESPERAR"
            row["risks"].append("Datos antiguos o fuente fallida; señal invalidada")
        if not row_fresh:
            for scenario in row.get("scenarios", []):
                if scenario["decision"] in ENTRY_DECISIONS:
                    scenario["decision"] = "ESPERAR"
    return data


class Scanner:
    def __init__(self, client=None, previous=None):
        self.client = client or PublicData()
        self.rotation = 0
        self.dex_rotation = 0
        self.metadata = None
        self.metadata_at = 0
        self.futures_metadata = None
        self.futures_metadata_at = 0
        self.futures_rotation = 0
        self.futures_rate_limited = False
        self.news_cache = None
        self.news_at = 0
        self.rate_limited = False
        self.previous = {r["id"]:r for r in (previous or [])}
        self.higher_cache = {}

    def headlines(self, now):
        from .context import NEWS_URL, _request_bytes, parse_rss
        if self.news_cache is None or now-self.news_at >= 900:
            try:
                articles = [a.public() for a in parse_rss(_request_bytes(NEWS_URL), now)
                            if 0 <= now-a.published_at <= 6*3600]
                self.news_cache = dict(status="ok", fetched_at=now, url=NEWS_URL, articles=articles)
            except (OSError, ValueError):
                self.news_cache = dict(status="error", fetched_at=now, url=NEWS_URL, articles=[], error="RSS no disponible")
            self.news_at = now
        result = copy.deepcopy(self.news_cache)
        result["articles"] = [a for a in result["articles"]
                              if 0 <= now-datetime.fromisoformat(a["published_at"]).timestamp() <= 6*3600]
        return result

    def details(self, quote, news_ok, market="spot"):
        if (self.rate_limited if market == "spot" else self.futures_rate_limited):
            raise RateLimit("Consultas pausadas por límite de la fuente")
        base, prefix = (SPOT, "/api/v3") if market == "spot" else (FUTURES, "/fapi/v1")
        try:
            raw = self.client.get(base, prefix+"/klines", symbol=quote["symbol"], interval="15m", limit=100)
            hour = self.client.get(base, prefix+"/klines", symbol=quote["symbol"], interval="1h", limit=100)
            now = time.time()
            bars = bars_from(raw, quote["symbol"], "15m", quote, now)
            hourly = bars_from(hour, quote["symbol"], "1h", quote, now)
            row = analyze_market(quote, bars, hourly, news_ok, market)
            from .market_research import technical_series, trend_summary, derivatives
            row["chart"] = {"15m":technical_series(bars), "1h":technical_series(hourly)}
            row["higher_trends"] = {"1h":trend_summary(hourly)}
            row["research_errors"] = []
            baseline = mean(b.volume for b in bars[-24:-4])
            row["volume_acceleration"] = mean(b.volume for b in bars[-4:])/baseline if baseline > 0 else None
            row["change_1h"] = (bars[-1].close/bars[-5].close-1)*100
            for interval, seconds in (("4h",14400),("1d",86400)):
                key = (market,quote["symbol"],interval)
                epoch = int(now//seconds)
                try:
                    cached = self.higher_cache.get(key)
                    if cached is None or cached[0] != epoch:
                        raw = self.client.get(base,prefix+"/klines",symbol=quote["symbol"],interval=interval,limit=100)
                        higher = bars_from(raw,quote["symbol"],interval,quote,time.time())
                        self.higher_cache[key] = (epoch,higher)
                    else:
                        higher = cached[1]
                    row["chart"][interval] = technical_series(higher)
                    row["higher_trends"][interval] = trend_summary(higher)
                except (OSError,ValueError,KeyError,TypeError,IndexError):
                    row["research_errors"].append(interval+": contexto no disponible")
            if market == "futures" and not self.futures_rate_limited:
                row["derivatives"] = derivatives(self.client,quote["symbol"],quote["price"],time.time())
            return row
        except RateLimit:
            if market == "spot":
                self.rate_limited = True
            else:
                self.futures_rate_limited = True
            raise

    def market_cycle(self, market, news_ok, started):
        source = "Binance Spot" if market == "spot" else "Binance Futures"
        rows, errors = [], []
        coverage = {market+"_universe":0, market+"_liquid":0, market+"_analyzed":0, market+"_attempted":0}
        base, prefix = (SPOT, "/api/v3") if market == "spot" else (FUTURES, "/fapi/v1")
        try:
            attr = "metadata" if market == "spot" else "futures_metadata"
            metadata = getattr(self, attr)
            if metadata is None or started-getattr(self, attr+"_at") > 3600:
                params = dict(permissions="SPOT", showPermissionSets="false", symbolStatus="TRADING") if market == "spot" else {}
                metadata = self.client.get(base, prefix+"/exchangeInfo", **params)
                setattr(self, attr, metadata)
                setattr(self, attr+"_at", started)
            tickers = self.client.get(base, prefix+"/ticker/24hr")
            books = self.client.get(base, prefix+"/ticker/bookTicker")
            universe = select_universe(metadata, tickers, books, time.time(), market)
            if not universe:
                raise DataError("Sin cotizaciones recientes")
            rotation_attr = "rotation" if market == "spot" else "futures_rotation"
            selected, rotation, liquid_count = pick_candidates(universe, getattr(self, rotation_attr), market)
            setattr(self, rotation_attr, rotation)
            coverage.update({market+"_universe": len(universe), market+"_liquid":liquid_count,
                             market+"_attempted":len(selected)})
            if market == "futures":
                funding = {}
                try:
                    funding = {p["symbol"]:p for p in self.client.get(FUTURES, "/fapi/v1/premiumIndex")}
                except (OSError, ValueError, KeyError, TypeError):
                    errors.append("Funding no disponible: entradas de futuros bloqueadas")
                for q in selected.values():
                    try:
                        p = funding[q["symbol"]]
                        q["funding_rate"] = number(p["lastFundingRate"])
                        q["funding_fresh"] = 0 <= time.time()-number(p["time"])/1000 <= 180
                        if not q["funding_fresh"]:
                            errors.append(q["symbol"]+": funding antiguo; entrada bloqueada")
                    except (ValueError, KeyError, TypeError):
                        q.update(funding_rate=None, funding_fresh=False)
                        errors.append(q["symbol"]+": funding no disponible; entrada bloqueada")
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = {pool.submit(self.details, q, news_ok, market):symbol for symbol,q in selected.items()}
                for job in as_completed(jobs):
                    try:
                        row = job.result()
                        row["major"] = major_symbol(row["symbol"]) in MAJORS
                        rows.append(row)
                        coverage[market+"_analyzed"] += 1
                    except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError):
                        errors.append(source+" · "+jobs[job]+": análisis incompleto; esperar")
            source_state = dict(status="partial" if errors else "ok", fetched_at=started, url=base)
        except (OSError, ValueError, KeyError, TypeError):
            source_state = dict(status="error", fetched_at=started, url=base, error="No se pudo actualizar este mercado")
        # Fixed watchlist must remain visible when a symbol or provider is unavailable.
        present = {major_symbol(r["symbol"]) for r in rows}
        for symbol in MAJORS:
            if symbol not in present:
                rows.append(unavailable_major(symbol, market, started,
                    "Datos incompletos o mercado no disponible; no hay señal válida"))
        return rows, coverage, source_state, errors

    def cycle(self):
        started = time.time()
        sources, candidates, errors = {}, [], []
        self.rate_limited = False
        coverage = dict(spot_universe=0, spot_liquid=0, spot_analyzed=0, spot_attempted=0,
                        dex_profiles=0, dex_attempted=0, dex_analyzed=0)
        news = self.headlines(started)
        sources["CoinDesk RSS"] = {k:v for k,v in news.items() if k != "articles"}
        self.futures_rate_limited = False
        for market in ("spot", "futures"):
            rows, market_coverage, source_state, market_errors = self.market_cycle(market, news["status"] == "ok", started)
            candidates.extend(rows)
            coverage.update(market_coverage)
            sources["Binance Spot" if market == "spot" else "Binance Futures"] = source_state
            errors.extend(market_errors)
        try:
            profiles = self.client.get(DEX, "/token-profiles/latest/v1")
            profiles += self.client.get(DEX, "/token-profiles/recent-updates/v1")
            unique = {}
            for p in profiles:
                chain, address = p.get("chainId", ""), p.get("tokenAddress", "")
                if re.fullmatch(r"[a-z0-9-]{1,40}", chain) and re.fullmatch(r"[A-Za-z0-9]{1,128}", address):
                    unique[(chain, address)] = p
            keys = list(unique)
            coverage["dex_profiles"] = len(keys)
            selected_keys = [keys[(self.dex_rotation+i) % len(keys)] for i in range(min(10, len(keys)))] if keys else []
            self.dex_rotation = (self.dex_rotation+10) % max(1, len(keys))
            coverage["dex_attempted"] = len(selected_keys)
            dex_errors = 0
            for chain, address in selected_keys:
                try:
                    pairs = self.client.get(DEX, f"/token-pairs/v1/{chain}/{address}")
                    matching = [p for p in pairs if p.get("baseToken", {}).get("address") == address and p.get("chainId") == chain]
                    if not matching:
                        raise DataError("Sin par verificable")
                    pair = max(matching, key=lambda p: number((p.get("liquidity") or {}).get("usd", 0)))
                    row = analyze_dex(pair, chain, address, time.time())
                    from .market_research import token_security, optional_number
                    row.update(market_cap=optional_number(pair.get("marketCap")),fdv=optional_number(pair.get("fdv")),valuation_source="DEX Screener")
                    row["security"] = token_security(self.client,chain,address,time.time())
                    if any(c["risk"] is True and c["key"] in ("is_honeypot","cannot_sell_all") for c in row["security"].get("checks",[])):
                        row["decision"] = "DESCARTAR"
                        row["risks"].append("Proveedor detecta restricción de venta o posible honeypot")
                    candidates.append(row)
                    coverage["dex_analyzed"] += 1
                except RateLimit:
                    dex_errors += 1
                    break
                except (OSError, ValueError, KeyError, TypeError):
                    dex_errors += 1
            sources["DEX Screener"] = dict(status="partial" if dex_errors else "ok", fetched_at=started, url="https://docs.dexscreener.com/api/reference")
        except (OSError, ValueError, KeyError, TypeError):
            sources["DEX Screener"] = dict(status="error", fetched_at=started, url=DEX, error="No se pudieron consultar perfiles DEX")
        # General news remains general; matching words is never independent corroboration.
        aliases = {"BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "DOGE": ["dogecoin"], "PEPE": ["pepe"]}
        for candidate in candidates:
            asset = major_symbol(candidate["symbol"])[:-4] if candidate["source"] in ("Binance Spot", "Binance Futures") else candidate["symbol"]
            terms = aliases.get(asset.upper(), [asset.lower()] if len(asset) >= 4 else [])
            candidate["news_ids"] = [a["id"] for a in news["articles"] if any(re.search(r"\b"+re.escape(term)+r"\b", a["title"].lower()) for term in terms)]
            if not candidate["news_ids"]:
                candidate["risks"].append("Sin titular reciente vinculado; análisis principalmente técnico")
        from .market_research import discovery, whale_activity, market_valuations, unlock_calendar
        valuation, valuation_source = market_valuations(self.client,time.time())
        for row in candidates:
            asset = major_symbol(row["symbol"])[:-4]
            if row["source"] != "DEX Screener" and asset in valuation:
                row.update(valuation[asset])
        sources["CoinGecko"] = valuation_source
        securities = [r["security"] for r in candidates if "security" in r]
        sources["GoPlus"] = dict(status="ok" if securities and all(s["status"]=="ok" for s in securities) else "partial" if securities else "unavailable",
                                  fetched_at=time.time(),note="Riesgos de contratos DEX; redes no cubiertas permanecen desconocidas")
        ranked = discovery(candidates,self.previous)
        whales = whale_activity(self.client,candidates,time.time())
        sources["Grandes transacciones BTC"] = dict(status=whales["status"],fetched_at=whales["observed_at"],note=whales["note"])
        sources["Whale Alert"] = dict(status=whales["attributed_status"],fetched_at=whales["observed_at"],note=whales["attributed_note"])
        calendar = unlock_calendar(self.client,time.time())
        sources["Calendario de desbloqueos"] = {k:v for k,v in calendar.items() if k!="events"}
        self.previous = {r["id"]:r for r in candidates}
        priority = {**{key:0 for key in ENTRY_DECISIONS}, "ESPERAR":1, "INVESTIGAR":2, "DESCARTAR":3}
        candidates.sort(key=lambda c: (0 if c.get("major") else 1,
            MAJORS.index(major_symbol(c["symbol"])) if c.get("major") else priority[c["decision"]],
            0 if c.get("market") == "spot" else 1, -c["score"], c["id"]))
        return dict(schema_version=3, mode="analysis", execution_enabled=False,
                    status="ok" if any(c.get("analysis_ok", True) for c in candidates) else "error", started_at=started, completed_at=time.time(),
                    updated_at=iso(time.time()), next_scan_at=time.time()+POLL_SECONDS,
                    pid=os.getpid(), poll_seconds=POLL_SECONDS, sources=sources, coverage=coverage,
                    candidates=candidates, news=news["articles"], errors=errors,
                    discovery_ids=ranked,whales=whales,calendar=calendar,
                    watchlist=list(MAJORS), methodology="Seguimiento fijo más líderes y rotación. Spot y futuros se analizan por separado; long/short requieren su propio contrato, velas y funding. Checklist 15m/1h; DEX solo investigación.",
                    limitations=["No cubre todas las monedas ni todas las redes. Perfiles DEX recientes no representan un listado completo ni verifican el contrato.",
                                 "Puntuaciones heurísticas sin validación de rentabilidad; no son probabilidades de subida.",
                                 "RSS de una fuente editorial, sin corroboración independiente; ausencia de noticias no demuestra ausencia de riesgos."])


def run_scanner(path, once=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=path.with_suffix(".log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("candidates",[])
    except (OSError,ValueError,TypeError):
        previous = []
    scanner = Scanner(previous=previous)
    from .signal_history import SignalHistory
    history = SignalHistory(path.with_suffix(".history.sqlite3"))
    with ProcessLock(path.with_suffix(".lock")):
        while not stopping:
            try:
                report = scanner.cycle()
                try:
                    report["history"] = history.publish(report)
                except (OSError, ValueError, sqlite3.Error):
                    report["history"] = dict(status="error",note="Historial no disponible; no se inventan resultados",recent=[])
                write_snapshot(path, report)
                logging.info("Solo análisis: %s, cobertura=%s", report["status"], report["coverage"])
            except Exception as exc:
                logging.error("Escaneo incompleto: %s", type(exc).__name__)
                try:
                    old = json.loads(path.read_text(encoding="utf-8"))
                    old.update(status="error", error="Escaneo incompleto; señales invalidadas")
                    write_snapshot(path, old)
                except (OSError, ValueError):
                    pass
                if once:
                    return 1
            if once:
                print(json.dumps({"status": report["status"], "coverage": report["coverage"]}, ensure_ascii=False))
                return 0 if report["status"] == "ok" else 1
            deadline = time.monotonic()+POLL_SECONDS
            while not stopping and time.monotonic() < deadline:
                time.sleep(.5)
    return 0
