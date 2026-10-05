/* Smoke test: boot app.js (layout shell + panels) under a DOM stub.
   Live mode: fetch is stubbed to the real collector at 127.0.0.1:8792,
   so panels are asserted against live payloads. Then a co-process boots
   the same app with ?demo and the panels are asserted there too.
   Run: node smoke/shell.mjs */
import { spawn } from 'node:child_process';

const MODE = process.env.SPECULUM_SMOKE_MODE || 'live'; // 'live' | 'demo'

/* --- DOM stub ---------------------------------------------------------------- */
const docById = new Map();
class ClassList {
  constructor(el) { this.el = el; }
  add(...c) { for (const x of c) if (!this.el._cls.has(x)) this.el._cls.add(x); }
  remove(...c) { for (const x of c) this.el._cls.delete(x); }
  contains(c) { return this.el._cls.has(c); }
  toggle(c, force) {
    const on = force != null ? force : !this.el._cls.has(c);
    on ? this.el._cls.add(c) : this.el._cls.delete(c);
    return on;
  }
}
class FakeEl {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = tag; this.children = []; this._cls = new Set();
    this._attrs = new Map(); this._listeners = {}; this._data = {};
    this.style = {};
    this.hidden = false;
    this.parentNode = null;
    this.offsetWidth = 100;
    this.clientWidth = 0; this.clientHeight = 0;
    this._textC = undefined;
  }
  get textContent() {
    if (this._textC !== undefined) return this._textC;
    const t = c => c == null ? '' : c.nodeType === 3 ? c.textContent : c.nodeType === 1 ? c.textContent : String(c);
    return this.children.map(t).join('');
  }
  set textContent(v) { this._textC = String(v); this.children = []; }
  get className() { return [...this._cls].join(' '); }
  set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get classList() { return new ClassList(this); }
  get dataset() {
    const self = this;
    return new Proxy(this._data, {
      get: (t, k) => t[k],
      set: (t, k, v) => { t[k] = v; self._attrs.set('data-' + k, String(v)); return true; },
    });
  }
  setAttribute(k, v) {
    this._attrs.set(k, String(v));
    if (k === 'hidden') this.hidden = v != null && v !== 'false';
    if (k === 'id') {
      const id = String(v);
      if (docById.has(id) && docById.get(id) !== this) {
        console.log('IDOVERWRITE', id, new Error().stack.split('\n').slice(2, 5).join(' <- '));
      }
      docById.set(id, this);
    }
  }
  getAttribute(k) { return this._attrs.has(k) ? this._attrs.get(k) : null; }
  removeAttribute(k) { this._attrs.delete(k); if (k === 'hidden') this.hidden = false; }
  append(...cs) { for (const c of cs) { if (c == null) continue; if (typeof c === 'object') c.parentNode = this; this.children.push(c); } }
  replaceChildren(...cs) { this.children = cs; for (const c of cs) if (c && typeof c === 'object') c.parentNode = this; }
  addEventListener(t, fn) { (this._listeners[t] ??= []).push(fn); }
  removeEventListener() {}
  focus() {}
  scrollTo() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 120, height: 232, right: 120, bottom: 232 }; }
  get nextElementSibling() { return null; }
  get firstElementChild() { return this.children.find(c => c && c.nodeType === 1) || null; }
  get lastElementChild() { for (let i = this.children.length - 1; i >= 0; i--) if (this.children[i] && this.children[i].nodeType === 1) return this.children[i]; return null; }
  get innerHTML() { return ''; }
  set innerHTML(v) { this._textC = String(v); this.children = []; }
  getContext() {
    return {
      setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
      closePath() {}, stroke() {}, fill() {}, fillText() {}, fillRect() {},
      setLineDash() {}, createLinearGradient: () => ({ addColorStop() {} }),
      fillStyle: '', strokeStyle: '', font: '', textAlign: '',
      lineWidth: 1, lineJoin: '', lineCap: '', globalAlpha: 1,
      shadowColor: '', shadowBlur: 0,
    };
  }
  querySelector() { return null; }
}
/* pre-create the shells index.html provides */
const preRefs = {};
for (const [id, cls] of Object.entries({
  topbar: 'topbar', deck: 'deck', foot: 'foot', alerts: 'alert-strip',
  'p-kpi': 'g-kpi', 'p-throughput': 'g-throughput', 'p-gpu': 'g-gpu',
  'p-context': 'g-context', 'p-ledger': 'g-ledger', 'p-requests': 'g-requests',
  'p-engines': 'g-engines', 'p-events': 'g-events', 'p-pool': 'g-pool',
  'p-timeline': 'g-timeline', 'p-storage': 'g-storage', 'p-leader': 'g-leader',
})) {
  const n = new FakeEl(id === 'deck' ? 'main' : id === 'foot' ? 'footer' : 'section');
  n.setAttribute('id', id);
  n.className = cls;
  preRefs[id] = n;
}
const doc = {
  documentElement: new FakeEl('html'),
  createElement: t => new FakeEl(t),
  createTextNode: t => ({ nodeType: 3, textContent: t }),
  addEventListener() {}, removeEventListener() {},
  querySelector: sel => (sel && sel.startsWith('#') ? docById.get(sel.slice(1)) || null : null),
  querySelectorAll: () => [],
};
const mq = { matches: false };
globalThis.document = doc;
globalThis.window = { devicePixelRatio: 2, addEventListener() {} };
globalThis.matchMedia = () => mq;
globalThis.location = { search: MODE === 'demo' ? '?demo' : '' };
const store = new Map();
/* seed the persisted view: Basic skips the paint work of the hidden panels, so
   the panel assertions below need Advanced (the demo co-process inherits this) */
if (process.env.SPECULUM_SMOKE_VIEW) store.set('speculum.ui.view', process.env.SPECULUM_SMOKE_VIEW);
globalThis.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.EventSource = class { close() {} addEventListener() {} };
/* fetch: relative api/* goes to the live collector (real network);
   every api/snapshot body is captured so assertions compare against the
   exact payload the app consumed. */
const realFetch = globalThis.fetch.bind(globalThis);
let capturedSnapshot = null;
globalThis.fetch = async input => {
  const u = String(input);
  if (u.startsWith('api/')) {
    const r = await realFetch('http://127.0.0.1:8792/' + u);
    if (u.startsWith('api/snapshot') && r.ok) {
      const j = await r.json();
      capturedSnapshot = j;
      return { ok: true, status: r.status, json: async () => j };
    }
    return r;
  }
  return realFetch(input);
};
let rafQ = [];
globalThis.requestAnimationFrame = fn => { rafQ.push(fn); return rafQ.length; };
globalThis.setInterval = () => 0;      // no timers: polling/retries never fire
globalThis.addEventListener = () => {};
/* pump the app's rAF paint loop: one frame per tick, real clock advances
   between frames so the in-app 1 Hz text / 5 Hz paint gates fire */
async function pumpRaf(frames, stepMs = 100) {
  const t0 = performance.now();
  for (let i = 0; i < frames; i++) {
    const nowMs = t0 + (i + 1) * stepMs;
    const q = rafQ.splice(0, rafQ.length);
    for (const fn of q) fn(nowMs);
    await new Promise(r => setTimeout(r, 25));
  }
}

const root = doc.documentElement;
const vm = await import(new URL('../viewmodel.js', import.meta.url).href);

let fails = 0;
const ok = (cond, name) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); if (!cond) fails++; };

/* --- DOM-walk helpers --------------------------------------------------------- */
const kids = n => (n ? n.children : []);
const byClass = (n, cls) => kids(n).filter(c => c && c.nodeType === 1 && c._cls.has(cls));
/* class-membership match (like CSS .cls): elements may carry a base class plus
   state classes (e.g. "engine-card is-muted"), so exact string compare misses them */
const allClass = (n, cls, out = []) => {
  for (const c of kids(n)) {
    if (!c || c.nodeType !== 1) continue;
    if (c._cls.has(cls)) out.push(c);
    allClass(c, cls, out);
  }
  return out;
};
/* tag-based walk (the stub keeps tagName lowercase; browsers uppercase it) */
const allTag = (n, tag, out = []) => {
  for (const c of kids(n)) {
    if (!c || c.nodeType !== 1) continue;
    if (String(c.tagName).toLowerCase() === tag) out.push(c);
    allTag(c, tag, out);
  }
  return out;
};
const panelOf = shellId => byClass(docById.get(shellId), 'panel')[0]
  || kids(docById.get(shellId)).find(c => c && c.nodeType === 1);
/* first child of a .stat-value: raw string or text node → plain text */
const statValueText = statVal => {
  const c = kids(statVal)[0];
  if (c == null) return '';
  if (typeof c === 'string') return c;
  return c.nodeType === 3 ? c.textContent : c.textContent;
};

/* --- viewmodel: engine registry ------------------------------------------- */
const reg = vm.buildEngineRegistry([{ key: 'zin', label: 'Z' }, { key: 'alpha', label: 'A' }]);
ok(reg.list[0].key === 'alpha' && reg.list[1].key === 'zin', 'Registry sorted by engine key');
ok(reg.byKey.get('alpha').index === 0 && reg.byKey.get('alpha').colorIndex === 0, 'First engine index 0');
ok(reg.byKey.get('zin').index === 1 && reg.byKey.get('zin').colorIndex === 1, 'Second engine index 1');
const many = Array.from({ length: 7 }, (_, i) => ({ key: 'e' + i, up: true }));
const reg7 = vm.buildEngineRegistry(many);
ok(reg7.list.length === 7 && reg7.byKey.get('e6').colorIndex === 0, 'Color index wraps at 6');
ok(vm.buildEngineRegistry([]).list.length === 0 && vm.buildEngineRegistry(null).list.length === 0, 'Registry handles empty/absent');
const fromObj = vm.buildEngineRegistry({ b: { key: 'b' }, a: { key: 'a' } });
ok(fromObj.list[0].key === 'a', 'Registry accepts object map too');

/* --- viewmodel: engine state words ------------------------------------------ */
ok(vm.engineState({ up: true, latched: false }).word === 'Running', 'Running word');
ok(vm.engineState({ up: false, latched: false }).word === 'Stopped', 'Stopped word');
ok(vm.engineState({ up: false, latched: true }).word === 'Unresponsive', 'Latched word');
ok(vm.engineState({ up: null, latched: false }).word === 'No data', 'Unknown word');
ok(vm.engineState(null).word === 'No data', 'Missing engine word');
ok(vm.engineBadgeStatus({ up: true, latched: false }) === 'ok', 'Badge ok');
ok(vm.engineBadgeStatus({ up: false, latched: true }) === 'crit', 'Badge crit');
ok(vm.engineBadgeStatus({ up: false, latched: false }) === 'idle', 'Badge idle');

/* --- viewmodel: token ledger --------------------------------------------------- */
{
  const now = 2_000_000;
  const mk = (t, o, c, f) => ({ t, output: o, cache: c, fresh: f, prompt: c + f });
  const st = {
    t: now,
    engines: [
      { key: 'a', up: true, counters: { 'llamacpp:tokens_predicted_total': 1000, 'llamacpp:prompt_tokens_total': 900, 'ninfer:prefix_cache_hit_tokens_total': 600 } },
      { key: 'b', up: true, counters: { generated: 100, prompt: 80, cache: 30 } },
    ],
    requests: [
      mk(now - 100, 50, 20, 30),    // 1h + 24h
      mk(now - 7200, 60, 10, 50),   // 24h only (2 h old)
      mk(now - 80000, 70, 0, 70),   // 24h only (~22 h old)
      mk(now - 200000, 80, 0, 80),  // outside both
    ],
  };
  const L = vm.tokenLedger(st, now);
  const by = k => L.cols.find(c => c.key === k);
  ok(by('1h').generated === 50 && by('1h').cached === 20 && by('1h').fresh === 30 && by('1h').reqs === 1, 'Ledger 1h window sums');
  ok(by('24h').generated === 180 && by('24h').reqs === 3, 'Ledger 24h window sums');
  ok(by('since').generated === 1100 && by('since').cached === 630 && by('since').fresh === 350, 'Ledger since-start = live + demo counter keys');
  ok(by('since').reqs == null, 'Ledger since-start has no request count');
  ok(L.notes.length === 0, 'Ledger: no notes when buffer uncapped and counters present');
  ok(L.footer.cache != null && Math.abs(L.footer.cache - (30 / 260) * 100) < 1e-9, 'Ledger footer cache % over whole buffer');
  const L2 = vm.tokenLedger({ engines: [], requests: [] }, now);
  ok(L2.cols[2].generated == null && L2.notes.some(n => n.includes('no requests')) && L2.notes.some(n => n.includes('counters')), 'Ledger empty state notes');
  const L3 = vm.tokenLedger({ engines: [], requests: new Array(500).fill(mk(now - 10, 1, 1, 1)) }, now);
  ok(L3.notes.some(n => n.includes('capped')), 'Ledger notes when the request buffer is capped');
}

/* --- viewmodel: overall state ------------------------------------------------ */
const O = vm.overallState;
ok(O({ mode: 'offline' }).state === 'offline', 'Offline state');
ok(O({ mode: 'boot' }).state === 'paused', 'Booting state');
ok(O({ mode: 'live', paused: true }).state === 'paused', 'Paused state');
ok(O({ mode: 'live', alerts: [], engines: [{ up: true, latched: false }] }).state === 'live', 'Live state');
ok(O({ mode: 'live', alerts: ['x'], engines: [] }).state === 'degraded', 'Degraded on alert');
ok(O({ mode: 'live', alerts: [], engines: [{ up: false, latched: false }] }).state === 'live', 'Stopped (not latched) engine is normal — live');
ok(O({ mode: 'live', alerts: [], engines: [{ up: true, latched: true }] }).state === 'degraded', 'Degraded on latched engine');
ok(O({ mode: 'demo', alerts: [], engines: [{ up: true, latched: false }] }).state === 'live', 'Demo counts as live');

/* --- viewmodel: staleness ------------------------------------------------------- */
ok(vm.staleInfo(null, 1e9) == null, 'No data = not stale');
ok(vm.staleInfo(1000, 5000) == null, 'Under 10s = fresh');
const si = vm.staleInfo(1000, 17000);
ok(si && si.seconds === 16, 'Stale after 16s reports seconds');
ok(vm.staleLabel({ seconds: 16 }) === 'Stale — last update 16s ago', 'Stale label format');

/* --- boot app.js (shell) ----------------------------------------------------------- */
await import(new URL('../app.js', import.meta.url).href);
await new Promise(r => setImmediate(r)); // let in-flight fetches settle

ok(root.dataset.theme === 'dark', 'Default theme applied to <html>');
ok(root.getAttribute('data-ripple') == null, 'Ripple off by default on boot');
ok(docById.get('p-kpi') === preRefs['p-kpi'], 'Shell identity preserved through app.js boot');
const topbar = docById.get('topbar');
ok(topbar && topbar.children.length === 3, 'Top bar: brand + spacer + cluster');
ok(topbar.children[0].className === 'brand', 'Brand present');
const cluster = topbar.children[2];
ok(cluster.className === 'cluster', 'Status cluster present');
const pill = cluster.children[0];
if (MODE === 'live') {
  ok(pill.getAttribute('data-state') === 'paused', 'Status pill starts paused/booting');
  ok(pill.children[1] && pill.children[1].textContent === 'Booting', 'Pill word = Booting before first data');
} else {
  ok(pill.className === 'status-pill' && pill.getAttribute('data-state') === 'live', 'Demo: pill is live right after boot');
}
ok(cluster.children.length === 7, 'Cluster: pill + 4 kv chips + view control + settings menu');
ok(docById.get('tb-gpu') && docById.get('tb-driver') && docById.get('tb-uptime') && docById.get('tb-feed'), 'GPU/driver/uptime/feed chips exist');
const viewCtl = cluster.children[5];
ok(viewCtl.className === 'segmented' && viewCtl.children.length === 2, 'View segmented control: Basic / Advanced');
ok(viewCtl.children[0].textContent === 'Basic' && viewCtl.children[1].textContent === 'Advanced', 'View control labels');
ok(viewCtl.getAttribute('aria-label') === 'Panel view' &&
   viewCtl.children.every(b => b.tagName === 'button' && b.getAttribute('aria-pressed') !== null), 'View control a11y (group + aria-pressed buttons)');
const seeded = process.env.SPECULUM_SMOKE_VIEW || 'basic';
ok(localStorage.getItem('speculum.ui.view') === (process.env.SPECULUM_SMOKE_VIEW || null), 'View default is not written until chosen');
ok(docById.get('deck').className.includes('view-' + seeded) &&
   viewCtl.children[seeded === 'basic' ? 0 : 1].getAttribute('aria-pressed') === 'true',
   `View at boot: ${seeded} applied to #deck before first paint, and pressed`);
if (seeded === 'basic') {
  /* the ledger always writes its 4 rows when it renders, so an empty tbody
     here means the paint loop skipped a panel Basic does not show */
  const lTbody = allTag(docById.get('p-ledger'), 'tbody')[0];
  ok(lTbody && lTbody.children.length === 0,
     'Basic: hidden panels do no paint work (ledger tbody still empty)');
}
viewCtl.children[1]._listeners.click[0]();
ok(docById.get('deck').className.includes('view-advanced') &&
   localStorage.getItem('speculum.ui.view') === 'advanced', 'Advanced applies to #deck + persists');
const menuEl = cluster.children[6];
ok(menuEl.className === 'menu' && menuEl.children[1].hidden === true, 'Settings menu present, closed');
ok(menuEl.children[1].children.length === 3, 'Settings menu: theme, motion, ripple rows');
ok(menuEl.children[0].getAttribute('aria-haspopup') === 'menu', 'Menu button a11y');
menuEl.children[0]._listeners.click[0]();
ok(menuEl.children[1].hidden === false, 'Menu opens on click');
const rows = menuEl.children[1].children;
ok(rows[0].textContent.includes('Theme') && rows[1].textContent.includes('Reduce motion') && rows[2].textContent.includes('Ripple'), 'Settings rows labeled');
const themeBtns = rows[0].children[1].children;
ok(themeBtns.length === 2 && themeBtns[0].textContent === 'Dark' && themeBtns[1].textContent === 'Light', 'Theme segmented control');
themeBtns[1]._listeners.click[0]();
ok(root.dataset.theme === 'light', 'Theme switch applies to <html>');
ok(store.get('speculum.ui.theme') === 'light', 'Theme choice persisted');
const ripBtns = rows[2].children[1].children;
ok(ripBtns.length === 2 && ripBtns[1].textContent === 'On', 'Ripple segmented Off/On');
ripBtns[1]._listeners.click[0]();
ok(root.getAttribute('data-ripple') === 'on' && store.get('speculum.ui.ripple') === 'on', 'Ripple on: root attr + persisted');
const motBtns = rows[1].children[1].children;
ok(motBtns.length === 3, 'Motion segmented System/On/Off');
motBtns[1]._listeners.click[0]();
ok(root.getAttribute('data-ripple') == null, 'Reduce motion forces ripple off');
ok(root.dataset.motion === 'reduced', 'data-motion=reduced set');
ok(store.get('speculum.ui.motion') === 'on', 'Motion choice persisted');

/* deck shells */
const ids = ['p-kpi', 'p-throughput', 'p-gpu', 'p-context', 'p-ledger', 'p-requests', 'p-engines', 'p-events', 'p-pool',
             'p-timeline', 'p-storage'];
ok(ids.every(id => docById.get(id)), 'All 11 panel shells present');
ok(ids.every(id => docById.get(id).className.startsWith('g-')), 'Shells carry grid classes');
ok(docById.get('foot').textContent.includes('pause'), 'Foot shows key help');

/* --- panels: structure ---------------------------------------------------------- */
const panelIds = ['p-kpi', 'p-throughput', 'p-gpu', 'p-context', 'p-ledger', 'p-requests', 'p-engines', 'p-events', 'p-pool'];
const panels = panelIds.map(id => ({ id, panel: panelOf(id) }));
ok(panels.every(x => x.panel && byClass(x.panel, 'panel-head').length === 1 && byClass(x.panel, 'panel-body').length === 1), 'All 9 shells hold a built panel with head + body');
ok(panels.every(x => allClass(x.panel, 'stale-chip').length === 1), 'Every panel head carries the stale chip');

if (MODE === 'live') {
  /* --- panels: live payload assertions -------------------------------------- */
  await pumpRaf(80); // ~8 s of paint loop: snapshot lands, panels paint
  ok(capturedSnapshot != null, 'Live snapshot was fetched from the collector');
  if (capturedSnapshot) {
    const S = capturedSnapshot;
    ok(pill.children[1].textContent !== 'Booting', 'Pill word left Booting after live data');
    ok(['live', 'degraded', 'paused'].includes(pill.getAttribute('data-state')), 'Pill shows a data state after live data');

    /* KPI strip: values for every key the payload has must not be '—' */
    const kpiPanel = panels[0].panel;
    const kpiGroups = allClass(kpiPanel, 'kpi-groups')[0];
    const kpiStats = kpiGroups ? allClass(kpiGroups, 'stat') : [];
    const ORDER = ['tps', 'rpm', 'ttft', 'tpot', 'p95', 'cache', 'reingest', 'mtp', 'vram', 'queue'];
    ok(kpiStats.length === ORDER.length, 'KPI strip has 10 stats');
    let filled = 0, present = 0;
    for (let i = 0; i < ORDER.length; i++) {
      const k = ORDER[i];
      const v = S.kpi.last[k];
      const text = statValueText(kids(kpiStats[i])[1]); // .stat children: label, value, delta
      const has = v != null && isFinite(v);
      if (has) present++;
      if (has && text !== '—') filled++;
    }
    ok(present > 0 && filled === present, `KPI values != '—' for keys the payload has (${filled}/${present})`);
    ok(allClass(kpiPanel, 'sparkline').length === ORDER.length, 'KPI strip has 10 sparklines');

    /* Engines: one card per engine in the state (payload engines, strata_up adds at most one) */
    const engKeys = new Set(Object.keys(S.engines || {}));
    if (S.strata && S.strata.up != null && !engKeys.has('strata')) engKeys.add('strata');
    const engCards = allClass(panels[6].panel, 'engine-card');
    ok(engCards.length === engKeys.size, `Engine cards = payload engine count (${engCards.length}/${engKeys.size})`);
    ok(engCards.every(c => byClass(c, 'engine-card').length === 0 && c.children.length >= 2), 'Engine cards have head + content');

    /* Pool: one cell per session of a live engine (strata counted even when down) */
    let expSessions = 0;
    for (const e of Object.values(S.engines || {})) {
      if (!e.up && e.key !== 'strata') continue;
      expSessions += (e.sessions || []).length;
    }
    const cells = allClass(panels[8].panel, 'pool-cell');
    ok(cells.length === expSessions, `Pool cells = session count (${cells.length}/${expSessions})`);

    /* Events: rows = min(60, deduped buffer of the snapshot) */
    const seen = new Set(); let evCount = 0;
    for (const ev of (S.events || []).slice(0, 120)) {
      const k = (ev.t || 0) + '|' + ev.level + '|' + (ev.msg || '');
      if (seen.has(k)) continue;
      seen.add(k); evCount++;
    }
    const evTbody = allTag(panels[7].panel, 'tbody')[0];
    const evTrs = evTbody ? kids(evTbody).filter(c => c && c.nodeType === 1 && String(c.tagName).toLowerCase() === 'tr' && !String(c.className).includes('row-detail')) : [];
    ok(evTrs.length === Math.min(60, evCount), `Event rows = min(events, 60) (${evTrs.length}/${Math.min(60, evCount)})`);

    /* Throughput legend = engine count */
    const legend = allClass(panels[1].panel, 'legend')[0];
    ok(kids(legend).length === engKeys.size, 'Throughput legend = engine count');
    ok(allClass(panels[1].panel, 'tp-canvas').length === 1, 'Throughput canvas present');
  }
} else {
  /* --- panels: demo simulator assertions ------------------------------------ */
  await pumpRaf(60); // ~6 s: sim steps + paints
  const kpiPanel = panels[0].panel;
  const kpiGroups = allClass(kpiPanel, 'kpi-groups')[0];
  const kpiStats = kpiGroups ? allClass(kpiGroups, 'stat') : [];
  const vals = kpiStats.map(s => statValueText(kids(s)[1]));
  ok(kpiStats.length === 10 && vals.every(v => v !== '—'), 'Demo: all 10 KPI values != "—"');
  const engCards = allClass(panels[6].panel, 'engine-card');
  ok(engCards.length === 1, 'Demo: one engine card (simulated runtime)');
  const cells = allClass(panels[8].panel, 'pool-cell');
  ok(cells.length === 32, 'Demo: 32 pool cells from poolFakes');
  const evPanel = panels[7].panel;
  const evTrs = kids(allTag(evPanel, 'tbody')[0] || { children: [] }).filter(c => c && c.nodeType === 1 && String(c.tagName).toLowerCase() === 'tr');
  ok(evTrs.length > 0 && evTrs.length <= 120, `Demo: event rows render (row+detail trs, ${evTrs.length})`);
  const ctxPanel = panels[3].panel;
  const ctxRows = allClass(ctxPanel, 'ctx-row');
  ok(ctxRows.length === 9, 'Demo: 9 context rows (sim sessions)');
  const reqRows = kids(allTag(panels[5].panel, 'tbody')[0] || { children: [] }).filter(c => c && c.nodeType === 1 && String(c.tagName).toLowerCase() === 'tr');
  ok(reqRows.length >= 1, 'Demo: request rows render');
  const mix = allClass(panels[4].panel, 'mix');
  ok(mix.length === 3, 'Demo: ledger has 3 mix bars (one per column)');
}

if (MODE === 'live') {
  /* second boot: ?demo co-process, panels must render there too */
  const demo = await new Promise(resolve => {
    const c = spawn(process.execPath, [new URL(import.meta.url).pathname], {
      env: { ...process.env, SPECULUM_SMOKE_MODE: 'demo', SPECULUM_SMOKE_VIEW: 'advanced' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let out = '';
    c.stdout.on('data', d => { out += d; });
    c.stderr.on('data', d => { out += d; });
    c.on('close', code => resolve({ code, out }));
  });
  if (!demo.out.startsWith('PASS')) console.log(demo.out.trimEnd().split('\n').slice(-12).join('\n'));
  ok(demo.code === 0, 'Demo co-process boot (?demo) passes all smoke assertions');
} else {
  console.log('demo co-process assertions complete');
}

console.log(fails === 0 ? '\nALL PASS' : `\n${fails} FAILURES`);
process.exit(fails ? 1 : 0);
