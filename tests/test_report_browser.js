// Tests the panel clock and reload controls without opening a browser.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { execFileSync } = require('node:child_process');
const { resolve } = require('node:path');
const vm = require('node:vm');

const script = JSON.parse(execFileSync('python', ['-c',
  'import json; from bot.report import REPORT_BROWSER_SCRIPT; print(json.dumps(REPORT_BROWSER_SCRIPT))'],
  { cwd: resolve(__dirname, '..'), encoding: 'utf8' }));
const timestamp = Date.parse('2026-09-07T21:00:00Z');

function panel({ heartbeat = new Date(timestamp).toISOString(), now = timestamp + 20000,
                 paused = false, blockedStorage = false } = {}) {
  let current = now, reloads = 0, tick, preference = paused ? 'paused' : null;
  const nodes = Object.fromEntries(['last-cycle', 'cycle-age', 'recency-status',
    'refresh-auto', 'refresh-countdown', 'refresh-now'].map(id => [id, {
      textContent: '', dataset: {}, checked: true, listeners: {},
      getAttribute(name) { return name === 'datetime' ? heartbeat : null; },
      addEventListener(name, callback) { this.listeners[name] = callback; }
    }]));
  vm.runInNewContext(script, {
    Date: { now: () => current, parse: Date.parse },
    document: { getElementById: id => nodes[id] },
    window: {
      location: { pathname: '/reports/paper.html', reload() { reloads++; } },
      sessionStorage: {
        getItem() { if (blockedStorage) throw Error('Unavailable'); return preference; },
        setItem(_key, value) { if (blockedStorage) throw Error('Unavailable'); preference = value; }
      },
      setInterval(callback, delay) { assert.equal(delay, 1000); tick = callback; }
    }
  });
  return { nodes, get reloads() { return reloads; }, get preference() { return preference; },
    advance(milliseconds) { current += milliseconds; tick(); },
    changeAutomatic(enabled) {
      nodes['refresh-auto'].checked = enabled;
      nodes['refresh-auto'].listeners.change();
    },
    refresh() { nodes['refresh-now'].listeners.click(); }
  };
}

test('a previously healthy snapshot ages into a stale report', () => {
  const p = panel({ paused: true });
  assert.equal(p.nodes['cycle-age'].textContent, '20 s');
  assert.equal(p.nodes['recency-status'].dataset.state, 'recent');
  p.advance(160000);
  assert.equal(p.nodes['recency-status'].dataset.state, 'recent');
  p.advance(1000);
  assert.equal(p.nodes['cycle-age'].textContent, '181 s');
  assert.equal(p.nodes['recency-status'].textContent, 'Sin actualización reciente');
  assert.equal(p.nodes['recency-status'].dataset.state, 'stale');
});

test('automatic reload runs every thirty seconds without resetting heartbeat age', () => {
  const p = panel();
  p.advance(29999);
  assert.equal(p.reloads, 0);
  p.advance(1);
  assert.equal(p.reloads, 1);
  p.advance(1000);
  assert.equal(p.reloads, 1);
  p.advance(150000);
  assert.equal(p.nodes['recency-status'].dataset.state, 'stale');
  assert.equal(p.nodes['cycle-age'].textContent, '201 s');
});

test('pausing and resuming reload preserves a working age counter', () => {
  const p = panel();
  p.changeAutomatic(false);
  assert.equal(p.preference, 'paused');
  p.advance(60000);
  assert.equal(p.reloads, 0);
  assert.equal(p.nodes['cycle-age'].textContent, '80 s');
  p.changeAutomatic(true);
  assert.equal(p.preference, 'automatic');
  p.advance(29999);
  assert.equal(p.reloads, 0);
  p.advance(1);
  assert.equal(p.reloads, 1);
});

test('manual reload remains available with automatic reload paused', () => {
  const p = panel({ paused: true });
  p.refresh();
  assert.equal(p.reloads, 1);
  assert.equal(p.nodes['refresh-auto'].checked, false);
});

test('missing, invalid, and future heartbeat dates never appear recent', () => {
  for (const heartbeat of ['', 'invalid']) {
    const p = panel({ heartbeat });
    assert.equal(p.nodes['recency-status'].textContent, 'Sin ciclo registrado');
    assert.equal(p.nodes['recency-status'].dataset.state, 'unknown');
  }
  const p = panel({ heartbeat: new Date(timestamp + 100000).toISOString() });
  assert.equal(p.nodes['recency-status'].textContent, 'Fecha del ciclo adelantada');
  assert.equal(p.nodes['recency-status'].dataset.state, 'stale');
});

test('unavailable file-page storage does not break controls or freshness checks', () => {
  const p = panel({ blockedStorage: true });
  assert.equal(p.nodes['refresh-auto'].checked, true);
  p.changeAutomatic(false);
  p.advance(200000);
  assert.equal(p.reloads, 0);
  assert.equal(p.nodes['recency-status'].dataset.state, 'stale');
  p.refresh();
  assert.equal(p.reloads, 1);
});
