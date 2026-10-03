# Speculum — mirror-glass LLM runtime monitor

Single small project. HTML + CSS + JS, everything on screen is rendered from
`app.js`; `index.html` carries only the panel shells. Since 2026-10-03 the
dashboard is **live**: a small stdlib-only collector
(`collector/speculum.py`) polls the local runtime and serves the panels plus
`/api/snapshot` (JSON) and `/api/stream` (SSE, 1 Hz).

## Run

**Live (default).** The collector serves the static files and the feed:

```
python3 collector/speculum.py            # http://127.0.0.1:8792/
```

or as a systemd `--user` service (recommended for 24/7):

```
cp collector/speculum.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now speculum
journalctl --user -u speculum -f
```

The unit binds `127.0.0.1:8792` only, runs with `CUDA_VISIBLE_DEVICES=`
(hidden from the GPU — it only reads `nvidia-smi` as a subprocess) and a
`MemoryMax=64M` ceiling. To see it off-box, publish tailnet-only with
`tailscale serve`; never to LAN or public.

**Demo (simulator).** `?demo` runs the seeded simulation with no backend:
serve the directory any way you like (e.g. `python3 -m http.server 8792`) and
open `http://localhost:8792/?demo`.

Keys: `P` pause, `R` reseed (demo) / resync (live), lanes glow when focused.

## Sources (all optional — panels degrade to an honest "no data")

| Source | What it feeds |
|---|---|
| `nvidia-smi` (one long-lived `-lms 1000` subprocess, read line by line) | header node/driver, hardware gauges, VRAM KPI |
| `/proc/stat` `/proc/meminfo` `/proc/loadavg` `/proc/uptime`, `VmRSS` of `llama-server` / `ninfer-serve` / `strata` | host readout (incl. Strata's system-RAM footprint) |
| llama-swap `:9090` — `/running`, `/v1/models`, `/api/metrics/activity`, `/api/events` | model lanes, per-request records (cached vs fresh prompt, outputs), event stream |
| NInfer backend (port from `/running` `proxy`, e.g. `:5803`) — `/metrics`, `/slots`, plus `GET /logs/stream/<model>` on llama-swap | decode/prefill tok-s, KV slots, per-request TTFT/queue/prefill/decode/MTP, engine-latch red alert on a run of "service unavailable" |
| Strata `:8080` — `/v1/models`, `/metrics`, `/slots` (when started by hand) | second model lane + context rings |

Bounded buffers: 60 min @ 1 s for GPU/host, last 500 inference requests,
last 200 events.

## Layout

- `glass.css` — the material only: AETHER // NODE house palette, mirror glass,
  glow, motion, a11y (no external font fetches).
- `dashboard.css` — layout only, layered after `glass`.
- `app.js` — the live feed (EventSource, snapshot poll fallback), the paint
  loop, the pointer light; the seeded simulator lives behind `?demo`.
- `collector/speculum.py` — stdlib-only collector (Python 3, one process).
- `collector/speculum.service` — systemd `--user` unit.

## Panels

- **Bar** — real node (GPU name), driver, active model, host uptime, feed
  state (sse/poll/demo/offline), status orb (rose on alerts or heat).
- **Alert strip** — red bar for active alerts (e.g. the NInfer engine latch:
  a run of "service unavailable" from the backend).
- **KPI strip** — decode tok/s, requests/min, p95 total, first token (TTFT),
  per token (TPOT), VRAM, cache hit, re-ingest tax, MTP acceptance, queue
  depth. Each card carries its own sparkline (1 Hz history from the
  collector) and its own glow colour.
- **Signal** — rolling throughput, one trace per engine (NInfer / llama.cpp /
  Strata / sim), autoscaled so the panel fills; focus fill on click.
- **Hardware gauges** — arc gauges for GPU temp, util, draw, VRAM (warn
  thresholds from the real `power.limit` / VRAM total), plus SM/mem clock,
  fan, PCIe, power limit, and a host readout: CPU %, RAM, load, and the RSS
  of the engine processes (Strata eats system RAM — it is shown).
- **Context map** — one ring per live KV slot (NInfer `/slots`, Strata
  `/slots`): arc length is prompt tokens vs the model window (262,144
  NInfer / 131,072 Strata), a write head at the arc end, total context used
  in the centre, session count in the panel hint.
- **Token ledger** — generated / fresh prefill / cached-reused token volume
  over 1 h, 24 h and since engine start (engine counters), with proportional
  bars, plus reqs buffered, avg tok/req, sessions, re-ingest tax, MTP,
  power limit.
- **Context graph** — the newest inference requests as stacked bars against
  their model window: cached (prefix hit) / fresh prefill (the re-ingest
  tax) / generated, with per-request TTFT detail on hover, and the 15-min
  re-ingest tax, MTP acceptance and cache-hit KPIs.
- **Model lanes** — one per engine: origin, window, backend, decode tok/s,
  queue, MTP; dimmed and marked when stopped (e.g. Strata when not running).
- **Stream** — live runtime events: request completions, engine lifecycle,
  WARN/ERROR lines, latch alerts.
- **Pool** — one cell per live KV slot, fill = prompt tokens vs window.

## Material contract

- Registered `@property` for `--edge`, `--glow`, `--sheen`, `--lit` (always
  `syntax:`, never `format:`).
- Three layers per panel: pointer specular (`::before`), rotating conic mirror
  rim (`::after`, masked to the border ring), sheen sweep (child element).
- Glow is per panel via `--glow-c` and scales with `--glow` through
  `color-mix(in oklab, ...)`. Relative colour syntax `oklch(from var(--x) ...)`
  does not resolve with a `var()` source in Chromium, so it is not used.
- Mirror floor: the deck reflects into the bottom of the frame via `body::after`.
- `prefers-reduced-motion` kills all animation and drops the blur.

## Verified

- `node --check app.js` — clean.
- Headless Chromium over http: no console or page errors, 83 running animations,
  `rim` and `sweep` active, mask-composite `exclude` applied, backdrop-filter
  resolving, every canvas has drawn ink, zero panel overlap, reduced-motion
  pass reports 0 animations.
- Seeded RNG (`mulberry32`) — same seed, same dashboard. It must use
  `Math.imul`; a plain multiply overflows past 2^53 and the low bits the `>>>`
  needs are gone, which collapses the generator to a constant.
- Screenshot: `render.png` at 1440x1020.

## Not verified

- **Browser render of the live feed.** 2026-10-03: the collector's JSON was
  verified end-to-end (snapshot, SSE tick, request/log parsing) and `app.js`
  was exercised over the real payloads with a Node DOM stub (all panels
  render, no exceptions), but headless Chromium/Firefox is not available on
  this box, so the live view has not been screenshot-verified. Open
  `http://127.0.0.1:8792/` in a browser to eyeball it.
- The simulator (`?demo`) still uses invented numbers — that is its job.

## Origin

Built in a single agent turn on 2026-10-03 by a local model: Strata serving ISTA-DASLab's Qwen3.8-Flash-Next
GSQ-RCO IQ2_XS on one RTX 4090, driven by the Pi coding agent, from a one-line prompt asking for a glass,
enthusiast-style dashboard for a locally hosted LLM. About 5-10 minutes, 61.9K of a 131K context.
Everything on screen is simulated today; wiring it to live llama-swap / NInfer / Strata / nvidia-smi data is next.
