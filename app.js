/* =========================================================================
   app.js — Speculum monitor. Owns the paint loop and all panel rendering.

   Live mode (default): one JSON snapshot primes every panel, then a 1 Hz
   SSE tick (`/api/stream`) keeps numbers hot; `/api/snapshot` polling is
   the fallback when SSE is unavailable. Panels with no data show an honest
   "no data" state instead of invented numbers.

   Demo mode (`?demo`): the seeded simulator stands in for the collector;
   same state shape, same paint code.
   ========================================================================= */

const $ = (s, r = document) => r.querySelector(s);
const root = document.documentElement;
const DEMO = new URLSearchParams(location.search).has('demo');
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* --- house palette (AETHER // NODE) -------------------------------------- */
const COL = {
  cyan: '#66d9ff', cyanHi: '#b9ecff', purple: '#9d8cff', green: '#6fe7ad',
  yellow: '#f7c76a', red: '#ff7e91', orange: '#ff9d5c',
  ink: '#eef4fb', ink2: '#c2ccda', muted: '#7e8b9e', faint: '#505c6d',
  grid: 'rgba(148,174,211,0.10)', hair: 'rgba(148,174,211,0.25)',
};
const ORIGIN_COLORS = { ninfer: COL.cyan, llama: COL.purple, strata: COL.green, sim: COL.yellow };
const accentOf = e => (e && ORIGIN_COLORS[e.origin]) || COL.purple;

/* deterministic resampling for ?demo: same seed, same dashboard */
function mulberry32(a) {
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 0x21F0FFAD);
    t = Math.imul(t ^ (t >>> 7), 0x84222325);
    return ((t ^ (t >>> 16)) >>> 0) / 4294967296;
  };
}
let rand = mulberry32(0xC0FFEE);

/* --- state (both modes fill exactly this shape) --------------------------- */
const state = {
  mode: 'boot',       // boot | live | offline | demo
  feed: '—',          // sse | poll | sim | —
  paused: false,
  gpu: null,          // {name, driver, temp, util, power, powerLimit, vram, vramTotal, clockSm, clockMem, fan, pcie}
  host: null,         // {cpu, perCore[], ramUsed, ramTotal, load[], uptime, procs[]}
  kpi: {},            // tps rpm p95 ttft tpot vram cache reingest mtp queue (ms for p95/ttft/tpot, GB for vram, % for cache/reingest/mtp)
  kpiHist: {},        // key -> 1 Hz series
  engHist: {},        // engine key -> 1 Hz throughput series
  hostHist: { cpu: [], ram: [] },
  engines: [],        // [{key,label,origin,up,latched,queue,rates,mtp,backend,window,sessions[],counters}]
  strata: { up: null, models: [] },
  models: [],
  alerts: [],
  requests: [],       // newest first (client buffer, capped)
  events: [],         // newest first
  sessions: [],       // flattened: {id, engine, origin, window, used, cached, accent}
  poolFakes: [],      // demo-only
  t: 0,
};
const KPI_KEYS = ['tps', 'rpm', 'p95', 'ttft', 'tpot', 'vram', 'cache', 'reingest', 'mtp', 'queue'];
const HIST_CAP = 3600;

const KPIS = [
  { key: 'tps',      label: 'Decode',         unit: 'tok/s', accent: COL.cyan,   fmt: v => v == null ? '—' : String(Math.round(v)) },
  { key: 'rpm',      label: 'Requests / min', accent: COL.purple, fmt: v => v == null ? '—' : String(Math.round(v)) },
  { key: 'p95',      label: 'p95 total',      unit: 's',     accent: COL.orange, fmt: v => v == null ? '—' : (v / 1000).toFixed(1) },
  { key: 'ttft',     label: 'First token',    unit: 's',     accent: COL.red,    fmt: v => v == null ? '—' : (v / 1000).toFixed(2) },
  { key: 'tpot',     label: 'Per token',      unit: 'ms',    accent: COL.green,  fmt: v => v == null ? '—' : String(Math.round(v)) },
  { key: 'vram',     label: 'VRAM',           unit: 'GB',    accent: COL.cyanHi, fmt: v => v == null ? '—' : v.toFixed(1) },
  { key: 'cache',    label: 'Cache hit',      unit: '%',     accent: COL.green,  fmt: v => v == null ? '—' : v.toFixed(1) },
  { key: 'reingest', label: 'Re-ingest tax',  unit: '%',     accent: COL.yellow, fmt: v => v == null ? '—' : v.toFixed(1) },
  { key: 'mtp',      label: 'MTP accept',     unit: '%',     accent: COL.cyan,   fmt: v => v == null ? '—' : v.toFixed(1) },
  { key: 'queue',    label: 'Queue depth',    accent: COL.purple, fmt: v => v == null ? '—' : String(Math.round(v)) },
];

const GAUGES = [
  { key: 'temp',  label: 'GPU temp',  unit: '°C' },
  { key: 'util',  label: 'GPU util',  unit: '%',  max: 100, warn: 95 },
  { key: 'power', label: 'Draw',      unit: 'W' },
  { key: 'vram',  label: 'VRAM',      unit: 'GB' },
];

/* --- small helpers -------------------------------------------------------- */
function fmtTok(n) {
  if (n == null) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}
function clockStr(t) {
  const d = new Date((t || Date.now() / 1000) * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map(x => String(x).padStart(2, '0')).join(':');
}
function upStr(sec) {
  if (sec == null) return '—';
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
}
function fit(c) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = c.clientWidth, h = c.clientHeight;
  const W = Math.max(1, Math.round(w * dpr)), H = Math.max(1, Math.round(h * dpr));
  if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
  const ctx = c.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}
function pushHist(arr, v, cap = HIST_CAP) {
  if (!Array.isArray(arr)) return;
  arr.push(v == null ? 0 : v);
  if (arr.length > cap) arr.splice(0, arr.length - cap);
}
function normGpu(g) {
  if (!g) return null;
  const num = v => { const x = parseFloat(v); return isFinite(x) ? x : null; };
  return {
    name: g.name || '—',
    driver: g.driver_version || '—',
    temp: num(g['temperature.gpu']),
    util: num(g['utilization.gpu']),
    power: num(g['power.draw']),
    powerLimit: num(g['power.limit']) || 450,
    vram: num(g['memory.used_gb']),
    vramTotal: num(g['memory.total_gb']),
    clockSm: num(g.clock_sm_mhz != null ? g.clock_sm_mhz : g['clocks.sm']),
    clockMem: num(g.clock_mem_mhz != null ? g.clock_mem_mhz : g['clocks.mem']),
    fan: num(g.fan_rpm != null ? g.fan_rpm : g['fan.speed']) || null,
    pcie: g.pcie || '—',
  };
}
function normHost(h) {
  if (!h) return null;
  return {
    cpu: h.cpu_total_pct != null ? h.cpu_total_pct : null,
    perCore: h.cpu_per_core || [],
    ramUsed: h.ram_used_gb, ramTotal: h.ram_total_gb,
    load: h.load || [],
    uptime: h.uptime_s,
    procs: h.procs || [],
  };
}

/* --- feed ------------------------------------------------------------------ */
let es = null, pollTimer = null;

function fetchSnap() {
  const r = fetch('api/snapshot', { cache: 'no-store' });
  return r.then(x => { if (!x.ok) throw new Error('http ' + x.status); return x.json(); });
}

function applySnapshot(s) {
  state.gpu = normGpu(s.gpu && s.gpu.last);
  state.host = normHost(s.host);
  state.kpi = { ...(s.kpi && s.kpi.last) };
  state.kpiHist = {};
  for (const k of KPI_KEYS) state.kpiHist[k] = [...((s.kpi && s.kpi.hist && s.kpi.hist[k]) || [])];
  state.engines = Object.values(s.engines || {});
  state.engHist = {};
  for (const e of state.engines) state.engHist[e.key] = [...(e.hist || [])];
  state.hostHist = (s.host && s.host.hist)
    ? { cpu: [...s.host.hist.cpu], ram: [...s.host.hist.ram_gb] }
    : { cpu: [], ram: [] };
  state.strata = s.strata || { up: null, models: [] };
  state.models = s.models || [];
  state.alerts = s.alerts || [];
  for (const r of s.requests || []) pushRequest(r);
  for (const ev of (s.events || []).slice(0, 120)) pushEvent(ev);
  syncEnginesFromTick({ engines: s.engines, strata_up: (s.strata || {}).up });
  state.mode = 'live';
  refreshDerived();
}

function applyTick(d) {
  if (d.gpu !== undefined) state.gpu = normGpu(d.gpu);
  if (d.host !== undefined) state.host = normHost(d.host);
  for (const k of KPI_KEYS) {
    state.kpi[k] = d.kpi ? d.kpi[k] : state.kpi[k];
    pushHist(state.kpiHist[k], d.kpi ? d.kpi[k] : null);
  }
  state.strata.up = d.strata_up != null ? d.strata_up : state.strata.up;
  state.alerts = d.alerts || state.alerts;
  syncEnginesFromTick(d);
  for (const r of d.new_requests || []) pushRequest(r);
  for (const ev of d.new_events || []) pushEvent(ev);
  if (state.mode === 'offline') state.mode = 'live';
  state.feed = 'sse';
  refreshDerived();
}

function syncEnginesFromTick(d) {
  for (const key of Object.keys(d.engines || {})) {
    const t = d.engines[key];
    let e = state.engines.find(x => x.key === key);
    if (!e) {
      e = { key, label: key, origin: key, up: t.up, latched: t.latched, queue: t.queue,
            rates: t.rates || null, mtp: t.mtp, backend: null, window: null,
            sessions: t.sessions || [], counters: t.counters || null };
      state.engines.push(e);
      state.engHist[key] = [];
    }
    e.up = t.up != null ? t.up : e.up;
    e.latched = !!t.latched;
    e.queue = t.queue != null ? t.queue : e.queue;
    e.rates = t.rates != null ? t.rates : e.rates;
    e.mtp = t.mtp != null ? t.mtp : e.mtp;
    if (t.sessions) e.sessions = t.sessions;
    if (t.counters) e.counters = t.counters;
    if (t.label) e.label = t.label;
    if (t.origin) e.origin = t.origin;
    if (t.window) e.window = t.window;
    if (t.backend) e.backend = t.backend;
    if (t.hist) state.engHist[e.key] = [...t.hist];
    if (t.rate != null) pushHist(state.engHist[e.key], t.rate);
    else if (t.rates) {
      const v = t.rates.decode_tps != null ? t.rates.decode_tps : t.rates.gen_tps_inst;
      pushHist(state.engHist[e.key], v != null ? v : 0);
    }
  }
  if (d.strata_up != null) {
    let s = state.engines.find(x => x.key === 'strata');
    if (!s) {
      s = { key: 'strata', label: 'Strata', origin: 'strata', up: d.strata_up, latched: false,
            queue: null, rates: null, mtp: null, backend: null, window: 131072,
            sessions: [], counters: null };
      state.engines.push(s);
      state.engHist.strata = [];
    }
    s.up = d.strata_up;
    pushHist(state.engHist.strata, d.strata_up ? (s.rates && s.rates.decode_tps) || 0 : 0);
  }
}

function pushRequest(r) {
  if (!r || r.id && state.requests.some(x => x.id === r.id)) return;
  state.requests.unshift(r);
  if (state.requests.length > 500) state.requests.length = 500;
}
const evSeen = new Set();
function pushEvent(ev) {
  if (!ev) return;
  const k = (ev.t || 0) + '|' + ev.level + '|' + (ev.msg || '');
  if (evSeen.has(k)) return;
  evSeen.add(k);
  if (evSeen.size > 400) { const it = evSeen.values(); for (let i = 0; i < 200; i++) evSeen.delete(it.next().value); }
  state.events.unshift(ev);
  if (state.events.length > 120) state.events.length = 120;
}

function refreshDerived() {
  state.sessions = [];
  for (const e of state.engines) {
    if (!e.up && e.origin !== 'strata') continue;
    for (const s of e.sessions || []) {
      state.sessions.push({
        id: s.id + (s.session ? '-' + s.session : ''),
        engine: e.label, origin: e.origin,
        window: s.window || e.window, used: s.used, cached: s.cached,
        accent: accentOf(e), processing: s.processing,
      });
    }
  }
}

function openSSE() {
  if (es) { try { es.close(); } catch {} es = null; }
  es = new EventSource('api/stream');
  es.onopen = () => { state.feed = 'sse'; };
  es.addEventListener('tick', ev => { try { applyTick(JSON.parse(ev.data)); } catch {} });
  es.onerror = () => {
    /* server may be restarting — poll until SSE recovers */
    try { es.close(); } catch {}
    es = null;
    state.feed = 'poll';
    startPoll();
  };
}

function startPoll() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const s = await fetchSnap();
      applySnapshot(s);
      state.feed = 'poll';
    } catch {
      state.mode = 'offline';
    }
  }, 2000);
}

async function bootLive() {
  try {
    const s = await fetchSnap();
    applySnapshot(s);
    openSSE();
  } catch {
    state.mode = 'offline';
    startPoll();
    setInterval(() => { if (state.mode === 'offline' && !es) openSSE(); }, 10000);
  }
}

/* --- demo simulator (same state shape) ------------------------------------ */
const DEMO_MODELS = [
  { id: 'llama-3.3-70b', label: 'Llama 3.3 70B', ctx: 131072, share: 0.34 },
  { id: 'qwen3-32b', label: 'Qwen3 32B', ctx: 65536, share: 0.27 },
  { id: 'mistral-small-24b', label: 'Mistral Small 24B', ctx: 32768, share: 0.21 },
  { id: 'phi-4-14b', label: 'Phi-4 14B', ctx: 16384, share: 0.18 },
];
let demo = null;

function initDemo() {
  demo = {
    tps: 140, rpm: 60, p95: 340, ttft: 210, tpot: 26, cache: 62, queue: 3,
    gpu: { name: 'RTX 4090 (sim)', driver: '615.71.09 (sim)', temp: 61, util: 74,
           power: 268, powerLimit: 450, vram: 18.4, vramTotal: 24,
           clockSm: 1725, clockMem: 10251, fan: 1480, pcie: 'PCIe 4 x16' },
    host: { cpu: 38, perCore: Array(8).fill(0).map(() => 20 + rand() * 40),
            ramUsed: 18.9, ramTotal: 32.5, load: [2.1, 2.4, 2.6], uptime: 41 * 3600,
            procs: [{ name: 'sim-engine', pid: 1, rss_gb: 12.3, cmd: 'simulated' }] },
    engine: { key: 'sim', label: 'simulated runtime', origin: 'sim', up: true, latched: false,
              queue: 3, rates: { decode_tps: 140 }, mtp: 71, backend: null, window: 131072,
              sessions: [], counters: { generated: 0, prompt: 0, cache: 0 } },
    reqId: 0,
  };
  state.strata = { up: true, models: [] };
  for (let i = 0; i < 9; i++) {
    const m = DEMO_MODELS[i % DEMO_MODELS.length];
    demo.engine.sessions.push({
      id: `s${1000 + i}`, window: m.ctx,
      used: Math.floor(m.ctx * (0.18 + rand() * 0.6)),
      cached: Math.floor(m.ctx * (0.15 + rand() * 0.5)), processing: false,
    });
  }
  state.poolFakes = Array.from({ length: 32 }, () => rand());
  state.engines = [demo.engine];
  state.engHist = { sim: [] };
  state.mode = 'demo';
  state.feed = 'sim';
  refreshDerived();
  for (let i = 0; i < 720; i++) demoStep();   /* long warmup: no straight ramps */
  for (let i = 0; i < 6; i++) demoEvent();
  renderText(); paint();
}

function demoStep() {
  const d = demo;
  state.t += 1;
  const load = 0.45 + 0.4 * Math.sin(state.t / 40) + rand() * 0.15;
  const drift = (v, lo, hi, pull) => v + (lo + (hi - lo) * pull - v) * 0.06 + (rand() - 0.5) * 2;
  d.tps = Math.max(12, drift(d.tps, 60, 260, load));
  d.rpm = Math.max(20, drift(d.rpm, 120, 340, load));
  d.p95 = Math.max(90, drift(d.p95, 220, 620, load * 0.9));
  d.ttft = Math.max(60, drift(d.ttft, 120, 430, load));
  d.tpot = Math.max(8, drift(d.tpot, 18, 46, load));
  d.queue = Math.max(0, drift(d.queue, 0, 14, load * 0.7));
  d.cache = Math.min(96, Math.max(20, drift(d.cache, 45, 82, 0.6 + rand() * 0.3)));
  d.gpu.util = Math.min(100, Math.max(0, drift(d.gpu.util, 30, 99, load)));
  d.gpu.power = Math.max(60, drift(d.gpu.power, 120, 380, load));
  d.gpu.temp = Math.max(34, drift(d.gpu.temp, 42, 86, load * 0.9));
  d.gpu.fan = Math.max(400, drift(d.gpu.fan, 700, 2600, load));
  d.gpu.vram = Math.min(d.gpu.vramTotal, Math.max(6, drift(d.gpu.vram, 12, 23.5, 0.55 + rand() * 0.3)));
  d.host.cpu = Math.max(2, drift(d.host.cpu, 10, 95, load));
  d.host.uptime += 1;
  state.gpu = { ...d.gpu };
  state.host = { ...d.host, perCore: d.host.perCore.map(v => Math.max(0, Math.min(100, v + (rand() - 0.5) * 14))) };
  state.kpi = {
    tps: d.tps, rpm: d.rpm, p95: d.p95, ttft: d.ttft, tpot: d.tpot,
    vram: d.gpu.vram, cache: d.cache, reingest: Math.max(0, 100 - d.cache),
    mtp: 71 + (rand() - 0.5) * 8, queue: d.queue,
  };
  for (const k of KPI_KEYS) pushHist(state.kpiHist[k] ??= [], state.kpi[k]);
  pushHist(state.engHist.sim ??= [], d.tps);
  pushHist(state.hostHist.cpu, d.host.cpu);
  pushHist(state.hostHist.ram, d.host.ramUsed);
  d.engine.rates = { decode_tps: Math.round(d.tps) };
  d.engine.queue = Math.round(d.queue);
  for (const s of d.engine.sessions) {
    s.used = Math.min(s.window, Math.max(256, s.used + Math.floor((rand() - 0.45) * 900)));
    if (s.used < 400 && rand() < 0.05) s.used = Math.floor(s.window * rand());
  }
  if (state.t % 5 === 0) {
    const m = DEMO_MODELS[Math.floor(rand() * DEMO_MODELS.length)];
    const prompt = Math.floor(m.ctx * (0.15 + rand() * 0.5));
    const cache = Math.floor(prompt * (0.5 + rand() * 0.45));
    pushRequest({
      id: `sim-${(d.reqId += 1)}`, t: Date.now() / 1000, model: m.label, origin: 'sim',
      prompt, cache, fresh: prompt - cache, output: Math.floor(80 + rand() * 400),
      cache_pct: prompt ? (100 * cache / prompt) : 0,
      total_s: 1 + rand() * 8, ttft_s: 0.2 + rand() * 1.5,
      decode_tps: d.tps, window: m.ctx,
    });
  }
  if (rand() < 0.1) demoEvent();
  refreshDerived();
}

const DEMO_EVENTS = [
  ['ok', () => `stream complete · ${Math.floor(rand() * 900 + 60)} tok · ${(rand() * 3 + 0.4).toFixed(2)}s`],
  ['ok', () => `cache hit · ${Math.floor(rand() * 4000 + 400)} tok reused`],
  ['info', () => `kv write · s${Math.floor(rand() * 9)}`],
  ['warn', () => `kv eviction · slot ${Math.floor(rand() * 32)} · ${Math.floor(rand() * 3000 + 500)} tok dropped`],
  ['warn', () => `queue pressure · ${Math.round(demo.queue)} waiting`],
  ['err', () => `upstream timeout · retry ${Math.floor(rand() * 3) + 1}/3`],
];
function demoEvent() {
  const pick = DEMO_EVENTS[Math.floor(rand() * DEMO_EVENTS.length)];
  pushEvent({ t: Date.now() / 1000, level: pick[0], msg: pick[1]() });
}

/* --- build ------------------------------------------------------------------ */
function shell(el) {
  const s = document.createElement('div');
  s.className = 'sheen';
  el.prepend(s);
}

function buildBar() {
  const bar = $('#bar');
  shell(bar);
  bar.insertAdjacentHTML('beforeend', `
    <span class="orb" id="orb"></span>
    <div class="brand"><b>Speculum</b><span>local LLM runtime</span></div>
    <div class="spacer"></div>
    <span class="chip">node <b id="c-node">—</b></span>
    <span class="chip">driver <b id="c-driver">—</b></span>
    <span class="chip">model <b id="c-model">—</b></span>
    <span class="chip">uptime <b id="c-uptime">—</b></span>
    <span class="chip" id="c-feed">feed <b id="feedval">…</b></span>
    <span class="chip">state <b id="runstate">boot</b></span>
  `);
}

function buildAlertbar() {
  const p = $('#alertbar');
  shell(p);
  p.insertAdjacentHTML('beforeend', `<b>ALERT</b><span id="alertmsg"></span>`);
}

function buildKpi() {
  const wrap = $('#kpi');
  for (const k of KPIS) {
    const el = document.createElement('article');
    el.className = 'card glass';
    el.style.setProperty('--glow-c', k.accent);
    el.innerHTML = `
      <div class="label">${k.label}</div>
      <div class="value" data-kpi="${k.key}">—</div>
      <div class="delta" data-delta="${k.key}"></div>
      <canvas data-spark="${k.key}"></canvas>`;
    shell(el);
    wrap.append(el);
  }
}

function buildSignal() {
  const p = $('#signal');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Signal</h2><p class="hint" id="sighint">tokens / s, rolling 60 min</p></div>
    <canvas id="chart" aria-label="Rolling throughput chart"></canvas>
    <div class="legend" id="legend"></div>`);
}

function buildGauges() {
  const p = $('#gauges');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Hardware</h2><p class="hint" id="hwname">—</p></div>
    <div class="gauges">
      ${GAUGES.map(g => `<div class="gauge"><canvas data-gauge="${g.key}"></canvas><span>${g.label}</span></div>`).join('')}
    </div>
    <div class="readout" id="hw-readout"></div>
    <div class="readout" id="host-readout"></div>`);
}

function buildContext() {
  const p = $('#context');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Context map</h2><p class="hint" id="ctxhring">no live sessions</p></div>
    <canvas id="context-canvas"></canvas>
    <div class="ctx-legend" id="ctx-legend"></div>`);
}

function buildLedger() {
  const p = $('#ledger');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Token ledger</h2><p class="hint">generated / fresh prefill / cached — 1 h · 24 h · since engine start</p></div>
    <div class="ledger" id="ledger-rows"></div>
    <div class="mini" id="mini"></div>`);
}

function buildCtxGraph() {
  const p = $('#ctxgraph');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Context graph</h2><p class="hint" id="cghint">prompt tokens per request vs model window, most recent first</p></div>
    <div class="cg-rows" id="cgrows"></div>
    <div class="cg-legend">
      <span><i style="background:${COL.cyan}"></i>cached (prefix hit)</span>
      <span><i style="background:${COL.yellow}"></i>fresh prefill (re-ingest)</span>
      <span><i style="background:${COL.green}"></i>generated</span>
    </div>
    <div class="cg-kpis">
      <span>re-ingest tax · 15 min <b id="cg-tax">—</b></span>
      <span>mtp acceptance · 15 min <b id="cg-mtp">—</b></span>
      <span>cache hit · 15 min <b id="cg-cache">—</b></span>
      <span class="warn" id="cg-reqs">no requests buffered</span>
    </div>`);
}

function buildLanes() {
  const wrap = $('#lanes');
  for (const key of ['ninfer', 'strata']) {
    const el = document.createElement('article');
    el.className = 'lane glass';
    el.dataset.lanekey = key;
    el.tabIndex = 0;
    el.innerHTML = `
      <div class="name"><span class="dot"></span><span class="lname">—</span></div>
      <div class="sub">—</div>
      <div class="meter"><i></i></div>
      <div class="grid">
        <div>tok/s<b data-l="tps">—</b></div>
        <div>queue<b data-l="queue">—</b></div>
        <div>mtp<b data-l="mtp">—</b></div>
      </div>`;
    shell(el);
    wrap.append(el);
  }
}

function buildStream() {
  const p = $('#stream');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Stream</h2><p class="hint">runtime events</p></div>
    <ul id="events" aria-live="polite"></ul>`);
}

function buildPool() {
  const p = $('#pool');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Pool</h2><p class="hint" id="poolhint">kv slots</p></div>
    <div id="poolbody"></div>`);
}

/* --- paint ------------------------------------------------------------------ */
function paintSpark(canvas, data, accent) {
  const { ctx, w, h } = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  const d = data.slice(-360);
  if (d.length < 2) { ctx.fillStyle = COL.faint; ctx.font = '11px ui-monospace, monospace'; ctx.textAlign = 'center'; ctx.fillText('no data', w / 2, h / 2 + 4); return; }
  const lo = Math.min(...d), hi = Math.max(...d);
  const span = hi - lo || 1;
  ctx.lineJoin = 'round';
  ctx.shadowColor = accent;
  ctx.shadowBlur = 8;
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  d.forEach((v, i) => {
    const x = (i / (d.length - 1)) * w;
    const y = h - 3 - ((v - lo) / span) * (h - 8);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.globalAlpha = 0.14;
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  ctx.fillStyle = accent; ctx.fill();
  ctx.globalAlpha = 1;
}

function paintChart() {
  const c = $('#chart'); if (!c) return;
  const { ctx, w, h } = fit(c);
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = COL.grid;
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = (i / 4) * h;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  const series = state.engines
    .map(e => ({ e, data: (state.engHist[e.key] || []).slice(-360) }))
    .filter(s => s.data.length > 1);
  let hi = 0;
  for (const s of series) for (const v of s.data) hi = Math.max(hi, v);
  if (!series.length || hi <= 0) {
    ctx.fillStyle = COL.faint; ctx.font = '12px ui-monospace, monospace'; ctx.textAlign = 'center';
    ctx.fillText('no throughput data yet — waiting for requests', w / 2, h / 2);
    return;
  }
  hi *= 1.12;
  for (const { e, data } of series) {
    const accent = accentOf(e);
    const focused = state.focus === e.key;
    ctx.shadowColor = accent;
    ctx.shadowBlur = focused ? 16 : 7;
    ctx.globalAlpha = state.focus && !focused ? 0.3 : 1;
    ctx.strokeStyle = accent;
    ctx.lineWidth = focused ? 2.4 : 1.5;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - 4 - (Math.max(0, v) / hi) * (h - 12);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
    if (focused) {
      ctx.globalAlpha = 0.13; ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
      ctx.fillStyle = accent; ctx.fill();
    }
    ctx.globalAlpha = 1; ctx.shadowBlur = 0;
  }
}

function gaugeValue(g) {
  const g2 = state.gpu;
  if (!g2) return null;
  if (g.key === 'temp') return { v: g2.temp, max: 95, warn: 80 };
  if (g.key === 'util') return { v: g2.util, max: 100, warn: 95 };
  if (g.key === 'power') return { v: g2.power, max: g2.powerLimit || 450, warn: (g2.powerLimit || 450) * 0.9 };
  if (g.key === 'vram') return { v: g2.vram, max: g2.vramTotal || 24, warn: (g2.vramTotal || 24) * 0.92 };
  return null;
}

function paintGauge(canvas, g) {
  const { ctx, w, h } = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2 + 4, r = Math.min(w, h) / 2 - 10;
  const a0 = Math.PI * 0.75, a1 = Math.PI * 2.25;
  const gv = gaugeValue(g);

  ctx.lineCap = 'round';
  ctx.strokeStyle = 'rgba(148,174,211,0.14)';
  ctx.lineWidth = 7;
  ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke();

  if (!gv || gv.v == null) {
    ctx.fillStyle = COL.faint; ctx.font = '600 14px ui-monospace, monospace'; ctx.textAlign = 'center';
    ctx.fillText('no gpu', cx, cy + 1);
    return;
  }
  const pct = Math.max(0, Math.min(1, gv.v / gv.max));
  const hot = gv.v >= gv.warn;
  const accent = hot ? COL.red : COL.cyan;
  ctx.shadowColor = accent; ctx.shadowBlur = 14;
  ctx.strokeStyle = accent; ctx.lineWidth = 7;
  ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + (a1 - a0) * pct); ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.strokeStyle = 'rgba(148,174,211,0.28)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 10; i++) {
    const a = a0 + (a1 - a0) * (i / 10);
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (r - 6), cy + Math.sin(a) * (r - 6));
    ctx.lineTo(cx + Math.cos(a) * (r - 11), cy + Math.sin(a) * (r - 11));
    ctx.stroke();
  }
  const txt = g.key === 'util' ? Math.round(gv.v) : g.key === 'power' ? Math.round(gv.v) : gv.v.toFixed(g.key === 'vram' ? 1 : 0);
  ctx.fillStyle = COL.ink;
  ctx.font = '600 15px ui-monospace, monospace';
  ctx.textAlign = 'center';
  ctx.fillText(String(txt), cx, cy + 1);
  ctx.fillStyle = COL.muted;
  ctx.font = '500 10px ui-monospace, monospace';
  ctx.fillText(g.unit, cx, cy + 14);
}

function paintContext() {
  const c = $('#context-canvas'); if (!c) return;
  const { ctx, w, h } = fit(c);
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2;
  const R = Math.min(w, h) / 2 - 10;

  ctx.strokeStyle = 'rgba(148,174,211,0.12)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
  for (let i = 0; i < 12; i++) {
    const a = (i / 12) * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * R * 0.46, cy + Math.sin(a) * R * 0.46);
    ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    ctx.stroke();
  }

  const ses = state.sessions;
  if (!ses.length) {
    ctx.fillStyle = COL.faint; ctx.font = '12px ui-monospace, monospace'; ctx.textAlign = 'center';
    ctx.fillText('no live sessions', cx, cy - 4);
    ctx.font = '10px ui-monospace, monospace';
    ctx.fillText('no engine slots reporting', cx, cy + 12);
    return;
  }
  const n = ses.length;
  ses.forEach((s, i) => {
    const r = R * (0.46 + 0.54 * ((i + 1) / n));
    const frac = s.window ? Math.min(1, (s.used || 0) / s.window) : 0;
    const a0 = -Math.PI / 2;
    const a1 = a0 + Math.PI * 2 * frac;
    ctx.strokeStyle = 'rgba(148,174,211,0.14)';
    ctx.lineWidth = 5;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    ctx.shadowColor = s.accent; ctx.shadowBlur = 12;
    ctx.strokeStyle = s.accent; ctx.lineWidth = 5; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke();
    ctx.shadowBlur = 0;
    if (frac > 0) {
      ctx.fillStyle = s.accent;
      ctx.shadowColor = s.accent; ctx.shadowBlur = 10;
      ctx.beginPath();
      ctx.arc(cx + Math.cos(a1) * r, cy + Math.sin(a1) * r, 2.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  });

  if (!REDUCED) {
    const a = -Math.PI / 2 + ((Date.now() / 1000 / 12) % 1) * Math.PI * 2;
    const grad = ctx.createLinearGradient(cx, cy, cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    grad.addColorStop(0, 'rgba(238,244,251,0.30)');
    grad.addColorStop(1, 'rgba(238,244,251,0)');
    ctx.strokeStyle = grad; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R); ctx.stroke();
  }

  const total = ses.reduce((s, x) => s + (x.used || 0), 0);
  ctx.fillStyle = COL.ink;
  ctx.font = '600 15px ui-monospace, monospace';
  ctx.textAlign = 'center';
  ctx.fillText(fmtTok(total), cx, cy + 1);
  ctx.fillStyle = COL.muted;
  ctx.font = '500 9px ui-monospace, monospace';
  ctx.fillText('ctx used', cx, cy + 13);
}

function paint() {
  for (const k of KPIS) {
    const c = document.querySelector(`canvas[data-spark="${k.key}"]`);
    if (c) paintSpark(c, state.kpiHist[k.key] || [], k.accent);
  }
  for (const g of GAUGES) {
    const c = document.querySelector(`canvas[data-gauge="${g.key}"]`);
    if (c) paintGauge(c, g);
  }
  paintChart();
  paintContext();
}

/* --- text readouts ----------------------------------------------------------- */
function renderText() {
  /* header */
  $('#c-node').textContent = state.gpu ? state.gpu.name : 'no gpu';
  $('#c-driver').textContent = state.gpu ? state.gpu.driver : '—';
  const active = state.engines.find(e => e.up) || state.engines[0];
  $('#c-model').textContent = active ? (active.label + (active.latched ? ' ⚠' : '')) : '—';
  $('#c-uptime').textContent = state.host ? upStr(state.host.uptime) : '—';
  $('#runstate').textContent = state.paused ? 'paused' : state.mode;
  const feed = $('#feedval');
  feed.textContent = state.feed;
  $('#c-feed').className = 'chip' + (state.mode === 'offline' ? ' alert' : state.mode === 'live' ? ' ok' : '');
  const warn = (state.alerts && state.alerts.length) || (state.gpu && state.gpu.temp != null && state.gpu.temp >= 80);
  $('#orb').classList.toggle('warn', !!warn);

  /* alert bar */
  const ab = $('#alertbar');
  if (state.alerts && state.alerts.length) {
    ab.classList.add('on');
    $('#alertmsg').textContent = state.alerts[0] + (state.alerts.length > 1 ? `  (+${state.alerts.length - 1} more)` : '');
  } else {
    ab.classList.remove('on');
  }

  /* kpi cards */
  for (const k of KPIS) {
    const el = document.querySelector(`[data-kpi="${k.key}"]`);
    const d = document.querySelector(`[data-delta="${k.key}"]`);
    if (!el) continue;
    const v = state.kpi[k.key];
    el.innerHTML = v == null ? '—' : `${k.fmt(v)}${k.unit ? ` <small>${k.unit}</small>` : ''}`;
    const h = state.kpiHist[k.key] || [];
    if (d) {
      if (h.length >= 2 && v != null) {
        const prev = h[h.length - 2] || 0;
        const diff = v - prev;
        d.textContent = `${diff >= 0 ? '+' : ''}${diff.toFixed(1)}`;
        d.className = 'delta ' + (diff >= 0 ? 'up' : 'down');
      } else { d.textContent = ''; d.className = 'delta'; }
    }
  }

  /* signal legend */
  $('#legend').innerHTML = state.engines.map(e =>
    `<span><i style="background:${accentOf(e)};box-shadow:0 0 10px ${accentOf(e)}"></i>${e.label}${e.up ? '' : ' · down'}</span>`).join('');

  /* gauges readout */
  $('#hwname').textContent = state.gpu ? `${state.gpu.name} · ${state.gpu.pcie}` : 'no gpu data';
  $('#hw-readout').innerHTML = state.gpu ? [
    ['sm clock', state.gpu.clockSm != null ? state.gpu.clockSm + ' MHz' : '—'],
    ['mem clock', state.gpu.clockMem != null ? state.gpu.clockMem + ' MHz' : '—'],
    ['fan', state.gpu.fan != null ? Math.round(state.gpu.fan) + ' rpm' : 'n/a'],
    ['power limit', Math.round(state.gpu.powerLimit || 0) + ' W'],
  ].map(([a, b]) => `<span>${a} <b>${b}</b></span>`).join('') : '<span>—</span>';
  $('#host-readout').innerHTML = state.host ? [
    ['cpu', state.host.cpu != null ? state.host.cpu.toFixed(1) + '%' : '—'],
    ['ram', `${state.host.ramUsed != null ? state.host.ramUsed.toFixed(1) : '—'} / ${state.host.ramTotal != null ? state.host.ramTotal.toFixed(1) : '—'} GB`],
    ['load', state.host.load.map(x => x.toFixed(2)).join(' · ') || '—'],
    ['top procs', (state.host.procs || []).slice(0, 2).map(p => `${p.name} ${p.rss_gb != null ? p.rss_gb + 'G' : '?'}`).join(' · ') || '—'],
  ].map(([a, b]) => `<span>${a} <b>${b}</b></span>`).join('') : '<span>—</span>';

  /* context map */
  $('#ctxhring').textContent = state.sessions.length
    ? `${state.sessions.length} live session${state.sessions.length > 1 ? 's' : ''} vs model window` : 'no live sessions';
  $('#ctx-legend').innerHTML = state.sessions.slice(0, 8).map(s =>
    `<span><i style="background:${s.accent}"></i>${s.id} · ${s.engine} · ${fmtTok(s.used)}/${fmtTok(s.window)}</span>`).join('')
    || '<span>—</span>';

  /* ledger */
  renderLedger();
  renderCtxGraph();

  /* lanes */
  for (const el of document.querySelectorAll('.lane')) {
    const key = el.dataset.lanekey;
    const e = state.engines.find(x => x.key === key);
    el.style.setProperty('--glow-c', e ? accentOf(e) : COL.purple);
    el.dataset.off = e && !e.up ? '1' : '0';
    const dot = el.querySelector('.dot');
    const acc = e ? (e.latched ? COL.red : accentOf(e)) : COL.faint;
    dot.style.color = acc; dot.style.background = acc;
    el.querySelector('.lname').textContent = e ? e.label : key;
    el.querySelector('.sub').textContent = e
      ? `${e.origin} · ${e.window ? fmtTok(e.window) + ' ctx' : 'ctx ?'} · ${e.backend ? e.backend.replace('http://', '') : e.up ? 'ready' : 'stopped'}`
      : 'not running';
    const meter = el.querySelector('.meter i');
    const acc2 = e ? accentOf(e) : COL.faint;
    meter.style.background = acc2;
    meter.style.boxShadow = `0 0 14px ${acc2}`;
    let occ = 0;
    if (e) for (const s of e.sessions || []) occ = Math.max(occ, s.window ? (s.used || 0) / s.window : 0);
    meter.style.width = (e && e.up ? Math.round(occ * 100) : 0) + '%';
    const rate = e && e.rates ? (e.rates.decode_tps != null ? e.rates.decode_tps : e.rates.gen_tps_inst) : null;
    el.querySelector('[data-l="tps"]').textContent = rate != null ? Math.round(rate) : '—';
    el.querySelector('[data-l="queue"]').textContent = e && e.queue != null ? e.queue : '—';
    el.querySelector('[data-l="mtp"]').textContent = e && e.mtp != null ? e.mtp + '%' : '—';
  }

  /* pool */
  renderPool();

  /* events */
  const ul = $('#events');
  if (ul) {
    if (!state.events.length) {
      ul.innerHTML = '<li><span class="t">—</span><span class="lv" style="color:var(--faint)">idle</span><span class="msg" style="color:var(--faint)">no events yet</span></li>';
    } else {
      ul.innerHTML = state.events.slice(0, 14).map(ev => {
        const t = ev.t ? clockStr(ev.t) : clockStr();
        return `<li data-lv="${ev.level}"><span class="t">${t}</span><span class="lv">${ev.level}</span><span class="msg">${String(ev.msg).replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]))}</span></li>`;
      }).join('');
    }
  }

  $('#foot').innerHTML = `<kbd>P</kbd> pause · feed: <code>${state.feed}</code> · mode: <code>${state.mode}</code> · collector serves <code>/api/snapshot</code> + <code>/api/stream</code> · <code>?demo</code> = simulator`;
}

function renderLedger() {
  const reqs = state.requests;
  const nowT = Date.now() / 1000;
  const win = (sec) => reqs.filter(r => (r.t || 0) >= nowT - sec);
  const sum = (arr, f) => arr.reduce((a, r) => a + (f(r) || 0), 0);
  const engineGen = () => {
    let g = 0;
    for (const e of state.engines) {
      const c = e.counters || {};
      g += (c['llamacpp:tokens_predicted_total'] != null)
        ? c['llamacpp:tokens_predicted_total']
        : (c['tokens_predicted_total'] != null ? c['tokens_predicted_total'] : 0);
    }
    return g || null;
  };
  const engineFresh = () => {
    let p = 0, ch = 0;
    for (const e of state.engines) {
      const c = e.counters || {};
      p += c['llamacpp:prompt_tokens_total'] || 0;
      ch += c['ninfer:prefix_cache_hit_tokens_total'] || 0;
    }
    return (p || ch) ? Math.max(0, p - ch) : null;
  };
  const engineCache = () => {
    let ch = 0;
    for (const e of state.engines) ch += (e.counters || {})['ninfer:prefix_cache_hit_tokens_total'] || 0;
    return ch || null;
  };
  const windows = [
    { label: '1 h', sec: 3600 },
    { label: '24 h', sec: 86400 },
    { label: 'since start', sec: null },
  ];
  const kinds = [
    { name: 'generated', color: COL.green, winSum: a => sum(a, r => r.output), since: engineGen },
    { name: 'fresh prefill', color: COL.yellow, winSum: a => sum(a, r => r.fresh), since: engineFresh },
    { name: 'cached (reused)', color: COL.cyan, winSum: a => sum(a, r => r.cache), since: engineCache },
  ];
  const vals = [];
  for (const k of kinds) for (const w of windows) {
    const a = w.sec ? win(w.sec) : reqs;
    vals.push(w.sec ? k.winSum(a) : k.since());
  }
  const maxv = Math.max(1, ...vals.map(v => v || 0));
  $('#ledger-rows').innerHTML = kinds.map((k, ki) =>
    windows.map((w, wi) => {
      const v = vals[ki * 3 + wi];
      const barw = v ? Math.max(2, 100 * v / maxv) : 0;
      return `<div class="row">
        <span class="rl">${k.name} <small>· ${w.label}</small></span>
        <div class="rb"><i style="width:${barw}%;background:${k.color};box-shadow:0 0 14px ${k.color}66"></i></div>
        <span class="rv">${v ? fmtTok(v) : '—'}</span>
      </div>`;
    }).join('')
  ).join('');

  const n = reqs.length;
  const avg = n ? Math.round(reqs.reduce((a, r) => a + (r.output || 0) + (r.cache || 0) + (r.fresh || 0), 0) / n) : null;
  $('#mini').innerHTML = [
    ['reqs buffered', String(n)],
    ['avg tok/req', avg != null ? fmtTok(avg) : '—'],
    ['sessions', String(state.sessions.length)],
    ['re-ingest 15m', state.kpi.reingest != null ? state.kpi.reingest.toFixed(1) + '%' : '—'],
    ['mtp 15m', state.kpi.mtp != null ? state.kpi.mtp.toFixed(1) + '%' : '—'],
    ['power limit', state.gpu ? Math.round(state.gpu.powerLimit || 0) + ' W' : '—'],
  ].map(([a, b]) => `<span>${a} <b>${b}</b></span>`).join('');
}

function renderCtxGraph() {
  const rows = state.requests.slice(0, 14);
  const el = $('#cgrows');
  if (!rows.length) {
    el.innerHTML = '<div class="nodata"><b>NO DATA</b><span>no inference requests buffered yet</span></div>';
  } else {
    el.innerHTML = rows.map(r => {
      const scale = r.window || (r.cache || 0) + (r.fresh || 0) + (r.output || 0) || 1;
      const w = x => Math.max(0, Math.min(100, 100 * (x || 0) / scale));
      const id = String(r.id || '').replace(/^req#|^sim-|swap-/g, '');
      const lbl = `<b>${clockStr(r.t)}</b> ${r.model || r.origin || '?'}${id ? ' · ' + id : ''}`;
      const tail = r.window ? `${fmtTok((r.cache || 0) + (r.fresh || 0) + (r.output || 0))} / ${fmtTok(r.window)}` : fmtTok(r.output || 0);
      return `<div class="cg-row">
        <span class="cg-lbl" title="${r.detail || ''}">${lbl}</span>
        <div class="cg-bar">
          <i style="width:${w(r.cache)}%;background:${COL.cyan}"></i><i style="width:${w(r.fresh)}%;background:${COL.yellow}"></i><i style="width:${w(r.output)}%;background:${COL.green}"></i>
        </div>
        <span class="cg-val">${tail}</span>
      </div>`;
    }).join('');
  }
  const set = (id, v, suffix = '') => { const e = $(id); if (e) e.textContent = v == null ? '—' : v + suffix; };
  set('#cg-tax', state.kpi.reingest != null ? state.kpi.reingest.toFixed(1) : null, '%');
  const m = $('#cg-mtp');
  if (m) m.textContent = state.kpi.mtp != null ? state.kpi.mtp.toFixed(1) + '%' : '—';
  set('#cg-cache', state.kpi.cache != null ? state.kpi.cache.toFixed(1) : null, '%');
  $('#cg-reqs').textContent = `${state.requests.length} buffered (last 14 shown)`;
}

function renderPool() {
  const body = $('#poolbody');
  if (!body) return;
  const ses = state.sessions;
  const demo = state.mode === 'demo';
  const count = demo ? 32 : Math.max(ses.length, 0);
  const cells = body.querySelectorAll('.cell');
  $('#poolhint').textContent = demo ? '32 kv slots (sim)' : `${count} kv slot${count === 1 ? '' : 's'} · fill = prompt vs window`;
  if (!count) {
    body.innerHTML = '<div class="nodata"><b>NO DATA</b><span>no engine slots reporting</span></div>';
    return;
  }
  if (cells.length !== count) {
    body.innerHTML = Array.from({ length: count }, (_, i) => {
      const s = ses[i] || {};
      const acc = demo ? [COL.cyan, COL.purple, COL.green, COL.yellow, COL.orange][i % 5] : (s.accent || COL.purple);
      return `<div class="cell"><span>${demo ? 'kv' + i : s.id || 's' + i}</span><i style="background:${acc};box-shadow:0 0 12px ${acc}"></i></div>`;
    }).join('');
  }
  const cs = body.querySelectorAll('.cell i');
  for (let i = 0; i < count; i++) {
    let p = 0;
    if (demo) p = state.poolFakes[i] || 0;
    else { const s = ses[i]; p = s && s.window ? Math.min(1, (s.used || 0) / s.window) : 0; }
    if (cs[i]) cs[i].style.height = Math.round(p * 100) + '%';
  }
}

/* --- pointer light ----------------------------------------------------------- */
let pending = null;
addEventListener('pointermove', e => {
  if (REDUCED) return;
  const card = e.target.closest('.glass');
  if (!card) return;
  pending = { card, x: e.clientX, y: e.clientY };
}, { passive: true });

function applyLight() {
  if (pending) {
    const { card, x, y } = pending;
    const r = card.getBoundingClientRect();
    card.style.setProperty('--mx', `${((x - r.left) / r.width * 100).toFixed(1)}%`);
    card.style.setProperty('--my', `${((y - r.top) / r.height * 100).toFixed(1)}%`);
    pending = null;
  }
  requestAnimationFrame(applyLight);
}

/* --- keys ---------------------------------------------------------------------- */
addEventListener('keydown', e => {
  if (e.key === 'p' || e.key === 'P') state.paused = !state.paused;
  if (e.key === 'r' || e.key === 'R') {
    if (state.mode === 'demo') {
      rand = mulberry32((Math.random() * 2 ** 32) | 0);
      for (const k of Object.keys(state.kpiHist)) state.kpiHist[k] = [];
      for (const k of Object.keys(state.engHist)) state.engHist[k] = [];
    } else {
      fetchSnap().then(applySnapshot).catch(() => {});
    }
  }
});

/* --- run ------------------------------------------------------------------------ */
buildBar(); buildAlertbar(); buildKpi(); buildSignal(); buildGauges(); buildContext();
buildLedger(); buildCtxGraph(); buildLanes(); buildStream(); buildPool();

if (DEMO) {
  initDemo();
} else {
  bootLive();
  renderText(); paint();
}

let last = performance.now(), textAcc = 0, paintAcc = 0, simAcc = 0;
function loop(nowMs) {
  const dt = (nowMs - last) / 1000; last = nowMs;
  if (state.mode === 'demo' && !state.paused) {
    simAcc += dt;
    if (simAcc >= 0.25) { simAcc = 0; demoStep(); }
  }
  textAcc += dt;
  if (textAcc >= 1) { textAcc = 0; renderText(); }
  paintAcc += dt;
  if (paintAcc >= 0.2) { paintAcc = 0; paint(); }
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
