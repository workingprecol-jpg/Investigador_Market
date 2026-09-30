"""Read-only research enrichment; observations never issue orders or change signals."""
from __future__ import annotations

import os
import re

from .scanner import (DataError, FUTURES, GOPLUS, MEMPOOL, COINGECKO, COINGLASS, WHALE,
                      number, major_symbol)
from .technical import ema

CHAIN_IDS = {"ethereum":"1", "bsc":"56", "polygon":"137", "arbitrum":"42161",
             "base":"8453", "optimism":"10", "avalanche":"43114", "linea":"59144",
             "monad":"143", "robinhood":"4663", "plasma":"9745"}
COIN_IDS = {"BTC":"bitcoin", "ETH":"ethereum", "SOL":"solana", "BNB":"binancecoin",
            "XRP":"ripple", "ADA":"cardano", "DOGE":"dogecoin", "AVAX":"avalanche-2",
            "LINK":"chainlink", "DOT":"polkadot", "LTC":"litecoin", "TRX":"tron",
            "SUI":"sui", "TON":"the-open-network", "PEPE":"pepe"}


def optional_number(value):
    try:
        return number(value)
    except (ValueError, TypeError, OverflowError):
        return None


def technical_series(bars):
    """Closed OHLCV plus seeded EMA20 and candle-based, window-anchored VWAP."""
    result, weighted, volume = [], 0., 0.
    closes = [b.close for b in bars]
    for i, b in enumerate(bars):
        volume += b.volume
        weighted += (b.high+b.low+b.close)/3*b.volume
        result.append(dict(time=b.open_time/1000, close_at=b.close_time/1000,
                           open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
                           ema20=float(f"{ema(closes[:i+1],20):.12g}") if i >= 19 else None,
                           vwap=float(f"{weighted/volume:.12g}") if volume else None))
    return result


def trend_summary(bars):
    values = [b.close for b in bars]
    fast, slow = ema(values,20), ema(values,50)
    trend = "Alcista" if fast > slow and values[-1] > fast else "Bajista" if fast < slow and values[-1] < fast else "Mixta"
    return dict(trend=trend, ema20=fast, ema50=slow, close=values[-1], candle_at=bars[-1].close_time/1000)


def derivatives(client, symbol, price, now):
    result = dict(status="ok", observed_at=now, open_interest=None, flow=None,
                  funding_history=[], errors=[], source="Binance Futures")
    try:
        raw = client.get(FUTURES,"/futures/data/openInterestHist",symbol=symbol,period="15m",limit=9)
        points = sorted([dict(time=number(p["timestamp"])/1000,
                              units=number(p["sumOpenInterest"],positive=True),
                              usd=number(p["sumOpenInterestValue"],positive=True))
                         for p in raw if p["symbol"]==symbol], key=lambda p:p["time"])
        if not points or not 0 <= now-points[-1]["time"] <= 1800:
            raise DataError("OI antiguo")
        for a,b in zip(points,points[1:]):
            if b["time"]-a["time"] != 900:
                raise DataError("Serie OI incompleta")
        last = points[-1]
        result["open_interest"] = dict(**last, change_1h_pct=(last["units"]/points[-5]["units"]-1)*100 if len(points)>=5 else None, history=points)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        result["errors"].append("Open interest no disponible o antiguo")
    try:
        raw = client.get(FUTURES,"/futures/data/takerlongshortRatio",symbol=symbol,period="15m",limit=9)
        points = sorted([dict(time=number(p["timestamp"])/1000,
                              buy=number(p["buyVol"]),sell=number(p["sellVol"]))
                         for p in raw if number(p["timestamp"])/1000+900 <= now],key=lambda p:p["time"])
        if len(points)<4 or not 0 <= now-(points[-1]["time"]+900) <= 1800:
            raise DataError("Flujo antiguo")
        recent=points[-4:]
        if any(p["buy"]<0 or p["sell"]<0 for p in recent) or any(b["time"]-a["time"]!=900 for a,b in zip(recent,recent[1:])):
            raise DataError("Flujo inválido")
        buy, sell = sum(p["buy"] for p in recent), sum(p["sell"] for p in recent)
        result["flow"] = dict(time=recent[-1]["time"]+900,buy_units=buy,sell_units=sell,
                              buy_share_pct=buy/(buy+sell)*100 if buy+sell else None,
                              delta_units=buy-sell, delta_usdt_estimate=(buy-sell)*price, history=points)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        result["errors"].append("Flujo comprador/vendedor no disponible o antiguo")
    try:
        raw=client.get(FUTURES,"/fapi/v1/fundingRate",symbol=symbol,limit=12)
        result["funding_history"] = sorted([dict(time=number(p["fundingTime"])/1000,rate=number(p["fundingRate"]))
                                           for p in raw if p["symbol"]==symbol and 0 <= now-number(p["fundingTime"])/1000 <= 7*86400],key=lambda p:p["time"])
        if not result["funding_history"]:
            raise DataError("Sin historial funding")
    except (OSError, ValueError, KeyError, TypeError):
        result["errors"].append("Historial de funding no disponible")
    result["status"] = "partial" if result["errors"] else "ok"
    return result


def security_result(raw, chain, address, now):
    if raw.get("code")!=1:
        raise DataError("Proveedor de riesgos no disponible")
    result = raw.get("result") or {}
    token = result.get(address) if chain=="solana" else result.get(address.lower())
    if not isinstance(token,dict) or not token:
        raise DataError("Contrato sin informe")
    checks=[]
    def flag(key,label,inverted=False):
        value=token.get(key)
        value=value.get("status") if isinstance(value,dict) else value
        state=str(value) if value is not None else None
        risk=(state=="0" if inverted else state=="1") if state in ("0","1") else None
        checks.append(dict(key=key,label=label,risk=risk,observed="Detectado" if risk is True else "No detectado" if risk is False else "Desconocido"))
    if chain=="solana":
        for key,label in [("mintable","Puede emitir más tokens"),("freezable","Puede congelar saldos"),
                          ("balance_mutable_authority","Puede modificar saldos"),("closable","Cuenta cerrable")]:
            flag(key,label)
        checks.append(dict(key="selling",label="Restricciones de venta",risk=None,observed="Sin prueba independiente de venta"))
    else:
        flag("is_open_source","Contrato sin código abierto",True)
        for key,label in [("is_honeypot","Posible honeypot"),("cannot_sell_all","Restricciones para vender"),
                          ("is_mintable","Puede emitir más tokens"),("transfer_pausable","Puede pausar transferencias"),
                          ("is_blacklisted","Puede bloquear direcciones"),("is_proxy","Contrato modificable/proxy")]:
            flag(key,label)
    for key,label in [("buy_tax","Impuesto de compra"),("sell_tax","Impuesto de venta")]:
        tax=optional_number(token.get(key))
        if tax is not None and not 0<=tax<=1: tax=None
        checks.append(dict(key=key,label=label,risk=tax>.1 if tax is not None else None,observed=f"{tax*100:.2f}%" if tax is not None else "Desconocido"))
    holders = token.get("holders") or []
    percentages=[]
    for holder in holders[:10]:
        tag=str(holder.get("tag","")).lower()
        if any(word in tag for word in ("burn","exchange","pool","liquidity","locker")): continue
        percent=optional_number(holder.get("percent"))
        if percent is not None and 0<=percent<=1: percentages.append(percent)
    concentration=sum(percentages)*100 if percentages else None
    checks.append(dict(key="concentration",label="Concentración de holders devueltos sin entidades conocidas",
                       risk=concentration>50 if concentration is not None else None,
                       observed=f"{concentration:.2f}% · muestra de hasta 10; etiquetas incompletas" if concentration is not None else "Desconocida"))
    # Liquidity ownership must concern the selected pool, not any pool for the token.
    checks.append(dict(key="liquidity_lock",label="Propiedad/bloqueo de liquidez del par seleccionado",risk=None,observed="Pendiente de verificación del par; no se certifica bloqueo"))
    return dict(status="ok",verdict="Riesgos detectados" if any(c["risk"] is True for c in checks) else "Sin riesgos detectados en campos disponibles",
                source="GoPlus",chain=chain,address=address,observed_at=now,checks=checks,
                holder_count=optional_number(token.get("holder_count")),
                note="Detección parcial del proveedor; desconocido no significa seguro. No es una auditoría.")


def token_security(client, chain, address, now):
    if chain=="solana" and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}",address):
        path="/api/v1/solana/token_security"
    elif chain in CHAIN_IDS and re.fullmatch(r"0x[0-9a-fA-F]{40}",address):
        path="/api/v1/token_security/"+CHAIN_IDS[chain]
    else:
        return dict(status="unsupported",source="GoPlus",checks=[],note="Red o contrato no cubierto por esta integración; riesgos desconocidos")
    try:
        return security_result(client.get(GOPLUS,path,contract_addresses=address),chain,address,now)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(status="error",source="GoPlus",observed_at=now,checks=[],note="No se pudo verificar el contrato; riesgos desconocidos")


def discovery(rows, previous):
    btc=next((r for r in rows if r["id"]=="spot:BTCUSDT" and r.get("analysis_ok")),None)
    btc_change=btc["change_24h"] if btc else None
    for row in rows:
        if row.get("analysis_ok") is False: continue
        old=previous.get(row["id"],{})
        relative=(row["change_24h"]-btc_change) if btc_change is not None and row["source"]!="DEX Screener" else None
        old_liquidity=old.get("liquidity_usd")
        liquidity=row.get("liquidity_usd")
        growth=(liquidity/old_liquidity-1)*100 if old_liquidity and liquidity is not None and 0<row["observed_at"]-old.get("observed_at",0)<=86400 else None
        eligible=liquidity>=100_000 and row.get("age_hours",0)>=24 if row["source"]=="DEX Screener" else row["volume_24h"]>=2_000_000 and row["spread_bps"]<=20
        volume=row.get("relative_volume",0)
        acceleration=row.get("volume_acceleration")
        score=min(100,max(0,(relative or 0))*3+min(40,volume*15)+min(20,max(0,(acceleration or 0)-1)*10)) if row["source"]!="DEX Screener" else row["score"]
        row["discovery"]=dict(score=round(score,1),eligible=eligible,relative_btc_pp=relative,
                              volume_acceleration=acceleration,liquidity_growth_pct=growth,
                              liquidity_baseline_at=old.get("observed_at") if growth is not None else None,
                              market_cap=row.get("market_cap"),fdv=row.get("fdv"),
                              note="Ranking de actividad alcista; no probabilidad ni señal de entrada. Falta de datos = desconocido.")
    return [r["id"] for r in sorted((r for r in rows if r.get("discovery",{}).get("eligible")),key=lambda r:r["discovery"]["score"],reverse=True)]


def btc_transactions(raw, price, now, threshold=1_000_000):
    events=[]
    for tx in raw[:10]:
        txid=tx.get("txid","")
        value=optional_number(tx.get("value"))
        if not re.fullmatch(r"[0-9a-f]{64}",txid) or value is None or value<0: continue
        btc=value/100_000_000
        if btc*price>=threshold:
            events.append(dict(id=txid,symbol="BTC",amount=btc,usd=btc*price,observed_at=now,
                               from_entity="Desconocida",to_entity="Desconocida",direction="Sin atribución",
                               confirmed=False,url="https://mempool.space/tx/"+txid,
                               note="Valor total de salidas, puede incluir cambio. No demuestra compra/venta ni identifica ballena."))
    return events


def whale_activity(client, rows, now):
    result=dict(status="partial",source="mempool.space",observed_at=now,events=[],threshold_usd=1_000_000,
                note="Muestra de las 10 últimas transacciones BTC pendientes; no cubre todas las ballenas ni flujos de exchanges.",
                attributed_status="not_configured",attributed_note="Flujos atribuidos multired requieren clave Whale Alert Enterprise; no se contrató ningún servicio.")
    btc=next((r for r in rows if r["id"]=="spot:BTCUSDT" and r.get("analysis_ok") and 0<=now-r["observed_at"]<=900),None)
    try:
        if not btc: raise DataError("Precio BTC no confirmado")
        raw=client.get(MEMPOOL,"/api/mempool/recent")
        result["sample_size"]=min(10,len(raw))
        result["events"]=btc_transactions(raw,btc["price"],now)
    except (OSError, ValueError, KeyError, TypeError):
        result.update(status="error",note="Muestra pública BTC no disponible; no se infieren movimientos")
    key=os.environ.get("WHALE_ALERT_API_KEY")
    if key:
        result["attributed_status"]="partial"
        result["attributed_note"]="Whale Alert REST Enterprise · muestra de hasta 100 transacciones por red en los últimos bloques; cobertura parcial."
        for chain in ("bitcoin","ethereum","solana"):
            try:
                state=client.get(WHALE,"/"+chain+"/status",api_key=key)
                height=int(number(state["end_height"]))
                raw=client.get(WHALE,"/"+chain+"/transactions",api_key=key,start_height=max(0,height-5),limit=100)
                result["events"].extend(attributed_transactions(raw.get("transactions",[]),chain,now))
            except (OSError, ValueError, KeyError, TypeError):
                result["attributed_note"]+=" "+chain+": fuente no disponible (comprobar plan REST)."
    result["events"]=sorted({e["id"]:e for e in result["events"]}.values(),key=lambda e:e["usd"],reverse=True)[:30]
    return result


def attributed_transactions(raw, chain, now):
    events=[]
    for tx in raw:
        try:
            timestamp=number(tx["timestamp"])
            if not 0<=now-timestamp<=3600 or not re.fullmatch(r"(?:0x)?[A-Za-z0-9]{32,128}",tx["hash"]): continue
            for i,sub in enumerate(tx.get("sub_transactions",[])):
                if sub.get("transaction_type")!="transfer": continue
                outputs=sub.get("outputs",[])
                inputs=sub.get("inputs",[])
                origins={a.get("owner") for a in inputs if a.get("owner")}
                destinations={a.get("owner") for a in outputs if a.get("owner")}
                # Ignore identified internal transfers; unknown owners cannot be inferred.
                if origins and origins==destinations: continue
                amount=sum(number(a["amount"]) for a in outputs)
                usd=amount*number(sub["unit_price_usd"],positive=True)
                if usd<1_000_000: continue
                from_exchange=any(a.get("owner_type")=="exchange" for a in inputs)
                to_exchange=any(a.get("owner_type")=="exchange" for a in outputs)
                direction="Hacia exchange" if to_exchange and not from_exchange else "Desde exchange" if from_exchange and not to_exchange else "Transferencia sin dirección concluyente"
                events.append(dict(id=chain+":"+tx["hash"]+":"+str(i),symbol=str(sub["symbol"]).upper()[:24],chain=chain,amount=amount,usd=usd,observed_at=now,event_at=timestamp,
                                   from_entity=", ".join(sorted(origins)) or "Desconocida",to_entity=", ".join(sorted(destinations)) or "Desconocida",
                                   direction=direction,confirmed=True,tx_hash=tx["hash"],note="Transferencia observada, no prueba de compra/venta; atribución del proveedor."))
        except (ValueError,KeyError,TypeError): continue
    return events


def market_valuations(client, now):
    key=os.environ.get("COINGECKO_DEMO_API_KEY")
    if not key:
        return {},dict(status="not_configured",fetched_at=now,note="Market cap/FDV global de monedas principales requiere clave Demo de CoinGecko. DEX usa valores del par.")
    try:
        raw=client.get(COINGECKO,"/api/v3/coins/markets",_headers={"x-cg-demo-api-key":key},vs_currency="usd",ids=",".join(COIN_IDS.values()),per_page=100,page=1)
        reverse={v:k for k,v in COIN_IDS.items()}
        from datetime import datetime
        values={}
        for coin in raw:
            updated=datetime.fromisoformat(coin["last_updated"].replace("Z","+00:00")).timestamp()
            if coin["id"] in reverse and 0<=now-updated<=1800:
                values[reverse[coin["id"]]]=dict(market_cap=optional_number(coin.get("market_cap")),fdv=optional_number(coin.get("fully_diluted_valuation")),valuation_at=updated,valuation_source="CoinGecko")
        return values,dict(status="ok" if values else "error",fetched_at=now,note="CoinGecko · activos por ID verificado, no por símbolo ambiguo")
    except (OSError,ValueError,KeyError,TypeError):
        return {},dict(status="error",fetched_at=now,note="Valoraciones no disponibles; no se inventan market caps")


def unlock_calendar(client, now):
    result=dict(status="not_configured",fetched_at=now,events=[],source="CoinGlass",
                note="Calendario automático requiere API CoinGlass con acceso a unlocks (Startup o superior). No se contrató ningún plan; fechas desconocidas.")
    key=os.environ.get("COINGLASS_API_KEY")
    if not key: return result
    try:
        raw=client.get(COINGLASS,"/api/coin/unlock-list",_headers={"CG-API-KEY":key},per_page=250,page=1)
        if str(raw.get("code"))!="0": raise DataError("Plan sin acceso")
        for coin in raw.get("data",[]):
            timestamp=optional_number(coin.get("next_unlock_date"))
            if timestamp is None or not 0<=timestamp/1000-now<=30*86400: continue
            symbol=str(coin.get("symbol", ""))
            if not re.fullmatch(r"[A-Z0-9]{1,24}",symbol): continue
            result["events"].append(dict(symbol=symbol,name=str(coin.get("name",symbol))[:80],time=timestamp/1000,
                                          tokens=optional_number(coin.get("next_unlock_tokens")),
                                          circulating_pct=optional_number(coin.get("next_unlock_of_circulating")),
                                          source="CoinGlass",note="Identificación por activo del proveedor; no asociar a un contrato DEX solo por símbolo."))
        result.update(status="partial",note="Primera página de hasta 250 activos; próximos 30 días. Fuente de terceros, cobertura parcial; no demuestra ausencia de otros desbloqueos.")
        result["events"].sort(key=lambda e:e["time"])
        return result
    except (OSError,ValueError,KeyError,TypeError):
        result.update(status="error",note="Calendario no disponible; comprobar clave y acceso del plan. Fechas desconocidas.")
        return result
