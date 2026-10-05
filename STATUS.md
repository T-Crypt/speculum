# STATUS

Where Speculum stands, what is deliberately not done, and what comes next.
Progress is tracked **here**, not in `AGENTS.md` — that file is durable guidance
(architecture, invariants, workflow) and must not accumulate stale notes.

Last updated: 2026-10-05, on branch `engine-coverage` (open PR against `main`).

## Shipped on `main`

| Area | State |
|---|---|
| Collector + feed | GPU/host/llama-swap/NInfer/Strata poll threads, `State` under one RLock, `/api/snapshot`, SSE `/api/stream` @ 1 Hz, poll fallback. |
| History | SQLite (`collector/history.py`): requests, minute/hour rollups, model spans, daily prune, `/api/history` `/api/requests` `/api/storage` `/api/export`. `retention_days = 0` is a no-op. |
| Engine adapters | Ollama, llama.cpp, vLLM, LM Studio, Unsloth Studio, generic OpenAI, config-driven `custom`. Fingerprint discovery with one shared backoff scheduler. |
| Windows host support | `collector/hostinfo.py`: `/proc` on Linux, Win32 `ctypes` on Windows, one interface (`load()` is `None` there). Merged as PR#1. |
| UI redesign, phases 1–3 | Done. See below. |
| Tests | 86 in the collector suite (3 skipped), 52 in `test_engines.py`, 2 Node smoke scripts. |

## In flight — `engine-coverage` (this branch)

Widens engine coverage from 7 adapter types to 12 and makes discovery able to
reach beyond localhost. Details and the per-engine citation table are in
[ENGINE-COVERAGE.md](ENGINE-COVERAGE.md).

**New adapters:** FreeToken (1919), SGLang (30000), KoboldCpp (5001), TabbyAPI (5000), LocalAI (8080).
**Hardened:** vLLM now derives liveness from `/version` + `/v1/models` instead of `/metrics`, and reports models and window.
**Discovery:** `[discovery] targets = [...]` adds opt-in remote hosts for a collector running away from the GPU box (a Proxmox LXC, say). Claims are keyed on `host:port`, localhost is probed first, remote probes use the longer timeout.
**Tests:** +29 in `test_engines.py`, including `FingerprintMatrix` — every real engine against every adapter fingerprint, with **separate** Ollama and llama.cpp stubs (a single hybrid stub serving both `/api/version` and `/props` makes each match the other and hides real regressions).

Two rules the adapters now hold to, both of which exist because the alternative
was a wrong number:

- **Optional endpoints are never liveness signals.** SGLang's `enable_metrics`
  defaults to `False`; a `/metrics`-based check reported a healthy server as down
  and latched a false red alert.
- **Look-alikes are excluded explicitly.** Strata and KoboldCpp both serve a
  llama.cpp-shaped `/props`; TabbyAPI is OpenAI-compatible and fingerprints only
  on `/tabby/config`, deliberately with no `/v1/models` fallback.

## Deliberately not supported

Recorded so they are not re-litigated. Full reasoning in
[ENGINE-COVERAGE.md](ENGINE-COVERAGE.md#deliberately-not-supported).

| Engine | Why |
|---|---|
| TGI | Maintenance mode by its own README; directs users to vLLM/SGLang/llama.cpp, all supported. |
| LMDeploy | Default port unverified — `lmdeploy/api_server.py` is a 404 on `main`. Guessing a port means probing something that may not exist. |
| MLC-LLM | Defaults to **8000**, which `vllm` claims; its metric names carry no prefix, so it cannot be reliably distinguished. Discovered as generic `openai`. |
| Jan | Local server is on **1337**, which no adapter claims. Configurable as `[[engine]] type = "openai"`. |
| TensorRT-LLM, PowerInfer | Watchable today via `[[engine]] type = "custom"`. |

Ports that were wrong in common use, recorded so they are not reintroduced:
**Jan is 1337** (not 1234 — that is LM Studio), **MLC-LLM is 8000** (not 8080),
**TabbyAPI is 5000** (not 5007).

## UI redesign — phases 1–3 complete

Phases 1 (tokens + primitives) and 2 (layout shell) are done and were merged
earlier. **Phase 3 (panels) is also done** — this was recorded as "stopped in
progress" in `AGENTS.md` for some time and that note had gone stale:

- All **12** panel builders exist and are called from the run section in `app.js`
  (`buildKpi` `buildThroughput` `buildGpu` `buildLedger` `buildContext`
  `buildTimeline` `buildStorage` `buildRequests` `buildLeaderboard` `buildEngines`
  `buildEvents` `buildPool`), plus `buildTopbar`.
- `tokenLedger(state)` is implemented in `viewmodel.js` and consumed by three panels.
- `glass.css` / `dashboard.css` are deleted; all panel CSS lives in `components.css`.
- `README.md` documents the new UI; 7 screenshots in `assets/`.

## Phase 4 — cleanup: partially done, one item genuinely open

Done: old stylesheets deleted, README synced, screenshots refreshed.

**Open: the "zero engine-name literals in UI files" bar is not met, and as
originally written it cannot be.** A grep over `app.js` `ui.js` `viewmodel.js`
`ripple.js` `index.html` returns 9 hits. Every one is a *data* reference, not a
display name:

- `app.js` — `state.strata`, `e.key === 'strata'`, `state.engHist.strata`. The
  collector emits Strata as a **dedicated top-level payload field** (`strata`,
  `strata_up`) alongside `engines[]`, because it is the one engine with its own
  metrics/log stream path. Reading it is correct; the alternative is a payload
  redesign.
- `viewmodel.js` — counter-key mappings like `ninfer:prefix_cache_hit_tokens_total`
  and the `llamacpp:*` names. These are metric keys the collector actually emits.

Reaching literal zero would mean folding `strata` into `engines[]` and moving
counter-key mapping into the payload. That is a real change to the snapshot
contract, not a cleanup task — it should be decided on its own merits rather than
driven by a grep target. **Recommendation: amend the invariant to "no engine
*display name* is hardcoded in UI code" (which already holds) and drop the
literal-zero bar.** Needs an operator decision.

Also unverified, unchanged: **no browser confirmation.** There is no headless
Chromium on this box, so panel render is verified only through the DOM-stub smoke
tests over real payload shapes. Both themes and 1440/1024/768 px have not been
eyeballed against the finished panels.

## Next steps

Ordered; each is independently shippable.

1. **Review and merge this PR** (`engine-coverage` → `main`). Code and tests are
   complete and green; no auto-merge was requested.
2. **Decide the Phase 4 literal-zero question** above. One line in `AGENTS.md`
   either way, so the bar stops sitting unmeetable.
3. **Browser verification pass** for the 12 panels: both themes, 1440/1024/768 px,
   no console errors, canvases have ink. Refresh `assets/` if anything moved.
   Blocked on a browser being available.
4. **LXC deployment of the collector** — the groundwork landed here
   (`[discovery] targets`). Still to do: an install note for running the collector
   in a Proxmox LXC pointed at the GPU box, and a check that a
   GPU-less container degrades cleanly. That degradation is already verified —
   with `nvidia-smi` absent the GPU thread sets `present = False`, warns once, and
   returns without raising — so this is packaging, not new code.
5. **Optional adapters**, only if a real deployment needs them: LMDeploy (once its
   port is verifiable from source), MLC-LLM (needs a distinguishing marker),
   Jan (1337, trivial).