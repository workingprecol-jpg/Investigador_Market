"""Coordinator and durable futures execution. All exchange writes target Demo."""
import copy
import math
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .context import MarketContextAgent
from .exchange import BinanceDemoClient, ExchangeError, UnknownExecution
from .risk import RiskAgent, new_state
from .technical import LiquidityAgent, strategy_agents
from .models import Advice
from .governance import effective_config, queue_review, apply_ready_promotion, validate_patch
from .shadow import ShadowLab
from .telemetry import ActivityRecorder


def number(value, positive=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("Dato numerico de mercado invalido")
    return result


class Coordinator:
    def __init__(self, config, store, client=None, context=None):
        self.base_config = config
        self.store = store
        self.client = client or BinanceDemoClient()
        self.state = store.load() or new_state(config)
        self.state.setdefault("strategy_profile", "trend")
        for key, expected in (("mode", config.mode), ("initial_equity", config.initial_equity),
                              ("symbols", list(config.symbols)), ("leverage", config.leverage),
                              ("strategy_profile", config.strategy_profile)):
            if self.state[key] != expected:
                raise ValueError(f"El estado persistido no coincide con {key}; usa otra ruta de estado")
        self.c = effective_config(config, self.state)
        for symbol, position in self.state["positions"].items():
            position.setdefault("position_id", f"legacy-{symbol}-{position['opened_at']}")
            position.setdefault("decision_id", None)
            position.setdefault("entry_context", {"regime": "unknown", "version": 1, "legacy": True})
        config = self.c
        self.trend, self.momentum, self.score_weights = strategy_agents(config.strategy_profile)
        self.liquidity = LiquidityAgent(config.max_spread_bps, config.min_quote_volume)
        self.context = context or MarketContextAgent(enabled=config.news_enabled, grok_enabled=config.grok_enabled,
                                                     require_fresh=config.require_fresh_news,
                                                     refresh_seconds=config.news_refresh_seconds, store=store)
        self.risk = RiskAgent(config)
        self.configured = set()
        self.shadow = ShadowLab(self.c)
        self.activity = ActivityRecorder(self.store.path.with_suffix(".activity.json"))

    def daily_maintenance(self, report=None):
        now = time.time()
        if report is not None and not report.get("preview"):
            accepted = queue_review(self.state, report, self.base_config, now)
            # Existing conservative reductions are evaluated at daily close, not after each fill.
            # Use only completed trades known inside the review's cutoff.
            cutoff = datetime.fromisoformat(report["cutoff_utc"].replace("Z", "+00:00")).timestamp()
            historical = copy.deepcopy(self.state)
            rows = [t for t in historical["trades"] if t["closed_at"] < cutoff]
            # Partial exits form a single completed position for daily adaptation.
            grouped, closed = {}, []
            for trade in rows:
                if not trade.get("position_id"):
                    closed.append(trade)
                    continue
                group = grouped.setdefault(trade["position_id"], {"net_pnl": 0.0, "closed_at": 0.0, "complete": False})
                group["net_pnl"] += trade["net_pnl"]
                group["closed_at"] = max(group["closed_at"], trade["closed_at"])
                group["complete"] = group["complete"] or trade.get("position_closed", False)
            closed.extend(g for g in grouped.values() if g["complete"])
            historical["trades"] = sorted(closed, key=lambda t: t["closed_at"])
            if accepted and historical["adaptation_count"] <= len(historical["trades"]):
                self.risk.adapt(historical)
                for key in ("risk_multiplier", "adaptation_count", "adaptations"):
                    self.state[key] = historical[key]
            self.save("daily_review_applied", {"date": report["date"], "decision": report.get("decision")})
        if apply_ready_promotion(self.state, self.base_config, now):
            self.c = effective_config(self.base_config, self.state)
            self.risk = RiskAgent(self.c)
            self.liquidity = LiquidityAgent(self.c.max_spread_bps, self.c.min_quote_volume)
            self.context = MarketContextAgent(enabled=self.c.news_enabled, grok_enabled=self.c.grok_enabled,
                require_fresh=self.c.require_fresh_news, refresh_seconds=self.c.news_refresh_seconds,
                store=self.store)
            self.shadow = ShadowLab(self.c)
            self.save("strategy_promoted", self.state["strategy"]["history"][-1])
        if not self.state["pending_promotions"] and not any(e.get("status") == "running" for e in self.state["experiments"].values()):
            while self.state["proposal_queue"]:
                proposal = self.state["proposal_queue"].pop(0)
                try:
                    validate_patch(self.base_config, proposal["patch"], current=self.c, agent=proposal["agent"])
                    if proposal.get("base_version") != self.state["strategy"]["version"]:
                        continue
                    if self.shadow.start(self.state, proposal, now):
                        self.state["experiments"][proposal["id"]]["source_version"] = self.state["strategy"]["version"]
                        self.save("shadow_started", {"proposal_id": proposal["id"], "agent": proposal["agent"]})
                        break
                except (KeyError, ValueError, TypeError):
                    continue

    def _audit_decision(self, analysis, status, reason):
        analysis["execution"] = dict(status=status, reason=reason)
        if status in ("candidate", "filled") and analysis["score"] > 0:
            buy_decision = "COMPRAR"
        elif (status == "pending" or self.state["halt_reason"] or self.state["daily_paused"]
              or "Datos de mercado incompletos" in reason or "envejecio" in reason
              or "Informacion de contrato no disponible" in reason
              or any(a["agent"] == "market_context" and a["veto"]
                     for a in analysis["agents"])):
            buy_decision = "ESPERAR"
        else:
            buy_decision = "NO_COMPRAR"
        analysis["buy_decision"] = buy_decision
        self.state["decisions_audit"].append(dict(ts=time.time(), decision_id=analysis["decision_id"],
            symbol=analysis["symbol"], side="LONG" if analysis["score"]>0 else "SHORT" if analysis["score"]<0 else "WAIT",
            score=analysis["score"], status=status, reason=reason, buy_decision=buy_decision,
            news_evidence_ids=analysis.get("news_evidence_ids", []),
            veto_agents=[a["agent"] for a in analysis["agents"] if a["veto"]], version=analysis["strategy_version"]))
        self.state["decisions_audit"] = self.state["decisions_audit"][-100:]
        self.save("decision_audit", self.state["decisions_audit"][-1])

    def save(self, kind=None, data=None):
        self.store.save(self.state, kind, data)

    def halt(self, reason):
        self.state["halt_reason"] = reason
        self.save("halt", {"reason": reason})

    def _id(self, prefix):
        return "fb-" + prefix + "-" + uuid.uuid4().hex[:24]

    def _equity(self, marks):
        unrealized = []
        for symbol, position in self.state["positions"].items():
            if position["direction"] not in (-1, 1):
                raise ValueError("Direccion de posicion invalida")
            unrealized.append(position["direction"] * number(position["quantity"], True) *
                              (number(marks[symbol], True) - number(position["entry_price"], True)))
        return number(number(self.state["balance"]) + math.fsum(unrealized))

    def _paper_fill(self, side, quantity, snapshot):
        price = snapshot.ask if side == "BUY" else snapshot.bid
        price *= 1 + (1 if side == "BUY" else -1) * self.c.slippage_bps / 10000
        return price, quantity * price * self.c.fee_rate

    def _record_entry(self, intent, quantity, price, fee):
        symbol = intent["symbol"]
        if symbol in self.state["positions"]:
            raise RuntimeError("Entrada duplicada bloqueada")
        direction = 1 if intent["side"] == "BUY" else -1
        # Stops are anchored to execution, not the potentially stale signal price.
        distance = intent["plan"]["stop_distance"]
        self.state["positions"][symbol] = dict(symbol=symbol, direction=direction, quantity=quantity,
            entry_price=price, entry_fee=fee, opened_at=intent["created_at"],
            stop=price-direction*distance, target=price+direction*distance*self.c.reward_risk_ratio,
            risk_usdt=intent["plan"]["risk_usdt"], funding=0.0, next_funding_time=0,
            stop_id=None, stop_client_id=None,
            position_id=intent.get("position_id", self._id("position")), decision_id=intent.get("decision_id"),
            entry_context=intent.get("entry_context", {"regime": "unknown", "version": self.state["strategy"]["version"]}))
        self.state["balance"] -= fee

    def _record_exit(self, intent, quantity, price, fee):
        symbol = intent["symbol"]
        p = self.state["positions"][symbol]
        if quantity > p["quantity"] * 1.000001:
            raise RuntimeError("Salida supera posicion registrada")
        ratio = min(1.0, quantity / p["quantity"])
        gross = p["direction"] * quantity * (price-p["entry_price"])
        allocated_entry_fee = p["entry_fee"] * ratio
        allocated_funding = p["funding"] * ratio
        trade = dict(symbol=symbol, direction=p["direction"], quantity=quantity,
            entry_price=p["entry_price"], exit_price=price, opened_at=p["opened_at"],
            closed_at=time.time(), gross_pnl=gross, fees=allocated_entry_fee+fee,
            funding=allocated_funding, net_pnl=gross-allocated_entry_fee-fee+allocated_funding,
            reason=intent.get("reason", "exit"), mode=self.c.mode,
            position_id=p.get("position_id"), decision_id=p.get("decision_id"),
            entry_context=p.get("entry_context", {}), position_closed=ratio>=.999999,
            remaining_quantity=max(0, p["quantity"]-quantity))
        self.state["trades"].append(trade)
        self.state["balance"] += gross - fee
        if ratio >= .999999:
            del self.state["positions"][symbol]
        else:
            p["quantity"] -= quantity
            p["entry_fee"] *= 1-ratio
            p["funding"] *= 1-ratio
            p["risk_usdt"] *= 1-ratio
            self.state["halt_reason"] = "Salida parcial: revisar remanente protegido"

    def _consume_order(self, cid, order):
        intent = self.state["pending"][cid]
        status = order.get("status")
        quantity = number(order.get("executedQty", 0))
        terminal = status in ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH")
        if (quantity < 0 or quantity > intent["quantity"] * 1.000001
                or (status == "FILLED" and not math.isclose(quantity, intent["quantity"], rel_tol=1e-6, abs_tol=1e-10))
                or order.get("symbol", intent["symbol"]) != intent["symbol"]
                or order.get("clientOrderId", cid) != cid):
            self.halt("Respuesta de orden no coincide con intencion registrada")
            return False
        if not terminal and (intent["role"] != "entry" or quantity == 0):
            self.halt("Orden abierta o parcial; requiere conciliacion antes de nuevas entradas")
            return False
        if quantity > 0:
            rows = self.client.trades(intent["symbol"], order["orderId"])
            if any(str(r.get("orderId", order["orderId"])) != str(order["orderId"]) for r in rows):
                self.halt("Fills corresponden a otra orden")
                return False
            filled = sum(number(r["qty"], True) for r in rows)
            if not math.isclose(filled, quantity, rel_tol=1e-6, abs_tol=1e-10):
                self.halt("Fills aun incompletos; orden persistida para conciliar")
                return False
            price = sum(number(r["qty"], True)*number(r["price"], True) for r in rows)/filled
            if any(r.get("commissionAsset", "USDT") != "USDT" for r in rows):
                self.halt("Comision en activo no soportado; revisar contabilidad")
                return False
            fee = sum(number(r.get("commission", 0)) for r in rows)
            if intent["role"] == "entry":
                previous = intent.get("accounted_quantity", 0)
                if quantity < previous:
                    self.halt("Cantidad ejecutada retrocedio; conciliacion detenida")
                    return False
                delta = quantity - previous
                if delta > 1e-12:
                    delta_fee = fee - intent.get("accounted_fee", 0)
                    if previous == 0:
                        self._record_entry(intent, quantity, price, fee)
                    else:
                        p = self.state["positions"][intent["symbol"]]
                        p["quantity"] = quantity
                        p["entry_price"] = price
                        p["entry_fee"] += delta_fee
                        self.state["balance"] -= delta_fee
                    intent["accounted_quantity"] = quantity
                    intent["accounted_fee"] = fee
            else:
                self._record_exit(intent, quantity, price, fee)
        if not terminal:
            self.state["halt_reason"] = "Entrada parcial: detener nuevas entradas y proteger fills confirmados"
            self.save("partial_fill", {"client_id": cid, "quantity": quantity})
            self._protect(intent["symbol"], None)
            return False
        del self.state["pending"][cid]
        self.save("fill", {"client_id": cid, "symbol": intent["symbol"], "quantity": quantity,
                           "status": status, "role": intent["role"]})
        return True

    def reconcile_pending(self, roles=None):
        for cid, intent in list(self.state["pending"].items()):
            if roles is not None and intent["role"] not in roles:
                continue
            order = self.client.get_order(intent["symbol"], cid)
            if order is None:
                self.halt("Orden incierta no localizada; no se reenvia automaticamente")
                continue
            self._consume_order(cid, order)

    def _submit(self, intent, snapshot):
        cid = self._id(intent["role"])
        intent["created_at"] = time.time()
        if self.c.mode == "paper":
            price, fee = self._paper_fill(intent["side"], intent["quantity"], snapshot)
            if intent["role"] == "entry":
                self._record_entry(intent, intent["quantity"], price, fee)
            else:
                # A gap through a stop is filled at the worse currently available quote.
                if "paper_exit_price" in intent:
                    price = intent["paper_exit_price"]
                    fee = intent["quantity"] * price * self.c.fee_rate
                self._record_exit(intent, intent["quantity"], price, fee)
            self.save("paper_fill", {"symbol": intent["symbol"], "role": intent["role"], "price": price})
            return True
        self.state["pending"][cid] = intent
        self.save("order_intent", {"client_id": cid, **intent})
        try:
            order = self.client.place_market(intent["symbol"], intent["side"], intent["quantity"], cid,
                                            reduce_only=intent["role"] == "exit")
        except UnknownExecution:
            self.halt("Resultado de orden incierto: conciliar por client ID, nunca repetir")
            return False
        except ExchangeError:
            del self.state["pending"][cid]
            self.save("order_rejected", {"symbol": intent["symbol"], "client_id": cid})
            raise
        if order.get("status") != "FILLED":
            order = self.client.get_order(intent["symbol"], cid)
            if order is None:
                self.halt("Confirmacion de orden pendiente")
                return False
        return self._consume_order(cid, order)

    def _protect(self, symbol, snapshot):
        p = self.state["positions"][symbol]
        if self.c.mode != "demo" or p["stop_id"] or p.get("sync_mismatch"):
            return
        if p["stop_client_id"]:
            existing = self.client.get_stop_by_client_id(symbol, p["stop_client_id"])
            if existing and existing.get("algoStatus") in ("NEW", "TRIGGERING", "TRIGGERED", "FINISHED"):
                p["stop_id"] = existing["algoId"]
                self.save("stop_reconciled", {"symbol": symbol})
                return
            self.halt("Stop incierto no localizado: cierre de emergencia")
            for cid, intent in list(self.state["pending"].items()):
                if intent["symbol"] == symbol and intent["role"] == "entry":
                    try:
                        self.client.cancel_order(symbol, cid)
                        order = self.client.get_order(symbol, cid)
                        if order and order.get("status") in ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"):
                            self._consume_order(cid, order)
                    except ExchangeError:
                        self.halt("Entrada parcial sin stop y cancelacion incierta: requiere intervencion inmediata")
                        return
            self.exit_position(symbol, snapshot, "stop_uncertain")
            return
        p["stop_client_id"] = self._id("stop")
        self.save("stop_intent", {"symbol": symbol, "client_id": p["stop_client_id"]})
        try:
            stop = self.client.protection_stop(symbol, "SELL" if p["direction"] == 1 else "BUY",
                                                p["stop"], p["stop_client_id"])
            p["stop_id"] = stop["algoId"]
            self.save("stop_accepted", {"symbol": symbol, "algo_id": p["stop_id"]})
        except (ExchangeError, KeyError):
            self.halt("No se pudo confirmar stop: cierre de emergencia")
            # Cancel any unfilled entry remainder before trying to flatten its fills.
            for cid, intent in list(self.state["pending"].items()):
                if intent["symbol"] == symbol and intent["role"] == "entry":
                    try:
                        self.client.cancel_order(symbol, cid)
                        order = self.client.get_order(symbol, cid)
                        if order and order.get("status") in ("FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"):
                            self._consume_order(cid, order)
                    except ExchangeError:
                        self.halt("Entrada parcial sin stop y cancelacion incierta: requiere intervencion inmediata")
                        return
            self.exit_position(symbol, snapshot, "protection_failed")

    def exit_position(self, symbol, snapshot, reason, paper_exit_price=None):
        if any(i["symbol"] == symbol for i in self.state["pending"].values()):
            return
        p = self.state["positions"][symbol]
        if p.get("sync_mismatch"):
            self.halt("Posicion difiere en Binance; no se opera sobre cantidades no conciliadas")
            return
        stop_id = p["stop_id"]
        intent = dict(role="exit", symbol=symbol, side="SELL" if p["direction"] == 1 else "BUY",
                      quantity=p["quantity"], reason=reason)
        if paper_exit_price is not None:
            intent["paper_exit_price"] = paper_exit_price
        done = self._submit(intent, snapshot)
        if done and symbol not in self.state["positions"] and self.c.mode == "demo" and stop_id:
            try:
                self.client.cancel_stop(symbol, stop_id)
            except ExchangeError:
                self.halt("Salida ejecutada; revisar cancelacion del stop restante")

    def _sync_demo(self):
        if self.state["demo_wallet_offset"] is None:
            account = self.client.account()
            wallet = number(account["totalWalletBalance"])
            if (any(number(p["positionAmt"]) != 0 for p in self.client.positions())
                    or self.state["positions"] or self.state["pending"]):
                self.halt("Cuenta Demo no vacia: no se puede establecer capital inicial dedicado")
                raise ExchangeError("Dedicated empty Demo account required for initial baseline")
            if number(account["availableBalance"]) < self.c.initial_equity:
                raise ValueError("Saldo Demo disponible menor a 100 USDT configurados")
            self.state["demo_wallet_offset"] = wallet - self.c.initial_equity
            self.save("demo_capital_reserved", {"capital": self.c.initial_equity})
        # Recover filled entries first, then attribute funding before consuming exits.
        self.reconcile_pending({"entry"})
        self._sync_income()
        self.reconcile_pending({"exit"})
        actual = {p["symbol"]: p for p in self.client.positions() if number(p["positionAmt"]) != 0}
        for symbol in actual.keys() - self.state["positions"].keys():
            self.halt("Cuenta Demo tiene posiciones ajenas/no conciliadas; requiere cuenta dedicada")
        for symbol, p in list(self.state["positions"].items()):
            if symbol not in actual:
                if not p["stop_id"] and p["stop_client_id"]:
                    recovered = self.client.get_stop_by_client_id(symbol, p["stop_client_id"])
                    if recovered:
                        p["stop_id"] = recovered["algoId"]
                        self.save("stop_recovered", {"symbol": symbol, "algo_id": p["stop_id"]})
                if not p["stop_id"]:
                    p["sync_mismatch"] = True
                    self.halt("Posicion ausente sin salida registrada")
                    continue
                stop = self.client.get_stop(symbol, p["stop_id"])
                order_id = stop.get("actualOrderId")
                if not order_id or str(order_id) == "0":
                    p["sync_mismatch"] = True
                    self.halt("Posicion ausente; salida externa o stop pendiente de confirmar")
                    continue
                rows = self.client.trades(symbol, order_id)
                qty = sum(number(t["qty"], True) for t in rows)
                if not math.isclose(qty, p["quantity"], rel_tol=1e-6, abs_tol=1e-10):
                    self.halt("Ejecucion de stop pendiente de conciliacion completa")
                    continue
                if any(t.get("commissionAsset", "USDT") != "USDT" for t in rows):
                    self.halt("Comision de stop en activo no soportado")
                    continue
                price = sum(number(t["qty"], True)*number(t["price"], True) for t in rows)/qty
                self._record_exit(dict(symbol=symbol, reason="exchange_stop"), qty, price,
                                  sum(number(t.get("commission", 0)) for t in rows))
                self.save("exchange_stop_filled", {"symbol": symbol, "price": price})
            else:
                matches = (actual[symbol].get("positionSide", "BOTH") == "BOTH"
                    and math.isclose(number(actual[symbol]["positionAmt"]), p["direction"]*p["quantity"],
                                     rel_tol=1e-6, abs_tol=1e-10))
                p["sync_mismatch"] = not matches
                if not matches:
                    self.halt("Cantidad o sentido en Binance difiere del registro local")
        # Dedicated Demo account: wallet delta includes actual fees, funding and realized PnL.
        account = self.client.account()
        wallet = number(account["totalWalletBalance"])
        self.state["balance"] = wallet - self.state["demo_wallet_offset"]
        self.state["equity"] = number(account["totalMarginBalance"]) - self.state["demo_wallet_offset"]
        self.save("demo_reconciled", {"balance": self.state["balance"], "equity": self.state["equity"]})

    def _sync_income(self):
        # Do not let a malformed later row leave funds changed without durable IDs.
        working = copy.deepcopy(self.state)
        beginning = int(working["created_at"]*1000)
        cursor = working.get("income_cursor", beginning)
        # Binance income can arrive after its economic timestamp. Re-read a day,
        # deduplicating durable IDs instead of skipping delayed funding records.
        rows = self.client.income(max(beginning, cursor - 86_400_000))
        seen = set(working.get("income_seen", []))
        halt_reason = None
        for row in sorted(rows, key=lambda r: int(r["time"])):
            key = str(row.get("tranId")) + ":" + str(row.get("incomeType"))
            stamp = int(row["time"])
            if key in seen:
                continue
            seen.add(key)
            kind = row.get("incomeType")
            if row.get("asset", "USDT") != "USDT" and number(row["income"]) != 0:
                halt_reason = "Ingreso en activo no soportado en cuenta Demo"
                continue
            if kind == "FUNDING_FEE":
                amount = number(row["income"])
                working["actual_funding_total"] = working.get("actual_funding_total", 0) + amount
                symbol = row.get("symbol")
                eligible = []
                current = working["positions"].get(symbol)
                if current and current["opened_at"]*1000 <= stamp:
                    eligible.append((current, False))
                eligible.extend((t, True) for t in working["trades"] if t["symbol"] == symbol
                                and t["opened_at"]*1000 <= stamp <= t["closed_at"]*1000)
                total_qty = sum(t["quantity"] for t, _ in eligible)
                if total_qty:
                    for target, closed in eligible:
                        allocated = amount * target["quantity"] / total_qty
                        target["funding"] += allocated
                        if closed:
                            target["net_pnl"] += allocated
                else:
                    halt_reason = "Financiacion sin posicion propia en su fecha; revisar cuenta dedicada"
            elif kind not in ("COMMISSION", "REALIZED_PNL", "COMMISSION_REBATE") and number(row["income"]) != 0:
                halt_reason = "Transferencia/reset/cambio externo detectado en cuenta Demo dedicada"
            cursor = max(cursor, stamp)
        working["income_cursor"] = cursor
        # Retain all IDs: losing duplicates could double-count delayed income rows.
        working["income_seen"] = sorted(seen)
        if halt_reason:
            working["halt_reason"] = halt_reason
        self.state = working
        self.save("income_reconciled", {"rows": len(rows), "cursor": cursor})

    def _fund_paper(self, symbol, premium, now):
        p = self.state["positions"].get(symbol)
        if not p:
            return
        next_time = int(number(premium["nextFundingTime"]))
        previous = number(p["next_funding_time"])
        current_rate = number(premium["lastFundingRate"])
        mark = number(premium["markPrice"], True)
        now = number(now, True)
        if next_time < 0 or previous < 0:
            raise ValueError("Fecha de financiacion invalida")
        amount = None
        if previous and now*1000 >= previous:
            # Latest observed rate is an estimate; paper report labels it explicitly.
            rate = number(p.get("funding_rate", 0))
            amount = number(-p["direction"] * number(p["quantity"], True) * mark * rate)
            self.state["balance"] += amount
            p["funding"] += amount
            if now*1000 - previous > 8*3600*1000:
                self.state["halt_reason"] = "Paper tuvo interrupcion larga; financiacion incompleta, revisar muestra"
        p["next_funding_time"] = next_time if next_time > now*1000 else 0
        p["funding_rate"] = current_rate
        if amount is not None:
            # Payment and cursor are one durable transaction, including after a crash.
            self.save("paper_funding_estimate", {"symbol": symbol, "amount": amount})

    def cycle(self):
        self.state["healthy"] = False
        errors = {}
        if self.store.path.with_suffix(".STOP").exists():
            self.halt("Parada solicitada mediante archivo STOP")
        if self.c.mode == "demo":
            try:
                self._sync_demo()
            except (ExchangeError, ValueError, KeyError, TypeError, OSError) as exc:
                errors["reconciliation"] = str(exc)
            # Recovered fills need protection before unrelated market reads can fail.
            for symbol in list(self.state["positions"]):
                try:
                    self._protect(symbol, None)
                except (ExchangeError, ValueError, KeyError, TypeError, OSError) as exc:
                    errors[symbol + ":protection"] = str(exc)
                    self.halt("No se pudo verificar proteccion de " + symbol)
        snapshots, premiums, marks = {}, {}, {}
        minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}[self.c.interval]
        # A failed symbol must never prevent management of another open position.
        # Visit held symbols first to minimize time before stop handling.
        symbols = list(dict.fromkeys([*self.state["positions"], *self.c.symbols]))
        for symbol in symbols:
            try:
                snap = self.client.snapshot(symbol, self.c.interval, 240)
                fetched_at = number(snap.fetched_at, True)
                current = time.time()
                if snap.symbol != symbol or current - fetched_at > 90 or fetched_at > current + 10:
                    raise ValueError("Datos de mercado desactualizados o simbolo incorrecto")
                if not snap.candles or not 0 <= current*1000-number(snap.candles[-1].close_time) <= (minutes*60+90)*1000:
                    raise ValueError("Ultima vela cerrada demasiado antigua o futura")
                if number(snap.ask, True) < number(snap.bid, True):
                    raise ValueError("Cotizacion cruzada")
                snapshots[symbol] = snap
            except (ExchangeError, ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                errors[symbol + ":snapshot"] = str(exc)
            try:
                premium = self.client.premium_index(symbol)
                mark = number(premium["markPrice"], True)
                number(premium["lastFundingRate"])
                if premium.get("symbol", symbol) != symbol:
                    raise ValueError("Marca corresponde a otro simbolo")
                if "time" in premium and not -10 <= time.time()-number(premium["time"])/1000 <= 90:
                    raise ValueError("Precio de marca desactualizado")
                premiums[symbol], marks[symbol] = premium, mark
                if self.c.mode == "paper":
                    self._fund_paper(symbol, premium, time.time())
            except (ExchangeError, ValueError, KeyError, TypeError, OSError) as exc:
                errors[symbol + ":mark"] = str(exc)
        self.activity.publish_market(snapshots, premiums)
        all_marked = all(symbol in marks for symbol in self.state["positions"])
        if all_marked:
            equity = self._equity(marks) if self.c.mode == "paper" else self.state["equity"]
            self.risk.update_limits(self.state, equity, time.time())
        for symbol, p in list(self.state["positions"].items()):
            snap = snapshots.get(symbol)
            try:
                if self.c.mode == "demo":
                    if p["stop_id"]:
                        active = self.client.open_stops(symbol)
                        if not any(str(stop["algoId"]) == str(p["stop_id"]) for stop in active):
                            self.halt("Stop no esta activo; cierre preventivo/conciliacion")
                elif snap is None:
                    # No executable paper quote: flag the outage, never invent a fill.
                    continue
                price = marks.get(symbol)
                reason, fill = None, None
                if price is not None:
                    if p["direction"]*(price-p["stop"]) <= 0:
                        reason = "stop_loss"
                    elif p["direction"]*(price-p["target"]) >= 0:
                        reason = "take_profit"
                if self.c.mode == "paper":
                    # Conservative bar replay: stop first if both levels crossed.
                    for bar in snap.candles:
                        if bar.open_time < p["opened_at"]*1000:
                            continue
                        hit_stop = bar.low <= p["stop"] if p["direction"] == 1 else bar.high >= p["stop"]
                        hit_target = bar.high >= p["target"] if p["direction"] == 1 else bar.low <= p["target"]
                        if hit_stop or hit_target:
                            level = p["stop"] if hit_stop else p["target"]
                            reason = "stop_loss" if hit_stop else "take_profit"
                            if hit_stop:
                                level = min(level, bar.open) if p["direction"] == 1 else max(level, bar.open)
                            fill = level*(1-p["direction"]*self.c.slippage_bps/10000)
                            break
                if time.time()-p["opened_at"] > self.c.max_holding_hours*3600:
                    reason = reason or "time_exit"
                if self.state["halt_reason"] or self.state["daily_paused"]:
                    reason = reason or "risk_stop"
                if reason:
                    if snap is not None:
                        # A stopped position cannot immediately reenter its exit candle.
                        self.state["last_candles"][symbol] = snap.candles[-1].close_time
                        self.save("exit_candle", {"symbol": symbol, "candle": snap.candles[-1].close_time})
                    self.exit_position(symbol, snap, reason, fill)
            except (ExchangeError, ValueError, KeyError, TypeError, OSError) as exc:
                errors[symbol + ":management"] = str(exc)
                self.halt("No se pudo completar gestion de " + symbol)
        # News refresh is independent of closed-candle analysis, after position management.
        if isinstance(self.context, MarketContextAgent):
            self.state["news"] = self.context.poll()
        # Revalue remaining loss from the current mark to each existing stop.
        # Entry risk alone understates the exposure of positions with floating profit.
        for symbol, p in self.state["positions"].items():
            if symbol in marks and symbol in premiums:
                price = marks[symbol]
                costs = price*(2*self.c.fee_rate + 2*self.c.slippage_bps/10000 +
                               abs(number(premiums[symbol]["lastFundingRate"])))
                marked_risk = p["quantity"]*(max(0, p["direction"]*(price-p["stop"])) + costs)
                p["risk_usdt"] = max(p["risk_usdt"], number(marked_risk))
        # Entry processing is serial to reserve aggregate risk after every fill.
        for symbol, snap in snapshots.items():
            if symbol not in premiums:
                continue
            if all(s in marks for s in self.state["positions"]):
                self.risk.update_limits(self.state, self._equity(marks), time.time())
            candle_id = snap.candles[-1].close_time
            previous_analysis = self.state.get("last_analysis", {}).get(symbol)
            new_observation = self.state["last_observations"].get(symbol, -1) < candle_id or previous_analysis is None
            if new_observation:
                trend = self.activity.run("trend", "Analizar tendencia", symbol, self.trend.analyze, snap)
                momentum = self.activity.run("momentum", "Analizar indicadores", symbol, self.momentum.analyze, snap)
                liquidity = self.activity.run("liquidity", "Comprobar liquidez", symbol, self.liquidity.analyze, snap)
                context = self.activity.run("market_context", "Revisar noticias y contexto", symbol, self.context.analyze, snap)
            else:
                cached = {a["agent"]: Advice(**a) for a in previous_analysis["agents"]}
                trend, momentum = cached["trend"], cached["momentum"]
                liquidity = self.activity.run("liquidity", "Comprobar liquidez", symbol, self.liquidity.analyze, snap)
                context = cached["market_context"]
            reports = [trend, momentum, liquidity, context]
            score = self.score_weights[0]*trend.score + self.score_weights[1]*momentum.score
            direction = 1 if score > 0 else -1 if score < 0 else 0
            risk = self.activity.run("risk", "Evaluar riesgo de entrada", symbol,
                                     self.risk.analyze, snap, self.state, direction, momentum.details.get("atr", 0),
                                     number(premiums[symbol]["lastFundingRate"]))
            reports.append(risk)
            direction_allowed = direction == 1 or (direction == -1 and self.c.allow_shorts)
            allowed = (not errors and direction_allowed and not any(a.veto for a in reports) and abs(score) >= self.c.entry_threshold
                       and trend.score*direction > 0 and momentum.score*direction >= 0
                       and trend.score*direction >= self.c.min_trend_score
                       and momentum.score*direction >= self.c.min_momentum_score)
            # Forward experiments observe identical opportunities, including rejected ones.
            if any(e.get("status") == "running" for e in self.state["experiments"].values()):
                shadow_premium = dict(premiums[symbol], _entries_blocked=bool(errors))
                try:
                    shadow_premium["_symbol_filters"] = self.client.exchange_info(symbol)
                except (ExchangeError, ValueError, KeyError, TypeError, OSError, AttributeError):
                    # Exits remain possible; missing filters can only block new paper entries.
                    shadow_premium["_entries_blocked"] = True
                try:
                    self.shadow.update(self.state, snap, shadow_premium, reports, time.time())
                    self.state.pop("shadow_error", None)
                except (ExchangeError, ValueError, KeyError, TypeError, OSError) as exc:
                    self.state["shadow_error"] = type(exc).__name__
                    self.save("shadow_error", {"symbol": symbol, "type": type(exc).__name__})
            if not new_observation:
                continue
            self.state["last_observations"][symbol] = candle_id
            decision_id = f"v{self.state['strategy']['version']}:{symbol}:{candle_id}"
            reason = ("Datos de mercado incompletos" if errors else
                      "Shorts deshabilitados para este perfil" if not direction_allowed else
                      "; ".join(a.reason for a in reports if a.veto) or
                      ("Senal aprobada" if allowed else "Sin acuerdo o fuerza suficiente"))
            duplicate = self.state["last_candles"].get(symbol, -1) >= candle_id
            if duplicate:
                allowed = False
                reason = "Vela ya procesada o usada para una salida"
            context_headlines = context.details.get("headlines", [])
            evidence = [article["id"] for article in context.details.get("relevant_headlines", [])
                        if isinstance(article, dict) and isinstance(article.get("id"), str)]
            for index in context.details.get("explicit_risk_headline_ids", []):
                if (type(index) is int and isinstance(context_headlines, list)
                        and 0 <= index < len(context_headlines)
                        and isinstance(context_headlines[index], dict)
                        and isinstance(context_headlines[index].get("id"), str)):
                    evidence.append(context_headlines[index]["id"])
            analysis = dict(symbol=symbol, candle=candle_id, score=score, allowed=allowed,
                            agents=[asdict(advice) for advice in reports], decision_id=decision_id,
                            news_evidence_ids=list(dict.fromkeys(evidence)),
                            regime=trend.details.get("regime", "unknown"), strategy_version=self.state["strategy"]["version"],
                            strategy_profile=self.c.strategy_profile,
                            execution=dict(status="candidate" if allowed else "blocked", reason=reason),
                            market=dict(bid=snap.bid, ask=snap.ask, mark_price=marks[symbol], funding_rate=premiums[symbol]["lastFundingRate"]))
            self.state.setdefault("last_analysis", {})[symbol] = analysis
            self._audit_decision(analysis, "candidate" if allowed else "blocked", reason)
            self.save("analysis", analysis)
            if not allowed:
                continue
            self.state["last_candles"][symbol] = candle_id
            try:
                qty = number(self.client.normalize_quantity(symbol, risk.details["quantity"], snap.price), True)
            except ExchangeError:
                self.save("entry_skipped", {"symbol": symbol, "reason": "Informacion de contrato no disponible"})
                self._audit_decision(analysis, "skipped", "Informacion de contrato no disponible")
                continue
            except ValueError:
                self.save("entry_skipped", {"symbol": symbol, "reason": "Minimos de Binance exceden riesgo/margen"})
                self._audit_decision(analysis, "skipped", "Minimos de Binance exceden riesgo/margen")
                continue
            if qty > risk.details["quantity"] * 1.000001:
                raise ValueError("Cantidad normalizada excede presupuesto")
            if time.time() - snap.fetched_at > self.c.poll_seconds:
                self.save("entry_skipped", {"symbol": symbol, "reason": "Cotizacion envejecio durante analisis"})
                self._audit_decision(analysis, "skipped", "Cotizacion envejecio durante analisis")
                continue
            if self.c.mode == "demo":
                if self.client.open_orders(symbol) or self.client.open_stops(symbol):
                    self.halt("Ordenes existentes no pertenecen a una posicion registrada")
                    break
                if symbol not in self.configured:
                    self.client.configure_symbol(symbol, leverage=5)
                    self.configured.add(symbol)
            if time.time() - snap.fetched_at > self.c.poll_seconds:
                self.save("entry_skipped", {"symbol": symbol, "reason": "Cotizacion envejecio durante configuracion"})
                self._audit_decision(analysis, "skipped", "Cotizacion envejecio durante configuracion")
                continue
            intent = dict(role="entry", symbol=symbol, side="BUY" if direction == 1 else "SELL",
                          quantity=qty, plan=risk.details, decision_id=decision_id, position_id=self._id("position"),
                          entry_context=dict(regime=analysis["regime"], version=analysis["strategy_version"],
                                             strategy_profile=analysis["strategy_profile"],
                                             agents=analysis["agents"]))
            done = self._submit(intent, snap)
            self._audit_decision(analysis, "filled" if done and symbol in self.state["positions"] else "pending",
                                 "Entrada registrada" if done else "Conciliacion pendiente")
            if done and symbol in self.state["positions"]:
                self._protect(symbol, snap)
                if self.c.mode == "paper":
                    self._fund_paper(symbol, premiums[symbol], time.time())
        if all(symbol in marks for symbol in self.state["positions"]):
            self.state["equity"] = self._equity(marks)
            self.risk.update_limits(self.state, self.state["equity"], time.time())
        self.state["heartbeat"] = time.time()
        self.state["healthy"] = not errors and not bool(self.state["pending"]) and not bool(self.state["halt_reason"])
        self.state["market_errors"] = errors
        if not errors:
            self.state["equity_history"].append(dict(ts=self.state["heartbeat"], equity=self.state["equity"]))
            self.state["equity_history"] = self.state["equity_history"][-2160:]
            try:
                for decision in self.shadow.evaluate(self.state, self.state["heartbeat"]):
                    self.save("shadow_validation", decision)
                    if decision.get("recommendation") == "promote":
                        self.state["pending_promotions"].append(decision)
            except (ValueError, KeyError, TypeError) as exc:
                self.state["shadow_error"] = type(exc).__name__
        self.save("cycle", {"equity": self.state["equity"], "positions": len(self.state["positions"]),
                            "halt": self.state["halt_reason"], "daily_paused": self.state["daily_paused"],
                            "market_errors": errors})
        return self.state
