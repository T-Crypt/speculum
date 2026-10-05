# AGENTS.md — Speculum

Calm, dense observability dashboard for a local LLM runtime: one RTX 4090 box running
llama-swap → NInfer (and optionally Strata) serving local models, watched by a
stdlib-only Python collector that polls everything and serves the panels plus
a 1 Hz feed.

## Architecture (two parts, zero build)

```
nvidia-smi · /proc · llama-swap :9090 · NInfer backend (port from /running proxy) · Strata :8080
        │ (each optional; all poll threads degrade to "no data")
        ▼
collector/speculum.py  — one Python 3 process, daemon threads, shared State under one RLock
        │
        ├─ serves static files from repo root (index.html, tokens.css, components.css,
        │  layout.css, app.js, ui.js, viewmodel.js, ripple.js)
        ├─ GET /api/snapshot  — one-shot full JSON: t, collector, gpu{last,hist}, host, kpi{last,hist},
        │                       models, running, engines, strata, requests[:200], events[:100], alerts, gen
        └─ GET /api/stream    — SSE: `event: tick` @ 1 Hz (hot values + new_events/new_requests)
                                (push `req`/`ev`/`alert` events documented but not written; clients
                                 only listen for `tick` — new data rides inside it)
        ▼
app.js  — EventSource with snapshot-poll fallback; owns paint loop, all panels, and the
          `?demo` simulator (mulberry32, fixed seed) that fills the exact same state shape
          without a backend. Primitives: ui.js · adapters: viewmodel.js · ripple: ripple.js
```

- Binds **127.0.0.1:8792 only**. Remote viewing is tailnet-only (`tailscale serve`) — never LAN or public.
- Collector footprint: `MemoryMax=64M` in the systemd unit. It is a thin poller + ring buffers, not an app server.

## File map

|File|Owns|
|---|---|
|`collector/speculum.py`|All live data: GPU/host/llama-swap/NInfer/Strata poll threads, `State`, KPI assembly @ 1 Hz, snapshot/tick, HTTP server. Stdlib only.|
|`collector/history.py`|SQLite history: one writer thread, 10 s commits, requests held 130 s for the merge window, minute/hour rollups, daily prune, `/api/history`, `/api/requests`, `/api/storage`, `/api/export`. `retention_days = 0` is a no-op. Stdlib `sqlite3` only.|
|`collector/engines.py`, `collector/config.py`|Engine adapters + the single backoff scheduler; `speculum.toml` loader.|
|`collector/speculum.service`|systemd `--user` unit. `CUDA_VISIBLE_DEVICES=` (GPU stays the llama-swap's), 64 M ceiling, nice 10.|
|`index.html`|Page shell: 48 px top bar header, `#deck` with nine named section shells (`p-kpi` … `p-pool`), foot, one module script. Panels are built into the shells by `app.js`.|
|`tokens.css`|Design tokens: dark theme (AETHER // NODE: `#05070a` base + faint radial wash, hairline borders, cyan/purple/green/yellow/orange/red series) on `:root`, light theme (darkened house hues, AA on white) on `[data-theme="light"]`; typography scale (11/12/13/14/30, Chakra Petch→system-ui display, Inter→system-ui body, mono numbers, tabular nums); spacing 4–32; radius 6/4; status + 6-series palettes; fixed semantic token colors (cached/fresh/generated); 2 px accent focus ring; reduced-motion kill switch (OS media query + `data-motion="reduced"`). Every text token is WCAG AA (4.5:1) on every surface it can sit on — documented in the file header.|
|`components.css`|UI primitives (sanctioned soft effects only: panel inner-top highlight, hover border tint, status-pill glow): Panel, PanelHeader, Stat (value+unit+delta+spark), Meter (fill+threshold ticks, `n/a` state), Sparkline sizing, Badge (status + series swatch + muted), StatusPill, DataTable (sticky header, numeric/mono columns, expandable rows), EmptyState, Skeleton, filter chips, segmented control, settings menu, stale chip, and the optional ripple styles (`data-ripple="on"` gated).|
|`layout.css`|Layout only: 48 px sticky top bar, 12-column deck grid (→ 6 columns at 1100 px, → 1 column at 768 px, no horizontal scroll), foot. No material, no tokens.|
|`app.js`|Feed (SSE + poll fallback + boot), `state`, demo simulator, paint loop, settings, and all panels. One `<script type="module">` in the HTML; `ui.js`/`viewmodel.js`/`ripple.js` are ES imports of that one module — still no build, no dependencies. Live and demo fill exactly the same state shape; the paint loop is mode-agnostic.|
|`ui.js`|DOM factories for every primitive + formatters (`fmtTok/fmtDur/clockStr/upStr/decodeEscapes/deltaText`) + `prefs` (localStorage `speculum.ui.*`, all try/catch) + `cssVar`/`seriesColor` + `paintSpark` (1.5 px line, soft same-colour glow, .28 → 0 area fill) + `Menu`. Presentational only: no data logic, no engine names.|
|`viewmodel.js`|Pure adapters (no DOM/fetch): `buildEngineRegistry` (engines from the payload, sorted by key, series color by stable index), engine/overall state words, staleness (10 s), token-ledger math. Live and demo states pass through the same functions.|
|`ripple.js`|The single, isolated ripple implementation. One 400 ms pulse (max opacity 0.08, accent) on data update; off by default; persisted under `speculum.ui.ripple`; applied via `<html data-ripple="on">`; zero cost (no listeners, no-op) while off; forced off whenever `prefers-reduced-motion: reduce` is set, whatever the setting says.|
|`smoke/`|Node DOM-stub smoke tests (no browser needed): `ui-primitives.mjs` (every primitive + ripple state machine + persistence) and `shell.mjs` (boots the real `app.js` shell + view-model unit checks).|
|`assets/`|Live screenshots captured on the monitor box, referenced from `README.md`.|
|`README.md`|User-facing doc — synced with the new UI; screenshots live in `assets/`.|

## Hard invariants (do not break)

1. **Python 3 stdlib only.** No pip, no vendored deps in `collector/`. The systemd unit runs bare `python3`. Floor: 3.9 with no config file; a TOML config needs 3.11+ (`config.py` imports `tomllib` lazily, only when a file is read).
2. **One GPU reader.** Exactly one long-lived `nvidia-smi -lms 1000` subprocess, read line by line, restarted on death. Never open a CUDA/NVML context in-process; never spawn a second nvidia-smi per poll. `CUDA_VISIBLE_DEVICES=` must stay set — the GPU belongs to llama-swap.
3. **All sources optional.** GPU, llama-swap, NInfer backend, Strata may each be down. Every panel degrades to an honest "no data"; a missing source never throws, never fabricates numbers, never takes the feed offline.
4. **Bounded memory.** 60 min @ 1 s ring buffers (GPU/host/KPI), last 500 inference requests, last 200 events. Snapshot trims to 200 requests / 100 events. No unbounded growth — the 64 M ceiling is real.
5. **Thread discipline.** One shared `State` guarded by a single `threading.RLock()`. Pollers mutate under lock; `snapshot()`/`tick()` copy out under lock; SSE handlers never block on it. New threads: `daemon=True`, started in `main()`.
6. **UI: no build, no dependencies.** One `<script type="module">` in `index.html`; the JS files are plain ES modules. Live and demo modes must fill **exactly the same state shape** (`state` + `applySnapshot`/`applyTick` normalization) — the paint loop is mode-agnostic.
7. **`mulberry32` must use `Math.imul`.** A plain `*` overflows past 2^53, the low bits `>>>` needs are gone, and the seeded generator collapses to a constant.
8. **Design contract** (tokens/components/layout CSS + `ripple.js`): no `backdrop-filter`, blur, or radial gauges. Operator override 2026-10-03 lifts the flat-only rule for the sanctioned soft effects: the faint radial body wash, panel inner-top highlight, hover border tint, status-pill glow, and the sparkline area-fill glow. All colors come from `:root` tokens; components never hardcode hex or engine names. Text tokens pass WCAG AA (4.5:1) on the surfaces they sit on (the two `--text-3` deviations from the raw spec, and the light-theme status/accent darkening, are documented in `tokens.css`). `prefers-reduced-motion: reduce` — or the user's Reduce-motion = On — kills all animation and disables the ripple regardless of its setting. The ripple is Off by default, toggleable, and persisted.
9. **No path escape.** Static handler resolves against repo root and 403s anything outside it.

## Key mechanics worth knowing before editing

- **NInfer backend discovery:** llama-swap `/running` reports the proxy port (e.g. `:5803`); `ninfer_backend_poll()` follows the active entry, polling its `/metrics` + `/slots`. Per-request records (TTFT, queue, prefill, decode, MTP) come from the NInfer upstream log stream relayed through llama-swap `GET /logs/stream/<model>`, parsed against `req#N done |` lines.
- **Engine registry (UI):** the UI builds its engine list, legends, cards, and series colors from the engine list the payload already provides (`viewmodel.buildEngineRegistry`) — sorted by engine key, color by stable index from the 6-color series palette. Adding/removing an engine in the backend requires zero UI code changes. Never hardcode engine display names in UI code; the lowercase `strata_up` tick field is a backend payload field name and is read as data.
- **Engine latch:** a run of "service unavailable" from a backend latches that engine red in the engine cards / top-bar Degraded state (`add_alert`/`clear_alert` in `State`).
- **Context / pool:** one bar per live KV slot (NInfer `/slots`, Strata `/slots`); fill = prompt tokens vs model window from the payload.
- **KPIs** (`KPI_KEYS`): `tps, rpm, p95, ttft, tpot, vram, cache, reingest, mtp, queue`. Computed @ 1 Hz in `kpi_thread`; per-KPI history feeds the sparklines. KPI-strip groups and value thresholds are defined in `app.js` (Phase 3).
- **Staleness:** no feed tick for > 10 s → every panel shows an amber "Stale — last update Xs ago" chip in its header; data stays visible, never blank. Top-bar pill goes Offline when the feed is down.
- **Feed fallback:** `openSSE()` → on failure `startPoll()` snapshots; top bar shows feed type (sse/poll/sim/—).
- **Settings (top-bar menu):** Theme (dark default / light via `data-theme`), Reduce motion (system/on/off via `data-motion`), Ripple (off/on via `data-ripple`) — persisted under `speculum.ui.*`, applied on boot and on change.
- **Keys:** `P` pause · `R` reseed (demo) / resync (live).

## Redesign status (in progress)

Full visual + structural redesign from the old glass/hobby dashboard (glass.css "mirror glass" material, radial gauges, all-monospace, ripple-on-every-card) to a flat, dense, professional observability UI. Phased, each phase verified before the next:

- [x] **Phase 1 — tokens & primitives.** Token file (dark + light themes), typography, and all primitives (Panel, PanelHeader, Stat, Meter, Sparkline, Badge, StatusPill, DataTable, EmptyState, Skeleton, chips, settings menu) + the isolated optional ripple module.
- [x] **Phase 2 — layout shell.** 48 px top bar (overall-state pill Live/Degraded/Offline, GPU, driver, uptime, feed type, settings menu), 12-col grid, engine registry in `viewmodel.js`. Old `glass.css`/`dashboard.css` deleted; `index.html` is the new shell.
- [~] **Phase 3 — panels** — **STOPPED IN PROGRESS (2026-10-03)**. `app.js` ships the shell (topbar/foot/settings) + feed + demo; all panel-specific CSS is in `components.css` (KPI groups, throughput chart/tooltip, GPU readouts, ledger mix bars, context rows, request rows, engine-card insets + grid, event pills, pool grid); the `THRESHOLDS` / `kpiStatus` / `KPI_GROUPS` / `KPI_SPECS` block sits at the top of `app.js` right after `KPI_KEYS`. What is NOT written yet: the nine panel builders/renderers/painters, `tokenLedger(state)` in `viewmodel.js`, and the nine `buildX()` calls in the run section (it still calls only `buildTopbar()`). Order when resuming — each verified against the live feed before the next: **KPI strip** (grouped stats + thresholds + 60-min sparklines + vs-15m deltas) → **Throughput** (axes, gridlines, hover crosshair+tooltip, range selector 15m/1h/6h/24h, registry legend) → **GPU** (horizontal meters + key/value readouts, n/a handling) → **Token ledger** (table + per-column stacked mix bars + footer stats) → **Context usage** (per-session bars, warn 75 % / crit 90 %) → **Request history** (table: time/model/stacked bar vs window/prompt-window; capped list; header stats) → **Engines** (one inset card per registry engine, muted state in words) → **Events** (severity pills + filters + pause autoscroll + escape decoding + row expand) → **KV pool** (slot grid vs window).

**Phase 3 resume notes (state as of the stop):**

- **Insertion points:** main panels section goes in `app.js` immediately before the `/* --- paint loop */` comment; the nine `buildX()` calls go in the run section after `buildTopbar()`. `makePanel(id, title, hint, {flush, tools})` = `Panel()` factory + `StaleChip()` appended to `p.head` + `p.el` appended to `$(id)` + `registerPanel({id, el, head, body, chip, paint, render, onData})`. Pulse contract: `panelPulse(p, sig)` pulses `p.el` only when the signature string changes (no-op while ripple is off).
- **Confirmed payload facts** (verified against `collector/speculum.py` and live snapshots — re-verify nothing, these are ground truth): engine counter keys `llamacpp:tokens_predicted_total` / `llamacpp:prompt_tokens_total` / `ninfer:prefix_cache_hit_tokens_total` (demo engines use `generated`/`prompt`/`cache` — the ledger view-model must accept both); request records have `t` (epoch **seconds**), `model`, `prompt`, `cache`, `fresh`, `output`, `total_s`, `ttft_s`/`queue_s` (seconds), `prefill_tps`/`decode_tps`, `mtp_acc`/`mtp_tot`, `window`; KPI units: `ttft`/`p95`/`tpot` are **milliseconds**, `vram` is GB; `clockStr(t)` takes seconds; `state.engines` is a list of engine objects (the snapshot's dict is converted in `applySnapshot`); `state.sessions` is rebuilt by `refreshDerived()` → `{id, engine, engineKey, origin, window, used, cached, processing}`; tick fields `kpi` / `kpi_hist` / `eng_hist` / host values are already wired in `applyTick`.
- **Panel designs** (decided, not yet coded — one line each): KPI = `.kpi-groups` grid of 4 groups, per-KPI `Stat` (unit span, spark in the group tone, status `dataset.status` from `kpiStatus`, delta vs 15 min via `deltaText`); Throughput = 232 px canvas, 1-s x-axis, 4 y-gridlines, one line per registry engine from `engHist[key]` (gaps on non-finite), crosshair + left-clamped tooltip rebuilt from last-paint geometry, legend = swatch+name+state badge; GPU = hint `name · driver`, 4 `Meter`s (temp/100, util/100, power/`powerLimit`, vram/`vramTotal` — tick fractions are static so meters are built once and updated via `m.set()`), then two `.readout-col` kv lists (Device: SM/mem clock, fan, power limit, PCIe · Host: CPU, RAM, load 1/5/15, top-2 procs) with `n/a` styling; Ledger = `tokenLedger(state)` in `viewmodel.js` (window sums over `requests[]` for 1h/24h + counter-derived since-start → rows generated/fresh/cached × [1h, 24h, since], footer reqs/MTP/cache/re-ingest, honest `.ledger-note` strings when the buffer is capped or empty), table + one 100 %-stacked `.mix` bar **per column** (generated/fresh/cached in `--tok-*`); Context = one `.ctx-row` per session (cap 20 + "…N more"): mono id, engine badge, `used / window` text, mini meter with 0.75/0.90 ticks, empty → `EmptyState('No active sessions')`; Requests = 7-col table (time/model/`.mix` cached-vs-fresh bar vs window/prompt-window/TTFT/decode), newest first, 14 rows + "Show more" (14 more, cap 50), head stats re-ingest/MTP/cache/buffered; Engines = `.engine-grid` of inset `.engine-card`s (swatch + name + state `Badge`, `origin · window ctx · backend` subline, tok-s/queue/MTP stats, `engHist` sparkline, `is-muted` + word for down engines); Events = table in `.ev-wrap` (time/`.lv-pill`/`.ev-msg` with `decodeEscapes`, expandable rows for full text), severity `Chips` + text filter + pause-autoscroll tool, cap 60; Pool = `.pool-grid` of `.pool-cell` per session (demo: 32 `poolFakes`), fill height `used/window`, fill color = engine series color via `engineKey`, `.pct` + `.sid`, empty → `EmptyState('No active slots')`.
- **Smoke extension** (`smoke/shell.mjs`, before panels land): add a `fetch` stub mapping relative `'api/snapshot'` → `http://127.0.0.1:8792/api/snapshot` (real network, live collector), an `EventSource` class stub, `setInterval` no-op, an rAF queue pump, `location {search:''}`, `localStorage` Map, `getComputedStyle` → `''`, `matchMedia {matches:false}`; extend the DOM stub with `replaceChildren`, `dataset = {}`, `getBoundingClientRect() → {left:0, top:0, width:120, height:232}`, `hidden`, `focus()`, `scrollTo`. After panels land, assert against live payloads: KPI values ≠ '—', engine-card count = payload engine count, pool cells = session count, event rows = min(events, 60).
- [ ] **Phase 4 — cleanup.** Delete dead styles/components and the last old-design code, run the "Strata"/"NInfer" literal grep over UI files (must be zero), verify AA contrast in both themes, check layout at 1440 / 1024 / 768 px, refresh the screenshots in `assets/` (needs a real browser), and sync `README.md` (done 2026-10-05: documents the new UI, screenshots in `assets/`).

## Verification (no browser test suite exists — smoke, don't guess)

```sh
python3 -m py_compile collector/speculum.py        # collector parses
node smoke/ui-primitives.mjs                        # primitives + ripple state machine (DOM stub)
node smoke/shell.mjs                                # boots the real app.js shell + view-model checks
python3 collector/speculum.py                       # → http://127.0.0.1:8792/
curl -s localhost:8792/api/snapshot | python3 -m json.tool | head   # JSON contract
curl -sN -m 3 localhost:8792/api/stream | head                              # SSE ticks
python3 -m http.server 8792  →  http://localhost:8792/?demo               # simulator, no backend
```

- The UI files are ES modules: `node --check` (CJS parse) does **not** apply to them; the smoke scripts import and execute them, which is the parse+run check.
- No headless Chromium on this box: live-view render is verified via the DOM-stub smoke tests over real payload shapes. If a browser becomes available, eyeball `http://127.0.0.1:8792/` (all panels paint, no console errors, canvases have ink), check both themes and 1440/1024/768 px, and refresh the screenshots in `assets/` if the design changed.
- Demo values are intentionally invented — that is their job; never "fix" them into real-looking data.

## Don't

- Add dependencies, a build step, a framework, or a database server. (The one sanctioned store is the stdlib
  SQLite file in `collector/history.py`.)
- Expose anything past 127.0.0.1 (or tailnet).
- Cache, buffer, or retain data without a cap.
- Put layout in `tokens.css`/`components.css`, or material in `layout.css`.
- Make live mode depend on simulator code or vice versa.
- Hardcode engine names in the UI, or reintroduce radial gauges into the CSS.
  (Operator override 2026-10-03: soft glow and subtle gradients are sanctioned —
  page wash, panel inner-top highlight, status-pill glow, sparkline area fill.
  `backdrop-filter`/blur remain out.)
