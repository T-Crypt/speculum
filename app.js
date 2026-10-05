/* =========================================================================
   app.js — Speculum monitor. Feed (SSE + poll fallback), state, paint loop,
   and all panel rendering. Live and demo modes fill exactly the same
   state shape; the paint layer is mode-agnostic.
   UI primitives: ui.js · view-model: viewmodel.js · ripple: ripple.js
   No build, no dependencies. One <script type="module">.
   ========================================================================= */

import {
  prefs, cssVar, invalidateStyleCache, seriesColor, motionReduced,
  fmtTok, fmtNum, fmtDur, clockStr, upStr, decodeEscapes, deltaText, hexToRgba,
  el, Panel, StaleChip, Stat, Meter, sparkCanvas, paintSpark,
  Badge, StatusPill, DataTable, TableRow, EmptyState, Skeleton,
  Chips, Segmented, Menu,
} from './ui.js';
import {
  buildEngineRegistry, engineState, engineBadgeStatus,
  overallState, staleInfo, staleLabel, tokenLedger,
} from './viewmodel.js';
import { applyRippleSetting, pulse } from './ripple.js';

const $ = (s, r = document) => r.querySelector(s);
const root = document.documentElement;
const DEMO = new URLSearchParams(location.search).has('demo');

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
  sessions: [],       // flattened: {id, engine, engineKey, origin, window, used, cached}
  poolFakes: [],      // demo-only
  t: 0,
};
const KPI_KEYS = ['tps', 'rpm', 'p95', 'ttft', 'tpot', 'vram', 'cache', 'reingest', 'mtp', 'queue'];
const HIST_CAP = 3600;
/* --- KPI thresholds and specs (single source of truth for value coloring) ---- */
const THRESHOLDS = {
  queue:    { warn: 4,    crit: 10,    dir: 'high' },
  ttft:     { warn: 1000, crit: 2500,  dir: 'low'  },
  tpot:     { warn: 50,   crit: 120,   dir: 'low'  },
  p95:      { warn: 5000, crit: 15000, dir: 'low'  },
  cache:    { warn: 50,   crit: 25,    dir: 'low'  },
  reingest: { warn: 50,   crit: 75,    dir: 'high' },
  vram:     { warn: 0.85, crit: 0.95,  dir: 'frac' }, // of total
  temp:     { warn: 80,   crit: 90,    dir: 'high' },
  ctx:      { warn: 0.75, crit: 0.90,  dir: 'frac' }, // of window
};
function kpiStatus(key, v, extra = {}) {
  if (v == null || !isFinite(v)) return null;
  const t = THRESHOLDS[key];
  if (!t) return null;
  if (t.dir === 'frac') {
    const f = v / (extra && extra.total > 0 ? extra.total : 1);
    return f >= t.crit ? 'crit' : f >= t.warn ? 'warn' : null;
  }
  if (t.dir === 'high') return v >= t.crit ? 'crit' : v >= t.warn ? 'warn' : null;
  return v <= t.crit ? 'crit' : v <= t.warn ? 'warn' : null;
}
/* tone = series palette index for the group's accent bar / value / spark
   (1 cyan, 2 purple, 3 green, 4 yellow); queue sits in Capacity with VRAM
   so no group column is a single orphan tile */
const KPI_GROUPS = [
  { name: 'Throughput', tone: 1, keys: ['tps', 'rpm'] },
  { name: 'Latency',    tone: 2, keys: ['ttft', 'tpot', 'p95'] },
  { name: 'Cache',      tone: 3, keys: ['cache', 'reingest', 'mtp'] },
  { name: 'Capacity',   tone: 4, keys: ['vram', 'queue'] },
];
const KPI_TONE = {};
for (const g of KPI_GROUPS) for (const k of g.keys) KPI_TONE[k] = g.tone;
const KPI_SPECS = {
  tps:      { label: 'Decode rate',    unit: 'tok/s', fmt: v => String(Math.round(v)), diff: d => String(Math.round(d)) },
  rpm:      { label: 'Requests / min', fmt: v => String(Math.round(v)), diff: d => String(Math.round(d)) },
  queue:    { label: 'Queue depth',    unit: 'req',   fmt: v => String(Math.round(v)), diff: d => String(Math.round(d)) },
  ttft:     { label: 'First token',    unit: 's',     fmt: v => (v / 1000).toFixed(2), diff: d => (d / 1000).toFixed(2) + 's' },
  tpot:     { label: 'Time / token',   unit: 'ms',    fmt: v => String(Math.round(v)), diff: d => String(Math.round(d)) + 'ms' },
  p95:      { label: 'p95 total',      unit: 's',     fmt: v => (v / 1000).toFixed(1), diff: d => (d / 1000).toFixed(1) + 's' },
  cache:    { label: 'Cache hit',      unit: '%',     fmt: v => v.toFixed(1), diff: d => d.toFixed(1) + 'pt' },
  reingest: { label: 'Re-ingest tax',  unit: '%',     fmt: v => v.toFixed(1), diff: d => d.toFixed(1) + 'pt' },
  mtp:      { label: 'MTP accept',     unit: '%',     fmt: v => v.toFixed(1), diff: d => d.toFixed(1) + 'pt' },
  vram:     { label: 'VRAM',           unit: 'GB',    fmt: v => v.toFixed(1), diff: d => d.toFixed(1) },
};
/* latency KPIs read 0 (or null) when the engine is idle — no requests to
   measure — which must display as a muted "—", never cross a "low" threshold
   into red */
const IDLE_ZERO = new Set(['ttft', 'p95', 'tpot']);
/* value 15 min (900 s @ 1 Hz) back in a KPI history, or null when unknown */
function histPrev15m(hist) {
  if (!Array.isArray(hist) || hist.length < 2) return null;
  const i = hist.length - 1 - 900;
  const v = i >= 0 ? hist[i] : hist[0];
  return isFinite(v) ? v : null;
}
/* --- small helpers (data-side) ------------------------------------------- */
function fit(c) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = (typeof c.clientWidth === 'number' ? c.clientWidth : 0) || 0;
  const h = (typeof c.clientHeight === 'number' ? c.clientHeight : 0) || 0;
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
const feed = { lastTick: null };
let es = null, pollTimer = null;

function fetchSnap() {
  const r = fetch('api/snapshot', { cache: 'no-store' });
  return r.then(x => { if (!x.ok) throw new Error('http ' + x.status); return x.json(); });
}

/* history rollups for the Throughput DB ranges: rows are
   per-minute for 6h/24h and per-hour for 7d/30d, oldest first */
function fetchHistory(range) {
  const r = fetch('api/history?range=' + range, { cache: 'no-store' });
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
  feed.lastTick = performance.now();
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
  feed.lastTick = performance.now();
  refreshDerived();
}

function syncEnginesFromTick(d) {
  if (d.engines) {
    /* the collector removes an engine that is gone (NInfer without a live ninfer-serve); so does the
       card list. Strata is kept: its card follows the tick's strata_up below. */
    const live = new Set(Object.keys(d.engines));
    state.engines = state.engines.filter(e => live.has(e.key) || e.key === 'strata');
  }
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
    e.reason = t.reason || null;
    e.idle_vram = t.idle_vram || null;
    if (t.hist) state.engHist[e.key] = [...t.hist];
    if (t.rate != null) pushHist(state.engHist[e.key], t.rate);
    else if (t.rates) {
      const v = t.rates.decode_tps != null ? t.rates.decode_tps : t.rates.gen_tps_inst;
      pushHist(state.engHist[e.key], v != null ? v : 0);
    }
  }
  if (d.strata_up != null) {
    /* the tick's strata_up field names the engine key; the display label
       and window come from the payload itself (see the engines map),
       never from a UI-side literal */
    let s = state.engines.find(x => x.key === 'strata');
    if (!s) {
      s = { key: 'strata', label: 'strata', origin: 'strata', up: d.strata_up, latched: false,
            queue: null, rates: null, mtp: null, backend: null, window: null,
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
    if (!e.up && e.key !== 'strata') continue;
    for (const s of e.sessions || []) {
      state.sessions.push({
        id: s.id + (s.session ? '-' + s.session : ''),
        engine: e.label, engineKey: e.key, origin: e.origin,
        window: s.window || e.window, used: s.used, cached: s.cached,
        processing: s.processing,
      });
    }
  }
}

function openSSE() {
  if (es) { try { es.close(); } catch {} es = null; }
  es = new EventSource('api/stream');
  es.onopen = () => { state.feed = 'sse'; feed.lastTick = performance.now(); };
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
function mulberry32(a) {
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 0x21F0FFAD);
    t = Math.imul(t ^ (t >>> 7), 0x84222325);
    return ((t ^ (t >>> 16)) >>> 0) / 4294967296;
  };
}
let rand = mulberry32(0xC0FFEE);

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
  feed.lastTick = performance.now();
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

/* --- ui settings (persisted under speculum.ui.*) --------------------------- */
const ui = {
  theme: prefs.theme,
  motion: prefs.reduceMotion,
  ripple: prefs.ripple,
  /* ?view=basic|advanced overrides the stored choice for this page (bookmarkable), without saving it */
  view: ['basic', 'advanced'].includes(new URLSearchParams(location.search).get('view'))
    ? new URLSearchParams(location.search).get('view') : prefs.view,   // 'basic' | 'advanced'
  panels: [],   // panel modules register: { el, head, chip }
};

function applyTheme() {
  root.dataset.theme = ui.theme;
  invalidateStyleCache();
}
function applyMotion() {
  if (ui.motion === 'on') root.dataset.motion = 'reduced';
  else delete root.dataset.motion;
  applyRippleSetting(); // ripple must track the effective motion state
}

/* --- view (Basic / Advanced, persisted under speculum.ui.view) -------------- */
/* Basic is the everyday five: KPI, throughput, GPU & host, model timeline,
   engines. Advanced shows all twelve sections. The class on #deck drives
   layout.css; p.hidden keeps the paint loop off the sections that are not shown. */
const BASIC_PANELS = new Set(['p-kpi', 'p-throughput', 'p-gpu', 'p-timeline', 'p-engines']);
function panelVisible(p) { return ui.view === 'advanced' || BASIC_PANELS.has(p.id); }
function applyView() {
  const deck = $('#deck');
  deck.classList.toggle('view-basic', ui.view === 'basic');
  deck.classList.toggle('view-advanced', ui.view === 'advanced');
  for (const p of ui.panels) p.hidden = !panelVisible(p);
}

/* --- top bar ----------------------------------------------------------------- */
function settingsRow(label, sub, control) {
  return el('div', { class: 'menu-row' },
    el('div', {}, el('div', { class: 'menu-row-label' }, label,
      sub ? el('small', {}, sub) : null)),
    control);
}

function buildTopbar() {
  const bar = $('#topbar');
  const pill = StatusPill({ state: 'paused', label: 'Booting' });
  const chip = (id, label, cls = '') => el('span', { class: 'kv' + (cls ? ' ' + cls : '') },
    label, el('b', { id }, '—'));
  const viewCtl = Segmented([{ value: 'basic', label: 'Basic' }, { value: 'advanced', label: 'Advanced' }], ui.view, v => {
    ui.view = v; prefs.view = v; applyView();
  });
  viewCtl.setAttribute('aria-label', 'Panel view');
  const menu = Menu('Settings', [
    settingsRow('Theme', null,
      Segmented([{ value: 'dark', label: 'Dark' }, { value: 'light', label: 'Light' }], ui.theme, v => {
        ui.theme = v; prefs.theme = v; applyTheme();
      })),
    settingsRow('Reduce motion', 'also follows the OS preference',
      Segmented([{ value: 'system', label: 'System' }, { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }], ui.motion, v => {
        ui.motion = v; prefs.reduceMotion = v; applyMotion();
      })),
    settingsRow('Ripple effect', 'single pulse on data update',
      Segmented([{ value: 'off', label: 'Off' }, { value: 'on', label: 'On' }], ui.ripple, v => {
        ui.ripple = v; prefs.ripple = v; applyRippleSetting();
      })),
  ]);
  bar.append(
    el('div', { class: 'brand' }, el('b', {}, 'Speculum'), el('span', {}, 'local LLM runtime')),
    el('span', { class: 'spacer' }),
    el('div', { class: 'cluster' },
      pill,
      chip('tb-gpu', 'GPU'),
      chip('tb-driver', 'Driver', 'hide-sm'),
      chip('tb-uptime', 'Uptime'),
      chip('tb-feed', 'Feed'),
      viewCtl,
      menu.el,
    ),
  );
  ui.pill = pill;
  ui.pillLabel = pill.children[1];
  ui.tb = {
    gpu: $('#tb-gpu'), driver: $('#tb-driver'),
    uptime: $('#tb-uptime'), feed: $('#tb-feed'),
  };
}

function renderTopbar() {
  const o = overallState({ mode: state.mode, paused: state.paused, alerts: state.alerts, engines: state.engines });
  ui.pill.setAttribute('data-state', o.state);
  ui.pill.setAttribute('aria-label', 'Overall state: ' + o.word + (state.alerts.length ? ` — ${state.alerts[0]}` : ''));
  ui.pillLabel.textContent = o.word;
  ui.tb.gpu.textContent = state.gpu ? state.gpu.name : 'no GPU';
  ui.tb.driver.textContent = state.gpu ? state.gpu.driver : '—';
  ui.tb.uptime.textContent = state.host ? upStr(state.host.uptime) : '—';
  ui.tb.feed.textContent = state.feed;
}

function renderFoot() {
  $('#foot').innerHTML =
    `<kbd>P</kbd> pause · <kbd>R</kbd> resync · feed: ${state.feed} · mode: ${state.mode} · ` +
    `<code>?demo</code> = seeded simulator`;
}

/* --- panel registry (panel modules push themselves in) ----------------------- */
function registerPanel(p) { p.hidden = !panelVisible(p); ui.panels.push(p); return p; }
function setPanelsStale() {
  const now = performance.now();
  const info = state.mode === 'demo' ? null : staleInfo(feed.lastTick, now);
  for (const p of ui.panels) {
    if (p.hidden) continue;
    p.el.classList.toggle('is-stale', !!info);
    if (p.chip) p.chip.textContent = info ? staleLabel(info) : 'Stale';
  }
}

/* --- panel scaffolding ---------------------------------------------------------- */
/* one section shell in index.html; returns a registered panel with a stale chip */
function makePanel(id, title, hint, { flush = false, tools = null } = {}) {
  const p = Panel({ title, hint, flush, tools });
  const chip = StaleChip();
  p.head.append(chip);
  $('#' + id).append(p.el);
  return registerPanel({ id, el: p.el, head: p.head, body: p.body, chip, paint: null, render: null, onData: null });
}
/* ripple pulse contract: pulse p.el only when the signature string changes
   (pulse() itself is a no-op while ripple / motion are off) */
function panelPulse(p, sig) {
  if (p._pulseSig === sig) return;
  p._pulseSig = sig;
  pulse(p.el);
}

/* --- P1 · KPI strip ----------------------------------------------------------- */
/* four dense group columns: tile = small-caps label, 30 px mono value in the
   group tone (threshold status wins), muted unit, delta vs 15 min, 28 px
   sparkline in the tone. KPI_GROUPS / KPI_SPECS are the single spec. */
function buildKpi() {
  const p = makePanel('p-kpi', 'Key metrics', '1 Hz · warn/crit thresholds · delta vs 15 min');
  const refs = {};
  const groups = [];
  for (const g of KPI_GROUPS) {
    const grp = el('div', { class: 'kpi-group', data: { tone: g.tone } });
    grp.append(el('div', { class: 'kpi-group-label' }, g.name));
    for (const k of g.keys) {
      const spec = KPI_SPECS[k];
      const spark = sparkCanvas('stat-spark', spec.label + ' trend');
      const st = Stat({ label: spec.label, unit: spec.unit || null, value: '—', spark });
      st._unitSpan = spec.unit ? el('span', { class: 'unit' }, spec.unit) : null;
      grp.append(st.el);
      refs[k] = st;
    }
    groups.push(grp);
  }
  p.body.append(el('div', { class: 'kpi-groups' }, groups));
  p.paint = st => {
    let sig = '';
    for (const k of KPI_KEYS) {
      const stt = refs[k], spec = KPI_SPECS[k];
      const v = st.kpi ? st.kpi[k] : null;
      let has = v != null && isFinite(v);
      const idle = has && IDLE_ZERO.has(k) && v === 0;   // idle: show "—", not red
      if (idle) has = false;
      const vargs = [has ? spec.fmt(v) : '—'];
      if (stt._unitSpan) vargs.push(stt._unitSpan);
      stt.value.replaceChildren(...vargs);
      const extra = k === 'vram'
        ? { total: (st.kpi && st.kpi.vram_total) || (st.gpu && st.gpu.vramTotal) || 0 }
        : {};
      const s2 = kpiStatus(k, has ? v : null, extra);
      if (s2) stt.value.dataset.status = s2;
      else if (idle) stt.value.dataset.status = 'idle';
      else stt.value.removeAttribute('data-status');
      const prev = histPrev15m(st.kpiHist && st.kpiHist[k]);
      stt.delta.textContent = has ? (deltaText(v, prev, spec.diff, 'vs 15m') || '') : '';
      paintSpark(stt.spark, (st.kpiHist && st.kpiHist[k]) || [],
        seriesColor((KPI_TONE[k] || 1) - 1));
      sig += k + ':' + (has ? Math.round(v) : 'n') + ';';
    }
    return sig;
  };
}

/* --- P2 · Throughput ------------------------------------------------------------ */
/* 1-s-resolution canvas: one line per registry engine (series color), 4 y
   gridlines, hover crosshair + tooltip; range 15m/1h/6h/24h/7d/30d.
   15m/1h read the 60-min in-memory ring (the drawn line spans what exists,
   right-anchored); 6h and longer read the collector's SQLite rollups through
   /api/history, one fetch per selected range and at most one per minute. */
const TP_RANGES = [
  { value: '15m', label: '15m', sec: 900 },
  { value: '1h', label: '1h', sec: 3600 },
  { value: '6h', label: '6h', sec: 21600, db: true, bucket: 60 },
  { value: '24h', label: '24h', sec: 86400, db: true, bucket: 60 },
  { value: '7d', label: '7d', sec: 604800, db: true, bucket: 3600 },
  { value: '30d', label: '30d', sec: 2592000, db: true, bucket: 3600 },
];
function tpRangeSec(v) { const r = TP_RANGES.find(x => x.value === v); return r ? r.sec : 3600; }
function niceTicks(max, n = 4) {
  if (!(max > 0) || !isFinite(max)) return [0, 1];
  const raw = max / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const top = Math.ceil(max / step) * step;
  const out = [];
  for (let v = 0; v <= top + step * 1e-6; v += step) out.push(v);
  return out;
}
/* EMA over the 1 s throughput samples: 8 s time constant
   (alpha = 1 - e^(-1/8) per sample). A paint-time projection — the raw
   history in state.engHist is never modified, the tooltip still reads it. */
function emaSmooth(data, alpha = 1 - Math.exp(-1 / 8)) {
  const out = new Array(data.length);
  let s = null;
  for (let i = 0; i < data.length; i++) {
    const v = data[i];
    if (v == null || !isFinite(v)) { out[i] = null; continue; }
    s = s == null ? v : s + alpha * (v - s);
    out[i] = s;
  }
  return out;
}
/* contiguous runs of finite values (gaps break a run) → [[i, v], …] per run */
function runsOf(arr) {
  const runs = [];
  let cur = null;
  for (let i = 0; i < arr.length; i++) {
    const v = arr[i];
    if (v == null || !isFinite(v)) { if (cur) { cur = null; } continue; }
    if (!cur) { cur = []; runs.push(cur); }
    cur.push([i, v]);
  }
  return runs;
}
/* x-axis labels relative to now: minutes for the 15 m / 1 h ranges
   ("-15m" "-10m" "-5m" "now", "-60m" … "now"), whole hours for the 6 h / 24 h
   DB ranges, whole days for 7 d / 30 d (thinned so the axis stays readable) */
function tpXTicks(sec) {
  if (sec > 3600) {
    const u = sec > 86400 ? 86400 : 3600;
    const thin = sec > 604800 ? 5 : sec > 86400 ? 2 : sec > 21600 ? 6 : 2;
    const out = [];
    for (let k = sec / u; k > 0; k -= thin) out.push({ frac: (sec - k * u) / sec, label: `−${k}${u === 86400 ? 'd' : 'h'}` });
    out.push({ frac: 1, label: 'now' });
    return out;
  }
  const n = sec <= 900 ? 3 : 4;
  const step = sec / n;
  const unit = s => (s >= 3600 ? `${s / 3600}h` : `${Math.round(s / 60)}m`);
  const out = [];
  for (let i = 0; i <= n; i++) out.push({ frac: i / n, label: i === n ? 'now' : '−' + unit((n - i) * step) });
  return out;
}
/* same labels for the tooltip header, over a DB range (bucket-snapped) */
function tpRelLabel(sec) {
  if (sec <= 0) return 'now';
  if (sec >= 86400) return '−' + Math.round(sec / 86400) + 'd';
  if (sec >= 3600) return '−' + Math.round(sec / 3600) + 'h';
  return '−' + Math.round(sec / 60) + 'm';
}
function buildThroughput() {
  const p = makePanel('p-throughput', 'Throughput', 'decode tok/s per engine · 1 s samples, 8 s smoothed', {
    tools: el('span', { class: 'tp-tools' },
      Segmented(TP_RANGES.map(r => ({ value: r.value, label: r.label })), '1h', v => {
        tp.range = v; tp.legendDirty = true;
      })),
  });
  const wrap = el('div', { class: 'tp-wrap' });
  const canvas = el('canvas', { class: 'tp-canvas', 'aria-label': 'Throughput per engine over time' });
  const tip = el('div', { class: 'tp-tip', hidden: true });
  const note = el('p', { class: 'tp-note', hidden: true });
  wrap.append(note, canvas, tip);
  const legend = el('div', { class: 'legend', 'aria-label': 'Engine legend' });
  p.body.append(wrap, legend);
  const tp = { range: '1h', canvas, wrap, tip, legend, legendDirty: true, geo: null, hoverX: null,
    hist: null, histAt: 0, histError: false, fetching: false, note, noteText: '' };
  canvas.addEventListener('mousemove', e => {
    const r = canvas.getBoundingClientRect();
    tp.hoverX = Math.max(0, Math.min(r.width, e.clientX - r.left));
  });
  canvas.addEventListener('mouseleave', () => {
    tp.hoverX = null;
    tip.hidden = true;
  });
  const setNote = txt => {
    if (tp.noteText === txt) return;   // the paint loop runs 5 Hz: write only on change
    tp.noteText = txt;
    tp.note.hidden = !txt;
    tp.note.textContent = txt || '';
  };
  /* one history fetch per selected DB range, then at most once a minute while
     that range stays selected; a failed fetch keeps the rows already drawn */
  function tpPoll(R) {
    if (DEMO || tp.fetching) return;
    if (tp.hist && tp.hist.range === R.value && performance.now() - tp.histAt < 60000) return;
    tp.fetching = true;
    fetchHistory(R.value).then(j => {
      tp.hist = { range: R.value, rows: Array.isArray(j.rows) ? j.rows : [] };
      tp.histError = false;
    }).catch(() => { tp.histError = true; }).finally(() => {
      tp.histAt = performance.now();
      tp.fetching = false;
    });
  }
  p.onData = () => { tp.legendDirty = true; };
  const hintEl = p.el.querySelector('.panel-hint');
  p.paint = st => {
    const reg = buildEngineRegistry(st.engines);
    const Rh = TP_RANGES.find(x => x.value === tp.range) || TP_RANGES[1];
    const hint = Rh.db ? `decode tok/s per engine · ${Rh.bucket >= 3600 ? 'hourly' : 'per-minute'} averages from history`
                       : 'decode tok/s per engine · 1 s samples, 8 s smoothed';
    if (hintEl && hintEl.textContent !== hint) hintEl.textContent = hint;
    /* history keeps engines that are not running now (NInfer when ninfer-serve is stopped): they get a
       line after the registry's, in the next series colours, and a muted "history" legend item */
    const hrowsAll = Rh.db && tp.hist && tp.hist.range === Rh.value ? tp.hist.rows : [];
    const past = [...new Set(hrowsAll.map(r => r.engine))].filter(k => k && !reg.byKey.has(k)).sort();
    const pastKey = past.join(',');
    if (pastKey !== tp.pastKey) { tp.pastKey = pastKey; tp.legendDirty = true; }
    if (tp.legendDirty) {
      tp.legendDirty = false;
      tp.legend.replaceChildren(...past.map((k, j) => el('span', { class: 'legend-item legend-item--past' },
        el('span', { class: 'swatch', style: { '--swatch': seriesColor(reg.list.length + j) } }),
        k, Badge({ status: 'neutral', label: 'history', dot: false, muted: true, swatch: null }))),
      ...reg.list.map(e => {
        const info = engineState(e);
        return el('span', { class: 'legend-item' },
          el('span', { class: 'swatch', style: { '--swatch': seriesColor(reg.byKey.get(e.key).colorIndex) } }),
          e.label || e.key,
          Badge({ status: engineBadgeStatus(e), label: info.word, dot: false,
            muted: info.muted && e.up !== true, swatch: null }));
      }));
    }
    const { ctx, w, h } = fit(tp.canvas);
    const R = TP_RANGES.find(x => x.value === tp.range) || TP_RANGES[1];
    const pad = { l: 40, r: 6, t: 8, b: 18 };
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    const series = [];
    let max = 0;
    if (R.db) {
      tpPoll(R);
      const hrows = tp.hist && tp.hist.range === R.value ? tp.hist.rows : [];
      if (!hrows.length) {
        setNote(DEMO || tp.histError ? 'history needs the collector' : 'no history rows in this range');
        ctx.clearRect(0, 0, w, h);
        tp.geo = null;
        tip.hidden = true;
        return R.value + '|no history';
      }
      setNote('');
      /* one bucket per rollup row across the whole range; a bucket with no row
         is zero, never interpolated across */
      const n = Math.round(R.sec / R.bucket);
      const base = ((st.t && st.t > 1e9) ? st.t : Date.now() / 1000) - R.sec;
      for (const e of reg.list) {
        const buckets = new Array(n).fill(0);
        for (const row of hrows) {
          if (row.engine !== e.key) continue;
          const t = row.minute != null ? row.minute : row.hour;
          if (t == null) continue;
          const i = Math.floor((t - base) / R.bucket);
          if (i < 0 || i >= n) continue;
          buckets[i] = row.decode_tps_avg || 0;
        }
        for (const v of buckets) if (isFinite(v) && v > max) max = v;
        series.push({ e, data: buckets, sm: buckets, color: seriesColor(reg.byKey.get(e.key).colorIndex) });
      }
      past.forEach((k, j) => {
        const buckets = new Array(n).fill(0);
        for (const row of hrows) {
          if (row.engine !== k) continue;
          const t = row.minute != null ? row.minute : row.hour;
          const i = t == null ? -1 : Math.floor((t - base) / R.bucket);
          if (i >= 0 && i < n) buckets[i] = row.decode_tps_avg || 0;
        }
        for (const v of buckets) if (isFinite(v) && v > max) max = v;
        series.push({ e: { key: k, label: k }, data: buckets, sm: buckets, color: seriesColor(reg.list.length + j) });
      });
    } else {
      for (const e of reg.list) {
        const full = st.engHist[e.key] || [];
        const data = full.slice(-R.sec);
        const sm = emaSmooth(full).slice(-R.sec);
        for (const v of sm) if (isFinite(v) && v > max) max = v;
        series.push({ e, data, sm, color: seriesColor(reg.byKey.get(e.key).colorIndex) });
      }
    }
    const ticks = niceTicks(Math.max(max, 1));
    const yMax = ticks[ticks.length - 1];
    /* x for index i of a right-anchored series of length n over R.sec seconds
       (the DB arrays span the whole range, so their index maps straight across) */
    const xAt = (i, n) => R.db ? pad.l + (i / Math.max(1, n - 1)) * iw
      : pad.l + iw - ((n - 1 - i) / Math.max(1, R.sec - 1)) * iw;
    const yAt = v => pad.t + ih - (v / yMax) * ih;
    ctx.clearRect(0, 0, w, h);
    ctx.font = `10px ${cssVar('--font-mono')}`;
    ctx.fillStyle = cssVar('--text-3');
    ctx.strokeStyle = cssVar('--border');
    ctx.lineWidth = 1;
    for (const tv of ticks) {
      const y = yAt(tv);
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + iw, y); ctx.stroke();
      ctx.textAlign = 'right'; ctx.fillText(fmtTok(tv), pad.l - 5, y + 3);
    }
    /* vertical gridlines at the relative-time ticks, then the baseline */
    const xticks = tpXTicks(R.sec);
    for (const t of xticks) {
      if (t.frac === 0 || t.frac === 1) continue;
      const x = pad.l + t.frac * iw;
      ctx.strokeStyle = cssVar('--border');
      ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ih); ctx.stroke();
    }
    ctx.strokeStyle = cssVar('--border-strong');
    ctx.beginPath(); ctx.moveTo(pad.l, pad.t + ih); ctx.lineTo(pad.l + iw, pad.t + ih); ctx.stroke();
    /* area fills: gradient .30 → 0 in the series colour, flat when the stub
       context cannot parse hex / build gradients (same rule as paintSpark) */
    const base = pad.t + ih;
    for (const s of series) {
      for (const run of runsOf(s.sm)) {
        if (run.length < 2) continue;
        const grad = hexToRgba(s.color) && typeof ctx.createLinearGradient === 'function'
          ? ctx.createLinearGradient(0, pad.t, 0, base) : null;
        if (grad) {
          grad.addColorStop(0, hexToRgba(s.color, 0.3));
          grad.addColorStop(1, hexToRgba(s.color, 0));
          ctx.fillStyle = grad;
        } else { ctx.globalAlpha = 0.3; ctx.fillStyle = s.color; }
        ctx.beginPath();
        ctx.moveTo(xAt(run[0][0], s.sm.length), base);
        for (const [i, v] of run) ctx.lineTo(xAt(i, s.sm.length), yAt(v));
        ctx.lineTo(xAt(run[run.length - 1][0], s.sm.length), base);
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }
    for (const s of series) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.shadowColor = s.color;
      ctx.shadowBlur = 8;
      for (const run of runsOf(s.sm)) {
        if (run.length < 2) continue;
        ctx.beginPath();
        run.forEach(([i, v], j) => {
          const x = xAt(i, s.sm.length), y = yAt(v);
          j === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        });
        ctx.stroke();
      }
      ctx.shadowBlur = 0;
    }
    /* x-axis time labels: relative minutes/hours for the selected range */
    for (const t of xticks) {
      const x = pad.l + t.frac * iw;
      ctx.fillStyle = cssVar('--text-3');
      ctx.textAlign = t.frac === 0 ? 'left' : t.frac === 1 ? 'right' : 'center';
      ctx.fillText(t.label, x, h - 5);
    }
    /* crosshair + tooltip rebuilt from last-paint geometry */
    tp.geo = { pad, iw, ih, R, series, yMax, w, h, xAt, yAt };
    if (tp.hoverX != null) {
      const gx = Math.max(pad.l, Math.min(pad.l + iw, tp.hoverX));
      ctx.strokeStyle = cssVar('--border-strong');
      ctx.beginPath(); ctx.moveTo(gx, pad.t); ctx.lineTo(gx, pad.t + ih); ctx.stroke();
      const secAgo = Math.round((pad.l + iw - gx) / iw * (R.sec - 1));
      const rows = [];
      const head = R.db ? tpRelLabel(Math.round(secAgo / R.bucket) * R.bucket)
        : (secAgo === 0 ? 'now' : '−' + secAgo + 's');
      let html = `<span class="t">${head}</span>`;
      for (const s of series) {
        const n = s.data.length;
        if (!n) continue;
        const i = Math.max(0, Math.min(n - 1, n - 1 - Math.round(secAgo * (n - 1) / Math.max(1, R.sec - 1))));
        const v = s.data[i];
        const val = isFinite(v) ? String(Math.round(v)) : '—';
        html += `<span class="row"><span class="swatch" style="background:${s.color}"></span>${s.e.label || s.e.key}<b>${val}</b></span>`;
        rows.push(s.e.key + ':' + val);
      }
      tip.innerHTML = html;
      tip.hidden = false;
      const left = gx + 14 + 140 > w ? Math.max(4, gx - 14 - 140) : gx + 14;
      tip.style.left = left + 'px';
    } else {
      tip.hidden = true;
    }
    return R.value + '|' + series.map(s => s.e.key + ':' + Math.round(s.data[s.data.length - 1] || 0)).join(',');
  };
  return { tp, p };
}

/* --- P3 · GPU & host -------------------------------------------------------------- */
/* four meters (static fractions, built once, updated via Meter.set) + two
   key/value readout columns. n/a styling wherever a source is missing. */
function buildGpu() {
  const p = makePanel('p-gpu', 'GPU & host', 'no data yet');
  const hintEl = [...p.head.children].find(c => c.className === 'panel-hint');
  const mTemp = Meter({ label: 'Temp', value: null, max: 100, unit: '°C',
    ticks: [{ at: 0.8, status: 'warn' }, { at: 0.9, status: 'crit' }] });
  const mUtil = Meter({ label: 'Util', value: null, max: 100, unit: '%' });
  const mPow  = Meter({ label: 'Power', value: null, unit: 'W' });
  const mVram = Meter({ label: 'VRAM', value: null, unit: 'GB' });
  const col = (title, rows) => {
    const c = el('div', { class: 'readout-col' });
    c.append(el('h3', {}, title));
    for (const r of rows) c.append(r.el);
    return c;
  };
  const kv = label => {
    const b = el('b', { class: 'na' }, 'n/a');
    const r = el('div', { class: 'readout' }, el('span', {}, label), b);
    return { el: r, val: b };
  };
  const rSm = kv('SM clock'), rMem = kv('Mem clock'), rFan = kv('Fan'),
        rPwLim = kv('Power limit'), rPcie = kv('Link');
  const rCpu = kv('CPU'), rRam = kv('RAM'), rLoad = kv('Load 1/5/15'),
        rProc1 = kv('Top process'), rProc2 = kv('2nd process');
  p.body.append(mTemp.el, mUtil.el, mPow.el, mVram.el,
    el('div', { class: 'readout-grid' },
      col('Device', [rSm, rMem, rFan, rPwLim, rPcie]),
      col('Host', [rCpu, rRam, rLoad, rProc1, rProc2])));
  const setR = (ref, val, fmt) => {
    const bad = val == null || (typeof val === 'number' && !isFinite(val));
    if (bad) {
      ref.val.textContent = 'n/a'; ref.val.classList.add('na');
    } else {
      ref.val.textContent = fmt(val); ref.val.classList.remove('na');
    }
  };
  p.render = st => {
    const g = st.gpu, ho = st.host;
    if (hintEl) hintEl.textContent = g ? `${g.name} · ${g.driver}` : 'no GPU data';
    mTemp.set(g ? g.temp : null, 100, g ? kpiStatus('temp', g.temp) : null);
    mUtil.set(g ? g.util : null, 100);
    mPow.set(g ? g.power : null, g ? g.powerLimit : null);
    mVram.set(g ? g.vram : null, g ? g.vramTotal : null,
      g && g.vram != null && g.vramTotal ? kpiStatus('vram', g.vram, { total: g.vramTotal }) : null);
    setR(rSm, g && g.clockSm, v => Math.round(v) + ' MHz');
    setR(rMem, g && g.clockMem, v => Math.round(v) + ' MHz');
    setR(rFan, g && g.fan, v => Math.round(v) + ' RPM');
    setR(rPwLim, g && g.powerLimit, v => Math.round(v) + ' W');
    setR(rPcie, g && g.pcie !== '—' ? g.pcie : null, v => v);
    setR(rCpu, ho && ho.cpu, v => fmtNum(v, 1) + '%');
    setR(rRam, ho && ho.ramUsed, v => `${fmtNum(v, 1)} / ${fmtNum(ho.ramTotal, 1)} GB`);
    setR(rLoad, ho && ho.load && ho.load.length ? ho.load.join('/') : null, v => v);
    const procs = (ho && ho.procs) || [];
    setR(rProc1, procs[0], pr => `${pr.name || pr.cmd || '?'} · ${fmtNum(pr.rss_gb, 1)} GB`);
    setR(rProc2, procs[1], pr => `${pr.name || pr.cmd || '?'} · ${fmtNum(pr.rss_gb, 1)} GB`);
  };
}

/* --- P4 · Token ledger ------------------------------------------------------------ */
/* rows generated/fresh/cached × [last hour, last 24 h, since start] (the view
   model), one 100 %-stacked mix bar per column, footer reqs/MTP/cache/re-ingest,
   honest notes when the request buffer is capped or empty. */
function buildLedger() {
  const p = makePanel('p-ledger', 'Token ledger', 'prompt + generated tokens by window', { flush: true });
  const dt = DataTable({ columns: [
    { label: 'Window', width: '128px' },
    { label: 'Last hour', num: true },
    { label: 'Last 24 h', num: true },
    { label: 'Since engine load', num: true },
  ], caption: 'Token ledger' });
  const foot = el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '4px 16px' } });
  const mkStat = label => {
    const s = el('span', { class: 'head-stat' }, el('span', {}, label), el('b', {}, '—'));
    foot.append(s);
    return s;
  };
  const fRe = mkStat('Re-ingest'), fMtp = mkStat('MTP accept'),
        fCache = mkStat('Cache hit'), fReqs = mkStat('Buffered');
  const notes = el('div');
  p.body.append(dt.wrap, foot, notes);
  const ROWS = ['generated', 'fresh', 'cached'];
  /* short labels: the label column is fixed-width and must not wrap */
  const LABEL = { generated: 'Generated', fresh: 'Fresh prefill', cached: 'Cached' };
  p.render = st => {
    const L = tokenLedger(st);
    const cells = (row, cls) => [
      { text: row === 'split' ? 'Split' : LABEL[row], cls: row === 'split' ? 'col-sub' : undefined },
      ...L.cols.map(c => {
        if (row === 'split') {
          const tot = (c.generated || 0) + (c.fresh || 0) + (c.cached || 0);
          const bar = el('div', { class: 'mix' });
          for (const part of ['generated', 'fresh', 'cached']) {
            const v = c[part] || 0;
            const i = el('i', { style: { width: tot > 0 ? (100 * v / tot).toFixed(2) + '%' : '0%', background: `var(--tok-${part})` } });
            bar.append(i);
          }
          return { el: bar };
        }
        return c[row] == null ? null : { text: fmtTok(c[row]), cls };
      }),
    ];
    dt.tbody.replaceChildren(...ROWS.map(r => TableRow(cells(r, 'col-num col-mono'))), TableRow(cells('split')));
    const setF = (elx, v, status) => {
      elx.children[1].textContent = v == null ? '—' : fmtNum(v, 1) + '%';
      if (status) elx.dataset.status = status; else elx.removeAttribute('data-status');
    };
    setF(fRe, L.footer.reingest, kpiStatus('reingest', L.footer.reingest));
    setF(fMtp, L.footer.mtp);
    setF(fCache, L.footer.cache, kpiStatus('cache', L.footer.cache));
    fReqs.children[1].textContent = String(L.footer.reqs);
    notes.replaceChildren(...L.notes.map(n => el('p', { class: 'ledger-note' }, n)));
    return sig4(L);
  };
  /* 1 Hz table + honest notes live in render; the pulse signature rides the same hook */
  p.onData = st => {
    const L = tokenLedger(st);
    panelPulse(p, `led:${L.footer.reqs}:${L.footer.cache != null ? Math.round(L.footer.cache) : '-'}:${L.notes.length}`);
  };
}
function sig4(L) {
  return L.cols.map(c => `${c.generated ?? '-'}|${c.fresh ?? '-'}|${c.cached ?? '-'}`).join(';');
}

/* --- P5 · Context residency ----------------------------------------------------------- */
/* stacked residency columns for the last 40 requests (oldest left, newest
   right): cached (green) + fresh prefill (orange) + generated (cyan), scaled
   to the max model window of the set — dashed line at the window (label from
   the data), faint line at 75 %. Hover column → prompt/cached/fresh/output/
   TTFT tooltip. One-line legend (layer key + 15-min re-ingest tax + cache
   hit), then the live per-session bars (cap 20 + "…N more"): mono id,
   engine badge, used / window, 10 px meter with 0.75 / 0.90 ticks. */
const RESID_N = 40;
function buildContext() {
  const p = makePanel('p-context', 'Context residency', 'last 40 requests vs model window · live sessions');
  const wrap = el('div', { class: 'ctx-wrap' });
  const canvas = el('canvas', { class: 'ctx-canvas', 'aria-label': 'Token residency of the last 40 requests' });
  const tip = el('div', { class: 'tp-tip', hidden: true });
  wrap.append(canvas, tip);
  const legend = el('div', { class: 'ctx-legend', 'aria-label': 'Residency legend' });
  const list = el('div', { class: 'ctx-list' });
  const more = el('p', { class: 'ledger-note', hidden: true });
  const empty = EmptyState('No active sessions');
  p.body.append(wrap, legend, list, more, empty);
  const cx = { cols: [], hoverIdx: null, hoverX: null, geo: null };
  canvas.addEventListener('mousemove', e => {
    const r = canvas.getBoundingClientRect();
    cx.hoverX = Math.max(0, Math.min(r.width, e.clientX - r.left));
  });
  canvas.addEventListener('mouseleave', () => { cx.hoverX = null; tip.hidden = true; });
  const CTX_MAX = 20;
  p.render = st => {
    const sessions = st.sessions || [];
    /* residency columns: last 40 requests, oldest left, newest right */
    const raw = (st.requests || []).slice(0, RESID_N).slice().reverse();
    cx.cols = raw.map(r => {
      const prompt = Math.max(0, r.prompt || 0);
      const cached = Math.min(prompt, r.cache || 0);
      const fresh = Math.min(prompt - cached, Math.max(0, r.fresh != null ? r.fresh : prompt - cached));
      return {
        model: r.model || r.id || '—', t: r.t, prompt, cached, fresh,
        output: Math.max(0, r.output || 0), ttft: r.ttft_s, window: r.window || 0,
      };
    });
    /* one-line legend: layer key + 15-min re-ingest tax + cache hit */
    const pct = v => (v == null || !isFinite(v)) ? '—' : fmtNum(v, 1) + '%';
    const re = st.kpi ? st.kpi.reingest : null;
    const ca = st.kpi ? st.kpi.cache : null;
    legend.replaceChildren(
      el('span', { class: 'swatch', style: { background: 'var(--tok-cached)' } }), 'Cached',
      el('span', { class: 'swatch', style: { background: 'var(--tok-fresh)' } }), 'Fresh prefill',
      el('span', { class: 'swatch', style: { background: 'var(--tok-generated)' } }), 'Generated',
      el('span', { class: 'spacer' }),
      el('span', { class: 'head-stat' }, 'Re-ingest 15m', el('b', {}, pct(re))),
      el('span', { class: 'head-stat' }, 'Cache hit', el('b', {}, pct(ca))));
    empty.hidden = sessions.length > 0 || cx.cols.length > 0;
    const reg = buildEngineRegistry(st.engines);
    const show = sessions.slice(0, CTX_MAX);
    const rows = show.map(s => {
      const ci = reg.byKey.get(s.engineKey) ? reg.byKey.get(s.engineKey).colorIndex : 0;
      const frac = s.window > 0 ? s.used / s.window : 0;
      const status = kpiStatus('ctx', s.used, { total: s.window });
      const bar = el('div', { class: 'meter', role: 'meter',
        ...(s.window ? { 'aria-valuemax': String(s.window), 'aria-valuenow': String(s.used) } : {}),
        'aria-label': (s.id || 'session') + ' context fill' });
      const fill = el('i', { class: 'meter-fill' });
      fill.style.width = (Math.max(0, Math.min(1, frac)) * 100).toFixed(2) + '%';
      if (status) fill.dataset.status = status;
      bar.append(fill);
      bar.append(el('i', { class: 'meter-tick', 'data-status': 'warn', style: { left: '75%' } }));
      bar.append(el('i', { class: 'meter-tick', 'data-status': 'crit', style: { left: '90%' } }));
      return el('div', { class: 'ctx-row' },
        el('span', { class: 'ctx-id' }, s.id || '—'),
        Badge({ status: 'idle', label: s.engine || s.engineKey || '—', dot: false,
          swatch: seriesColor(ci) }),
        bar,
        el('span', { class: 'ctx-used' },
          `${fmtTok(s.used)} / ${fmtTok(s.window)}`));
    });
    list.replaceChildren(...rows);
    const n = sessions.length;
    more.textContent = n > CTX_MAX ? `…${n - CTX_MAX} more` : '';
    more.hidden = n <= CTX_MAX;
    return cx.cols.length + ':' + sessions.map(s => s.id + s.engineKey).join(',');
  };
  /* stacked residency columns, painted on the 5 Hz paint loop */
  p.paint = () => {
    const { ctx, w, h } = fit(canvas);
    ctx.clearRect(0, 0, w, h);
    const cols = cx.cols;
    ctx.font = `10px ${cssVar('--font-mono')}`;
    if (!cols.length) {
      ctx.fillStyle = cssVar('--text-3');
      ctx.textAlign = 'center';
      ctx.fillText('no data', w / 2, h / 2 + 4);
      tip.hidden = true;
      return null;
    }
    const pad = { l: 46, r: 8, t: 16, b: 8 };
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    let maxWin = 0;
    for (const c of cols) if (c.window > maxWin) maxWin = c.window;
    if (!(maxWin > 0)) maxWin = 1;
    const yAt = tok => pad.t + ih - (Math.min(tok, maxWin) / maxWin) * ih;
    const base = pad.t + ih;
    /* faint 50 % gridline + the 75 % (warn) line */
    ctx.strokeStyle = cssVar('--border');
    ctx.lineWidth = 1;
    for (const f of [0.5, 0.75]) {
      const y = pad.t + ih - f * ih;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + iw, y); ctx.stroke();
    }
    ctx.fillStyle = cssVar('--text-3');
    ctx.textAlign = 'right';
    ctx.fillText(fmtTok(Math.round(0.5 * maxWin)), pad.l - 5, yAt(0.5 * maxWin) + 3);
    ctx.fillText('0', pad.l - 5, base + 3);
    ctx.strokeStyle = cssVar('--border-strong');
    ctx.beginPath(); ctx.moveTo(pad.l, base); ctx.lineTo(pad.l + iw, base); ctx.stroke();
    /* the columns */
    const n = cols.length;
    const cw = iw / n;
    const bw = Math.max(1, Math.floor(cw * 0.72));
    const LAYERS = [
      ['cached', '--tok-cached'],
      ['fresh', '--tok-fresh'],
      ['output', '--tok-generated'],
    ];
    cx.geo = { pad, iw, ih, base, n, cw, bw, yAt, maxWin };
    for (let i = 0; i < n; i++) {
      const c = cols[i];
      const x0 = pad.l + i * cw + (cw - bw) / 2;
      let y = base;
      for (const [key, tokVar] of LAYERS) {
        const v = c[key] || 0;
        if (v <= 0) continue;
        const hh = Math.min(base - pad.t, (v / maxWin) * ih);
        y -= hh;
        ctx.fillStyle = cssVar(tokVar);
        ctx.fillRect(x0, y, bw, hh);
      }
    }
    /* dashed line at the model window (the scale max), labelled from data */
    const yW = yAt(maxWin);
    ctx.strokeStyle = cssVar('--border-strong');
    ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(pad.l, yW); ctx.lineTo(pad.l + iw, yW); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = cssVar('--text-2');
    ctx.textAlign = 'right';
    ctx.fillText(fmtTok(maxWin) + ' window', pad.l + iw, yW - 4);
    /* hover crosshair + tooltip (rebuilt from last-paint geometry) */
    let hover = null;
    if (cx.hoverX != null && iw > 0) {
      const gx = Math.max(pad.l, Math.min(pad.l + iw, cx.hoverX));
      const i = Math.max(0, Math.min(n - 1, Math.floor((gx - pad.l) / cw)));
      hover = i;
      ctx.strokeStyle = cssVar('--border-strong');
      ctx.beginPath();
      ctx.moveTo(pad.l + i * cw + 0.5, pad.t);
      ctx.lineTo(pad.l + i * cw + 0.5, base);
      ctx.stroke();
      const c = cols[i];
      const line = (k, v) => `<span class="row">${k}<b>${v}</b></span>`;
      tip.innerHTML =
        `<span class="t">${c.model} · ${clockStr(c.t)}</span>` +
        line('prompt', fmtTok(c.prompt)) +
        line('cached', fmtTok(c.cached)) +
        line('fresh', fmtTok(c.fresh)) +
        line('output', fmtTok(c.output)) +
        line('TTFT', fmtDur(c.ttft != null ? c.ttft * 1000 : null));
      tip.hidden = false;
      const left = gx + 14 + 150 > w ? Math.max(4, gx - 15 - 150) : gx + 14;
      tip.style.left = left + 'px';
    } else {
      tip.hidden = true;
    }
    return cx.cols.length + ':' + (hover ?? '-') + ':' + cx.cols[cx.cols.length - 1].prompt;
  };
  /* ripple pulses on structural change (sessions appearing / leaving or a new
     request landing), not on the per-second used-token jitter */
  p.onData = st => {
    const sig = (st.sessions || []).map(s => s.id + s.engineKey).join(',') +
      '#' + ((st.requests && st.requests[0] && st.requests[0].id) || 0);
    panelPulse(p, sig);
  };
}

/* --- P6 · Request history ----------------------------------------------------------- */
/* newest first: time / model / cached-vs-fresh split / prompt / window / TTFT /
   decode. 14 rows + "Show more" (14 more, cap 50). Head stats: re-ingest, MTP,
   cache, buffered (same ledger footer as the token ledger). */
const REQ_STEP = 14, REQ_CAP = 50;
function buildRequests() {
  const p = makePanel('p-requests', 'Request history', 'newest first · client buffer capped at 500', { flush: true });
  const fRe = el('span', { class: 'head-stat' }, el('span', {}, 'Re-ingest'), el('b', {}, '—'));
  const fMtp = el('span', { class: 'head-stat' }, el('span', {}, 'MTP'), el('b', {}, '—'));
  const fCa = el('span', { class: 'head-stat' }, el('span', {}, 'Cache'), el('b', {}, '—'));
  const fBuf = el('span', { class: 'head-stat' }, el('span', {}, 'Buffered'), el('b', {}, '—'));
  const more = el('button', { type: 'button', class: 'chip', hidden: true }, 'Show more');
  let shown = REQ_STEP;
  more.addEventListener('click', () => {
    shown = Math.min(REQ_CAP, shown + REQ_STEP);
    more.hidden = shown >= REQ_CAP || shown >= state.requests.length;
  });
  p.head.append(fRe, fMtp, fCa, fBuf, more);
  const dt = DataTable({ columns: [
    { label: 'Time', mono: true, width: '64px' },
    { label: 'Model', width: '180px' },
    { label: 'Split (cached · fresh)', width: '140px' },
    { label: 'Prompt', num: true },
    { label: 'Window', num: true },
    { label: 'TTFT', num: true },
    { label: 'Decode', num: true },
  ], caption: 'Request history' });
  p.body.append(dt.wrap, EmptyState('No requests recorded yet'));
  const emptyEl = p.body.children[p.body.children.length - 1];
  p.render = st => {
    const L = tokenLedger(st);
    const setF = (elx, v, status) => {
      elx.children[1].textContent = v == null ? '—' : fmtNum(v, 1) + '%';
      if (status) elx.dataset.status = status; else elx.removeAttribute('data-status');
    };
    setF(fRe, L.footer.reingest, kpiStatus('reingest', L.footer.reingest));
    setF(fMtp, L.footer.mtp);
    setF(fCa, L.footer.cache, kpiStatus('cache', L.footer.cache));
    fBuf.children[1].textContent = String(L.footer.reqs);
    const reqs = st.requests || [];
    emptyEl.hidden = reqs.length > 0;
    const rows = reqs.slice(0, shown).map(r => TableRow([
      { text: clockStr(r.t), cls: 'col-mono' },
      { text: r.model || '—' },
      { el: mixBar(r) },
      { text: fmtTok(r.prompt), cls: 'col-num col-mono', title: `cached ${fmtTok(r.cache)} · fresh ${fmtTok(r.fresh)}` },
      { text: fmtTok(r.window), cls: 'col-num col-mono' },
      { text: fmtDur(r.ttft_s != null ? r.ttft_s * 1000 : null), cls: 'col-num col-mono' },
      { text: r.decode_tps != null ? `${fmtNum(r.decode_tps, 0)} t/s` : '—', cls: 'col-num col-mono' },
    ]));
    dt.tbody.replaceChildren(...rows);
    more.hidden = shown >= REQ_CAP || shown >= reqs.length;
    return reqs.length + '/' + shown;
  };
}
/* 100 %-stacked cached-vs-fresh prompt split; window is the bar's frame */
function mixBar(r) {
  const prompt = Math.max(0, r.prompt || 0);
  const cached = Math.min(prompt, r.cache || 0);
  const fresh = Math.min(prompt - cached, Math.max(0, r.fresh != null ? r.fresh : prompt - cached));
  const bar = el('div', { class: 'mix', role: 'img',
    'aria-label': `prompt split: ${fmtTok(cached)} cached, ${fmtTok(fresh)} fresh` });
  const seg = (v, tok) => {
    const i = el('i');
    i.style.width = prompt > 0 ? (100 * v / prompt).toFixed(2) + '%' : '0%';
    i.style.background = `var(--tok-${tok})`;
    bar.append(i);
  };
  seg(cached, 'cached');
  seg(fresh, 'fresh');
  return bar;
}

/* --- P7 · Engines ------------------------------------------------------------------- */
/* one inset card per engine from the payload registry (never a UI-side
   literal): swatch + label + state badge in words, origin · window · backend
   subline, tok-s / queue / MTP stats, throughput sparkline. Down engines are
   muted and stated in words. */
function buildEngines() {
  const p = makePanel('p-engines', 'Engines', 'registry from the runtime payload');
  const grid = el('div', { class: 'engine-grid' });
  p.body.append(grid);
  const cards = new Map(); // engine key -> { card, label, badge, sub, sTps, sQueue, sMtp, spark }
  p.render = st => {
    const reg = buildEngineRegistry(st.engines);
    for (const e of reg.list) {
      let c = cards.get(e.key);
      if (!c) {
        const spark = sparkCanvas('engine-spark', (e.label || e.key) + ' throughput');
        const sTps = el('b', {}, '—'), sQueue = el('b', {}, '—'), sMtp = el('b', {}, '—');
        const card = el('div', { class: 'engine-card' },
          el('div', { class: 'ec-head' },
            el('span', { class: 'swatch', style: { background: seriesColor(reg.byKey.get(e.key).colorIndex) } }),
            el('span', { class: 'ec-name' }, e.label || e.key),
            el('span', { class: 'spacer' })),
          el('div', { class: 'ec-sub' }),
          el('div', { class: 'ec-stats' },
            el('div', { class: 'ec-stat' }, el('span', {}, 'tok/s'), sTps),
            el('div', { class: 'ec-stat' }, el('span', {}, 'queue'), sQueue),
            el('div', { class: 'ec-stat' }, el('span', {}, 'MTP'), sMtp)),
          spark);
        c = { card, label: card.children[0].children[1], badge: null,
              sub: card.children[1], sTps, sQueue, sMtp, spark };
        const b = Badge({ status: 'idle', label: '—', dot: false });
        c.badge = b;
        card.children[0].append(b);
        cards.set(e.key, c);
        grid.append(card);
      }
      const info = engineState(e);
      c.label.textContent = e.label || e.key;
      c.badge.replaceChildren(info.word);
      c.badge.dataset.status = engineBadgeStatus(e);
      if (info.muted) c.badge.classList.add('badge--muted'); else c.badge.classList.remove('badge--muted');
      c.sub.textContent = [
        e.origin || '—',
        e.window ? `${fmtTok(e.window)} ctx` : null,
        /* holding VRAM with no request for idle_vram_min (collector) */
        e.idle_vram ? `idle ${Math.round(e.idle_vram.idle_s / 60)} min · ${(e.idle_vram.mib / 1024).toFixed(1)} GB VRAM` : null,
        e.reason || e.backend || (e.up === true ? 'local' : 'no backend'),
      ].filter(Boolean).join(' · ');
      c.card.classList.toggle('is-muted', info.muted);
      const rate = e.rates ? (e.rates.decode_tps != null ? e.rates.decode_tps : e.rates.gen_tps_inst) : null;
      c.sTps.textContent = rate != null ? String(Math.round(rate)) : '—';
      c.sQueue.textContent = e.queue != null ? String(e.queue) : '—';
      c.sMtp.textContent = e.mtp != null ? fmtNum(e.mtp, 1) + '%' : '—';
    }
    /* drop cards whose engine left the registry */
    for (const [key, c] of [...cards]) {
      if (!reg.byKey.has(key)) { cards.delete(key); c.card.parentNode && grid.replaceChildren(...[...grid.children].filter(x => x !== c.card)); }
    }
    return reg.list.map(e => `${e.key}:${e.up}:${e.latched}:${e.queue}`).join(';');
  };
  p.paint = st => {
    const reg = buildEngineRegistry(st.engines);
    for (const [key, c] of cards) {
      if (!reg.byKey.has(key)) continue;
      paintSpark(c.spark, emaSmooth(st.engHist[key] || []).slice(-600), seriesColor(reg.byKey.get(key).colorIndex));
    }
    return null;
  };
  p.onData = st => {
    const reg = buildEngineRegistry(st.engines);
    panelPulse(p, reg.list.map(e => `${e.key}:${e.up}:${e.latched}`).join(';'));
  };
}

/* --- P8 · Events -------------------------------------------------------------------- */
/* severity pills + message (escapes decoded at render time), rows expand to the
   full text; severity chips + text filter + autoscroll-pause in the head;
   newest first, capped at 60 rows. */
const EV_SEVS = [
  { value: 'all', label: 'All' },
  { value: 'req', label: 'Req' },
  { value: 'ok', label: 'OK' },
  { value: 'info', label: 'Info' },
  { value: 'warn', label: 'Warn', swatch: cssVar('--warn') },
  { value: 'err', label: 'Err', swatch: cssVar('--crit') },
];
const EV_CAP = 60;
function buildEvents() {
  const ev = { sev: 'all', filter: '', autoscroll: true, lastFirst: null };
  const filterInput = el('input', { type: 'text', class: 'ev-filter',
    placeholder: 'filter…', 'aria-label': 'Filter events by text' });
  filterInput.addEventListener('input', () => { ev.filter = filterInput.value.trim().toLowerCase(); });
  const pauseBtn = el('button', { type: 'button', class: 'chip', 'aria-pressed': 'true' }, 'Autoscroll');
  pauseBtn.addEventListener('click', () => {
    ev.autoscroll = !ev.autoscroll;
    pauseBtn.setAttribute('aria-pressed', String(ev.autoscroll));
  });
  const p = makePanel('p-events', 'Events', 'newest first · cap ' + EV_CAP,
    { flush: true, tools: el('span', { class: 'ev-tools' },
      Chips(EV_SEVS, 'all', v => { ev.sev = v; }), filterInput, pauseBtn) });
  const dt = DataTable({ columns: [
    { label: 'Time', mono: true, width: '64px' },
    { label: 'Level', width: '56px' },
    { label: 'Message' },
  ], caption: 'Events' });
  const wrap = el('div', { class: 'ev-wrap' }, dt.el);
  const empty = EmptyState('No events yet');
  p.body.append(wrap, empty);
  const match = e => {
    if (ev.sev !== 'all') {
      if (ev.sev === 'err') { if (e.level !== 'err' && e.level !== 'alert') return false; }
      else if (e.level !== ev.sev) return false;
    }
    if (ev.filter && !(e.msg || '').toLowerCase().includes(ev.filter)) return false;
    return true;
  };
  p.render = st => {
    const all = (st.events || []).slice(0, EV_CAP);
    const shown = all.filter(match);
    empty.hidden = shown.length > 0;
    const kids = [];
    for (const e of shown) {
      const row = TableRow([
        { text: clockStr(e.t), cls: 'col-mono' },
        { el: el('span', { class: 'lv-pill', data: { sev: e.level } }, e.level) },
        { el: el('span', { class: 'ev-msg' }, decodeEscapes(e.msg)) },
      ], { expandable: true });
      kids.push(row);
      const detail = el('tr', { class: 'row-detail' }, el('td', { colspan: 3 }, decodeEscapes(e.msg)));
      detail.hidden = true;
      kids.push(detail);
    }
    dt.tbody.replaceChildren(...kids);
    /* autoscroll: stay pinned to the newest row unless the user scrolled away
       or paused autoscroll */
    const first = shown.length ? (shown[0].t || 0) + '|' + (shown[0].level || '') : null;
    if (ev.autoscroll && ev.lastFirst && first && first !== ev.lastFirst && (wrap.scrollTop || 0) < 40) {
      wrap.scrollTo(0, 0);
    }
    ev.lastFirst = first;
    return all.length + '/' + shown.length;
  };
  p.onData = st => {
    const n = (st.events || []).length;
    panelPulse(p, 'ev:' + n + ':' + ((st.events && st.events[0] && st.events[0].t) || 0));
  };
}

/* --- P9 · KV pool --------------------------------------------------------------------- */
/* one cell per live KV slot: fill height = used vs window, fill color = the
   engine's series color, pct + session id. Demo fills 32 seeded fakes. */
function buildPool() {
  const p = makePanel('p-pool', 'KV pool', 'slots vs model window');
  const grid = el('div', { class: 'pool-grid' });
  const empty = EmptyState('No active slots');
  p.body.append(grid, empty);
  p.render = st => {
    const reg = buildEngineRegistry(st.engines);
    let items;
    if (st.mode === 'demo') {
      items = (st.poolFakes || []).map((f, i) => ({ id: `slot ${i + 1}`, engineKey: 'sim', pct: f }));
    } else {
      items = (st.sessions || []).map(s => ({
        id: s.id, engineKey: s.engineKey,
        pct: s.window > 0 ? s.used / s.window : 0,
      }));
    }
    empty.hidden = items.length > 0;
    grid.replaceChildren(...items.map(it => {
      const ci = reg.byKey.get(it.engineKey) ? reg.byKey.get(it.engineKey).colorIndex : 0;
      const h = Math.max(0, Math.min(1, it.pct || 0));
      const cell = el('div', { class: 'pool-cell', 'aria-label': `${it.id} at ${Math.round(h * 100)}%` },
        el('i', { class: 'fill', style: { height: (h * 100).toFixed(1) + '%', background: seriesColor(ci) } }));
      cell.append(el('span', { class: 'pct' }, Math.round(h * 100) + '%'));
      cell.append(el('span', { class: 'sid' }, it.id));
      return cell;
    }));
    return items.length + ':' + items.reduce((a, b) => a + Math.round(b.pct), 0);
  };
}

/* --- alert strip -------------------------------------------------------------- */
/* Active alerts (engine down, foreign VRAM, GPU temperature, queue) listed under the top bar in both
   views; the pill alone said "Degraded" without saying why. Hidden when there are none. */
function renderAlerts() {
  const box = $('#alerts');
  if (!box) return;
  const list = state.alerts || [];
  const sig = list.join('\n');
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.hidden = list.length === 0;
  box.replaceChildren(...list.map(a => el('div', { class: 'alert-line' },
    Badge({ status: 'warn', label: 'alert', dot: true }), el('span', { class: 'alert-msg' }, a))));
}

function fmtBytes(b) {
  if (b == null || !isFinite(b)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(b < 10 ? 1 : 0) : String(Math.round(b))) + ' ' + u[i];
}

/* --- model load timeline -------------------------------------------------------- */
/* One lane per engine, one bar per load span (GET /api/spans), coloured like the engine's throughput
   line. Fetched on select and at most once a minute; the DOM is rebuilt only when the data, the range
   or the minute changes. */
const TL_RANGES = [
  { value: '6h', label: '6h', sec: 21600 }, { value: '24h', label: '24h', sec: 86400 },
  { value: '7d', label: '7d', sec: 604800 }, { value: '30d', label: '30d', sec: 2592000 },
];
function buildTimeline() {
  const tl = { range: '24h', data: null, at: 0, fetching: false, error: false };
  const p = makePanel('p-timeline', 'Model timeline', 'load and unload per engine', {
    tools: Segmented(TL_RANGES.map(r => ({ value: r.value, label: r.label })), tl.range, v => {
      tl.range = v; tl.data = null; tl.at = 0;
    }),
  });
  const lanes = el('div', { class: 'tl-lanes' });
  const axis = el('div', { class: 'tl-axis' });
  const note = el('p', { class: 'tl-note', hidden: true });
  p.body.append(note, lanes, axis);
  function poll() {
    if (DEMO || tl.fetching || Date.now() - tl.at < 60000) return;
    tl.fetching = true;
    fetch('api/spans?range=' + tl.range, { cache: 'no-store' })
      .then(x => { if (!x.ok) throw new Error('http ' + x.status); return x.json(); })
      .then(d => { tl.data = d; tl.error = false; })
      .catch(() => { tl.error = true; })
      .finally(() => { tl.fetching = false; tl.at = Date.now(); });
  }
  p.render = st => {
    poll();
    const R = TL_RANGES.find(x => x.value === tl.range);
    const spans = tl.data && tl.data.range === tl.range ? tl.data.spans || [] : [];
    const msg = DEMO ? 'timeline needs the collector'
      : !tl.data ? (tl.error ? 'timeline needs the collector' : 'loading…')
      : spans.length ? '' : 'no model loads in this range';
    note.textContent = msg;
    note.hidden = !msg;
    const now = (st.t && st.t > 1e9) ? st.t : Date.now() / 1000;
    const t0 = now - R.sec;
    const sig = tl.range + '|' + (tl.data ? spans.length + ':' + tl.at : 'none') + '|' + Math.floor(now / 60);
    if (p.el.dataset.sig === sig) return sig;
    p.el.dataset.sig = sig;
    const reg = buildEngineRegistry(st.engines);
    const keys = [...new Set(spans.map(x => x.engine))].sort();
    const past = keys.filter(k => !reg.byKey.has(k));
    const colorOf = k => reg.byKey.has(k) ? seriesColor(reg.byKey.get(k).colorIndex)
      : seriesColor(reg.list.length + past.indexOf(k));
    const labelOf = k => { const e = reg.list.find(x => x.key === k); return e ? (e.label || k) : k; };
    const when = t => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    lanes.replaceChildren(...keys.map(k => {
      const track = el('div', { class: 'tl-track' });
      for (const sp of spans) {
        if (sp.engine !== k) continue;
        const a = Math.max(sp.loaded_at, t0), b = Math.min(sp.unloaded_at == null ? now : sp.unloaded_at, now);
        if (b <= a && sp.unloaded_at != null) continue;
        const left = (a - t0) / R.sec * 100, width = Math.max(0.3, (b - a) / R.sec * 100);
        const dur = fmtDur(((sp.unloaded_at == null ? now : sp.unloaded_at) - sp.loaded_at) * 1000);
        track.append(el('i', { class: 'tl-bar', style: { left: left.toFixed(2) + '%', width: width.toFixed(2) + '%', background: colorOf(k) },
          title: `${sp.model || k} · ${when(sp.loaded_at)} → ${sp.unloaded_at == null ? 'loaded' : when(sp.unloaded_at)} · ${dur}` }));
      }
      return el('div', { class: 'tl-lane' }, el('span', { class: 'tl-label' }, labelOf(k)), track);
    }));
    const marks = 4;
    axis.replaceChildren(...Array.from({ length: marks + 1 }, (_, i) => {
      const ago = R.sec * (1 - i / marks);
      return el('span', {}, i === marks ? 'now' : '−' + (R.sec >= 604800 ? Math.round(ago / 86400) + 'd' : Math.round(ago / 3600) + 'h'));
    }));
    return sig;
  };
}

/* --- per-model leaderboard -------------------------------------------------------- */
/* Requests, tokens, median decode, cache hit, MTP and tokens per watt-hour per (engine, model) over
   a range (GET /api/leaderboard). Energy is whole-card GPU power over the engine's busy minutes, idle
   draw included: it compares setups on this box, not models in the abstract. */
function buildLeaderboard() {
  const lb = { range: '24h', data: null, at: 0, fetching: false, error: false };
  const p = makePanel('p-leader', 'Leaderboard', 'per model · energy is whole-card GPU power over busy minutes', {
    tools: Segmented(TL_RANGES.map(r => ({ value: r.value, label: r.label })), lb.range, v => {
      lb.range = v; lb.data = null; lb.at = 0;
    }),
  });
  const dt = DataTable({ caption: 'Per-model leaderboard', columns: [
    { label: 'Model' }, { label: 'Engine' }, { label: 'Requests', num: true }, { label: 'Output', num: true },
    { label: 'Median decode', num: true }, { label: 'Cache hit', num: true }, { label: 'MTP', num: true },
    { label: 'Tokens / Wh', num: true },
  ] });
  const note = el('p', { class: 'tl-note', hidden: true });
  p.body.append(note, dt.wrap);
  function poll() {
    if (DEMO || lb.fetching || Date.now() - lb.at < 60000) return;
    lb.fetching = true;
    fetch('api/leaderboard?range=' + lb.range, { cache: 'no-store' })
      .then(x => { if (!x.ok) throw new Error('http ' + x.status); return x.json(); })
      .then(d => { lb.data = d; lb.error = false; })
      .catch(() => { lb.error = true; })
      .finally(() => { lb.fetching = false; lb.at = Date.now(); });
  }
  p.render = () => {
    poll();
    const rows = lb.data && lb.data.range === lb.range ? lb.data.rows || [] : [];
    const msg = DEMO ? 'leaderboard needs the collector'
      : !lb.data ? (lb.error ? 'leaderboard needs the collector' : 'loading…')
      : rows.length ? '' : 'no requests in this range';
    note.textContent = msg;
    note.hidden = !msg;
    const sig = lb.range + '|' + lb.at + '|' + rows.length;
    if (p.el.dataset.sig === sig) return sig;
    p.el.dataset.sig = sig;
    /* numeric cells carry col-num like their headers, so values line up under them */
    const num = v => ({ text: v != null ? v : '—', cls: 'col-num' });
    const pct = v => num(v != null ? fmtNum(v, 1) + '%' : null);
    dt.tbody.replaceChildren(...rows.map(r => TableRow([
      { text: r.model, title: r.model }, r.engine, num(fmtNum(r.requests, 0)), num(fmtTok(r.output)),
      num(r.decode_tps_median != null ? fmtNum(r.decode_tps_median, 1) + ' t/s' : null),
      pct(r.cache_pct), pct(r.mtp_pct), num(r.tok_per_wh != null ? fmtNum(r.tok_per_wh, 0) : null),
    ])));
    return sig;
  };
}

/* --- history storage ------------------------------------------------------------- */
/* The SQLite history's size and growth (GET /api/storage): what 30 / 60 / 90 days of retention cost on
   this box, the current choice highlighted. Fetched on load and every 5 minutes. */
function buildStorage() {
  const sto = { data: null, at: 0, fetching: false, error: false };
  const p = makePanel('p-storage', 'Storage', 'history database');
  const body = el('div', { class: 'sto' });
  p.body.append(body);
  function poll() {
    if (DEMO || sto.fetching || Date.now() - sto.at < 300000) return;
    sto.fetching = true;
    fetch('api/storage', { cache: 'no-store' })
      .then(x => { if (!x.ok) throw new Error('http ' + x.status); return x.json(); })
      .then(d => { sto.data = d; sto.error = false; })
      .catch(() => { sto.error = true; })
      .finally(() => { sto.fetching = false; sto.at = Date.now(); });
  }
  p.render = () => {
    poll();
    const d = sto.data;
    const sig = DEMO ? 'demo' : d ? JSON.stringify([d.bytes, d.retention_days, d.rows]) : (sto.error ? 'err' : 'wait');
    if (p.el.dataset.sig === sig) return sig;
    p.el.dataset.sig = sig;
    if (DEMO || !d) {
      body.replaceChildren(el('p', { class: 'sto-note' }, DEMO || sto.error ? 'storage needs the collector' : 'loading…'));
      return sig;
    }
    if (!d.enabled) {
      body.replaceChildren(el('p', { class: 'sto-note' }, 'history is off (retention_days = 0)'));
      return sig;
    }
    const row = (k, v, cls) => el('div', { class: 'sto-row' + (cls ? ' ' + cls : '') }, el('span', {}, k), el('b', {}, v));
    const proj = d.projection || {};
    /* export the last 24 h (the API caps at 50,000 rows), and apply the retention now */
    const prune = el('button', { class: 'btn', type: 'button', onclick: () => {
      if (!confirm(`Delete history older than ${d.retention_days} days now? (It runs daily anyway.)`)) return;
      fetch('api/prune', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then(x => x.json()).then(r => { if (r.storage) { sto.data = r.storage; sto.at = Date.now(); p.el.dataset.sig = ''; } })
        .catch(() => {});
    } }, 'Prune now');
    const actions = el('div', { class: 'sto-actions' },
      el('a', { class: 'btn', href: 'api/export?range=24h&format=csv', download: 'speculum-24h.csv' }, 'Export CSV'),
      el('a', { class: 'btn', href: 'api/export?range=24h&format=json', download: 'speculum-24h.json' }, 'Export JSON'),
      prune);
    body.replaceChildren(
      row('Size', fmtBytes(d.bytes)),
      row('Retention', d.retention_days + ' days'),
      row('Growth', d.bytes_per_day != null ? fmtBytes(d.bytes_per_day) + ' / day' : '—'),
      el('div', { class: 'sto-proj' }, ...['30', '60', '90'].map(n =>
        el('div', { class: 'sto-cell' + (String(d.retention_days) === n ? ' is-current' : '') },
          el('span', {}, n + ' d'), el('b', {}, fmtBytes(proj[n]))))),
      row('Rows', Object.entries(d.rows || {}).map(([k, v]) => `${k} ${fmtNum(v, 0)}`).join(' · '), 'sto-rows'),
      el('p', { class: 'sto-path', title: d.path || '' }, d.path || ''),
      actions);
    return sig;
  };
}

/* --- paint loop --------------------------------------------------------------- */
function paint() {
  /* panel paint hooks register here: p.paint(state, { reduced }) may return a
     signature string — the panel pulses only when it changes */
  for (const p of ui.panels) {
    if (p.hidden) continue;   // Basic: sections not shown do no paint work
    if (p.paint) {
      const sig = p.paint(state, { reduced: motionReduced() });
      if (typeof sig === 'string') panelPulse(p, sig);
    }
    if (p.onData) p.onData(state);
  }
}
function renderText() {
  renderTopbar();
  renderAlerts();
  renderFoot();
  setPanelsStale();
  for (const p of ui.panels) if (!p.hidden && p.render) p.render(state);
}

/* --- keys ------------------------------------------------------------------------ */
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

/* --- run --------------------------------------------------------------------------- */
applyTheme();
applyMotion();
applyRippleSetting();
applyView();      // class on #deck before first paint; panels get the flag at registration
buildTopbar();
buildKpi();
buildThroughput();
buildGpu();
buildLedger();
buildContext();
buildTimeline();
buildStorage();
buildRequests();
buildLeaderboard();
buildEngines();
buildEvents();
buildPool();

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
