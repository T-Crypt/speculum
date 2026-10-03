/* =========================================================================
   viewmodel.js — pure view-model adapters: raw state → per-panel data.
   No DOM, no fetch, no engine name literals. Live and demo states pass
   through the same functions.
   ========================================================================= */

/* --- engine registry --------------------------------------------------------
   Built from the engine list the backend already provides (state.engines).
   Series colors are assigned by stable index: engines sorted by key,
   palette of 6 from tokens. Adding or removing an engine in the backend
   requires zero UI code changes. */
export function buildEngineRegistry(engines) {
  const list = (Array.isArray(engines) ? engines : Object.values(engines || {}))
    .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  const byKey = new Map();
  list.forEach((e, i) => byKey.set(e.key, { e, index: i, colorIndex: i % 6 }));
  return { list, byKey };
}

/* engine state in words (state is always paired with a label, never
   color alone): running / stopped / unresponsive / no data */
export function engineState(e) {
  if (!e) return { key: 'unknown', word: 'No data', muted: true };
  if (e.latched) return { key: 'latched', word: 'Unresponsive', muted: false };
  if (e.up === true) return { key: 'running', word: 'Running', muted: false };
  if (e.up === false) return { key: 'stopped', word: 'Stopped', muted: true };
  return { key: 'unknown', word: 'No data', muted: true };
}
/* badge status token for the pill/badge primitives */
export function engineBadgeStatus(e) {
  const s = engineState(e);
  if (s.key === 'latched') return 'crit';
  if (s.key === 'running') return 'ok';
  if (s.key === 'stopped') return 'idle';
  return 'idle';
}

/* --- overall state (top-bar pill) -------------------------------------------- */
/* Live: feed flowing, no alerts, no engine latched/erroring. An engine that
   is simply not running (hand-started, or stopped) is normal operation, not
   a fault — the engine cards say so in words. Degraded: any alert, or any
   engine latched (unresponsive). Offline: feed not flowing. Demo counts as
   live. Booting/paused are transient UI states. */
export function overallState({ mode, paused = false, alerts = [], engines = [] }) {
  if (mode === 'offline') return { state: 'offline', word: 'Offline' };
  if (mode === 'boot') return { state: 'paused', word: 'Booting' };
  if (paused) return { state: 'paused', word: 'Paused' };
  const bad = alerts.length > 0 || engines.some(e => e.latched);
  return bad ? { state: 'degraded', word: 'Degraded' } : { state: 'live', word: 'Live' };
}

/* --- token ledger ---------------------------------------------------------------
   Per-column token sums: last hour / last 24 h (window sums over the client's
   request buffer) + since-start (derived from engine counters, which accept
   both the live llamacpp/ninfer metric names and the demo generated/prompt/
   cache keys). Notes state honestly when the buffer is capped or empty.
   Returns { cols, footer, notes }. */
const LEDGER_COUNTER_KEYS = [
  ['llamacpp:tokens_predicted_total', 'generated'],
  ['llamacpp:prompt_tokens_total', 'prompt'],
  ['ninfer:prefix_cache_hit_tokens_total', 'cache'],
];
export function tokenLedger(state, nowS = null, { bufferCap = 500 } = {}) {
  const reqs = Array.isArray(state.requests) ? state.requests : [];
  let now = nowS;
  if (now == null) for (const r of reqs) if (r && isFinite(r.t) && (now == null || r.t > now)) now = r.t;
  if (now == null) now = (state.t && state.t > 1e9) ? state.t : Date.now() / 1000;

  const win = (sec) => {
    const cut = now - sec;
    let generated = 0, fresh = 0, cached = 0, n = 0;
    for (const r of reqs) {
      if (!r || r.t == null || r.t < cut) continue;
      n++;
      generated += r.output || 0;
      cached += r.cache || 0;
      fresh += r.fresh != null ? r.fresh : Math.max(0, (r.prompt || 0) - (r.cache || 0));
    }
    return { generated, fresh, cached, reqs: n };
  };

  /* since engine load from the engine token counters (counted since the
     engine process started, not since the collector started) */
  let sGen = 0, sPrompt = 0, sCached = 0, hasCounters = false;
  for (const e of Array.isArray(state.engines) ? state.engines : []) {
    const C = (e && e.counters) || null;
    if (!C) continue;
    let got = false;
    for (const [live, demoKey] of LEDGER_COUNTER_KEYS) {
      const v = C[live] != null ? C[live] : C[demoKey];
      if (v == null || !isFinite(v)) continue;
      got = true;
      if (live === 'llamacpp:tokens_predicted_total' || demoKey === 'generated') sGen += v;
      else if (live === 'llamacpp:prompt_tokens_total' || demoKey === 'prompt') sPrompt += v;
      else sCached += v;
    }
    if (got) hasCounters = true;
  }
  const since = hasCounters
    ? { generated: sGen, fresh: Math.max(0, sPrompt - sCached), cached: sCached, reqs: null }
    : { generated: null, fresh: null, cached: null, reqs: null };

  /* footer stats over the whole buffered window */
  let bGen = 0, bFresh = 0, bCached = 0, bPrompt = 0, mAcc = 0, mTot = 0;
  for (const r of reqs) {
    if (!r) continue;
    bGen += r.output || 0;
    bCached += r.cache || 0;
    bFresh += r.fresh != null ? r.fresh : Math.max(0, (r.prompt || 0) - (r.cache || 0));
    bPrompt += r.prompt != null ? r.prompt : (r.cache || 0) + (r.fresh || 0);
    if (r.mtp_tot) { mAcc += r.mtp_acc || 0; mTot += r.mtp_tot; }
  }
  const footer = {
    reqs: reqs.length,
    generated: bGen,
    cache: bPrompt > 0 ? (100 * bCached / bPrompt) : null,
    reingest: bPrompt > 0 ? (100 * bFresh / bPrompt) : null,
    mtp: mTot > 0 ? (100 * mAcc / mTot) : null,
  };

  const notes = [];
  if (reqs.length >= bufferCap) notes.push(`request buffer is capped at ${bufferCap} — window sums cover the newest requests only`);
  if (reqs.length === 0) notes.push('no requests recorded yet');
  if (!hasCounters) notes.push('engine-load totals unavailable — no engine exposes token counters');

  return {
    now,
    cols: [
      { key: '1h', label: 'Last hour', ...win(3600) },
      { key: '24h', label: 'Last 24 h', ...win(86400) },
      { key: 'since', label: 'Since engine load', ...since },
    ],
    footer,
    notes,
  };
}

/* --- staleness ------------------------------------------------------------------ */
/* feed silent for more than 10 s → stale. Data stays visible, marked. */
export const STALE_MS = 10000;
export function staleInfo(lastTickMs, nowMs, thresholdMs = STALE_MS) {
  if (lastTickMs == null) return null; // never had data: loading, not stale
  const age = nowMs - lastTickMs;
  if (age < thresholdMs) return null;
  return { seconds: Math.max(1, Math.round(age / 1000)) };
}
export function staleLabel(info) {
  return `Stale — last update ${info.seconds}s ago`;
}
