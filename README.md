# Speculum — mirror-glass LLM runtime monitor

Single small project, no backend. HTML + CSS + JS, everything on screen is
rendered from `app.js`; `index.html` carries only the panel shells.

## Run

Module scripts do not load from `file://`, so serve it:

```
python3 -m http.server 8792
open http://localhost:8792/index.html
```

Keys: `P` pause, `R` reseed, click a model lane to focus its trace.

## Layout

- `glass.css` — the material only: theme, mirror glass, glow, motion, a11y.
- `dashboard.css` — layout only, layered after `glass`.
- `app.js` — the simulated runtime, the paint loop, the pointer light.

## Panels

- **Bar** — node, driver, uptime, live/paused state, status orb.
- **KPI strip** — tokens/s, requests/min, p95, first token (TTFT), per token
  (TPOT), VRAM, cache hit, queue depth. Each card carries its own sparkline
  and its own glow colour.
- **Signal** — rolling 60s, one trace per model, glow stroke, focus fill.
- **Hardware gauges** — arc gauges for GPU temp, util, draw, VRAM, plus clock,
  fan, bus, batch readout. Gauges turn rose past their warn threshold.
- **Context map** — the signature piece: one ring per live session, arc length
  is context occupancy, a rotating scan line and a write head at the end of
  each arc, total tokens in the centre.
- **Token ledger** — lifetime, 30 day, 90 day, with proportional bars, plus
  today, avg tokens/request, sessions, error rate, power limit, quant.
- **Model lanes** — quant, context window, share, p95, throughput meter.
- **Stream** — live runtime events, level coloured.
- **Pool** — 32 KV slots as fill cells.

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

The numbers are simulated. Wiring it to a real runtime means replacing `step()`
with a fetch to the inference server's metrics endpoint; the render and paint
functions read only from `state`.

## Origin

Built in a single agent turn on 2026-10-03 by a local model: Strata serving ISTA-DASLab's Qwen3.8-Flash-Next
GSQ-RCO IQ2_XS on one RTX 4090, driven by the Pi coding agent, from a one-line prompt asking for a glass,
enthusiast-style dashboard for a locally hosted LLM. About 5-10 minutes, 61.9K of a 131K context.
Everything on screen is simulated today; wiring it to live llama-swap / NInfer / Strata / nvidia-smi data is next.
