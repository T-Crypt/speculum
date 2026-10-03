/* =========================================================================
   ui.js — Speculum UI primitives + shared view helpers.
   Presentational only: no data fetching, no metric math, no engine names.
   ES module (no build); imported by app.js.
   ========================================================================= */

/* --- settings (single persistence point: "speculum.ui.*") ---------------- */
const NS = 'speculum.ui.';
function load(k, d) {
  try { const v = localStorage.getItem(NS + k); return v == null ? d : v; }
  catch { return d; }
}
function save(k, v) { try { localStorage.setItem(NS + k, v); } catch {} }

export const prefs = {
  get theme() { return load('theme', 'dark'); },
  set theme(v) { save('theme', v); },
  get ripple() { return load('ripple', 'off'); },          // 'off' | 'on'
  set ripple(v) { save('ripple', v); },
  get reduceMotion() { return load('motion', 'system'); }, // 'system' | 'on'
  set reduceMotion(v) { save('motion', v); },
};

/* --- tokens --------------------------------------------------------------- */
const styleCache = new Map();
export function cssVar(name) {
  if (!styleCache.has(name)) {
    styleCache.set(name, getComputedStyle(document.documentElement).getPropertyValue(name).trim());
  }
  return styleCache.get(name);
}
export function invalidateStyleCache() { styleCache.clear(); }
/* series color by stable index (0-based), max 6 */
export function seriesColor(i) { return cssVar('--ser-' + ((i % 6) + 1)); }

export function motionReduced() {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return true;
  return prefs.reduceMotion === 'on';
}

/* --- formatting (display only) --------------------------------------------- */
export function fmtTok(n) {
  if (n == null || !isFinite(n)) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}
export function fmtNum(v, dp = 1) {
  if (v == null || !isFinite(v)) return '—';
  const s = Number(v).toFixed(dp);
  return dp === 0 ? s : s.replace(/\.0+$/, '');
}
export function fmtPct(v) { return v == null ? '—' : fmtNum(v, 1) + '%'; }
/* ms → human duration: "342 ms" / "1.24 s" */
export function fmtDur(ms) {
  if (ms == null || !isFinite(ms)) return '—';
  return ms < 1000 ? Math.round(ms) + ' ms' : (ms / 1000).toFixed(2) + ' s';
}
export function clockStr(t) {
  const d = new Date((t || Date.now() / 1000) * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map(x => String(x).padStart(2, '0')).join(':');
}
export function upStr(sec) {
  if (sec == null) return '—';
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
}
/* decode escaped sequences (\u003c → <) at render time only */
export function decodeEscapes(s) {
  return String(s ?? '')
    .replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\\\"/g, '"');
}

/* --- tiny DOM helper --------------------------------------------------------- */
export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k === 'data') for (const [dk, dv] of Object.entries(v)) n.dataset[dk] = dv;
    else if (k === 'aria') for (const [ak, av] of Object.entries(v)) n.setAttribute('aria-' + ak, av);
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else if (k === 'style') Object.assign(n.style, v);
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    n.append(c.nodeType ? c : document.createTextNode(c));
  }
  return n;
}

/* --- Panel --------------------------------------------------------------------- */
/* { el, head, body, title, hint } — tools appended to head by the caller */
export function Panel({ title, hint = '', flush = false, tools = null, ariaLabel }) {
  const head = el('div', { class: 'panel-head' },
    el('h2', { class: 'panel-title' }, title),
  );
  if (hint) head.append(el('p', { class: 'panel-hint' }, hint));
  head.append(el('span', { class: 'spacer', 'aria-hidden': 'true' }));
  const toolsBox = el('span', { class: 'panel-tools' }, tools);
  head.append(toolsBox);
  const body = el('div', { class: 'panel-body' });
  const root = el('section',
    { class: 'panel' + (flush ? ' panel--flush' : ''), ...(ariaLabel ? { 'aria-label': ariaLabel } : {}) },
    head, body);
  return { el: root, head, body, title: head.querySelector('.panel-title'), tools: toolsBox };
}
/* stale chip: insert into a panel head; text is set by the paint layer */
export function StaleChip() {
  return el('span', { class: 'stale-chip', role: 'status' }, 'Stale');
}

/* --- Stat -------------------------------------------------------------------------- */
/* { label, unit, value (node|text), status ('warn'|'crit' only when over threshold),
      delta (hidden when null), spark (canvas) } → { el, value, delta, spark } */
export function Stat({ label, unit = null, value = '—', status = null,
                       delta = null, spark = null, small = false, title = null }) {
  const val = el('div', { class: 'stat-value', ...(status ? { 'data-status': status } : {}), ...(title ? { title } : {}) });
  val.append(value == null ? '—' : value);
  if (unit) val.append(el('span', { class: 'unit' }, unit));
  const d = el('div', { class: 'stat-delta' }, delta);
  d.hidden = delta == null || delta === '';
  const root = el('div', { class: 'stat' + (small ? ' stat--sm' : '') },
    el('div', { class: 'stat-label' }, label), val, d);
  if (spark) root.append(spark);
  return { el: root, value: val, delta: d, spark };
}
/* delta content helper: direction arrow + value + comparison window */
export function deltaText(cur, prev, fmt, window = 'vs 15m') {
  if (cur == null || prev == null || !isFinite(cur) || !isFinite(prev)) return null;
  const diff = cur - prev;
  if (Math.abs(diff) < 1e-9) return null;
  const arrow = diff > 0 ? '↑' : '↓';
  return `${arrow} ${diff > 0 ? '+' : ''}${fmt(diff)} ${window}`;
}

/* --- Meter ----------------------------------------------------------------------------- */
/* { label, value, max, unit, ticks: [{ at: 0..1, status? }],
      status? (fill color), display? (override value text) }
   value == null → fill hidden, shows "n/a" in tertiary text. */
export function Meter({ label, value = null, max = null, unit = null,
                        ticks = [], status = null, display = null }) {
  const fill = el('i', { class: 'meter-fill' });
  const meter = el('div', { class: 'meter', role: 'meter',
    ...(max != null ? { 'aria-valuemax': String(max) } : {}),
    ...(value != null ? { 'aria-valuenow': String(value) } : {}) }, fill);
  for (const t of ticks) {
    const tk = el('i', { class: 'meter-tick', ...(t.status ? { 'data-status': t.status } : {}) });
    tk.style.left = (Math.max(0, Math.min(1, t.at)) * 100) + '%';
    meter.append(tk);
  }
  const v = el('span', { class: 'meter-value', ...(status ? { 'data-status': status } : {}) });
  const txt = display != null ? display
    : value == null ? 'n/a'
    : max != null ? `${fmtNum(value, value >= 100 ? 0 : 1)}${unit ? ' ' + unit : ''} / ${fmtNum(max, 0)}${unit ? ' ' + unit : ''}`
    : `${fmtNum(value, value >= 100 ? 0 : 1)}${unit ? ' ' + unit : ''}`;
  v.append(document.createTextNode(txt));
  if (value == null) v.classList.add('data-na');
  const root = el('div', { class: 'meter-row' },
    el('span', { class: 'meter-row-label' }, label), meter, v);
  root._fill = fill;
  return { el: root, fill, meter, valueEl: v,
    set(value, max2 = max, status2 = status) {
      root._value = value; root._max = max2 ?? null;
      if (value == null || max2 == null || max2 <= 0) {
        fill.style.width = '0%';
        fill.removeAttribute('data-status');
      } else {
        fill.style.width = (Math.max(0, Math.min(1, value / max2)) * 100).toFixed(2) + '%';
      }
      v.textContent = value == null ? 'n/a'
        : max2 != null ? `${fmtNum(value, value >= 100 ? 0 : 1)}${unit ? ' ' + unit : ''} / ${fmtNum(max2, 0)}${unit ? ' ' + unit : ''}`
        : `${fmtNum(value, value >= 100 ? 0 : 1)}${unit ? ' ' + unit : ''}`;
      v.classList.toggle('data-na', value == null);
      if (status2) { fill.dataset.status = status2; v.dataset.status = status2; }
      else { fill.removeAttribute('data-status'); v.removeAttribute('data-status'); }
    } };
}

/* --- Sparkline --------------------------------------------------------------------------- */
export function sparkCanvas(cls = '', label = 'trend') {
  const c = document.createElement('canvas');
  c.className = 'sparkline ' + cls.trim();
  c.setAttribute('role', 'img');
  c.setAttribute('aria-label', label);
  return c;
}
/* 1.5 px line in the passed accent with a soft same-color glow (static,
   nothing to reduce under reduced motion), area fill fading .28 → 0.
   data: oldest → newest. */
export function paintSpark(canvas, data, color) {
  if (!canvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth || 120, h = canvas.clientHeight || 32;
  const W = Math.max(1, Math.round(w * dpr)), H = Math.max(1, Math.round(h * dpr));
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const d = (data || []).slice(-360);
  if (d.length < 2) {
    ctx.fillStyle = cssVar('--text-3');
    ctx.font = `11px ${cssVar('--font-sans')}`;
    ctx.textAlign = 'center';
    ctx.fillText('no data', w / 2, h / 2 + 4);
    return;
  }
  const nums = d.map(v => (isFinite(v) ? v : null));
  const valid = nums.filter(v => v != null);
  if (!valid.length) return;
  let lo = Math.min(...valid), hi = Math.max(...valid);
  if (hi - lo < 1e-9) { hi += 1; }
  const col = color || cssVar('--accent');
  ctx.lineJoin = 'round';
  ctx.strokeStyle = col;
  ctx.lineWidth = 1.5;
  ctx.shadowColor = col;      /* sanctioned soft glow, same colour as the line */
  ctx.shadowBlur = 6;
  ctx.beginPath();
  let started = false;
  nums.forEach((v, i) => {
    if (v == null) { started = false; return; }
    const x = (i / (nums.length - 1)) * w;
    const y = h - 3 - ((v - lo) / (hi - lo)) * (h - 6);
    started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    started = true;
  });
  ctx.stroke();
  ctx.shadowBlur = 0;
  /* area fill: gradient alpha .28 → 0; flat 28 % fill when the colour is not
     hex-parseable (the DOM stub returns '' from cssVar) or the context lacks
     gradient support */
  const flat = !hexToRgba(col) || typeof ctx.createLinearGradient !== 'function';
  if (flat) {
    ctx.globalAlpha = 0.28;
    ctx.fillStyle = col;
  } else {
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, hexToRgba(col, 0.28));
    g.addColorStop(1, hexToRgba(col, 0));
    ctx.fillStyle = g;
  }
  ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
  ctx.fill();
  ctx.globalAlpha = 1;
}
/* '#rgb' / '#rrggbb' → 'rgba(r, g, b, a)'; null when unparseable */
export function hexToRgba(hex, a) {
  const m = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.exec(hex || '');
  if (!m) return null;
  let h = m[1];
  if (h.length === 3) h = h.split('').map(x => x + x).join('');
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

/* --- Badge / StatusPill -------------------------------------------------------------------- */
export function Badge({ status = 'idle', label, dot = true, swatch = null, muted = false }) {
  const b = el('span', { class: 'badge' + (muted ? ' badge--muted' : ''),
    ...(status ? { 'data-status': status } : {}) },
    swatch ? el('span', { class: 'swatch', style: { background: swatch } }) : null,
    dot ? el('i', { class: 'bdot', 'aria-hidden': 'true' }) : null,
    label);
  return b;
}
export function StatusPill({ state = 'paused', label = 'Paused' }) {
  return el('span', { class: 'status-pill', 'data-state': state,
    'aria-label': 'Overall state: ' + label },
    el('i', { class: 'pdot', 'aria-hidden': 'true' }), el('span', { class: 'pill-label' }, label));
}

/* --- DataTable --------------------------------------------------------------------------------- */
/* columns: [{ key, label, cls?, num?, mono? }] — shell only; the paint
   layer fills tbody. Returns { el, tbody, theadCols }. */
export function DataTable({ columns, caption = '' }) {
  const thead = el('thead');
  thead.append(el('tr', {}, columns.map(c => {
    const th = el('th', { scope: 'col', class: [c.num ? 'col-num' : '', c.mono ? 'col-mono' : ''].join(' ').trim() }, c.label);
    th.style.width = c.width || '';
    return th;
  })));
  const tbody = el('tbody');
  const t = el('table', { class: 'data-table', ...(caption ? { 'aria-label': caption } : {}) }, thead, tbody);
  return { el: t, thead, tbody, wrap: el('div', { class: 'table-wrap' }, t) };
}
/* one data row: cells = array of { text|node, cls?, title? } or raw values */
export function TableRow(cells, { expandable = false } = {}) {
  const tr = el('tr');
  for (const c of cells) {
    const td = el('td', {});
    if (c == null) { td.append('—'); td.classList.add('col-sub'); }
    else if (typeof c === 'object' && c.nodeType) td.append(c);
    else if (typeof c === 'object' && c.el) {
      if (c.cls) td.className = c.cls;
      if (c.title) td.title = c.title;
      td.append(c.el);
    } else {
      if (c && c.cls) td.className = c.cls;
      if (c && c.title) td.title = c.title;
      td.append(typeof c === 'object' ? String(c.text != null ? c.text : '—') : String(c));
    }
    tr.append(td);
  }
  if (expandable) {
    const btn = el('button', { class: 'data-table-expand', 'aria-label': 'Show detail', 'aria-expanded': 'false' }, '▸');
    btn.addEventListener('click', () => {
      const detail = tr.nextElementSibling;
      if (!detail || !detail.classList.contains('row-detail')) return;
      const open = btn.getAttribute('aria-expanded') === 'true';
      btn.setAttribute('aria-expanded', String(!open));
      detail.hidden = open;
      tr.classList.toggle('is-expanded', !open);
    });
    const lastTd = tr.lastElementChild;
    lastTd.append(btn);
  }
  return tr;
}

/* --- EmptyState / Skeleton ------------------------------------------------------------------------ */
export function EmptyState(text) {
  return el('div', { class: 'empty-state' }, text);
}
export function Skeleton(variant = 'line', width = '100%', height = null) {
  const s = el('div', { class: `skeleton skeleton--${variant}` });
  s.style.width = width;
  if (height) s.style.height = height;
  return s;
}

/* --- Chips / Segmented (filter + settings controls) --------------------------------------------------- */
export function Chips(options, active = null, onPick) {
  const wrap = el('span', { class: 'chips', role: 'group' });
  for (const o of options) {
    const b = el('button', {
      type: 'button', class: 'chip',
      'aria-pressed': String(o.value === active),
      ...(o.swatch ? { style: { '--swatch': o.swatch } } : {}),
    }, o.swatch ? el('span', { class: 'swatch', style: { background: o.swatch } }) : null, o.label);
    b.addEventListener('click', () => onPick(o.value));
    wrap.append(b);
  }
  return wrap;
}
export function Segmented(options, active = null, onChange) {
  const wrap = el('span', { class: 'segmented', role: 'group' });
  for (const o of options) {
    const b = el('button', { type: 'button', 'aria-pressed': String(o.value === active) }, o.label);
    b.addEventListener('click', () => {
      for (const x of wrap.children) x.setAttribute('aria-pressed', 'false');
      b.setAttribute('aria-pressed', 'true');
      onChange(o.value);
    });
    wrap.append(b);
  }
  return wrap;
}

/* --- Menu (top-bar settings) -------------------------------------------------------------------------- */
export function Menu(label, rows, { onOpen = () => {} } = {}) {
  const root = el('div', { class: 'menu' });
  const btn = el('button', { type: 'button', class: 'menu-btn', 'aria-haspopup': 'menu', 'aria-expanded': 'false' },
    el('span', {}, label));
  const panel = el('div', { class: 'menu-panel', role: 'menu', hidden: true }, rows);
  root.append(btn, panel);
  btn.addEventListener('click', () => {
    const open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    onOpen(open);
  });
  btn.addEventListener('keydown', e => { if (e.key === 'Escape' && !panel.hidden) { panel.hidden = true; btn.setAttribute('aria-expanded', 'false'); } });
  document.addEventListener('click', e => {
    if (!panel.hidden && !root.contains(e.target)) { panel.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
  });
  return { el: root, btn, panel };
}
