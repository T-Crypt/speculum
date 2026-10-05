# AGENTS.md — Speculum

Calm, dense observability dashboard for a local LLM runtime: one RTX 4090 box running
llama-swap → NInfer (and optionally Strata) serving local models, watched by a
stdlib-only Python collector that polls everything and serves the panels plus
a 1 Hz feed.

**This file is durable guidance only.** Architecture, invariants, and how to work
here. Current status and the roadmap live in `STATUS.md` — do not duplicate
progress notes here; they go stale and mislead the next reader.

## Architecture (two parts, zero build)

```
nvidia-smi · host metrics (hostinfo.py) · llama-swap :9090 · NInfer backend (port from /running proxy) · Strata :8080
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
- Portable by construction: `hostinfo.py` reads `/proc` on Linux and Win32 via `ctypes` on Windows behind
  one interface. `engines.py` is pure `urllib.request` — no paths, no subprocess, no `/proc`. Nothing in the
  collector needs per-platform tagging.

## File map

|File|Owns|
|---|---|
|`collector/speculum.py`|All live data: GPU/host/llama-swap/NInfer/Strata poll threads, `State`, KPI assembly @ 1 Hz, snapshot/tick, HTTP server. Stdlib only.|
|`collector/hostinfo.py`|Host metrics behind one interface, two backends: `/proc` on Linux, Win32 `ctypes` on Windows (`load()` is None there — no load average). The only module that touches system/process stats.|
|`collector/history.py`|SQLite history: one writer thread, 10 s commits, requests held 130 s for the merge window, minute/hour rollups, daily prune, `/api/history`, `/api/requests`, `/api/storage`, `/api/export`. `retention_days = 0` is a no-op. Stdlib `sqlite3` only.|
|`collector/engines.py`, `collector/config.py`|Engine adapters + the single backoff scheduler; `speculum.toml` loader.|
|`collector/speculum.service`|systemd `--user` unit. `CUDA_VISIBLE_DEVICES=` (GPU stays the llama-swap's), 64 M ceiling, nice 10.|
|`index.html`|Page shell: 48 px top bar header, `#deck` with nine named section shells (`p-kpi` … `p-pool`), foot, one module script. Panels are built into the shells by `app.js`.|
|`tokens.css`|Design tokens: dark + light themes, typography scale, spacing, radius, status + 6-series palettes, 2 px accent focus ring, reduced-motion kill switch. Every text token is WCAG AA on every surface it can sit on — documented in the file header.|
|`components.css`|UI primitives: Panel, PanelHeader, Stat, Meter, Sparkline, Badge, StatusPill, DataTable, EmptyState, Skeleton, chips, segmented control, settings menu, stale chip, optional ripple styles (`data-ripple="on"` gated).|
|`layout.css`|Layout only: 48 px sticky top bar, 12-column deck grid (→ 6 at 1100 px, → 1 at 768 px), foot. No material, no tokens.|
|`app.js`|Feed (SSE + poll fallback + boot), `state`, demo simulator, paint loop, settings, all panels. One `<script type="module">`; `ui.js`/`viewmodel.js`/`ripple.js` are ES imports of it — still no build, no dependencies.|
|`ui.js`|DOM factories for every primitive + formatters (`fmtTok/fmtDur/clockStr/upStr/decodeEscapes/deltaText`) + `prefs` (localStorage `speculum.ui.*`, all try/catch) + `cssVar`/`seriesColor` + `paintSpark` + `Menu`. Presentational only: no data logic, no engine names.|
|`viewmodel.js`|Pure adapters (no DOM/fetch): `buildEngineRegistry`, engine/overall state words, staleness (10 s), token-ledger math. Live and demo states pass through the same functions.|
|`ripple.js`|The single, isolated ripple implementation. Off by default, persisted under `speculum.ui.ripple`, applied via `<html data-ripple="on">`, no-op while off, forced off under `prefers-reduced-motion: reduce`.|
|`smoke/`|Node DOM-stub smoke tests: `ui-primitives.mjs` (every primitive + ripple state machine) and `shell.mjs` (boots the real `app.js` shell + view-model checks).|
|`ENGINE-COVERAGE.md`|Supported inference engines, how each is recognised, what is deliberately unsupported, and the port/API citation behind each claim.|
|`STATUS.md`|Current status and roadmap. The only place progress is tracked.|
|`assets/`|Live screenshots captured on the monitor box, referenced from `README.md`.|
|`docs/`|**Gitignored local scratch.** Scraped upstream docs and throwaway scripts. Never committed; promote anything worth keeping to the repo root instead.|

## Hard invariants (do not break)

1. **Python 3 stdlib only.** No pip, no vendored deps in `collector/`. The systemd unit runs bare `python3`. Floor: 3.9 with no config file; a TOML config needs 3.11+ (`config.py` imports `tomllib` lazily, only when a file is read).
2. **One GPU reader.** Exactly one long-lived `nvidia-smi -lms 1000` subprocess, read line by line, restarted on death. Never open a CUDA/NVML context in-process; never spawn a second nvidia-smi per poll. `CUDA_VISIBLE_DEVICES=` must stay set — the GPU belongs to llama-swap.
3. **All sources optional.** GPU, llama-swap, NInfer backend, Strata, every engine may each be down. Every panel degrades to an honest "no data"; a missing source never throws, never fabricates numbers, never takes the feed offline.
4. **Bounded memory.** 60 min @ 1 s ring buffers (GPU/host/KPI), last 500 inference requests, last 200 events. Snapshot trims to 200 requests / 100 events. No unbounded growth — the 64 M ceiling is real.
5. **Thread discipline.** One shared `State` guarded by a single `threading.RLock()`. Pollers mutate under lock; `snapshot()`/`tick()` copy out under lock; SSE handlers never block on it. New threads: `daemon=True`, started in `main()`.
6. **UI: no build, no dependencies.** One `<script type="module">` in `index.html`; the JS files are plain ES modules. Live and demo modes must fill **exactly the same state shape** (`state` + `applySnapshot`/`applyTick` normalization) — the paint loop is mode-agnostic.
7. **`mulberry32` must use `Math.imul`.** A plain `*` overflows past 2^53, the low bits `>>>` needs are gone, and the seeded generator collapses to a constant.
8. **Design contract** (tokens/components/layout CSS + `ripple.js`): all colors come from `:root` tokens; components never hardcode hex or engine names. `backdrop-filter`, blur, and radial gauges are out. Sanctioned soft effects: the faint radial body wash, panel inner-top highlight, hover border tint, status-pill glow, sparkline area fill. Text tokens pass WCAG AA (4.5:1) on the surfaces they sit on. `prefers-reduced-motion: reduce` — or the user's Reduce-motion = On — kills all animation and disables the ripple regardless of its setting.
9. **No path escape.** Static handler resolves against repo root and 403s anything outside it.

## Key mechanics worth knowing before editing

- **NInfer backend discovery:** llama-swap `/running` reports the proxy port (e.g. `:5803`); `ninfer_backend_poll()` follows the active entry, polling its `/metrics` + `/slots`. Per-request records (TTFT, queue, prefill, decode, MTP) come from the NInfer upstream log stream relayed through llama-swap `GET /logs/stream/<model>`, parsed against `req#N done |` lines.
- **Engine registry (UI):** the UI builds its engine list, legends, cards, and series colors from the engine list the payload already provides (`viewmodel.buildEngineRegistry`) — sorted by engine key, color by stable index from the 6-color series palette. Adding/removing an engine in the backend requires zero UI code changes. Never hardcode engine display names in UI code; the lowercase `strata_up` tick field is a backend payload field name and is read as data.
- **Engine recognition:** an adapter claims a server by what it **answers**, never by its port. Fingerprints must match response content — a fingerprint that falls back to a generic `/v1/models` check will claim whatever else shares the port. Optional endpoints (e.g. SGLang's `/metrics`, which `enable_metrics` defaults to `False`) must never be treated as a liveness signal, or a healthy server latches a false red alert.
- **Engine latch:** a run of "service unavailable" from a backend latches that engine red in the engine cards / top-bar Degraded state (`add_alert`/`clear_alert` in `State`).
- **Context / pool:** one bar per live KV slot (NInfer `/slots`, Strata `/slots`); fill = prompt tokens vs model window from the payload.
- **KPIs** (`KPI_KEYS`): `tps, rpm, p95, ttft, tpot, vram, cache, reingest, mtp, queue`. Computed @ 1 Hz in `kpi_thread`; per-KPI history feeds the sparklines. Strip groups and thresholds live in `app.js`.
- **Staleness:** no feed tick for > 10 s → every panel shows an amber "Stale — last update Xs ago" chip in its header; data stays visible, never blank. Top-bar pill goes Offline when the feed is down.
- **Feed fallback:** `openSSE()` → on failure `startPoll()` snapshots; top bar shows feed type (sse/poll/sim/—).
- **Settings (top-bar menu):** Theme (dark default / light via `data-theme`), Reduce motion (system/on/off via `data-motion`), Ripple (off/on via `data-ripple`) — persisted under `speculum.ui.*`, applied on boot and on change.
- **Keys:** `P` pause · `R` reseed (demo) / resync (live).

## Working in Speculum

**Start from the payload, not from a guess.** Nearly every question about the UI is
answered by the collector's own source plus a live snapshot, not by intuition:
`curl -s localhost:8792/api/snapshot | python3 -m json.tool`. When a panel needs a
field, read the field the collector actually emits. Get this wrong and the panel
renders `—` forever with no error to explain it.

**Honesty beats completeness.** A missing source shows "no data"; it never shows a
zero, a guess, or a plausible-looking interpolation. Demo values are intentionally
invented — that is their job, never "fix" them into real-looking data. If a number
cannot be measured, leave it absent.

**Keep the footprint.** The collector runs beside an inference endpoint, on a box
whose GPU is already busy, under a 64 M ceiling. Prefer a poll thread over a new
dependency; prefer bounded ring buffers over lists that grow. Anything that adds
imports, a build step, or a long-lived connection needs a reason in the PR.

**Verify, don't assume.** There is no browser test suite on this box. Run the
verification block below, and add a test for anything that could silently regress —
a wrong port, a false-red liveness path, an unbounded buffer. A claim that matters
gets a test or a committed doc, never only a scratch file.

**Keep it in the right place.** Architecture/invariants/workflow → this file.
Progress → `STATUS.md`. Engine facts → `ENGINE-COVERAGE.md`. User-facing → `README.md`.
Scratch → `docs/` (gitignored, local only).

**Watch the one real conflict.** `demo` and `live` share a paint loop by design, so
any change to the state shape breaks the simulator or the live view — usually
silently. Change `state`, `applySnapshot` or `applyTick` and you have changed both.

## Verification (smoke, don't guess)

```sh
python3 -m py_compile collector/*.py                        # collector parses
python3 -m unittest discover -s collector -p "test_*.py"     # full collector suite
node smoke/ui-primitives.mjs                                # primitives + ripple state machine
node smoke/shell.mjs                                        # real app.js shell + view-model checks
python3 collector/speculum.py                               # → http://127.0.0.1:8792/
curl -s localhost:8792/api/snapshot | python3 -m json.tool | head   # JSON contract
curl -sN -m 3 localhost:8792/api/stream | head                       # SSE ticks
python3 -m http.server 8792  →  http://localhost:8792/?demo          # simulator, no backend
```

- The UI files are ES modules: `node --check` (CJS parse) does **not** apply to them; the smoke scripts import and execute them, which is the parse+run check.
- No headless Chromium on this box: live-view render is verified via the DOM-stub smoke tests over real payload shapes. If a browser becomes available, eyeball `http://127.0.0.1:8792/` (all panels paint, no console errors, canvases have ink), check both themes and 1440/1024/768 px, and refresh `assets/`.

## Don't

- Add dependencies, a build step, a framework, or a database server. (The one sanctioned store is the stdlib
  SQLite file in `collector/history.py`.)
- Expose anything past 127.0.0.1 (or tailnet).
- Cache, buffer, or retain data without a cap.
- Put layout in `tokens.css`/`components.css`, or material in `layout.css`.
- Make live mode depend on simulator code or vice versa.
- Hardcode engine names in the UI, or reintroduce radial gauges into the CSS.
  (Sanctioned soft glow/gradients: page wash, panel inner-top highlight, status-pill glow,
  sparkline area fill. `backdrop-filter`/blur remain out.)
- Track progress here, or commit anything from `docs/`.