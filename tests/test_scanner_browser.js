const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const test=require('node:test');
const html=fs.readFileSync(require('node:path').join(__dirname,'../bot/scanner.html'),'utf8');
const code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const ctx=vm.createContext({URL,Date});vm.runInContext(code,ctx);const ui=vm.runInContext('ScannerUI',ctx);
test('stale, disconnected, failed and future data remove entry signal',()=>{
  const row={decision:'ENTRADA_CONDICIONAL',observed_at:1000};
  const data={completed_at:1000,status:'ok'};
  assert.equal(ui.effectiveDecision(row,data,1001,true),'ENTRADA_CONDICIONAL');
  assert.equal(ui.effectiveDecision(row,data,1901,true),'ESPERAR');
  assert.equal(ui.effectiveDecision(row,data,999,true),'ESPERAR');
  assert.equal(ui.effectiveDecision(row,data,1001,false),'ESPERAR');
  assert.equal(ui.effectiveDecision(row,{...data,status:'error'},1001,true),'ESPERAR');
});
test('links cannot navigate to javascript or lookalike market hosts',()=>{
  for(const url of ['javascript:alert(1)','https://dexscreener.com.evil.test/x','https://user:password@www.binance.com/','http://www.binance.com'])assert.equal(ui.safeLink(url),null);
  assert.ok(ui.safeLink('https://dexscreener.com/solana/ABC'));
});
test('filters support token address and effective stale decision',()=>{
  const rows=[{symbol:'NEW',source:'DEX Screener',chain:'solana',address:'abc123',decision:'INVESTIGAR',observed_at:1000},{symbol:'ABCUSDT',source:'Binance Spot',decision:'ENTRADA_CONDICIONAL',observed_at:1000}];
  assert.equal(ui.matching(rows,'abc123','DEX Screener').length,1);
  assert.equal(ui.matching(rows,'','Binance Spot','ESPERAR',{completed_at:1000,status:'ok'},1901,true).length,1);
});
test('scanner page contains no trading controls, remote scripts or unsafe HTML insertion',()=>{
  assert.ok(html.includes('SIN TRADES'));
  assert.equal(code.includes('innerHTML'),false);
  assert.equal(/<script[^>]+src=/.test(html),false);
  assert.equal(/method\s*:\s*['"](?:POST|PUT|DELETE)['"]/.test(code),false);
});
test('each directional entry fails closed for missing, stale or unavailable observations',()=>{
  for(const decision of ['SPOT_CONDICIONAL','LONG_CONDICIONAL','SHORT_CONDICIONAL']){
    const row={decision,observed_at:1000,analysis_ok:true};
    const data={completed_at:1000,status:'ok'};
    assert.equal(ui.isEntry(decision),true);
    assert.equal(ui.effectiveDecision(row,data,1001,true),decision);
    for(const invalid of [{...row,analysis_ok:false},{...row,observed_at:undefined},{...row,observed_at:0}])
      assert.equal(ui.effectiveDecision(invalid,data,1001,true),'ESPERAR');
    assert.equal(ui.effectiveDecision(row,{...data,completed_at:undefined},1001,true),'ESPERAR');
    assert.equal(ui.effectiveDecision(row,data,1901,true),'ESPERAR');
    assert.equal(ui.effectiveDecision(row,data,1001,false),'ESPERAR');
  }
});
test('major names and market filters preserve independent spot and futures decisions',()=>{
  const data={completed_at:1000,status:'ok'};
  const rows=[{symbol:'ETHUSDT',asset_name:'Ethereum',source:'Binance Spot',decision:'SPOT_CONDICIONAL',observed_at:1000},
              {symbol:'ETHUSDT',asset_name:'Ethereum',source:'Binance Futures',decision:'SHORT_CONDICIONAL',observed_at:1000}];
  assert.equal(ui.matching(rows,'ethereum','','',data,1001,true).length,2);
  assert.equal(ui.matching(rows,'ETH','Binance Futures','SHORT_CONDICIONAL',data,1001,true).length,1);
  assert.equal(ui.matching(rows,'ETH','Binance Spot','SHORT_CONDICIONAL',data,1001,true).length,0);
});
test('chart geometry handles flat and tiny prices and rejects invalid candles',()=>{
  for(const price of [100,0.00000001]){
    const points=[{open:price,high:price,low:price,close:price,volume:0,time:1000}];
    const g=ui.chartGeometry(points,[{label:'entry',value:price},{label:'invalid',value:price*2}]);
    assert.ok(g.max>g.min);
    assert.ok(Number.isFinite(g.y(price)));
    assert.equal(g.levels.length,1);
  }
  assert.equal(ui.chartGeometry([{open:2,high:1,low:1,close:2,volume:1,time:1000}]),null);
  const overlay=ui.chartGeometry([{open:100,high:101,low:99,close:100,volume:1,time:1000,ema20:105,vwap:90}]);
  for(const value of [90,105])assert.ok(overlay.y(value)>=18&&overlay.y(value)<=198);
});
test('discovery sorting keeps missing and ineligible evidence behind valid activity',()=>{
  const rows=[{id:'missing'},{id:'low',discovery:{score:99,eligible:false}},{id:'valid',discovery:{score:20,relative_btc_pp:3,eligible:true}}];
  assert.equal(ui.sortedRows(rows,'activity')[0].id,'valid');
  assert.equal(ui.sortedRows(rows,'relative')[0].id,'valid');
  assert.equal(ui.sortedRows(rows,'fixed'),rows);
});

const outlookNow=Date.UTC(2026,8,29,4,30)/1000;
const outlookSnapshot={completed_at:outlookNow,status:'ok'};
function trendRow(state='bullish',market='spot'){
  const prices=state==='bullish'?{ema20:105,ema50:100,close:110}:{ema20:95,ema50:100,close:90};
  return {market,source:market==='spot'?'Binance Spot':'Binance Futures',symbol:'BTCUSDT',bias:'spot',analysis_ok:true,observed_at:outlookNow-60,price:prices.close,
    higher_trends:Object.fromEntries([['1h',3600],['4h',14400],['1d',86400]].map(([tf,seconds])=>[tf,{...prices,candle_at:outlookNow-seconds/2}]))};
}
test('continuity uses market evidence even for bearish spot and ignores entry bias',()=>{
  assert.equal(ui.trendOutlook(trendRow(),outlookSnapshot,outlookNow).state,'bullish');
  const bearish=trendRow('bearish');
  assert.equal(ui.trendOutlook(bearish,outlookSnapshot,outlookNow).state,'bearish');
  assert.equal(bearish.bias,'spot');
  const neutral=trendRow();neutral.price=104;
  assert.equal(ui.trendOutlook(neutral,outlookSnapshot,outlookNow).state,'neutral');
});
test('conflicting daily direction prevents a single continuation headline',()=>{
  const row=trendRow();
  row.higher_trends['1d']={ema20:115,ema50:120,close:110,candle_at:outlookNow-43200};
  const result=ui.trendOutlook(row,outlookSnapshot,outlookNow);
  assert.equal(result.state,'mixed');
  assert.equal(result.horizons[2].state,'bearish');
});
test('review windows anchor to observation and remain stable on browser refresh',()=>{
  const row=trendRow(),result=ui.trendOutlook(row,outlookSnapshot,outlookNow);
  assert.deepEqual(Array.from(result.horizons,h=>h.until),[3600,14400,86400].map(s=>row.observed_at+s));
  assert.deepEqual(Array.from(ui.trendOutlook(row,outlookSnapshot,outlookNow+300).horizons,h=>h.until),Array.from(result.horizons,h=>h.until));
  assert.match(ui.outlookDate(row.observed_at),/28.*2026.*23:29/);
  assert.match(ui.outlookDate(result.horizons[0].until),/29.*2026.*00:29/);
});
test('unavailable or stale observations remove continuity and future review dates',()=>{
  const row=trendRow();
  for(const [r,s,now,connected] of [
    [row,outlookSnapshot,outlookNow,false],
    [row,{...outlookSnapshot,status:'error'},outlookNow,true],
    [row,outlookSnapshot,outlookNow+901,true],
    [{...row,observed_at:outlookNow+1},outlookSnapshot,outlookNow,true],
    [row,{...outlookSnapshot,completed_at:outlookNow+1},outlookNow,true],
    [{...row,analysis_ok:false},outlookSnapshot,outlookNow,true],
    [{...row,price:null},outlookSnapshot,outlookNow,true],
    [{...row,price:0},outlookSnapshot,outlookNow,true],
    [{...row,observed_at:undefined},outlookSnapshot,outlookNow,true]
  ]){
    const result=ui.trendOutlook(r,s,now,connected);
    assert.equal(result.state,'unknown');assert.equal(result.horizons.length,0);
  }
});
test('each horizon rejects missing, stale, future and invalid technical context',()=>{
  for(const invalid of [undefined,{ema20:105,ema50:null,close:110,candle_at:outlookNow-100},
    {ema20:0,ema50:100,close:110,candle_at:outlookNow-100},
    {ema20:105,ema50:100,close:110,candle_at:outlookNow+1},
    {ema20:105,ema50:100,close:110,candle_at:outlookNow-4501}]){
    const row=trendRow();row.higher_trends['1h']=invalid;
    const result=ui.trendOutlook(row,outlookSnapshot,outlookNow);
    assert.equal(result.state,'unknown');assert.equal(result.horizons[0].until,null);
    assert.equal(result.horizons[1].state,'bullish');
  }
});
test('spot and futures continuity remain independent and DEX gets no invented dates',()=>{
  assert.equal(ui.trendOutlook(trendRow('bullish','spot'),outlookSnapshot,outlookNow).state,'bullish');
  assert.equal(ui.trendOutlook(trendRow('bearish','futures'),outlookSnapshot,outlookNow).state,'bearish');
  const dex=ui.trendOutlook({...trendRow(),market:'dex',change_24h:500},outlookSnapshot,outlookNow);
  assert.equal(dex.state,'unknown');assert.equal(dex.horizons.length,0);
});
