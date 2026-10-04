/* Smoke test: execute ui.js + ripple.js against a minimal DOM stub.
   Verifies: markup of every primitive, ripple on/off/reduced-motion,
   localStorage persistence, formatters. Run: node smoke/ui-primitives.mjs */
/* --- minimal DOM stub ------------------------------------------------------ */
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
  setAttribute(k, v) { this._attrs.set(k, String(v)); if (k === 'hidden') this.hidden = v != null && v !== 'false'; }
  getAttribute(k) { return this._attrs.has(k) ? this._attrs.get(k) : null; }
  removeAttribute(k) { this._attrs.delete(k); if (k === 'hidden') this.hidden = false; }
  append(...cs) { for (const c of cs) { if (c == null) continue; if (typeof c === 'object') c.parentNode = this; this.children.push(c); } }
  addEventListener(t, fn) { (this._listeners[t] ??= []).push(fn); }
  removeEventListener() {}
  get nextElementSibling() { return null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  querySelector() { return null; }
  getContext() {
    return {
      setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
      closePath() {}, stroke() {}, fill() {}, fillText() {},
      fillStyle: '', strokeStyle: '', font: '', textAlign: '',
      lineWidth: 1, lineJoin: '', globalAlpha: 1,
    };
  }
  _html() {
    const a = [...this._attrs].map(([k, v]) => ` ${k}="${v}"`).join('');
    const c = this.children.map(n => (n && n.nodeType === 1 ? n._html() : n && n.nodeType === 3 ? n.textContent : String(n))).join('');
    return `<${this.tagName}${a} class="${this.className}">${c}`;
  }
}
const doc = {
  documentElement: new FakeEl('html'),
  createElement: t => new FakeEl(t),
  createTextNode: t => ({ nodeType: 3, textContent: t }),
  addEventListener() {}, removeEventListener() {},
  querySelector() { return null; },
};
const mq = { matches: false };
globalThis.document = doc;
globalThis.window = { devicePixelRatio: 2, addEventListener() {} };
globalThis.matchMedia = () => mq;
const store = new Map();
globalThis.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });

const ui = await import(new URL('../ui.js', import.meta.url).href);
const rip = await import(new URL('../ripple.js', import.meta.url).href);

let fails = 0;
const ok = (cond, name) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); if (!cond) fails++; };

/* --- Panel ------------------------------------------------------------------ */
const p = ui.Panel({ title: 'Context usage', hint: 'live sessions vs model window', flush: true });
ok(p.el.tagName === 'section' && p.el.className.includes('panel') && p.el.className.includes('panel--flush'), 'Panel builds .panel.panel--flush section');
ok(p.el.children[0].className === 'panel-head', 'Panel head first');
const headHtml = p.el.children[0]._html();
ok(headHtml.includes('panel-title') && headHtml.includes('Context usage'), 'Panel title rendered');
ok(headHtml.includes('panel-hint') && headHtml.includes('live sessions vs model window'), 'Panel hint rendered');
ok(headHtml.includes('panel-tools'), 'Panel tools slot present');

/* --- StaleChip --------------------------------------------------------------- */
const chip = ui.StaleChip();
ok(chip.className === 'stale-chip' && chip.getAttribute('role') === 'status', 'StaleChip markup');

/* --- Stat ---------------------------------------------------------------------- */
const s = ui.Stat({ label: 'First token', unit: 's', value: '0.42', delta: ui.deltaText(420, 380, d => (d / 1000).toFixed(2) + 's', 'vs 15m') });
ok(s.el.className === 'stat' && s.el.children[0].textContent === 'First token', 'Stat label');
ok(String(s.value.children[0]) === '0.42', 'Stat value');
ok(s.value.children[1].className === 'unit' && s.value.children[1].textContent === 's', 'Stat unit');
ok(s.delta.textContent === '↑ +0.04s vs 15m' && !s.delta.hidden, 'Stat delta arrow + window');
const s0 = ui.Stat({ label: 'Queue', delta: ui.deltaText(3, 3, d => String(d)) });
ok(s0.delta.hidden && s0.delta.textContent === '', 'Zero delta hidden (no meaningless +0.0)');

/* --- Meter --------------------------------------------------------------------- */
const m = ui.Meter({ label: 'Temperature', value: 71, max: 95, unit: '°C', status: 'ok', ticks: [{ at: 80 / 95, status: 'warn' }, { at: 90 / 95, status: 'crit' }] });
ok(m.el.className === 'meter-row', 'Meter row');
ok(m.meter.getAttribute('role') === 'meter' && m.meter.getAttribute('aria-valuenow') === '71', 'Meter a11y attrs');
ok(m.meter.children.length === 3, 'Meter has fill + 2 ticks');
ok(m.meter.children[1]._attrs.get('data-status') === 'warn', 'Tick warn status');
ok(m.meter.children[2]._attrs.get('data-status') === 'crit', 'Tick crit status');
ok(m.valueEl.textContent.includes('71') && m.valueEl.textContent.includes('95') && m.valueEl.textContent.includes('°C'), 'Meter shows value / max + unit');
ok(m.valueEl._attrs.get('data-status') === 'ok', 'Meter value status attr');
const mna = ui.Meter({ label: 'Fan', value: null });
ok(mna.valueEl.textContent === 'n/a', 'Missing value renders n/a');
m.set(92, 95, 'crit');
ok(m.meter.children[0].style.width === '96.84%', 'Meter.set width (92/95)');
ok(m.meter.children[0]._data.status === 'crit', 'Meter.set fill status');
ok(m.valueEl._attrs.get('data-status') === 'crit', 'Meter.set value status');
m.set(null);
ok(m.meter.children[0].style.width === '0%', 'Meter.set null clears fill');

/* --- Sparkline ------------------------------------------------------------------- */
const c = ui.sparkCanvas('', 'throughput');
ok(c.className === 'sparkline' && c.getAttribute('aria-label') === 'throughput', 'sparkCanvas markup');
ui.paintSpark(c, [1, 2, 3, null, 5, 6], '#4C8DFF');
ui.paintSpark(c, [], '#4C8DFF'); // empty-state path
ok(true, 'paintSpark executes (draw + empty paths)');

/* --- Badge / StatusPill ------------------------------------------------------------ */
const b = ui.Badge({ status: 'ok', label: 'Running', swatch: '#4C8DFF' });
ok(b.className === 'badge' && b._attrs.get('data-status') === 'ok', 'Badge status attr');
ok(b.children[0].className === 'swatch', 'Badge swatch (color paired with label)');
const bm = ui.Badge({ status: 'idle', label: 'Stopped', muted: true });
ok(bm.className.includes('badge--muted'), 'Badge muted variant');
const sp = ui.StatusPill({ state: 'degraded', label: 'Degraded' });
ok(sp.className === 'status-pill' && sp._attrs.get('data-state') === 'degraded', 'StatusPill state attr');

/* --- DataTable / TableRow ------------------------------------------------------------ */
const dt = ui.DataTable({ columns: [{ key: 't', label: 'Time', mono: true }, { key: 'tok', label: 'Tokens', num: true }], caption: 'Request history' });
ok(dt.el.className === 'data-table' && dt.thead.children[0].children.length === 2, 'DataTable columns');
ok(dt.thead.children[0].children[0]._attrs.get('scope') === 'col', 'Th scope');
ok(dt.thead.children[0].children[1].className.includes('col-num'), 'Numeric column class');
const tr = ui.TableRow([{ cls: 'col-mono', text: '12:00:01' }, { cls: 'col-num', text: '1,204' }]);
ok(tr.children.length === 2 && tr.children[1].className === 'col-num', 'TableRow cells');

/* --- EmptyState / Skeleton / Chips / Segmented / Menu ---------------------------------- */
ok(ui.EmptyState('No active sessions').textContent === 'No active sessions', 'EmptyState');
ok(ui.Skeleton('spark').className.includes('skeleton--spark'), 'Skeleton');
let picked = null;
const chips = ui.Chips([{ value: 'all', label: 'All' }, { value: 'warn', label: 'Warn', swatch: '#E5A443' }], 'warn', v => picked = v);
ok(chips.children.length === 2 && chips.children[1]._attrs.get('aria-pressed') === 'true', 'Chips active state');
chips.children[0]._listeners.click[0]();
ok(picked === 'all', 'Chips onPick fires');
let seg = null;
const sg = ui.Segmented([{ value: '15m', label: '15m' }, { value: '1h', label: '1h' }], '15m', v => seg = v);
sg.children[1]._listeners.click[0]();
ok(seg === '1h' && sg.children[1]._attrs.get('aria-pressed') === 'true', 'Segmented onChange');
const menu = ui.Menu('Settings', [ui.Skeleton('line')]);
ok(menu.btn._attrs.get('aria-haspopup') === 'menu' && menu.panel.hidden === true, 'Menu closed by default');
menu.btn._listeners.click[0]();
ok(menu.panel.hidden === false && menu.btn._attrs.get('aria-expanded') === 'true', 'Menu opens');

/* --- formatters ------------------------------------------------------------------------ */
ok(ui.fmtTok(1234567) === '1.2M' && ui.fmtTok(null) === '—' && ui.fmtTok(999) === '999', 'fmtTok');
ok(ui.fmtDur(342) === '342 ms' && ui.fmtDur(1240) === '1.24 s', 'fmtDur');
ok(typeof ui.clockStr(0) === 'string', 'clockStr');
ok(ui.upStr(41 * 3600) === '1d 17h', 'upStr');
ok(ui.decodeEscapes('a\\u003cb') === 'a<b' && ui.decodeEscapes('x\\ny') === 'x\ny', 'decodeEscapes');
ok(ui.deltaText(null, 5, d => d) == null, 'deltaText null-safe');
ok(typeof ui.seriesColor(7) === 'string', 'seriesColor index wrap');

/* --- ripple module ------------------------------------------------------------------------ */
const root = doc.documentElement;
ok(rip.applyRippleSetting() === false && root.getAttribute('data-ripple') == null, 'Ripple off by default');
const panel = new FakeEl('div'); panel.className = 'panel';
rip.pulse(panel);
ok(!panel.classList.contains('is-rippling'), 'Ripple pulse no-op while off');
store.set('speculum.ui.ripple', 'on');
ok(rip.applyRippleSetting() === true && root.getAttribute('data-ripple') === 'on', 'Ripple on via setting + root attr');
rip.pulse(panel);
ok(panel.classList.contains('is-rippling'), 'Ripple pulse applies class when on');
rip.pulse(panel); // back-to-back update restarts, no throw
ok(panel.classList.contains('is-rippling'), 'Ripple restart on consecutive updates');
mq.matches = true;
ok(rip.applyRippleSetting() === false && root.getAttribute('data-ripple') == null, 'Reduced motion forces ripple off regardless of setting');
ui.prefs.ripple = 'off';
ok(store.get('speculum.ui.ripple') === 'off', 'prefs.ripple persists to localStorage');
ui.prefs.theme = 'light';
ok(store.get('speculum.ui.theme') === 'light', 'prefs.theme persists');
ok(ui.prefs.reduceMotion === 'system', 'reduceMotion default = system');
ok(ui.prefs.view === 'basic', 'view default = basic');
ui.prefs.view = 'advanced';
ok(store.get('speculum.ui.view') === 'advanced', 'prefs.view persists');

console.log(fails === 0 ? '\nALL PASS' : `\n${fails} FAILURES`);
process.exit(fails ? 1 : 0);
