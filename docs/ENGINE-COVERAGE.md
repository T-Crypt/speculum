# Engine coverage

Which local inference servers Speculum watches, how it recognises each one, and
what it deliberately does not support. Every port and endpoint claim below was
read out of that project's own source or docs on 2026-10-05; the citation is in
the last column so a future reader can re-check it rather than trust it.

## How recognition works

An adapter never claims a server because of its **port**. It claims it because
of what the server **answers**. `Adapter.fingerprint(url)` returns True only for
its own engine, and discovery runs every adapter in a per-port order, taking the
first match:

| Port | Adapters tried, in order |
|---|---|
| 11434 | `ollama` |
| 8080 | `llamacpp` → `localai` → `openai` |
| 8000 | `vllm` → `openai` |
| 1234 | `lmstudio` |
| 8888 | `unsloth` |
| 1919 | `freetoken` |
| 30000 | `sglang` |
| 5000 | `tabbyapi` |
| 5001 | `koboldcpp` |

The order is load-bearing. The generic `openai` adapter matches any
OpenAI-compatible server, so it is **always last** — it claims only ports where
nothing more specific recognised the server. `test_engines.py::FingerprintMatrix`
asserts this, and also asserts that no specific adapter ever claims the wrong
engine.

Every port above is only a default. An engine on a non-default port is found,
or configured, three ways:

- **`[[engine]] port` (or `host`).** A configured engine never needs a full
  URL: `type = "llamacpp"` with `port = 8081` watches `127.0.0.1:8081`.
- **`[discovery] ports = [...]`.** Adds ports to the probe sweep on localhost
  and every `targets` host. A port already in the table keeps its ordered
  adapters; a new one is tried with every specific adapter, generic `openai`
  last.
- **`[llama_swap]` / `[strata]`.** The built-in threads are not discovery;
  override their `url`, `host` or `port`. NInfer follows llama-swap's
  `/running` proxy, so it needs no port of its own. Configured ports are
  skipped by discovery, so they are never mistaken for a separate engine.

## Supported

| Engine | Port | Fingerprint | Counters / rates | Sessions | Source of the port claim |
|---|---|---|---|---|---|
| Ollama | 11434 | `/api/version` → `{"version":…}` | loaded models | — | `/api/version` |
| llama.cpp | 8080 | `/props` → `default_generation_settings` | `llamacpp:*` Prometheus | `/slots` | `/props` |
| vLLM | 8000 | `/version` **and** `/v1/models` | `vllm:*` Prometheus *(optional)* | — | `entrypoints/serve/instrumentator/basic.py:53` |
| SGLang | 30000 | `/get_model_info` + `/v1/models` | `sglang:*` Prometheus *(optional)* | — | `arg_groups/fields/serving.py:66` `port: A[int, …] = 30000` |
| FreeToken | 1919 | `/health` + JSON `/v1/stats` | JSON `/v1/stats` | from `/v1/stats` | `docs/cli.md:43` `` \| `--port` \| 1919 \| Bind port \| `` |
| LM Studio | 1234 | `/api/v0/models` → each has `state` | — | — | `/api/v0/models` |
| Unsloth Studio | 8888 | `/api/health` → `service` names Unsloth | — | — | `/api/health` |
| KoboldCpp | 5001 | `/api/v1/model` → `{"result": str}` | — | — | `koboldcpp.py:130` `defaultport = 5001` |
| TabbyAPI | 5000 | `/tabby/config` | — | — | `tabby_config.yml` `network.port: 5000` |
| LocalAI | 8080 | `/healthz` → **204, empty body** | `localai_*` Prometheus | — | `localai_cli.md:98` `` `--address` `` default `:8080` |
| Any OpenAI-compatible | any | `/v1/models` → `{"data": [...]}` | — | — | generic fallback |
| Anything else | any | configured paths + metric map | configurable | configurable | `[[engine]] type = "custom"` |

### Two rules the adapters follow

**Metrics are optional, never a liveness signal.** vLLM can run with metrics
disabled, and SGLang's `enable_metrics` **defaults to False**
(`arg_groups/fields/observability.py:70`). The old vLLM adapter derived liveness
from `/metrics`, so a perfectly healthy server with metrics off was reported as
down and latched a red alert. Liveness now comes from `/health`, `/version` or
`/v1/models`; counters and rates are simply **absent** when Prometheus is not
served. Absent is not zero — nothing is invented to fill a gap.

**Look-alikes are excluded explicitly.** Three servers impersonate another:

- **Strata** serves a llama.cpp-shaped `/props` (`build_info` "Strata …") — excluded.
- **KoboldCpp** also serves one, adding `total_slots` (`koboldcpp.py:7106`) — excluded.
  Its `/slots` answers **501**, so there are no KV sessions to report; the card
  carries models and window only.
- **TabbyAPI** is OpenAI-compatible, so it is matched *only* on `/tabby/config`.
  It deliberately has **no** `/v1/models` fallback: on 5000 that would let any
  OpenAI-compatible server be labelled TabbyAPI.

## Deliberately not supported

| Engine | Why not |
|---|---|
| **TGI** (`huggingface/text-generation-inference`) | Maintenance mode. Its README states it accepts "pull requests for minor bug fixes … and lightweight maintenance" and directs users to vLLM, SGLang and llama.cpp instead. All three are supported here. |
| **LMDeploy** (`InternLM/lmdeploy`) | Default port unverified. `lmdeploy/api_server.py` is a 404 on `main`; the 23333 figure could not be traced to a current source file. Guessing a port would mean probing something that may not exist. |
| **TensorRT-LLM**, **PowerInfer** (`Tiiny-AI/PowerInfer`) | Datacenter-oriented. Both can be watched today via `[[engine]] type = "custom"` with `metrics`/`models` paths and a `[engine.map]`. |
| **MLC-LLM** (`mlc_llm serve`) | Defaults to **8000**, which `vllm` claims first. Its metric names carry no prefix (`prefill_tokens`, `decode_tokens`), so it cannot be reliably distinguished from any other server. Discovered as generic `openai`, or configure it explicitly. |
| **Jan** (`janhq/jan`) | Its local server is on **1337**, which no adapter claims. Add `[[engine]] type = "openai"` with that URL if you run it. |

## Ports the matrix corrected

Three figures in common use were wrong and are recorded here so they are not
reintroduced:

- **Jan is 1337**, not 1234. (1234 is LM Studio's.)
- **MLC-LLM defaults to 8000**, not 8080. (8080 is llama.cpp / LocalAI's.)
- **TabbyAPI is 5000**, not 5007.

## Platform support

The adapter layer is **platform-agnostic by construction**: adapters speak only
HTTP via `urllib.request` and parse JSON. There are no filesystem paths, no
`/proc` reads, no subprocesses and no `nvidia-smi` in `collector/engines.py`, so
every fingerprint and poll above behaves identically on Windows and Linux. No
per-engine platform tagging exists or is needed.

The **host panel** is separate, and is also portable. `collector/hostinfo.py`
reads system metrics behind one interface with two backends: `/proc` on Linux,
Win32 via `ctypes` on Windows. GPU reading uses one long-lived `nvidia-smi`
subprocess, which works on both.

## Running the collector away from the GPU box

Discovery probes `127.0.0.1` by default. To watch engines on another host — a
Proxmox LXC pointed at the inference box, say — list the hosts:

```toml
[discovery]
enabled  = true
targets  = ["10.0.0.41", "gpu-box.lan"]   # probed on the same port list
```

Localhost is always probed and is tried **first**, so a local engine always wins
its own port over a remote one. Discovery claims are keyed on `host:port`, not
on the bare port, so a remote 8080 stays discoverable while a local 8080 is
already claimed. Remote probes use the longer `TIMEOUT_REMOTE` (2 s).

To watch one engine on an arbitrary port, skip discovery and configure it:

```toml
[[engine]]
name    = "mlc"
type    = "custom"
url     = "http://127.0.0.1:8000"
health  = "/health"
models  = "/v1/models"
metrics = "/metrics"
[engine.map]
prompt_tokens = "prefill_tokens"
output_tokens = "decode_tokens"
```

## Adding an engine

1. Subclass `Adapter`: set `type`, `default_port`, a `fingerprint()` that
   matches **response content** and nothing looser, and a `poll()` returning the
   common schema. Optional blocks (`models`, `counters`, `rates`, `queue`,
   `window`, `sessions`, `extras`) may simply be omitted.
2. Register it in `ADAPTERS` and `TYPE_LABELS`.
3. Add its port to `DISCOVERY`, with the generic `openai` last if it also appears.
4. Add a stub server and tests: a positive fingerprint, a closed-port negative,
   and a poll asserting the schema. `AdaptersDegradeOnDeadPort` will check the
   last one for you across every adapter.

Two traps worth repeating: an adapter must not treat a missing optional
endpoint as "down", and a fingerprint that falls back to a generic OpenAI
model-list check will claim whatever else shares the port.
