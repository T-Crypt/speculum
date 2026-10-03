/* =========================================================================
   app.js — the monitor. Owns the simulated runtime, the gauges, the context
   map, and the paint loop. Everything on screen is rendered from this file;
   index.html carries only the panel shells.
   ========================================================================= */

const $ = (s, r = document) => r.querySelector(s);
const root = document.documentElement;

/* deterministic resampling: same seed, same dashboard */
function mulberry32(a) {
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    /* Math.imul keeps the hash inside 32 bits. A plain multiply overflows past
       2^53, the low bits the >>> needs are gone, and the RNG collapses to 0. */
    let t = Math.imul(a ^ (a >>> 15), 0x21F0FFAD);
    t = Math.imul(t ^ (t >>> 7), 0x84222325);
    return ((t ^ (t >>> 16)) >>> 0) / 4294967296;
  };
}
let rand = mulberry32(0xC0FFEE);

const ACCENTS = ['#79d7ff', '#b39cff', '#8ef5c4', '#ffd08a', '#ff9ac2'];
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* --- models --------------------------------------------------------------- */
const MODELS = [
  { id: 'llama-3.3-70b',        label: 'Llama 3.3 70B',   quant: 'Q4_K_M', ctx: 131072, accent: ACCENTS[0], share: 0.34 },
  { id: 'qwen3-32b',            label: 'Qwen3 32B',       quant: 'Q5_K_M', ctx: 65536,  accent: ACCENTS[1], share: 0.27 },
  { id: 'mistral-small-24b',    label: 'Mistral Small 24B', quant: 'Q6_K', ctx: 32768,  accent: ACCENTS[2], share: 0.21 },
  { id: 'phi-4-14b',            label: 'Phi-4 14B',       quant: 'Q8_0',   ctx: 16384,  accent: ACCENTS[3], share: 0.18 },
];

/* --- state ---------------------------------------------------------------- */
const state = {
  paused: false,
  focus: null,
  t: 0,
  gpu: { temp: 61, util: 74, power: 268, vram: 18.4, vramMax: 24, fan: 1480, clock: 1725, bus: 'PCIe 4.0 x16', driver: '560.35.05' },
  rt:  { tps: 142, rpm: 214, p50: 118, p95: 340, ttft: 210, tpot: 26, cache: 62, queue: 3, batch: 12, err: 0.4, uptime: 41 * 3600 + 1240 },
  tok: { lifetime: 1_244_812_004, d30: 214_330_118, d90: 561_907_442 },
  sessions: [],
  hist: {},
  pool: Array.from({ length: 32 }, () => rand()),
};

const KPIS = [
  { key: 'tps',   label: 'Tokens / s',     unit: '',   fmt: v => v.toFixed(1), accent: '#79d7ff' },
  { key: 'rpm',   label: 'Requests / min', unit: '',   fmt: v => String(Math.round(v)), accent: '#b39cff' },
  { key: 'p95',   label: 'p95 latency',    unit: 'ms', fmt: v => Math.round(v), accent: '#ffd08a' },
  { key: 'ttft',  label: 'First token',    unit: 'ms', fmt: v => Math.round(v), accent: '#ff9ac2' },
  { key: 'tpot',  label: 'Per token',      unit: 'ms', fmt: v => v.toFixed(1), accent: '#8ef5c4' },
  { key: 'vram',  label: 'VRAM',           unit: 'GB', fmt: v => v.toFixed(1), accent: '#b39cff' },
  { key: 'cache', label: 'Cache hit',      unit: '%',  fmt: v => v.toFixed(1), accent: '#8ef5c4' },
  { key: 'queue', label: 'Queue depth',    unit: '',   fmt: v => String(Math.round(v)), accent: '#ffd08a' },
];

const GAUGES = [
  { key: 'temp',  label: 'GPU temp', max: 95,  unit: '°C', warn: 78 },
  { key: 'util',  label: 'GPU util', max: 100, unit: '%',  warn: 95 },
  { key: 'power', label: 'Draw',     max: 400, unit: 'W',  warn: 340 },
  { key: 'vram',  label: 'VRAM',     max: 24,  unit: 'GB', warn: 22 },
];

for (const k of KPIS) state.hist[k.key] = Array.from({ length: 60 }, () => 0);
for (const m of MODELS) state.hist[m.id] = Array.from({ length: 60 }, () => 0);

/* sessions for the context map */
for (let i = 0; i < 9; i++) {
  const m = MODELS[i % MODELS.length];
  state.sessions.push({
    id: `s${(1000 + Math.floor(rand() * 9000))}`,
    model: m,
    window: m.ctx,
    used: Math.floor(m.ctx * (0.18 + rand() * 0.6)),
    accent: m.accent,
  });
}

/* --- helpers -------------------------------------------------------------- */
function fmtTokens(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(2) + ' B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + ' M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + ' k';
  return String(Math.round(n));
}
function clock() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}
function uptime(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return `${h}h ${String(m).padStart(2, '0')}m`;
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
function hex(a) { return a; }

/* --- build ---------------------------------------------------------------- */
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
    <div class="brand"><b>Speculum</b><span>local runtime monitor</span></div>
    <div class="spacer"></div>
    <span class="chip">node <b>rtx 3090</b></span>
    <span class="chip">driver <b id="driver"></b></span>
    <span class="chip">uptime <b id="uptime"></b></span>
    <span class="chip">state <b id="runstate">live</b></span>
  `);
}

function buildKpi() {
  const wrap = $('#kpi');
  for (const k of KPIS) {
    const el = document.createElement('article');
    el.className = 'card glass';
    el.style.setProperty('--glow-c', k.accent);
    el.innerHTML = `
      <div class="label" style="color:${k.accent}">${k.label}</div>
      <div class="value" data-kpi="${k.key}">—</div>
      <div class="delta" data-delta="${k.key}">—</div>
      <canvas data-spark="${k.key}"></canvas>`;
    shell(el);
    wrap.append(el);
  }
}

function buildSignal() {
  const p = $('#signal');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Signal</h2><p class="hint">tokens per second, rolling 60s</p></div>
    <canvas id="chart" aria-label="Rolling throughput chart"></canvas>
    <div class="legend" id="legend"></div>`);
  $('#legend').innerHTML = MODELS.map(m =>
    `<span><i style="background:${m.accent};box-shadow:0 0 10px ${m.accent}"></i>${m.label}</span>`).join('');
}

function buildGauges() {
  const p = $('#gauges');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Hardware</h2><p class="hint">live sensors</p></div>
    <div class="gauges">
      ${GAUGES.map(g => `<div class="gauge"><canvas data-gauge="${g.key}"></canvas><span>${g.label}</span></div>`).join('')}
    </div>
    <div class="readout" id="hw-readout"></div>`);
}

function buildContext() {
  const p = $('#context');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Context map</h2><p class="hint">one ring per live session</p></div>
    <canvas id="context-canvas"></canvas>
    <div class="ctx-legend" id="ctx-legend"></div>`);
}

function buildLedger() {
  const p = $('#ledger');
  shell(p);
  p.insertAdjacentHTML('beforeend', `
    <div class="panel-head"><h2>Token ledger</h2><p class="hint">generated tokens</p></div>
    <div class="ledger">
      ${[['lifetime', 'Lifetime'], ['d30', '30 days'], ['d90', '90 days']].map(([k, l]) => `
        <div class="row">
          <span class="rl">${l}</span>
          <span class="rv" data-tok="${k}">—</span>
          <div class="rb"><i data-bar="${k}"></i></div>
        </div>`).join('')}
    </div>
    <div class="mini" id="mini"></div>`);
}

function buildLanes() {
  const wrap = $('#lanes');
  for (const m of MODELS) {
    const el = document.createElement('article');
    el.className = 'lane glass';
    el.tabIndex = 0;
    el.dataset.model = m.id;
    el.style.setProperty('--glow-c', m.accent);
    el.innerHTML = `
      <div class="name"><span class="dot" style="color:${m.accent};background:${m.accent}"></span>${m.label}</div>
      <div class="sub">${m.quant} · ${m.ctx.toLocaleString()} ctx</div>
      <div class="meter"><i style="background:${m.accent};box-shadow:0 0 14px ${m.accent}"></i></div>
      <div class="grid">
        <div>tok/s<b data-lane="tps">—</b></div>
        <div>p95<b data-lane="p95">—</b></div>
        <div>share<b data-lane="share">—</b></div>
      </div>`;
    shell(el);
    el.addEventListener('click', () => setFocus(m.id));
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setFocus(m.id); } });
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
    <div class="panel-head"><h2>Pool</h2><p class="hint">32 kv slots</p></div>
    <div class="cells" id="cells"></div>`);
  const cells = $('#cells');
  for (let i = 0; i < 32; i++) {
    const c = document.createElement('div');
    c.className = 'cell';
    c.innerHTML = `<i style="background:${ACCENTS[i % ACCENTS.length]};box-shadow:0 0 12px ${ACCENTS[i % ACCENTS.length]}"></i>`;
    cells.append(c);
  }
}

function setFocus(id) {
  state.focus = state.focus === id ? null : id;
  for (const el of document.querySelectorAll('.lane')) {
    el.dataset.focus = el.dataset.model === state.focus ? '1' : '0';
  }
}

/* --- simulation ----------------------------------------------------------- */
function drift(v, lo, hi, pull) {
  const target = lo + (hi - lo) * pull;
  return v + (target - v) * 0.06 + (rand() - 0.5) * 2;
}

function step() {
  const g = state.gpu, r = state.rt;
  state.t += 1;

  const load = 0.45 + 0.4 * Math.sin(state.t / 40) + rand() * 0.15;
  r.tps = Math.max(12, drift(r.tps, 60, 260, load));
  r.rpm = Math.max(20, drift(r.rpm, 120, 340, load));
  r.p50 = Math.max(40, drift(r.p50, 90, 220, load));
  r.p95 = Math.max(r.p50 + 60, drift(r.p95, 220, 620, load * 0.9));
  r.cache = Math.min(96, Math.max(20, drift(r.cache, 45, 82, 0.6 + rand() * 0.3)));
  r.queue = Math.max(0, drift(r.queue, 0, 14, load * 0.7));
  r.ttft = Math.max(60, drift(r.ttft, 120, 430, load));
  r.tpot = Math.max(8, drift(r.tpot, 18, 46, load));
  r.err = Math.max(0, drift(r.err, 0, 2.4, rand()));
  r.batch = Math.round(drift(r.batch, 4, 24, load));
  r.uptime += 0.12;

  g.util = Math.min(100, Math.max(0, drift(g.util, 30, 99, load)));
  g.power = Math.max(60, drift(g.power, 120, 380, load));
  g.temp = Math.max(34, drift(g.temp, 42, 86, load * 0.9));
  g.fan = Math.max(400, drift(g.fan, 700, 2600, load));
  g.clock = Math.round(drift(g.clock, 1400, 1900, load));
  g.vram = Math.min(g.vramMax, Math.max(6, drift(g.vram, 12, 23.5, 0.55 + rand() * 0.3)));

  for (const k of KPIS) {
    const h = state.hist[k.key];
    h.push(r[k.key] ?? g[k.key]);
    if (h.length > 60) h.shift();
  }

  for (const m of MODELS) {
    const h = state.hist[m.id];
    h.push(r.tps * m.share * (0.85 + rand() * 0.3));
    if (h.length > 60) h.shift();
  }

  for (const s of state.sessions) {
    s.used = Math.min(s.window, Math.max(256, s.used + Math.floor((rand() - 0.45) * 900)));
    if (s.used < 400 && rand() < 0.05) s.used = Math.floor(s.window * rand());
  }

  for (let i = 0; i < state.pool.length; i++) {
    state.pool[i] = Math.min(1, Math.max(0, state.pool[i] + (rand() - 0.5) * 0.12));
  }

  const made = Math.round(r.tps * 0.12);
  state.tok.lifetime += made;
  state.tok.d30 += made;
  state.tok.d90 += made;

  if (rand() < 0.22) pushEvent();
}

/* --- event stream --------------------------------------------------------- */
const EVENTS = [
  ['ok',   () => `stream complete · ${Math.floor(rand() * 900 + 60)} tok · ${(rand() * 3 + 0.4).toFixed(2)}s`],
  ['ok',   () => `cache hit · ${Math.floor(rand() * 4000 + 400)} tok reused`],
  ['info', () => `batch merged · ${state.rt.batch} seqs · slot ${Math.floor(rand() * 32)}`],
  ['info', () => `kv write · ${state.sessions[Math.floor(rand() * state.sessions.length)].id}`],
  ['info', () => `eval sample · mmlu subset ${(rand() * 0.2 + 0.62).toFixed(2)}`],
  ['warn', () => `kv eviction · slot ${Math.floor(rand() * 32)} · ${Math.floor(rand() * 3000 + 500)} tok dropped`],
  ['warn', () => `queue pressure · ${Math.round(state.rt.queue)} waiting · p95 ${Math.round(state.rt.p95)}ms`],
  ['warn', () => `thermal · ${state.gpu.temp.toFixed(0)}°C · fan ${Math.round(state.gpu.fan)} rpm`],
  ['err',  () => `upstream timeout · retry ${Math.floor(rand() * 3) + 1}/3`],
  ['err',  () => `guardrail · prompt injection flagged · request dropped`],
];
let lastEvent = 0;

function pushEvent() {
  const ul = $('#events');
  if (!ul) return;
  const pick = EVENTS[Math.floor(rand() * EVENTS.length)];
  const li = document.createElement('li');
  li.dataset.lv = pick[0];
  li.innerHTML = `<span class="t">${clock()}</span><span class="lv">${pick[0]}</span><span class="msg">${pick[1]()}</span>`;
  ul.prepend(li);
  while (ul.children.length > 14) ul.lastChild.remove();
  const panel = $('#stream');
  panel.style.setProperty('--glow', '0.5');
  setTimeout(() => panel.style.setProperty('--glow', ''), 900);
}

/* --- paint ---------------------------------------------------------------- */
function paintSpark(canvas, data, accent) {
  const { ctx, w, h } = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  const lo = Math.min(...data), hi = Math.max(...data);
  const span = hi - lo || 1;
  ctx.lineJoin = 'round';
  ctx.shadowColor = accent;
  ctx.shadowBlur = 10;
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - 3 - ((v - lo) / span) * (h - 8);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0;
  ctx.globalAlpha = 0.16;
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  ctx.fillStyle = accent; ctx.fill();
  ctx.globalAlpha = 1;
}

function paintChart() {
  const c = $('#chart'); if (!c) return;
  const { ctx, w, h } = fit(c);
  ctx.clearRect(0, 0, w, h);

  ctx.strokeStyle = 'rgba(255,255,255,0.07)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = (i / 4) * h;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  let hi = 1;
  for (const m of MODELS) hi = Math.max(hi, Math.max(...state.hist[m.id]));
  hi *= 1.15;

  for (const m of MODELS) {
    const data = state.hist[m.id];
    const focused = state.focus === m.id;
    ctx.shadowColor = m.accent;
    ctx.shadowBlur = focused ? 18 : 8;
    ctx.globalAlpha = state.focus && !focused ? 0.35 : 1;
    ctx.strokeStyle = m.accent;
    ctx.lineWidth = focused ? 2.4 : 1.5;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = (i / (data.length - 1)) * w;
      const y = h - 4 - (v / hi) * (h - 12);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
    if (focused) {
      ctx.globalAlpha = 0.14; ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
      ctx.fillStyle = m.accent; ctx.fill();
    }
    ctx.globalAlpha = 1; ctx.shadowBlur = 0;
  }
}

function paintGauge(canvas, g) {
  const { ctx, w, h } = fit(canvas);
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2 + 4, r = Math.min(w, h) / 2 - 10;
  const v = state.gpu[g.key];
  const pct = Math.max(0, Math.min(1, v / g.max));
  const a0 = Math.PI * 0.75, a1 = Math.PI * 2.25;
  const hot = v >= g.warn;
  const accent = hot ? '#ff9ac2' : '#79d7ff';

  ctx.lineCap = 'round';
  ctx.strokeStyle = 'rgba(255,255,255,0.10)';
  ctx.lineWidth = 7;
  ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke();

  ctx.shadowColor = accent; ctx.shadowBlur = 14;
  ctx.strokeStyle = accent; ctx.lineWidth = 7;
  ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + (a1 - a0) * pct); ctx.stroke();
  ctx.shadowBlur = 0;

  ctx.strokeStyle = 'rgba(255,255,255,0.22)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 10; i++) {
    const a = a0 + (a1 - a0) * (i / 10);
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * (r - 6), cy + Math.sin(a) * (r - 6));
    ctx.lineTo(cx + Math.cos(a) * (r - 11), cy + Math.sin(a) * (r - 11));
    ctx.stroke();
  }

  ctx.fillStyle = '#e9f2f7';
  ctx.font = '600 15px ui-monospace, monospace';
  ctx.textAlign = 'center';
  ctx.fillText(`${g.key === 'util' || g.key === 'power' ? Math.round(v) : v.toFixed(1)}`, cx, cy + 1);
  ctx.fillStyle = 'rgba(233,242,247,0.55)';
  ctx.font = '500 10px ui-monospace, monospace';
  ctx.fillText(g.unit, cx, cy + 14);
}

function paintContext() {
  const c = $('#context-canvas'); if (!c) return;
  const { ctx, w, h } = fit(c);
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2, cy = h / 2;
  const R = Math.min(w, h) / 2 - 12;

  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();

  /* spokes */
  for (let i = 0; i < 12; i++) {
    const a = (i / 12) * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(a) * R * 0.30, cy + Math.sin(a) * R * 0.30);
    ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    ctx.stroke();
  }

  const n = state.sessions.length;
  state.sessions.forEach((s, i) => {
    const r = R * (0.34 + 0.66 * ((i + 1) / n));
    const frac = s.used / s.window;
    const a0 = -Math.PI / 2;
    const a1 = a0 + Math.PI * 2 * frac;

    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 5;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();

    ctx.shadowColor = s.accent; ctx.shadowBlur = 12;
    ctx.strokeStyle = s.accent; ctx.lineWidth = 5; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke();
    ctx.shadowBlur = 0;

    /* write head */
    ctx.fillStyle = s.accent;
    ctx.shadowColor = s.accent; ctx.shadowBlur = 10;
    ctx.beginPath();
    ctx.arc(cx + Math.cos(a1) * r, cy + Math.sin(a1) * r, 2.6, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
  });

  /* scan line */
  const a = -Math.PI / 2 + (state.t / 24) % (Math.PI * 2);
  const grad = ctx.createLinearGradient(cx, cy, cx + Math.cos(a) * R, cy + Math.sin(a) * R);
  grad.addColorStop(0, 'rgba(255,255,255,0.28)');
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.strokeStyle = grad; ctx.lineWidth = 1.4;
  ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R); ctx.stroke();

  const total = state.sessions.reduce((s, x) => s + x.used, 0);
  ctx.fillStyle = '#e9f2f7';
  ctx.font = '600 16px ui-monospace, monospace';
  ctx.textAlign = 'center';
  ctx.fillText(fmtTokens(total), cx, cy + 2);
  ctx.fillStyle = 'rgba(233,242,247,0.55)';
  ctx.font = '500 10px ui-monospace, monospace';
  ctx.fillText(`${n} live sessions`, cx, cy + 16);
}

function paint() {
  for (const k of KPIS) {
    const c = document.querySelector(`canvas[data-spark="${k.key}"]`);
    if (c) paintSpark(c, state.hist[k.key], k.accent);
  }
  for (const g of GAUGES) {
    const c = document.querySelector(`canvas[data-gauge="${g.key}"]`);
    if (c) paintGauge(c, g);
  }
  paintChart();
  paintContext();
}

/* --- text readouts -------------------------------------------------------- */
function render() {
  for (const k of KPIS) {
    const el = document.querySelector(`[data-kpi="${k.key}"]`);
    const d = document.querySelector(`[data-delta="${k.key}"]`);
    if (!el) continue;
    const v = state.rt[k.key] ?? state.gpu[k.key];
    el.innerHTML = `${k.fmt(v)}${k.unit ? ` <small>${k.unit}</small>` : ''}`;
    const h = state.hist[k.key];
    const prev = h[h.length - 2] ?? v;
    const diff = v - prev;
    d.textContent = `${diff >= 0 ? '+' : ''}${diff.toFixed(1)}`;
    d.className = 'delta ' + (diff >= 0 ? 'up' : 'down');
  }

  for (const m of MODELS) {
    const lane = document.querySelector(`.lane[data-model="${m.id}"]`);
    if (!lane) continue;
    const h = state.hist[m.id];
    const tps = h[h.length - 1];
    lane.querySelector('[data-lane="tps"]').textContent = tps.toFixed(1);
    lane.querySelector('[data-lane="p95"]').textContent = Math.round(state.rt.p95 * (0.7 + m.share));
    lane.querySelector('[data-lane="share"]').textContent = (tps / state.rt.tps * 100).toFixed(0) + '%';
    lane.querySelector('.meter i').style.width = Math.min(100, tps / state.rt.tps * 100 * 2.6) + '%';
  }

  for (const [k, v] of Object.entries(state.tok)) {
    const el = document.querySelector(`[data-tok="${k}"]`);
    if (el) el.textContent = fmtTokens(v);
    const bar = document.querySelector(`[data-bar="${k}"]`);
    if (bar) bar.style.width = (v / state.tok.lifetime * 100).toFixed(1) + '%';
  }

  const cells = document.querySelectorAll('#cells .cell i');
  state.pool.forEach((p, i) => { if (cells[i]) cells[i].style.height = Math.round(p * 100) + '%'; });

  const legend = $('#ctx-legend');
  if (legend) {
    legend.innerHTML = state.sessions.slice(0, 6).map(s =>
      `<span><i style="background:${s.accent}"></i>${s.id} · ${fmtTokens(s.used)}/${fmtTokens(s.window)}</span>`).join('');
  }

  const hw = $('#hw-readout');
  if (hw) hw.innerHTML = [
    ['clock', `${state.gpu.clock} MHz`],
    ['fan', `${Math.round(state.gpu.fan)} rpm`],
    ['bus', state.gpu.bus],
    ['batch', String(state.rt.batch)],
  ].map(([a, b]) => `<span>${a} <b>${b}</b></span>`).join('');

  const mini = $('#mini');
  if (mini) mini.innerHTML = [
    ['today', fmtTokens(Math.round(state.rt.tps * 86400 * 0.11))],
    ['avg tok/req', String(Math.round(state.rt.tps * 60 / state.rt.rpm * 3.2))],
    ['sessions', String(state.sessions.length)],
    ['error rate', state.rt.err.toFixed(2) + '%'],
    ['power limit', '350 W'],
    ['quant', 'Q4_K_M – Q8_0'],
  ].map(([a, b]) => `<span>${a} <b>${b}</b></span>`).join('');

  $('#uptime').textContent = uptime(state.rt.uptime);
  $('#driver').textContent = state.gpu.driver;
  $('#runstate').textContent = state.paused ? 'paused' : 'live';
  $('#orb').classList.toggle('warn', state.gpu.temp >= 78 || state.rt.queue > 9);
}

/* --- pointer light -------------------------------------------------------- */
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

/* --- keys ----------------------------------------------------------------- */
addEventListener('keydown', e => {
  if (e.key === 'p' || e.key === 'P') state.paused = !state.paused;
  if (e.key === 'r' || e.key === 'R') {
    rand = mulberry32((Math.random() * 2 ** 32) | 0);
    for (const k of Object.keys(state.hist)) state.hist[k].fill(0);
  }
});

/* --- run ------------------------------------------------------------------ */
buildBar(); buildKpi(); buildSignal(); buildGauges(); buildContext(); buildLedger(); buildLanes(); buildStream(); buildPool();

for (let i = 0; i < 60; i++) step();
for (let i = 0; i < 6; i++) pushEvent();
render(); paint();
applyLight();

let last = performance.now(), simAcc = 0, paintAcc = 0, textAcc = 0;
function loop(now) {
  const dt = (now - last) / 1000; last = now;
  if (!state.paused) {
    simAcc += dt;
    if (simAcc >= 0.12) { simAcc = 0; step(); }
  }
  textAcc += dt;
  if (textAcc >= 0.25) { textAcc = 0; render(); }
  paintAcc += dt;
  if (paintAcc >= 1 / 30) { paintAcc = 0; paint(); }
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
