import html
import json
import math
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPORT_BROWSER_SCRIPT = r"""(() => {
  'use strict';
  const cycle = document.getElementById('last-cycle');
  const age = document.getElementById('cycle-age');
  const status = document.getElementById('recency-status');
  const automatic = document.getElementById('refresh-auto');
  const countdown = document.getElementById('refresh-countdown');
  const refresh = document.getElementById('refresh-now');
  const heartbeat = Date.parse(cycle.getAttribute('datetime') || '');
  const preferenceKey = 'finalboss-report-autorefresh:' + window.location.pathname;
  try {
    automatic.checked = window.sessionStorage.getItem(preferenceKey) !== 'paused';
  } catch (_) {
    automatic.checked = true;
  }
  let refreshAt = Date.now() + 30000;
  function update() {
    const now = Date.now();
    if (!Number.isFinite(heartbeat)) {
      age.textContent = 'Sin datos';
      status.textContent = 'Sin ciclo registrado';
      status.dataset.state = 'unknown';
    } else if (heartbeat > now + 5000) {
      age.textContent = 'Reloj por verificar';
      status.textContent = 'Fecha del ciclo adelantada';
      status.dataset.state = 'stale';
    } else {
      const seconds = Math.max(0, (now - heartbeat) / 1000);
      age.textContent = Math.floor(seconds) + ' s';
      status.textContent = seconds <= 180 ? 'Datos recientes' : 'Sin actualización reciente';
      status.dataset.state = seconds <= 180 ? 'recent' : 'stale';
    }
    if (automatic.checked) {
      countdown.textContent = 'Próxima recarga en ' + Math.max(0, Math.ceil((refreshAt - now) / 1000)) + ' s';
      if (now >= refreshAt) {
        refreshAt = now + 30000;
        window.location.reload();
      }
    } else {
      countdown.textContent = 'Recarga automática pausada';
    }
  }
  automatic.addEventListener('change', () => {
    try {
      window.sessionStorage.setItem(preferenceKey, automatic.checked ? 'automatic' : 'paused');
    } catch (_) {}
    refreshAt = Date.now() + 30000;
    update();
  });
  refresh.addEventListener('click', () => window.location.reload());
  update();
  window.setInterval(update, 1000);
})();"""


def _atomic_write(path, content):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_state(path):
    path = Path(path).resolve()
    if not path.exists():
        raise ValueError("El bot aun no tiene estado. Ejecuta primero run --once.")
    connection = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT data FROM state WHERE id=1").fetchone()
        if not row:
            raise ValueError("El bot aun no completo su primer ciclo")
        return json.loads(row[0])
    finally:
        connection.close()


def build_report(state):
    trades = state["trades"]
    profit = sum(max(0, t["net_pnl"]) for t in trades)
    loss = -sum(min(0, t["net_pnl"]) for t in trades)
    last = state["heartbeat"]
    realized = state["balance"] - state["initial_equity"]
    pnl = state["equity"] - state["initial_equity"]
    now = datetime.now(timezone.utc)
    cycle_age = max(0, now.timestamp()-last) if last else None
    return dict(generated_at=now.isoformat(), mode=state["mode"],
        strategy_profile=state.get("strategy_profile", "trend"),
        initial_equity=state["initial_equity"], equity=state["equity"], balance=state["balance"],
        net_pnl=pnl, net_return_pct=pnl/state["initial_equity"]*100,
        realized_net_pnl=realized, unrealized_pnl=state["equity"]-state["balance"],
        closed_trades=len(trades), win_rate_pct=(100*sum(t["net_pnl"]>0 for t in trades)/len(trades)) if trades else None,
        profit_factor=profit/loss if loss else None,
        expectancy_usdt=sum(t["net_pnl"] for t in trades)/len(trades) if trades else None,
        drawdown_pct=(1-state["equity"]/state["peak_equity"])*100,
        positions=state["positions"], pending_orders=len(state["pending"]),
        halt_reason=state["halt_reason"], daily_paused=state["daily_paused"],
        healthy=state["healthy"], last_cycle=datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
        market_errors=state.get("market_errors", {}), last_analysis=state.get("last_analysis", {}),
        news=state.get("news", {}),
        risk_multiplier=state["risk_multiplier"], adaptations=state["adaptations"],
        leverage=state.get("leverage"), cycle_age_seconds=cycle_age,
        equity_history=state.get("equity_history", []),
        daily_review=state.get("daily_review", {}), daily_schedule=state.get("daily_schedule", {}),
        strategy=state.get("strategy", {}),
        experiments=state.get("experiments", {}),
        decisions_audit=state.get("decisions_audit", state.get("decision_audit", [])),
        funding_note="Estimacion con ultima tasa observada" if state["mode"] == "paper" else "Financiacion real de cuenta Demo",
        validation="SIN evidencia de rentabilidad" if len(trades)<100 else "Muestra disponible; requiere validacion fuera de muestra",
        note="Demo/paper no garantizan resultados reales. Profit factor indefinido si no hay perdidas.",
        recent_trades=trades[-50:])


def _esc(value):
    return html.escape(str(value), quote=True)


def _number(value, digits=2, signed=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return "Sin datos"
    return format(value, f"{'+' if signed else ''}.{digits}f")


def _details(title, value):
    if not value:
        return ""
    return f'<details><summary>{_esc(title)}</summary><pre>{_esc(json.dumps(value, ensure_ascii=False, indent=2))}</pre></details>'


def _time(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return "Fecha inválida"
    return str(value) if value is not None else "Sin datos"


def _colombia_time(value):
    if not value:
        return "Sin datos"
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return "Fecha sin zona horaria"
        return moment.astimezone(timezone(timedelta(hours=-5))).strftime("%d/%m/%Y %H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        return "Fecha inválida"


def _equity_chart(history):
    points = []
    for row in history if isinstance(history, list) else []:
        if not isinstance(row, dict):
            continue
        ts, equity = row.get("ts"), row.get("equity")
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (ts, equity)):
            points.append((ts, equity))
    points.sort(key=lambda point: point[0])
    if not points:
        return '<p class="empty">La curva comenzará con las próximas observaciones de equidad. No se reconstruye un historial ficticio.</p>'
    start, end = points[0][0], points[-1][0]
    low, high = min(v for _, v in points), max(v for _, v in points)
    padding = max((high-low)*.12, .1)
    bottom, top = low-padding, high+padding
    mapped = [(65+(ts-start)/max(end-start, 1)*770, 185-(value-bottom)/(top-bottom)*150) for ts, value in points]
    line = " ".join(f"{x:.2f},{y:.2f}" for x, y in mapped)
    labels = "".join(f'<text x="6" y="{y+4:.2f}">{value:.2f}</text><line x1="65" y1="{y:.2f}" x2="835" y2="{y:.2f}" class="gridline"/>'
                     for value, y in ((top, 35), ((top+bottom)/2, 110), (bottom, 185)))
    x, y = mapped[-1]
    return f'''<svg class="equity-chart" viewBox="0 0 860 215" role="img" aria-label="Equidad observada en USDT">
<title>Equidad observada: {len(points)} puntos, desde {_esc(_time(start))} hasta {_esc(_time(end))}</title>
{labels}<polyline points="{line}" fill="none" stroke="#087b60" stroke-width="3"/><circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="#087b60"/>
<text x="65" y="210">{_esc(_time(start))}</text><text x="835" y="210" text-anchor="end">{_esc(_time(end))}</text></svg>
<p class="caption">{len(points)} observaciones registradas · USDT · horas UTC · incluye PnL abierto y costes contabilizados.</p>'''


def _specialist_cards(report):
    names = [("trend", "Tendencia"), ("momentum", "Indicadores"), ("liquidity", "Liquidez"),
             ("context", "Noticias y contexto"), ("risk", "Riesgo y rendimiento"),
             ("research", "Investigación y validación")]
    daily = report.get("daily_review", {})
    daily = daily if isinstance(daily, dict) else {}
    evaluations = daily.get("specialists", {})
    if isinstance(evaluations, list):
        evaluations = {str(row.get("id", row.get("agent", row.get("specialist", "")))): row for row in evaluations if isinstance(row, dict)}
    if not isinstance(evaluations, dict):
        evaluations = {}
    result = []
    for key, label in names:
        rows = []
        for symbol, analysis in report.get("last_analysis", {}).items():
            for advice in analysis.get("agents", []) if isinstance(analysis, dict) else []:
                if isinstance(advice, dict) and advice.get("agent") == key:
                    rows.append(f'<li><b>{_esc(symbol)}</b> · {"Veto" if advice.get("veto") else "Informe"}<br>{_esc(advice.get("reason", "Sin explicación registrada"))}</li>')
        daily_key = {"context": "market_context", "research": "research_validation"}.get(key, key)
        evaluation = evaluations.get(key, evaluations.get(daily_key))
        current = '<ul class="agent-notes">'+"".join(rows)+'</ul>' if rows else '<p class="empty">Sin análisis de mercado registrado para este rol.</p>'
        if key == "research" and not rows:
            current = '<p>Revisa propuestas y compara estrategias en simulación prospectiva.</p>'
        review = _details("Evaluación diaria del especialista", evaluation) if evaluation else '<p class="caption">Evaluación diaria pendiente.</p>'
        if isinstance(evaluation, dict):
            findings = evaluation.get("findings", [])
            if isinstance(findings, list) and findings:
                review = '<ul class="agent-notes">'+"".join(f'<li>{_esc(item)}</li>' for item in findings[:3])+'</ul>'+review
        result.append(f'<article class="specialist"><h3>{_esc(label)}</h3>{current}{review}</article>')
    return "".join(result)


def _period_table(daily):
    periods = daily.get("periods", {})
    if not isinstance(periods, dict) or not periods:
        return '<p class="empty">Sin métricas diarias calculadas.</p>'
    rows = []
    for key, label in (("day", "Día cerrado"), ("days7", "7 días"), ("days30", "30 días"), ("all", "Historial completo")):
        period = periods.get(key)
        if not isinstance(period, dict):
            continue
        rows.append(f'<tr><td>{label}</td><td>{_number(period.get("verified_closed_positions"), 0)}</td><td>{_number(period.get("net_pnl"), 4, True)}</td><td>{_number(period.get("profit_factor"))}</td></tr>')
    return '<div class="table-wrap"><table><thead><tr><th>Periodo</th><th>Posiciones verificadas</th><th>Neto USDT</th><th>PF</th></tr></thead><tbody>'+"".join(rows)+'</tbody></table></div>'


def _experiments_summary(experiments):
    if not isinstance(experiments, dict) or not experiments:
        return '<p class="empty">No hay experimentos registrados. Aún no existe evidencia de mejora.</p>'
    rows = []
    for identifier, experiment in experiments.items():
        if not isinstance(experiment, dict):
            continue
        for key, label in (("baseline", "Referencia"), ("candidate", "Candidata")):
            portfolio = experiment.get(key)
            if not isinstance(portfolio, dict):
                continue
            initial, equity = portfolio.get("initial_equity"), portfolio.get("equity")
            net = equity-initial if all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in (initial, equity)) else None
            trades = portfolio.get("trades")
            count = str(len(trades)) if isinstance(trades, list) else "Sin datos"
            rows.append(f'<tr><td>{_esc(identifier)}<br><span class="caption">{_esc(experiment.get("status", "Sin estado"))}</span></td><td>{label}</td><td>{count}</td><td>{_number(net, 4, True)}</td></tr>')
    table = '<div class="table-wrap"><table><thead><tr><th>Experimento</th><th>Estrategia</th><th>Cierres</th><th>PnL total USDT</th></tr></thead><tbody>'+"".join(rows)+'</tbody></table></div>' if rows else ""
    return table+_details("Experimentos y resultados registrados", experiments)


def _audit_table(report):
    audit = report.get("decisions_audit", [])
    rows = []
    for record in reversed(audit[-30:] if isinstance(audit, list) else []):
        if not isinstance(record, dict):
            continue
        status = record.get("decision", record.get("status", "Aprobada" if record.get("allowed") is True else "Vetada" if record.get("allowed") is False else "Registrada"))
        if record.get("buy_decision") in ("COMPRAR", "ESPERAR", "NO_COMPRAR"):
            status = record["buy_decision"] + " · " + str(status)
        detail = record.get("reason", record.get("reasons", record.get("vetos", record.get("vetoes", ""))))
        rows.append(f'<tr><td>{_esc(_time(record.get("ts", record.get("timestamp"))))}</td><td>{_esc(record.get("symbol", "—"))}</td><td>{_esc(status)}</td><td>{_esc(detail)}{_details("Registro de decisión", record)}</td></tr>')
    return '<div class="table-wrap"><table><thead><tr><th>Hora UTC</th><th>Contrato</th><th>Decisión</th><th>Motivo y trazabilidad</th></tr></thead><tbody>'+(''.join(rows) or '<tr><td colspan="4">No hay decisiones auditadas registradas todavía.</td></tr>')+'</tbody></table></div>'


def _news_table(report):
    news = report.get("news")
    if not isinstance(news, dict):
        return '<p class="empty">La ingesta de noticias aún no ha publicado un estado.</p>'
    status = news.get("status", "not_checked")
    stamp = news.get("fetched_at") or "Sin consulta exitosa"
    rows = []
    for article in news.get("headlines", [])[:12] if isinstance(news.get("headlines"), list) else []:
        if not isinstance(article, dict):
            continue
        url = article.get("url", "")
        title = _esc(article.get("title", "Sin título"))
        if isinstance(url, str) and (url.startswith("https://www.coindesk.com/") or url.startswith("https://coindesk.com/")):
            title = f'<a href="{_esc(url)}" rel="noopener noreferrer" target="_blank">{title}</a>'
        symbols = article.get("symbols", [])
        symbols = ", ".join(symbols) if isinstance(symbols, list) and all(isinstance(s, str) for s in symbols) else ""
        rows.append(f'<tr><td>{_esc(article.get("published_at", ""))}</td><td>{title}</td><td>{_esc(symbols or "Mercado general / sin vínculo directo")}</td></tr>')
    table = '<div class="table-wrap"><table><thead><tr><th>Publicación UTC</th><th>Titular y fuente</th><th>Contratos mencionados</th></tr></thead><tbody>'+(''.join(rows) or '<tr><td colspan="3">Sin titulares recientes registrados.</td></tr>')+'</tbody></table></div>'
    return f'<p class="caption">Fuente: CoinDesk RSS · estado: {_esc(status)} · última consulta exitosa: {_esc(stamp)}. Un titular no confirma un hecho ni genera una orden.</p>'+table


def write_report(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() != ".html":
        _atomic_write(path, json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        return
    daily = report.get("daily_review", {})
    daily = daily if isinstance(daily, dict) else {}
    schedule = report.get("daily_schedule", {})
    schedule = schedule if isinstance(schedule, dict) else {}
    strategy = report.get("strategy", {})
    strategy = strategy if isinstance(strategy, dict) else {}
    age = report.get("cycle_age_seconds")
    cycle_status = "Sin ciclo registrado" if age is None else "Último ciclo correcto" if report["healthy"] else "Último ciclo con error"
    recency = "Sin ciclo registrado" if age is None else "Datos recientes" if age <= 180 else "Sin actualización reciente"
    mode_label = {"paper": "PAPER · simulación local", "demo": "DEMO · órdenes virtuales Binance"}.get(report["mode"], "Modo sin identificar")
    operation = report["halt_reason"] or ("Pausa diaria de entradas" if report["daily_paused"] else "Sin pausa registrada")
    cards = [("Equidad", f'{_number(report["equity"])} USDT'), ("Beneficio neto total", f'{_number(report["net_pnl"], signed=True)} USDT'),
             ("Operaciones cerradas", report["closed_trades"]), ("Caída desde máximo", f'{_number(report["drawdown_pct"])}%')]
    card_html = "".join(f'<article class="metric"><span>{_esc(k)}</span><strong>{_esc(v)}</strong></article>' for k,v in cards)
    rows = "".join(f'<tr><td>{_esc(_time(t.get("closed_at")))}</td><td>{_esc(t["symbol"])}</td><td>{"LONG" if t["direction"]==1 else "SHORT"}</td>'
                   f'<td>{_number(t.get("gross_pnl"), 4, True)}</td><td>{_number(t.get("fees"), 4)}</td><td>{_number(t.get("funding"), 4, True)}</td>'
                   f'<td>{_number(t["net_pnl"], 4, True)}</td><td>{_esc(t["reason"])}</td></tr>' for t in reversed(report["recent_trades"]))
    next_review = schedule.get("next_review_at", daily.get("next_review_at", daily.get("next_run_at")))
    review_date = daily.get("date", daily.get("last_review_date", "Pendiente"))
    decision = daily.get("decision", daily.get("status", "Sin evaluación diaria registrada"))
    body = f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Final Boss · Equipo de trading y revisión diaria</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f3f5f4;color:#182b29;font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}main{{max-width:1440px;margin:auto;padding:28px}}
header{{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:24px}}h1{{font-size:clamp(24px,4vw,38px);letter-spacing:-.035em;margin:4px 0}}h2{{font-size:20px;margin:0 0 14px}}h3{{font-size:16px;margin:0 0 12px}}p{{margin:10px 0}}.eyebrow{{font-size:12px;font-weight:700;letter-spacing:.13em;color:#4e665d}}.badge{{background:#dceee5;color:#115641;border:1px solid #afcdbe;padding:7px 12px;border-radius:20px;font-size:13px;font-weight:650;white-space:nowrap}}
.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}}.metric,.panel,.specialist{{background:white;border:1px solid #d8e1dc;border-radius:12px}}.metric{{padding:20px}}.metric span,.caption,.empty{{color:#536962;font-size:13px}}.metric strong{{display:block;font-size:clamp(21px,3vw,29px);margin-top:8px;letter-spacing:-.025em}}.panel{{padding:22px;margin-top:18px;min-width:0}}.columns{{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:18px}}.team{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}.specialist{{padding:18px;background:#fbfcfb;min-width:0}}.agent-notes{{padding-left:18px;margin:0;font-size:13px}}.agent-notes li{{margin-bottom:9px}}.state-line{{padding:13px 18px;background:#e8eee9;border-radius:8px;margin-top:15px}}.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:12px 10px;border-bottom:1px solid #e0e7e2;vertical-align:top}}th{{color:#536962;font-weight:650}}td{{overflow-wrap:anywhere}}.equity-chart{{width:100%;display:block;min-height:160px}}.equity-chart text{{font:11px system-ui;fill:#536962}}.gridline{{stroke:#e1e8e3;stroke-width:1}}details{{margin-top:12px}}summary{{cursor:pointer;color:#17624e;font-size:13px}}pre{{font:12px/1.5 ui-monospace,Consolas,monospace;background:#f0f4f1;border-radius:6px;padding:12px;max-height:360px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}}.facts{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.facts div{{padding:8px 0}}.facts span{{display:block;color:#536962;font-size:12px}}footer{{margin-top:22px;color:#536962;font-size:12px}}.warning{{border-left:3px solid #a57021;padding-left:12px}}a{{color:#17624e}}
@media(max-width:950px){{.columns{{grid-template-columns:1fr}}.team{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}@media(max-width:600px){{main{{padding:14px}}header{{align-items:flex-start;flex-direction:column;gap:8px}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}.metric{{padding:14px}}.panel{{padding:16px}}.team{{grid-template-columns:1fr}}.metric strong{{font-size:21px}}.facts{{grid-template-columns:1fr}}}}
.refresh-controls{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}.refresh-controls button{{font:inherit;background:#fff;border:1px solid #9bb5a9;border-radius:6px;padding:6px 12px;cursor:pointer}}.refresh-controls label{{font-size:13px}}#recency-status[data-state="recent"]{{color:#115641}}#recency-status[data-state="stale"]{{color:#865513}}
</style></head><body><main>
<header><div><div class="eyebrow">FINAL BOSS / BINANCE FUTURES DEMO</div><h1>Equipo de trading y revisión diaria</h1><p class="caption">Coordinador + seis especialistas · apalancamiento {_esc(report.get("leverage") or "Sin datos")}x</p></div><span class="badge">{_esc(mode_label)}</span></header>
<p class="caption"><a href="http://127.0.0.1:{8765 if report['mode'] == 'paper' else 8766}/">Abrir el panel de agentes en vivo en este PC</a> · Este archivo conserva el informe exportado.</p>
<div class="metrics">{card_html}</div>
<div class="state-line"><b id="recency-status">{_esc(recency)}</b> · antigüedad del ciclo: <span id="cycle-age">{_number(age, 0)+" s" if age is not None else "Sin datos"}</span>
<p><b>Estado registrado:</b> {_esc(operation)} · {_esc(cycle_status)}</p>
<p class="caption">Último ciclo: <time id="last-cycle" datetime="{_esc(report["last_cycle"] or "")}">{_esc(_colombia_time(report["last_cycle"]))}</time> Colombia (UTC−5). La recencia describe este informe; no verifica que el proceso siga activo.</p>
<div class="refresh-controls"><button id="refresh-now" type="button">Actualizar ahora</button><label><input id="refresh-auto" type="checkbox" checked> Recargar cada 30 segundos</label><span id="refresh-countdown" class="caption">Recarga automática cada 30 s</span></div>
<noscript><p class="caption">JavaScript está desactivado: recarga con F5. La antigüedad mostrada se calculó al generar este archivo.</p></noscript></div>
<div class="columns"><section class="panel"><h2>Equidad observada</h2>{_equity_chart(report.get("equity_history", []))}
<div class="facts"><div><span>Balance realizado</span>{_number(report["balance"])} USDT</div><div><span>PnL abierto</span>{_number(report["unrealized_pnl"], signed=True)} USDT</div><div><span>Profit factor de operaciones cerradas</span>{_number(report["profit_factor"])}</div><div><span>Expectativa por cierre</span>{_number(report["expectancy_usdt"], 4, True)} USDT</div></div>
<p class="caption warning">{_esc(report["validation"])}. {_esc(report["note"])}</p></section>
<section class="panel"><h2>Revisión diaria y versión activa</h2><div class="facts"><div><span>Día evaluado en Colombia</span>{_esc(review_date)}</div><div><span>Próxima revisión</span>{_esc(_time(next_review)) if next_review is not None else "00:10 Colombia · próxima fecha pendiente"}</div><div><span>Versión activa</span>{_esc(strategy.get("version", "Original · sin versión registrada"))}</div><div><span>Decisión registrada</span>{_esc(decision) if not isinstance(decision, (dict, list)) else "Ver evaluación detallada"}</div></div>
<p class="caption">La revisión considera el día cerrado, 7 días, 30 días e historial completo. Un cambio propuesto no equivale a una mejora validada.</p>{_period_table(daily)}{_details("Programación y estado de la revisión", schedule)}{_details("Informe de revisión diaria", daily)}{_details("Versión, ajustes e historial", strategy)}
</section></div>
<section class="panel"><h2>Los seis especialistas</h2><p class="caption">Análisis registrados por los módulos del motor. Sus puntuaciones no son probabilidades de ganar.</p><div class="team">{_specialist_cards(report)}</div></section>
<section class="panel"><h2>Noticias consultadas</h2>{_news_table(report)}</section>
<div class="columns"><section class="panel"><h2>Validación prospectiva</h2><p>La estrategia de referencia y la candidata se comparan en simulaciones paralelas con datos nuevos y costes contabilizados.</p>
{_experiments_summary(report.get("experiments", {}))}
<p class="caption">El estado de un experimento, su muestra y sus resultados deben revisarse juntos. La promoción está sujeta a los controles del motor.</p></section>
<section class="panel"><h2>Riesgo, contabilidad y posiciones</h2><div class="facts"><div><span>Posiciones abiertas</span>{len(report["positions"])}</div><div><span>Órdenes pendientes</span>{report["pending_orders"]}</div><div><span>Multiplicador de riesgo</span>{_number(report["risk_multiplier"])}</div><div><span>Resultado realizado neto</span>{_number(report["realized_net_pnl"], signed=True)} USDT</div></div><p class="caption">{_esc(report["funding_note"])}</p>{_details("Posiciones registradas", report["positions"])}{_details("Ajustes de riesgo registrados", report["adaptations"])}{_details("Errores de datos de mercado", report["market_errors"])}</section></div>
<section class="panel"><h2>Aprobaciones, descartes y vetos</h2><p class="caption">Últimas 30 decisiones registradas. Una aprobación de señal no confirma la ejecución de una orden.</p>{_audit_table(report)}</section>
<section class="panel"><h2>Operaciones cerradas recientes</h2><p class="caption">Hasta 50 cierres · todos los importes en USDT. Neto = bruto − comisiones + financiación.</p><div class="table-wrap"><table><thead><tr><th>Cierre UTC</th><th>Contrato</th><th>Dirección</th><th>Bruto</th><th>Comisiones</th><th>Financiación</th><th>Neto</th><th>Salida</th></tr></thead><tbody>
{rows or '<tr><td colspan="8">Aún no hay operaciones cerradas.</td></tr>'}</tbody></table></div></section>
<footer>Informe estático generado <time datetime="{_esc(report["generated_at"])}">{_esc(_colombia_time(report["generated_at"]))}</time> Colombia (UTC−5). El motor publica una versión al completar cada ciclo; este panel vuelve a cargar el archivo cada 30 segundos mientras la opción esté activa. Puedes pausarla para leer. Para seguimiento continuo abre <code>reports/{_esc(report["mode"])}.html</code>; las copias exportadas no son actualizadas por el motor. No se conecta a servicios externos desde este panel.</footer>
</main><script id="report-browser-controls">{REPORT_BROWSER_SCRIPT}</script></body></html>'''
    _atomic_write(path, body)
