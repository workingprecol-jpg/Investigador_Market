// Exercises the actual standalone dashboard script without a browser or network.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../bot/dashboard.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const baseTime = Date.parse('2026-09-07T21:00:00Z');

function core() {
  const sandbox = {window: {}};
  vm.runInNewContext(script, sandbox);
  return sandbox.window.FinalBossUI;
}

function report() {
  return {mode: 'paper', equity: 101.25, balance: 101, initial_equity: 100,
    positions: {}, closed_trades: 2, net_pnl: 1.25, net_return_pct: 1.25,
    last_cycle: new Date(baseTime-20000).toISOString(), server_time: new Date(baseTime).toISOString(),
    telemetry_available: true, runtime: {telemetry_fresh: true, cycle_fresh: true},
    agent_activity: {trend: {status: 'running', task: 'Analizando ETHUSDT',
      symbol: 'ETHUSDT', updated_at: (baseTime-1000)/1000, last_result: {reason: 'EMA alineadas'}}},
    recent_trades: [
      {symbol: 'ETHUSDT', direction: 1, net_pnl: 2, reason: 'target', closed_at: baseTime/1000},
      {symbol: 'DOGEUSDT', direction: -1, net_pnl: -.75, reason: 'stop', closed_at: baseTime/1000}
    ],
    market_data: {}, agent_events: [], equity_history: [], daily_schedule: {}, daily_review: {}};
}

test('a server timestamp never substitutes for a fresh bot heartbeat', () => {
  const ui = core(), r = report();
  assert.equal(ui.freshness(r, baseTime).fresh, true);
  assert.equal(ui.freshness(r, baseTime+160001).fresh, false);
  r.last_cycle = null;
  assert.equal(ui.freshness(r, baseTime).reason, 'Sin ciclo registrado');
  r.last_cycle = new Date(baseTime+6000).toISOString();
  assert.equal(ui.freshness(r, baseTime).fresh, false);
});

test('an agent can show running only with complete fresh runtime evidence', () => {
  const ui = core(), r = report();
  assert.equal(ui.normalizeAgent(r, 'trend', baseTime).status, 'running');
  assert.equal(ui.normalizeAgent(r, 'trend', baseTime, false).status, 'stale');
  assert.equal(ui.normalizeAgent(r, 'trend', baseTime+180001).status, 'stale');
  r.runtime.telemetry_fresh = false;
  assert.equal(ui.normalizeAgent(r, 'trend', baseTime).status, 'stale');
  delete r.runtime.telemetry_fresh;
  assert.equal(ui.normalizeAgent(r, 'trend', baseTime).status, 'stale');
  assert.equal(ui.normalizeAgent(r, 'research_validation', baseTime).status, 'waiting');
});

test('invalid financial snapshots and invalid candles are not accepted', () => {
  const ui = core(), r = report();
  assert.equal(ui.validateReport(r), true);
  for (const patch of [{equity: Infinity}, {balance: '101'}, {initial_equity: 0},
                        {positions: []}, {mode: 'real'}, {closed_trades: -1}]) {
    assert.equal(ui.validateReport({...r, ...patch}), false);
  }
  const candle = {open: 100, high: 103, low: 99, close: 102, open_time: baseTime};
  assert.equal(ui.validCandles([candle, {...candle, high: 90}, {...candle, open: NaN}]).length, 1);
  assert.equal(ui.price(null), '—');
});

class Element {
  constructor(tag='div') {
    this.tagName=tag; this.children=[]; this.attributes={}; this.dataset={}; this.style={};
    this.listeners={}; this.value=''; this.checked=false; this.hidden=false; this._text='';
    const classes=new Set();
    this.classList={toggle(name,on){if(on)classes.add(name);else classes.delete(name);}, contains:name=>classes.has(name)};
  }
  get childNodes(){return this.children;}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  set textContent(value){this._text=String(value);this.children=[];}
  append(...items){this.children.push(...items);}
  insertBefore(item,before){const index=this.children.indexOf(before);this.children.splice(index<0?this.children.length:index,0,item);}
  replaceChildren(...items){this._text='';this.children=[];this.append(...items);}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  getBoundingClientRect(){return {top:500};}
}

async function dashboard(initial=report()) {
  const nodes=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
  nodes['event-filter'].value='all'; nodes['history-filter'].value='all';
  let response=initial, failed=false, current=baseTime;
  const intervals=[], preferences=new Map();
  class Clock extends Date {static now(){return current;}}
  const sandbox={Date:Clock,Intl,AbortController,window:{
    sessionStorage:{getItem:key=>preferences.get(key),setItem:(key,value)=>preferences.set(key,value)},
    setTimeout:()=>1,clearTimeout:()=>{},setInterval:(callback,delay)=>intervals.push({callback,delay}),addEventListener:()=>{}
  },document:{getElementById:id=>nodes[id],createElement:tag=>new Element(tag),
    createElementNS:(_namespace,tag)=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),
    querySelectorAll:()=>[]},fetch:async()=>{if(failed)throw Error('offline');return {ok:true,json:async()=>response};}};
  vm.runInNewContext(script,sandbox);
  await new Promise(resolve=>setImmediate(resolve));
  return {nodes,preferences,ui:sandbox.window.FinalBossUI,
    async reload(value,fail=false){response=value;failed=fail;await nodes['refresh-button'].listeners.click();},
    pause(){nodes['auto-refresh'].checked=false;nodes['auto-refresh'].listeners.change();},
    advance(ms){current+=ms;intervals.filter(i=>i.delay===1000).forEach(i=>i.callback());}};
}

test('first render creates six specialists, a coordinator and actual financial values',async()=>{
  const panel=await dashboard();
  assert.equal(panel.nodes['agent-grid'].children.length,6);
  assert.match(panel.nodes['equity-value'].textContent,/101[,.]25/);
  assert.equal(panel.nodes['mode-badge'].textContent,'PAPER · SIMULACIÓN LOCAL');
  assert.equal(panel.nodes['connection'].dataset.tone,'recent');
  assert.equal(panel.nodes['agent-grid'].children[0].dataset.status,'running');
});

test('disconnect and invalid responses retain last values but remove running status',async()=>{
  const panel=await dashboard();
  const lastValue=panel.nodes['equity-value'].textContent;
  await panel.reload(null,true);
  assert.equal(panel.nodes['equity-value'].textContent,lastValue);
  assert.equal(panel.nodes['connection'].dataset.tone,'error');
  assert.equal(panel.nodes['agent-grid'].children[0].dataset.status,'stale');
  await panel.reload({mode:'real',equity:100000});
  assert.equal(panel.nodes['equity-value'].textContent,lastValue);
  assert.equal(panel.nodes['agent-grid'].children[0].dataset.status,'stale');
});

test('pausing only freezes panel updates and preserves the chosen preference',async()=>{
  const panel=await dashboard();
  panel.pause();
  assert.equal(panel.nodes['connection'].dataset.tone,'paused');
  assert.equal(panel.nodes['agent-grid'].children[0].dataset.status,'stale');
  assert.equal(panel.preferences.get('finalboss-dashboard-paused'),'yes');
  panel.advance(200000);
  assert.equal(panel.nodes['connection'].dataset.tone,'paused');
});

test('a running daily review loses its live badge when paused, disconnected or aged',async()=>{
  const runningReview=()=>{
    const r=report();
    r.daily_schedule={running:true};
    r.agent_activity.research_validation={status:'running',task:'Revisión diaria',updated_at:(baseTime-1000)/1000};
    return r;
  };
  for(const transition of ['pause','disconnect','age']){
    const panel=await dashboard(runningReview());
    assert.equal(panel.nodes['review-status'].dataset.status,'running',transition);
    if(transition==='pause')panel.pause();
    if(transition==='disconnect')await panel.reload(null,true);
    if(transition==='age')panel.advance(180001);
    assert.equal(panel.nodes['review-status'].dataset.status,'stale',transition);
    assert.equal(panel.nodes['review-status'].textContent,'Sin dato reciente',transition);
  }
  const missingEvidence=runningReview();
  delete missingEvidence.agent_activity.research_validation;
  const panel=await dashboard(missingEvidence);
  assert.equal(panel.nodes['review-status'].dataset.status,'stale');
});

test('history filters work and untrusted result strings become text nodes',async()=>{
  const r=report(), payload='<img src=x onerror="steal()">';
  r.agent_activity.trend.last_result.reason=payload;
  const panel=await dashboard(r);
  assert.match(panel.nodes['agent-grid'].children[0].textContent,/<img src=x/);
  assert.equal(panel.nodes['agent-grid'].children[0].children.some(n=>n.tagName==='img'),false);
  panel.nodes['history-filter'].value='ETHUSDT';
  panel.nodes['history-filter'].listeners.change();
  assert.match(panel.nodes['history-body'].textContent,/ETHUSDT/);
  assert.doesNotMatch(panel.nodes['history-body'].textContent,/DOGEUSDT/);
  assert.doesNotMatch(script,/\.innerHTML\s*=/);
});

test('news evidence is linked to the selected symbol and stale decisions become wait',async()=>{
  const r=report();
  r.news={status:'ok',source_ok:true,fetched_at:new Date(baseTime-1000).toISOString(),headlines:[
    {title:'Ethereum upgrade',url:'https://www.coindesk.com/markets/ethereum',symbols:['ETHUSDT'],published_at:new Date(baseTime-3000).toISOString()},
    {title:'<img src=x>',url:'javascript:alert(1)',symbols:['SOLUSDT'],published_at:new Date(baseTime-4000).toISOString()}
  ]};
  r.last_analysis={ETHUSDT:{buy_decision:'COMPRAR',execution:{reason:'Señal aprobada'}}};
  const panel=await dashboard(r);
  assert.match(panel.nodes['buy-guidance'].textContent,/ETHUSDT · COMPRAR/);
  assert.match(panel.nodes['news-body'].textContent,/Menciona ETHUSDT/);
  assert.match(panel.nodes['news-body'].textContent,/Sin vínculo directo con ETHUSDT/);
  const rows=panel.nodes['news-body'].children;
  assert.equal(rows[0].children[1].children[0].tagName,'a');
  assert.equal(rows[1].children[1].children.length,0);
  r.news.status='unavailable';
  r.news.source_ok=false;
  await panel.reload(r);
  assert.match(panel.nodes['buy-guidance'].textContent,/ESPERAR/);
  r.news.status='ok';
  r.news.source_ok=true;
  await panel.reload(r);
  r.healthy=false;
  await panel.reload(r);
  assert.match(panel.nodes['buy-guidance'].textContent,/ESPERAR/);
  r.healthy=true;
  await panel.reload(r);
  r.news.status='disabled';
  r.news.source_ok=false;
  await panel.reload(r);
  assert.match(panel.nodes['news-status'].textContent,/Noticias desactivadas/);
  r.news.status='ok';
  r.news.source_ok=true;
  await panel.reload(r);
  panel.advance(181000);
  assert.match(panel.nodes['buy-guidance'].textContent,/ESPERAR/);
});

test('the application is light only and performs no trading requests',()=>{
  assert.match(html,/color-scheme:light/);
  assert.doesNotMatch(html,/prefers-color-scheme|data-theme|dark-mode/i);
  assert.match(script,/fetch\('\/api\/dashboard'/);
  assert.doesNotMatch(script,/method:\s*['"](?:POST|PUT|PATCH|DELETE)/);
  assert.doesNotMatch(html,/<(?:script|link)[^>]+(?:src|href)="https?:/);
});
