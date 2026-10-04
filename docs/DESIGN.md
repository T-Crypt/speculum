# Speculum design: any engine, history, two views

Status: plan, 2026-10-03. Phases land one at a time; each must run live before the next starts.
`AGENTS.md` invariants still hold: Python standard library only, no build step, loopback by default,
every buffer capped, never a GPU context.

## Goals

1. Watch any inference engine, local or on the network: Ollama, llama.cpp, llama-swap, vLLM, LM Studio,
   NInfer, Strata, and any OpenAI-compatible server. A user adds an engine in a config file, no code.
2. Keep history for 30, 60 or 90 days in one small SQLite file and tell the user what it costs on disk.
3. Two views from the top bar: Basic (what is running, how fast, how full) and Advanced (everything).
4. Stay cheap enough to leave running: under 40 MB RSS and under 0.5% of one core when nothing is happening.
5. Start with one command on Linux and Windows, with no config.

## 1. Engines

### Common schema

Every engine fills the same record. Only the first block is required; the UI shows a panel or a field
only when the engine reports it (`caps`).

| Field | Required | Meaning |
|---|---|---|
| `key`, `label`, `type`, `url` | yes | identity; `type` names the adapter |
| `up`, `state` | yes | `state` is one of `running`, `idle`, `stopped`, `error`, `latched` |
| `parent` | no | key of the router in front of it (NInfer behind llama-swap has `parent = "llama-swap"`) |
| `models[]` | no | `{id, loaded, size_bytes, vram_bytes, ctx, expires}` |
| `counters` | no | `prompt_tokens`, `output_tokens`, `cached_tokens`, `requests` (monotonic) |
| `rates` | no | `decode_tps`, `prefill_tps` |
| `queue`, `window` | no | waiting requests; context window of the loaded model |
| `sessions[]` | no | KV slots: `{id, used, cached, window, processing}` |
| `extras` | no | engine-specific numbers (MTP acceptance, draft tokens) |
| `caps[]` | yes | which of the optional blocks this engine can fill |

A router is an engine. llama-swap gets its own entry (models it can load, what is loaded, its request
feed) and its backends point at it with `parent`. Requests seen through a router and again in the
backend's own log merge into one row (already built: match on prompt and output tokens within 120 s).

### What each adapter reads (no proxy in the request path)

| Type | Default port | Fingerprint | Reads |
|---|---|---|---|
| `ollama` | 11434 | `GET /api/version` returns `{"version"}` | `/api/ps` (loaded models, VRAM, expiry), `/api/tags` (catalog) |
| `llamacpp` | 8080 | `GET /props` has `default_generation_settings` | `/health`, `/metrics` (`llamacpp:` counters), `/slots` |
| `llama-swap` | 9090 | `GET /running` returns `{"running"}` | `/running`, `/v1/models`, `/api/metrics/activity`, `/api/events`, `/logs/stream/<model>` |
| `vllm` | 8000 | `/metrics` contains `vllm:` | `/metrics` (`vllm:` counters and histograms), `/v1/models` |
| `lmstudio` | 1234 | `GET /api/v0/models` returns `{"data"}` with `state` | `/api/v0/models` (loaded state, context) |
| `ninfer` | from parent | `/metrics` contains `ninfer:` | `/metrics`, `/slots`, request lines in the parent's log |
| `strata` | 8080 | `/v1/models` plus Strata metrics | `/v1/models`, `/metrics`, `/slots` |
| `openai` | any | `GET /v1/models` returns `{"data"}` | `/v1/models` only: up or down and the model list |
| `custom` | any | set in config | any of `health`, `models`, `metrics`, `slots` paths plus a name map |

Fingerprint by response, never by port: 8080 is llama.cpp, Strata and a hundred unrelated apps.
Per-request tokens and latency need the engine to report them (llama-swap's activity feed, NInfer's log,
llama.cpp `timings`). Engines that only expose totals (Ollama, LM Studio) get rates from counter deltas
and no per-request rows. An optional pass-through tap is a later feature and stays off by default.

### Config: `speculum.toml`

Optional. With no file, Speculum probes localhost and shows what it finds.

```toml
[server]
host = "127.0.0.1"
port = 8792

[discovery]
enabled = true              # probe the default ports on localhost

[history]
retention_days = 30         # 30, 60, 90 or 0 to turn history off

[[engine]]
name = "ollama-desk"
type = "ollama"
url  = "http://10.0.0.41:11434"

[[engine]]
name = "my-engine"
type = "custom"
url  = "http://127.0.0.1:7000"
health  = "/health"
models  = "/v1/models"
metrics = "/metrics"        # Prometheus text
[engine.map]                # schema field = metric name in the engine's /metrics
output_tokens = "myengine_generated_tokens_total"
prompt_tokens = "myengine_prompt_tokens_total"
queue         = "myengine_requests_waiting"
```

Secrets stay out of the file: `api_key_env = "MY_ENGINE_KEY"` names an environment variable.

### Scheduling (this is what keeps it light)

- One scheduler thread polls every engine in turn. Each engine carries `next_due` and a backoff.
- An engine that answers is polled every 1 s while it is busy or a browser is connected, every 5 s when idle.
- An engine that does not answer backs off 5, 15, 60, 300 s. Connect timeout on localhost is 0.3 s.
- Discovery probes the default ports once at start and every 60 s after that, skipping ports already claimed.
- Streams (llama-swap events, log tail) keep one thread each and exist only while that engine is up.
- With no browser connected, nothing is serialised for SSE and the KPI pass runs every 5 s.
- History samples move from deques of dicts to `array('f')` columns, the largest RSS saving available.

Phase 1 is additive: the adapters and the scheduler arrive beside the existing llama-swap, NInfer and Strata
threads, which keep working. Phase 1b folds those three into adapters and removes their threads.

## 2. History

SQLite from the standard library, WAL mode, one writer (the scheduler), commit every 10 s.
File: `~/.local/share/speculum/speculum.db` on Linux, `%LOCALAPPDATA%\Speculum\speculum.db` on Windows.

| Table | Row | Kept |
|---|---|---|
| `requests` | t, engine, model, prompt, cached, fresh, output, ttft_ms, total_ms, queue_ms, decode_tps, prefill_tps, mtp_acc, mtp_tot, status, client | `retention_days` |
| `rollup_1m` | minute, engine, requests, prompt, cached, output, decode_tps avg and max, queue max, gpu_w avg, vram max | `retention_days` |
| `rollup_1h` | hour, engine, same sums | 400 days |
| `model_spans` | engine, model, loaded_at, unloaded_at | `retention_days` |
| `events` | t, level, engine, message | `retention_days`, capped at 50,000 rows |

Rollup rows are written only for minutes in which the engine was up. Pruning runs once a day, then
`PRAGMA incremental_vacuum`.

Size, shown in Settings and at `/api/storage`: the real file size, plus a projection for each retention
choice computed from the measured bytes per day. First estimate, to be replaced by measurement:
a request row is about 250 bytes and a rollup row about 100 bytes. Three engines up all day and 500
requests a day come to roughly 17 MB at 30 days, 35 MB at 60 and 52 MB at 90.

API: `/api/history?range=24h|7d|30d&engine=<key>` returns rollups; `/api/requests?since=&limit=` pages
request rows; `/api/storage` returns sizes; `/api/export?format=csv|json&range=`.

## 3. UI

- **Basic / Advanced** toggle in the top bar, stored as `speculum.ui.view`. Basic is the default.
- **Basic:** one card per engine (state, loaded model, tok/s, VRAM share), four KPIs (tok/s, requests per
  minute, VRAM, cache hit), throughput chart, GPU and host, alerts, load and unload timeline.
- **Advanced:** adds Context residency, Token ledger, Request history, KV pool, Events, leaderboard.
- **Gaps:** paired panels share a grid row and stretch to the same height (`align-items: stretch`, panel
  body flexes). No panel leaves dead space beside a taller neighbour at 1440, 1024 or 768 px.
- **Range selector** reads the database past the 60-minute memory window (6 h, 24 h, 7 d, 30 d).

New features, in order of value for cost:

1. Engine cards for every discovered engine, with a "not reporting" reason instead of a blank.
2. Alerts from thresholds in the config: VRAM over 95%, engine latched, queue above N, GPU temperature.
3. Load and unload timeline: one lane per engine, one bar per model span.
4. Idle VRAM: a process holding VRAM with no request for 30 minutes gets flagged.
5. Energy: tokens per watt-hour, from GPU power integrated over the time an engine was busy.
6. Leaderboard per model: requests, tokens, median tok/s, cache hit, over the selected range.
7. Export of the selected range as CSV or JSON.
8. Storage panel: database size, projection per retention choice, a prune button.

## 4. Windows

- Host numbers come from a small `hostinfo` module: `/proc` on Linux, `ctypes` on Windows
  (`GlobalMemoryStatusEx`, `GetSystemTimes`, `psapi` for process memory). No third-party packages.
- GPU numbers come from the same single `nvidia-smi` child on both. No NVIDIA GPU: the panel says so.
- Start at logon: a systemd user unit on Linux, a Task Scheduler entry on Windows.

## 5. Install

`python3 speculum.py` runs it with no config and prints the URL. `install.sh` adds the systemd user
unit; `install.ps1` adds the scheduled task. Both are short enough to read before running.

## 6. Network support (next)

A hub in a Proxmox LXC shows every machine on the network.

- Engines that listen on the LAN or tailnet need nothing: the hub polls them directly from `[[engine]]`.
- A machine's GPU, host numbers and loopback-only engines come from a **node**: the same collector
  started with `--node`, which serves one endpoint, `/api/node`, on a LAN or tailnet address behind a
  bearer token read from an environment variable.
- The hub pulls each node every 2 to 5 s. Pull, because nodes sleep and the hub should not accept
  inbound connections; a node that stops answering backs off like any engine.
- Not in the first version: discovery by mDNS, TLS termination (use the tailnet or a reverse proxy).

A later second hat, usage per API key for someone hosting inference for a few users, needs the tap proxy
to see keys. The `client` column in `requests` is reserved for it.

## Not building

Model management, user accounts, a rules engine for alerts, a charting library, a database server,
anything that loads a model.

## Phases

1. Engine adapters, config, discovery, scheduler (1b: fold the old threads in, array history).
2. SQLite history, storage estimates, history API.
3. UI: Basic and Advanced, gap-free grid, engine cards, alerts, timeline, range from the database.
4. Windows host backend, `install.sh`, `install.ps1`.
5. README rewrite, preview placeholder, network section; later features 4 to 8 above.

## Handoff (2026-10-04)

**Phase 3 done** (37ec7ce .. 5d37a6c): Basic/Advanced toggle (`?view=`), gap-free grid in both views, engine cards
for every engine with a down reason (llama.cpp via llama-swap always; NInfer only while ninfer-serve answers /metrics;
Strata from its 0.1.38 JSON /metrics; Ollama, LM Studio, Unsloth Studio; `optional = true` for apps started now and
then), throughput range from the database (6h/24h/7d/30d), threshold alerts (foreign VRAM process, GPU temperature,
queue, VRAM 99%) with an alert strip, model load timeline (/api/spans), storage panel. 48 collector tests; smoke has
two failures that predate Phase 3 ("KPI strip has 10 sparklines", "Engine cards = payload engine count").

**Not done from section 3:** idle-VRAM flag (feature 4), energy per token (5), per-model leaderboard (6), export of
the selected range from the UI (7; the API exists), a prune button (8; needs an endpoint).
**Next: Phase 4** (Windows host backend, install.sh / install.ps1), then Phase 5 (README, network section).

## Handoff (2026-10-03, for the next session)

**State 2026-10-03 afternoon:** Phase 1 done (`09f26a4`; 1b, folding the old threads in, still open). Phase 2 done:
`collector/history.py`, schema v3, 36 unit tests. Live-verified: rollup_1m token sums equal the request rows
exactly; restarts add no duplicate rows (unique key + INSERT OR IGNORE, because llama-swap replays its activity
feed on every start). Review fixes applied: rollups no longer read requests before the 130 s hold releases them;
hours aggregate at H+1h+5min and self-heal after suspend; export caps at 50,000 rows with a truncation flag.
Deferred: the dedupe key can merge two genuinely identical requests in the same second (adding the source `id`
needs proof the id survives the replay). **Next: Phase 3** (UI: Basic/Advanced, engine cards, storage panel,
range selector reading `/api/history`).

**Phase 1 state (historical):** `collector/engines.py` was first written unwired; the rest of Phase 1 followed.

**Operator asks, verbatim intent - all must land somewhere in Phases 1-5:**
- Users add their own engines easily (config, no code). Engines list reads like: llama-swap, NInfer, Strata,
  Ollama x5, llama.cpp, vLLM, LM Studio, Unsloth servers, any OpenAI-compatible. llama-swap is its own engine.
- Scale to many engines across the network ("anything on the network combed and placed here").
- Fill the blank black space between modules; the current look and feel stays.
- Lightweight above all: safe to idle in the background; review the whole repo for weight.
- Rich log data; history kept 30 / 60 / 90 days with estimated DB size shown.
- Basic and Advanced views, toggled in the top bar.
- Windows support. Dumb-simple install (an Ollama user who just wants stats on 127.0.0.1).
- Maybe a second hat later: a small-scale inference hoster's dashboard.
- README.md: run the stop-slop pass on it, leave a placeholder for the operator's preview image, and a
  "Network support - coming soon" section (Proxmox LXC hub + lightweight node connector, section 6 above).
- Creative freedom: add features that provide valuable information and fit the minimal bones.
- Delegation: NInfer roles (`coder`, `agent`) do the work; Claude reviews, verifies live, commits.
