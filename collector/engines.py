#!/usr/bin/env python3
"""Engine adapters and scheduler for Speculum.

Stdlib only. An Adapter knows how to fingerprint its engine (by response,
never by port) and poll() the common schema:

    key, label, type, url, up, state, [parent], [models[]], [counters{}],
    [rates{}], [queue], [window], [sessions[]], [extras{}], caps[]

Missing optional blocks are simply absent; caps lists which are present.
The Scheduler is the ONE thread that polls every engine in turn, applying
the scheduler cadence: up and busy 1 s, up and idle 5 s, down backs off
5, 15, 60, 300 s; discovery probes the default localhost ports once at
start and every 60 s, by fingerprint.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
LOCAL_HOST = "127.0.0.1"
TIMEOUT_LOCAL = 0.3       # connect timeout on localhost
TIMEOUT_REMOTE = 2.0
DISCOVERY_EVERY = 60.0
DOWN_BACKOFF = (5, 15, 60, 300)
OPTIONAL_BLOCKS = ("models", "counters", "rates", "queue", "window",
                   "sessions", "extras")

# port -> adapters to try there, in order. Several ports are ambiguous and the
# order is what disambiguates them:
#   8080  llama.cpp, LocalAI, then any OpenAI-compatible server; Strata also
#         answers here but is excluded by name before this table is consulted
#   8000  vLLM, then any OpenAI-compatible server (MLC-LLM's mlc_llm serve
#         defaults here and has no health endpoint to fingerprint)
# Ports are probed on 127.0.0.1 by default; [discovery] targets in speculum.toml
# adds remote hosts on the same port list (see discovery_targets).
DISCOVERY = [
    (11434, ("ollama",)),
    (8080, ("llamacpp", "localai", "openai")),
    (8000, ("vllm", "openai")),
    (1234, ("lmstudio",)),
    (8888, ("unsloth",)),
    (1919, ("freetoken",)),
    (30000, ("sglang",)),
    (5000, ("tabbyapi",)),
    (5001, ("koboldcpp",)),
]


def discovery_targets(extra_hosts=()):
    """(host, port, adapter types) triples to probe: localhost on every port,
    then each extra host on the same port list. Localhost comes first so a
    local engine always wins its own port over a remote one."""
    hosts = []
    for h in extra_hosts or ():
        h = str(h).strip()
        if h and h not in hosts and h not in LOCAL_HOSTS:
            hosts.append(h)
    out = [(LOCAL_HOST, port, types) for port, types in DISCOVERY]
    out.extend((h, port, types) for h in hosts for port, types in DISCOVERY)
    return out


def parse_prom(text):
    """Tiny Prometheus text parser: sum the samples of each metric name,
    ignore labels. NaN / non-numeric values are skipped."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            v = float(parts[1])
        except ValueError:
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        name = parts[0].split("{", 1)[0]      # ignore labels
        out[name] = out.get(name, 0.0) + v
    return out


def _is_local(url):
    try:
        host = urllib.request.urlparse(url).hostname or ""
    except Exception:
        host = ""
    return host in LOCAL_HOSTS


def _fval(x):
    """Finite-or-None for JSON values (NaN/Infinity survive json.loads)."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _i(x):
    f = _fval(x)
    return None if f is None else int(f)


class Adapter:
    """Base adapter: identity fields, HTTP with per-URL timeout and an
    optional bearer token read from the named environment variable, and
    rate computation from counter deltas between polls."""

    type = "adapter"
    default_port = None

    def __init__(self, url=None, key=None, label=None, parent=None,
                 api_key_env=None):
        if url is None:
            url = ("http://127.0.0.1:%d" % self.default_port
                   if self.default_port else "")
        self.url = url
        self.key = key or self.type
        self.label = label or self.type
        self.parent = parent
        self.api_key_env = api_key_env
        self._timeout = TIMEOUT_LOCAL if _is_local(self.url) else TIMEOUT_REMOTE
        self._last = None          # (counters, t) of the last poll
        self._last_t = None
        self._busy = False         # set by _finish: queue or active sessions
        self._prev_up = None       # for up/down transition events

    # -- identity ------------------------------------------------------------

    @classmethod
    def fingerprint(cls, url):
        """Return True iff `url` is this kind of engine. Response-based;
        the default never matches."""
        return False

    # -- http ----------------------------------------------------------------

    def _headers(self):
        h = {"Accept": "*/*"}
        if self.api_key_env:
            tok = os.environ.get(self.api_key_env)
            if tok:
                h["Authorization"] = "Bearer " + tok
        return h

    def _req(self, path, timeout=None):
        req = urllib.request.Request(self.url + path, headers=self._headers())
        return urllib.request.urlopen(req,
                                      timeout=timeout or self._timeout)

    def get_json(self, path, timeout=None):
        with self._req(path, timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def get_text(self, path, timeout=None):
        with self._req(path, timeout) as r:
            return r.read(1 << 20).decode("utf-8", "replace")

    # -- schema / rates --------------------------------------------------------

    @staticmethod
    def _caps(rec):
        return [k for k in OPTIONAL_BLOCKS if k in rec]

    def _finish(self, rec, counters=None):
        """Fill the identity fields and caps, update the counter history
        used for rates, set _busy, and return the rec (the full schema)."""
        rec.setdefault("key", self.key)
        rec.setdefault("label", self.label)
        rec.setdefault("type", self.type)
        rec.setdefault("url", self.url)
        if self.parent:
            rec.setdefault("parent", self.parent)
        rec["caps"] = self._caps(rec)
        up = bool(rec.get("up"))
        if up and counters:
            t = time.time()
            self._last = (counters, t)
            self._last_t = t
        else:
            self._last = None
        busy = rec.get("queue") or 0
        ses = rec.get("sessions") or []
        if not busy:
            busy = any(s.get("processing") for s in ses if isinstance(s, dict))
        if up:
            busy = busy or rec.get("state") == "running"
        self._busy = bool(busy)
        return rec

    def rates(self, counters, t, mapping):
        """Rates from counter deltas since the previous poll.

        mapping: rate name -> (numerator counter name, denominator counter
        name or None); with None the denominator is the elapsed time."""
        out = {}
        prev, pt = (self._last or (None, None))
        if prev is not None and pt is not None and t - pt >= 0.5:
            dt = t - pt
            for name, (num, den) in mapping.items():
                n = counters.get(num)
                if n is None:
                    continue
                if den is not None:
                    d = counters.get(den)
                    if d is None:
                        continue
                    dd = d - prev.get(den, 0)
                    dn = n - prev.get(num, 0)
                    if dd > 0:
                        out[name] = round(dn / dd, 1)
                else:
                    dn = n - prev.get(num, 0)
                    if dn > 0 and dt > 0:
                        out[name] = round(dn / dt, 1)
        return out

    def poll(self):
        raise NotImplementedError


class OllamaAdapter(Adapter):
    type = "ollama"
    default_port = 11434

    @classmethod
    def fingerprint(cls, url):
        try:
            j = urllib.request.urlopen(
                urllib.request.Request(url + "/api/version"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = json.loads(j.read().decode("utf-8", "replace"))
            return isinstance(body, dict) and "version" in body
        except Exception:
            return False

    def poll(self):
        t = time.time()
        try:
            ps = self.get_json("/api/ps")
        except Exception:
            return self._finish({"up": False, "state": "error"})
        models = []
        loaded = False
        for m in ps.get("models") or []:
            models.append({
                "id": m.get("name"), "loaded": True,
                "size_bytes": m.get("size"),
                "vram_bytes": m.get("size_vram"),
                "ctx": None, "expires": m.get("expires_at"),
            })
            loaded = True
        rec = {"up": True,
               "state": "running" if loaded else "idle",
               "models": models,
               "counters": {"models_loaded": len(models) if models else 0}}
        return self._finish(rec, counters=rec["counters"])


def _is_strata(url):
    """True when the server at `url` says it is Strata (/health {"service": "strata"})."""
    try:
        r = urllib.request.urlopen(urllib.request.Request(url + "/health"), timeout=TIMEOUT_LOCAL)
        body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False
    return isinstance(body, dict) and body.get("service") == "strata"


class LlamaCppAdapter(Adapter):
    type = "llamacpp"
    default_port = 8080

    RATES = {"decode_tps": ("llamacpp:tokens_predicted_total",
                            "llamacpp:tokens_predicted_seconds_total"),
             "prefill_tps": ("llamacpp:prompt_tokens_total",
                             "llamacpp:prompt_seconds_total")}

    @classmethod
    def fingerprint(cls, url):
        try:
            p = urllib.request.urlopen(
                urllib.request.Request(url + "/props"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = json.loads(p.read().decode("utf-8", "replace"))
        except Exception:
            return False
        # Strata serves a llama.cpp-shaped /props too (build_info "Strata 0.1.38"); it has its own
        # poller, and as llama.cpp it reads as down (no llamacpp: counters) and raises a false alert.
        if isinstance(body, dict) and str(body.get("build_info", "")).startswith("Strata"):
            return False
        # KoboldCpp likewise serves a llama.cpp-shaped /props, but adds total_slots
        # (koboldcpp.py:7106) and answers /slots with 501. Left as llama.cpp it would
        # poll for counters that never come and read as down.
        if isinstance(body, dict) and "total_slots" in body:
            return False
        return isinstance(body, dict) and "default_generation_settings" in body

    def poll(self):
        t = time.time()
        try:
            counters = parse_prom(self.get_text("/metrics"))
        except Exception:
            return self._finish({"up": False, "state": "error"})
        if not any(k.startswith("llamacpp:") for k in counters):
            return self._finish({"up": False, "state": "error"})
        rec = {"up": True,
               "state": "running" if counters.get("llamacpp:requests_running", 0)
                        else "idle",
               "counters": {"prompt_tokens": counters.get("llamacpp:prompt_tokens_total"),
                            "output_tokens": counters.get("llamacpp:tokens_predicted_total")},
               "queue": int(counters.get("llamacpp:requests_waiting", 0)
                            + counters.get("llamacpp:requests_deferred", 0) or 0),
               "rates": self.rates(counters, t, self.RATES)}
        if not rec["rates"]:
            rec.pop("rates")
        try:
            slots = self.get_json("/slots")
        except Exception:
            slots = None
        if slots is not None:
            ses = []
            for s in slots:
                if not isinstance(s, dict):
                    continue
                ses.append({
                    "id": "s%d" % (s.get("id") or 0),
                    "window": s.get("n_ctx"),
                    "used": s.get("n_prompt_tokens"),
                    "cached": s.get("n_prompt_tokens_cache"),
                    "processing": bool(s.get("state") == "processing"),
                })
            rec["sessions"] = ses
        return self._finish(rec, counters=counters)


class VllmAdapter(Adapter):
    """vLLM's OpenAI server (default 127.0.0.1:8000).

    Liveness comes from /health + /v1/models, never from /metrics: vLLM can be
    started with metrics disabled (--disable-metrics / --disable-observability),
    and treating a missing /metrics as "engine down" latches a false red alert
    on a server that is serving requests perfectly well. Counters, queue and
    rates are therefore OPTIONAL and simply absent when /metrics is not served.
    """
    type = "vllm"
    default_port = 8000

    RATES = {"decode_tps": ("vllm:generation_tokens_total", None),
             "prefill_tps": ("vllm:prompt_tokens_total", None)}

    @classmethod
    def fingerprint(cls, url):
        # /version is the positive marker: vLLM serves {"version": ...} there
        # (vllm/entrypoints/serve/instrumentator/basic.py:53 `@router.get("/version")`).
        # It must NOT fall back to the bare OpenAI model list, because SGLang,
        # KoboldCpp and TabbyAPI all serve /v1/models and vLLM is tried first on
        # 8000 -- a fallback here would let vLLM claim any of them.
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/version"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        if not (isinstance(body, dict) and isinstance(body.get("version"), str)):
            return False
        # Require the OpenAI model list too, so a stray /version elsewhere
        # (many tools answer one) cannot match on its own.
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/v1/models"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            models = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        return isinstance(models, dict) and isinstance(models.get("data"), list)

    def poll(self):
        t = time.time()
        try:
            models = self.get_json("/v1/models").get("data") or []
        except Exception:
            return self._finish({"up": False, "state": "error"})
        out = []
        for m in models:
            if not isinstance(m, dict):
                continue
            out.append({"id": m.get("id"), "loaded": True,
                        "size_bytes": None, "vram_bytes": None,
                        "ctx": _i(m.get("max_model_len")
                                  or m.get("context_length")),
                        "expires": None})
        rec = {"up": True, "state": "running" if out else "idle"}
        if out:
            rec["models"] = out
            ctxs = [m["ctx"] for m in out if m.get("ctx")]
            if ctxs:
                rec["window"] = max(ctxs)
        # Optional: Prometheus, when this server serves it at all.
        counters = {}
        try:
            parsed = parse_prom(self.get_text("/metrics"))
        except Exception:
            parsed = {}
        if any(k.startswith("vllm:") or k.startswith("vllm_") for k in parsed):
            counters = {"prompt_tokens": parsed.get("vllm:prompt_tokens_total"),
                        "output_tokens": parsed.get("vllm:generation_tokens_total")}
            rec["queue"] = int(parsed.get("vllm:num_requests_waiting", 0) or 0)
            rec["state"] = ("running" if parsed.get("vllm:num_requests_running", 0)
                            else rec["state"])
            r = self.rates(parsed, t, self.RATES)
            if r:
                rec["rates"] = r
        if counters:
            rec["counters"] = counters
        return self._finish(rec, counters=parsed or None)


class FreeTokenAdapter(Adapter):
    """FreeToken (FlashML-org/FreeToken), an edge-native MoE serving engine;
    default 127.0.0.1:1919 (docs/cli.md:43 "| `--port` | 1919 | Bind port |").

    It serves NO Prometheus: the stats endpoint is JSON. GET /v1/stats reports
    throughput, latency, VRAM and pool occupancy (docs/cli.md:144), which is a
    near one-to-one fit for Speculum's counters/rates/sessions schema, so this
    adapter reads that instead of a /metrics text scrape.

    Field names are read defensively: /v1/stats has grown over releases, so each
    value is looked up under a couple of plausible keys and dropped when absent
    rather than reported as zero.
    """
    type = "freetoken"
    default_port = 1919

    @classmethod
    def fingerprint(cls, url):
        # /health needs no key and names the server; /v1/stats is the
        # distinctive one (no other engine here serves a JSON stats body).
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/health"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        try:
            stats = cls._stats(url)
        except Exception:
            return False
        return isinstance(stats, dict)

    @classmethod
    def _stats(cls, url):
        r = urllib.request.urlopen(
            urllib.request.Request(url + "/v1/stats"),
            timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
        body = json.loads(r.read().decode("utf-8", "replace"))
        # unwrap one level of envelope if present ({stats: {...}})
        if isinstance(body, dict) and isinstance(body.get("stats"), dict):
            return body["stats"]
        return body

    @staticmethod
    def _pick(d, *keys):
        for k in keys:
            if isinstance(d, dict) and d.get(k) is not None:
                return d[k]
        return None

    def poll(self):
        try:
            stats = self._stats(self.url)
        except Exception:
            return self._finish({"up": False, "state": "error"})
        if not isinstance(stats, dict):
            return self._finish({"up": False, "state": "error"})

        counters, rates, rec = {}, {}, {"up": True}

        tok = self._pick(stats, "tokens_generated", "generated_tokens",
                         "output_tokens", "tokens_predicted")
        if _fval(tok) is not None:
            counters["output_tokens"] = _fval(tok)
        pr = self._pick(stats, "tokens_prompt", "prompt_tokens",
                        "input_tokens")
        if _fval(pr) is not None:
            counters["prompt_tokens"] = _fval(pr)
        cached = self._pick(stats, "tokens_cached", "cached_tokens",
                            "prefix_cache_hit_tokens")
        if _fval(cached) is not None:
            counters["cached_tokens"] = _fval(cached)
        if counters:
            rec["counters"] = counters
            t = time.time()
            mapping = {}
            if "output_tokens" in counters:
                mapping["decode_tps"] = ("output_tokens", None)
            if "prompt_tokens" in counters:
                mapping["prefill_tps"] = ("prompt_tokens", None)
            r = self.rates(counters, t, mapping)
            if r:
                rec["rates"] = r

        q = self._pick(stats, "queue", "requests_waiting", "pending_requests")
        if _fval(q) is not None:
            rec["queue"] = int(_fval(q))

        vram = self._pick(stats, "vram_used_gb", "vram_gb", "vram_used")
        if _fval(vram) is not None:
            rec.setdefault("extras", {})["vram_gb"] = round(_fval(vram), 2)

        lat = self._pick(stats, "tokens_per_second", "decode_tokens_per_second",
                         "throughput", "tps")
        if _fval(lat) is not None:
            rec.setdefault("extras", {})["server_tps"] = round(_fval(lat), 2)

        busy = bool(q) or bool(self._pick(stats, "generating", "busy", "active"))
        rec["state"] = "running" if busy else "idle"
        return self._finish(rec, counters=counters or None)


class SglangAdapter(Adapter):
    """SGLang's OpenAI server (default 127.0.0.1:30000 --
    python/sglang/srt/arg_groups/fields/serving.py:66 `port: A[int, ...] = 30000`).

    Same lesson as vLLM: `enable_metrics` DEFAULTS TO FALSE
    (fields/observability.py:70), so /metrics is absent on a stock server and
    must never be treated as a liveness signal. /health, /health_generate,
    /get_model_info and /v1/models are all served unconditionally
    (srt/entrypoints/http_server.py).
    """
    type = "sglang"
    default_port = 30000

    RATES = {"decode_tps": ("sglang:generation_tokens_total", None),
             "prefill_tps": ("sglang:prompt_tokens_total", None)}

    @classmethod
    def fingerprint(cls, url):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/get_model_info"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/v1/models"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        return isinstance(body, dict) and isinstance(body.get("data"), list)

    def poll(self):
        t = time.time()
        try:
            models = self.get_json("/v1/models").get("data") or []
        except Exception:
            return self._finish({"up": False, "state": "error"})
        rec = {"up": True, "state": "idle"}
        if models:
            rec["models"] = [{"id": m.get("id"), "loaded": True,
                              "size_bytes": None, "vram_bytes": None,
                              "ctx": _i(m.get("max_model_len")
                                        or m.get("context_length")),
                              "expires": None}
                             for m in models if isinstance(m, dict)]
            rec["state"] = "running"
        # Optional Prometheus: only present with --enable-metrics.
        try:
            parsed = parse_prom(self.get_text("/metrics"))
        except Exception:
            parsed = {}
        if any(k.startswith("sglang:") for k in parsed):
            rec["queue"] = int(parsed.get("sglang:num_requests_waiting", 0) or 0)
            rec["counters"] = {
                "prompt_tokens": parsed.get("sglang:prompt_tokens_total"),
                "output_tokens": parsed.get("sglang:generation_tokens_total")}
            r = self.rates(parsed, t, self.RATES)
            if r:
                rec["rates"] = r
            if parsed.get("sglang:num_requests_running", 0):
                rec["state"] = "running"
        return self._finish(rec, counters=parsed or None)


class KoboldCppAdapter(Adapter):
    """KoboldCpp (LostRuins/koboldcpp, default 127.0.0.1:5001 --
    koboldcpp.py:130 `defaultport = 5001`).

    It IS OpenAI-compatible (/v1/models, /v1/chat/completions) but has no
    Prometheus and no /metrics. Its /slots answers 501 "This server does not
    support slots endpoint", so there are no KV session rows to report -- the
    card carries models and window only. /api/v1/model returns {"result": name},
    a shape no OpenAI-compatible server here uses, which makes a clean fingerprint.

    Note it also serves a llama.cpp-shaped /props (with default_generation_settings),
    so LlamaCppAdapter.fingerprint excludes it explicitly -- the same treatment
    Strata already gets.
    """
    type = "koboldcpp"
    default_port = 5001

    @classmethod
    def fingerprint(cls, url):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/api/v1/model"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        return isinstance(body, dict) and isinstance(body.get("result"), str)

    def poll(self):
        rec = {"up": True, "state": "idle"}
        try:
            body = self.get_json("/api/v1/model")
            name = body.get("result")
            if isinstance(name, str) and name:
                rec["models"] = [{"id": name, "loaded": True,
                                  "size_bytes": None, "vram_bytes": None,
                                  "ctx": None, "expires": None}]
                rec["state"] = "running"
        except Exception:
            return self._finish({"up": False, "state": "error"})
        # max_length is the live context budget; max_context_length is the cap.
        try:
            ml = _i((self.get_json("/api/v1/config/max_length") or {}).get("value"))
            if ml:
                rec["window"] = ml
                if rec.get("models"):
                    rec["models"][0]["ctx"] = ml
        except Exception:
            pass
        return self._finish(rec)


class TabbyApiAdapter(Adapter):
    """TabbyAPI (theroyallab/tabbyAPI), ExLlamaV3's API server; default
    127.0.0.1:5000 (tabby_config.yml `network.port: 5000`). OpenAI-compatible
    (/v1/models, /v1/chat/completions, /v1/completions) with no Prometheus."""
    type = "tabbyapi"
    default_port = 5000

    @classmethod
    def fingerprint(cls, url):
        # /tabby/config is the ExLlamaV3 admin namespace: only TabbyAPI serves
        # it, and nothing else here does. Deliberately NOT falling back to the
        # bare OpenAI model list -- on port 5000 that would happily match any
        # OpenAI-compatible server and mislabel it as TabbyAPI.
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/tabby/config"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            return r.status == 200
        except Exception:
            return False

    def poll(self):
        try:
            cfg = self.get_json("/tabby/config") or {}
        except Exception:
            return self._finish({"up": False, "state": "error"})
        rec = {"up": True, "state": "idle"}
        try:
            data = self.get_json("/v1/models").get("data") or []
        except Exception:
            data = []
        if data:
            rec["models"] = [{"id": m.get("id"), "loaded": True,
                              "size_bytes": None, "vram_bytes": None,
                              "ctx": _i(cfg.get("context_length")),
                              "expires": None}
                             for m in data if isinstance(m, dict)]
            rec["state"] = "running"
        ctx = _i(cfg.get("context_length"))
        if ctx:
            rec["window"] = ctx
        return self._finish(rec)


class LocalAIAdapter(Adapter):
    """LocalAI (mudler/LocalAI), default 127.0.0.1:8080 (--address default
    `:8080`). It IS OpenAI-compatible and serves Prometheus /metrics
    (core/http/metrics.go -> promhttp.Handler()), but until this adapter existed
    it matched the generic `openai` fingerprint on 8080 and was labelled
    "OpenAI" with no counters.

    /healthz answers 204 No Content (core/http/routes/health.go) -- an empty
    body on that exact path is distinctive, and no other engine here serves it.
    """
    type = "localai"
    default_port = 8080

    RATES = {"decode_tps": ("localai_completion_tokens_total", None)}

    @classmethod
    def fingerprint(cls, url):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/healthz"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = r.read(64)
        except Exception:
            return False
        return r.status == 204 and not body.strip()

    def poll(self):
        t = time.time()
        try:
            data = self.get_json("/v1/models").get("data") or []
        except Exception:
            return self._finish({"up": False, "state": "error"})
        rec = {"up": True, "state": "running" if data else "idle"}
        if data:
            rec["models"] = [{"id": m.get("id"), "loaded": True,
                              "size_bytes": None, "vram_bytes": None,
                              "ctx": _i(m.get("context_length")),
                              "expires": None}
                             for m in data if isinstance(m, dict)]
        try:
            parsed = parse_prom(self.get_text("/metrics"))
        except Exception:
            parsed = {}
        keys = ("localai_completion_tokens_total", "localai_prompt_tokens_total",
                "llamacpp:prompt_tokens_total", "llamacpp:tokens_predicted_total")
        if any(k in parsed for k in keys):
            rec["counters"] = {
                "prompt_tokens": parsed.get("localai_prompt_tokens_total"),
                "output_tokens": parsed.get("localai_completion_tokens_total")}
            r = self.rates(parsed, t, self.RATES)
            if r:
                rec["rates"] = r
        return self._finish(rec, counters=parsed or None)


class LmStudioAdapter(Adapter):
    type = "lmstudio"
    default_port = 1234

    @classmethod
    def fingerprint(cls, url):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/api/v0/models"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            return False
        return all(isinstance(m, dict) and "state" in m for m in data)

    def poll(self):
        try:
            data = self.get_json("/api/v0/models").get("data") or []
        except Exception:
            return self._finish({"up": False, "state": "error"})
        models, loaded, window = [], False, None
        for m in data:
            st = m.get("state")
            is_loaded = st == "loaded"
            models.append({
                "id": m.get("id"), "loaded": is_loaded,
                "size_bytes": _i(m.get("size_bytes")),
                "ctx": _i(m.get("context_length")),
                "expires": None,
            })
            if is_loaded:
                loaded = True
                w = _i(m.get("context_length"))
                if w and (not window or w > window):
                    window = w
        rec = {"up": True,
               "state": "running" if loaded else "idle",
               "models": models}
        if window:
            rec["window"] = window
        return self._finish(rec)


class UnslothAdapter(Adapter):
    """Unsloth Studio (`unsloth studio`, default 127.0.0.1:8888). /api/health needs no key and names the
    service ("Unsloth UI Backend"), which also tells it apart from a Jupyter server on 8888. The
    OpenAI-compatible /v1/models needs a Studio API key: set api_key_env (UNSLOTH_STUDIO_AUTH_TOKEN by
    default) to list the loaded models; without one the card is up and says a key is needed."""
    type = "unsloth"
    default_port = 8888

    def __init__(self, url, key=None, label=None, parent=None, api_key_env=None):
        super().__init__(url, key, label, parent, api_key_env or "UNSLOTH_STUDIO_AUTH_TOKEN")

    @classmethod
    def fingerprint(cls, url):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/api/health"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        return isinstance(body, dict) and "unsloth" in str(body.get("service", "")).lower()

    def poll(self):
        try:
            health = self.get_json("/api/health")
        except Exception:
            return self._finish({"up": False, "state": "error"})
        if health.get("status") != "healthy":
            return self._finish({"up": False, "state": "error"})
        rec = {"up": True, "state": "idle"}
        try:
            data = self.get_json("/v1/models").get("data") or []
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rec["reason"] = "API key needed for models (%s)" % self.api_key_env
            return self._finish(rec)
        except Exception:
            return self._finish(rec)
        rec["models"] = [{"id": m.get("id"), "loaded": True, "size_bytes": None, "ctx": None,
                          "expires": None} for m in data if isinstance(m, dict)]
        rec["state"] = "running" if rec["models"] else "idle"
        return self._finish(rec)


class OpenAIAdapter(Adapter):
    type = "openai"
    default_port = None

    @classmethod
    def fingerprint(cls, url):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/v1/models"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            body = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False
        return isinstance(body, dict) and isinstance(body.get("data"), list)

    def poll(self):
        try:
            data = self.get_json("/v1/models").get("data") or []
        except Exception:
            return self._finish({"up": False, "state": "error"})
        rec = {
            "up": True, "state": "running",
            "models": [{"id": m.get("id"), "loaded": True,
                        "size_bytes": None, "vram_bytes": None,
                        "ctx": _i(m.get("context_length")), "expires": None}
                       for m in data if isinstance(m, dict)],
            "counters": {"models": len(data)},
        }
        return self._finish(rec, counters=rec["counters"])


class CustomAdapter(Adapter):
    """Config-driven engine: optional paths for health/models/metrics/slots
    plus a map of schema field -> metric name in the engine's metrics text."""

    type = "custom"
    default_port = None

    COUNTER_FIELDS = ("prompt_tokens", "output_tokens", "cached_tokens",
                      "requests")
    QUEUE_FIELDS = ("queue",)

    def __init__(self, url=None, key=None, label=None, parent=None,
                 api_key_env=None, health=None, models=None, metrics=None,
                 slots=None, cmap=None):
        super().__init__(url, key, label, parent, api_key_env)
        self.health = health
        self.models = models
        self.metrics = metrics
        self.slots = slots
        self.map = cmap or {}

    @classmethod
    def fingerprint(cls, url):
        return False          # custom engines are never discovered

    def poll(self):
        t = time.time()
        try:
            if self.health is not None:
                self.get_text(self.health)
            elif self.metrics is None and self.models is None:
                self.get_text("/")
        except Exception:
            return self._finish({"up": False, "state": "error"})
        rec = {"up": True, "state": "running"}
        counters = {}
        if self.metrics is not None:
            try:
                m = parse_prom(self.get_text(self.metrics))
            except Exception:
                m = {}
            for field in self.COUNTER_FIELDS:
                name = self.map.get(field)
                if name and name in m:
                    counters[field] = m[name]
            for field in self.QUEUE_FIELDS:
                name = self.map.get(field)
                if name and name in m:
                    rec[field] = int(m[name])
            if counters:
                rec["counters"] = counters
                mapping = {}
                if "output_tokens" in counters:
                    mapping["decode_tps"] = ("output_tokens", None)
                if "requests" in counters:
                    mapping["req_rate"] = ("requests", None)
                r = self.rates(counters, t, mapping)
                if r:
                    rec["rates"] = r
        if self.models is not None:
            try:
                data = self.get_json(self.models).get("data") or []
                rec["models"] = [{"id": m.get("id"), "loaded": True,
                                  "size_bytes": None, "vram_bytes": None,
                                  "ctx": _i(m.get("context_length")),
                                  "expires": None}
                                 for m in data if isinstance(m, dict)]
            except Exception:
                pass
        if self.slots is not None:
            try:
                ss = self.get_json(self.slots)
                if not isinstance(ss, list):
                    ss = ss.get("slots") or []
                ses = []
                for s in ss:
                    if not isinstance(s, dict):
                        continue
                    ses.append({
                        "id": "s%d" % (s.get("id") or 0),
                        "window": s.get("n_ctx"),
                        "used": s.get("n_prompt_tokens"),
                        "cached": s.get("n_prompt_tokens_cache"),
                        "processing": bool(s.get("state") == "processing"),
                    })
                rec["sessions"] = ses
            except Exception:
                pass
        if not rec.get("state"):
            rec["state"] = "idle"
        return self._finish(rec, counters=counters or None)


ADAPTERS = {
    "ollama": OllamaAdapter,
    "llamacpp": LlamaCppAdapter,
    "vllm": VllmAdapter,
    "sglang": SglangAdapter,
    "freetoken": FreeTokenAdapter,
    "koboldcpp": KoboldCppAdapter,
    "tabbyapi": TabbyApiAdapter,
    "localai": LocalAIAdapter,
    "lmstudio": LmStudioAdapter,
    "unsloth": UnslothAdapter,
    "openai": OpenAIAdapter,
    "custom": CustomAdapter,
}

# card labels for engines found by discovery (configured engines use
# their [[engine]] name)
TYPE_LABELS = {"ollama": "Ollama", "llamacpp": "llama.cpp", "vllm": "vLLM",
               "sglang": "SGLang", "freetoken": "FreeToken",
               "koboldcpp": "KoboldCpp", "tabbyapi": "TabbyAPI",
               "localai": "LocalAI", "lmstudio": "LM Studio",
               "unsloth": "Unsloth", "openai": "OpenAI"}


def make_adapter(eng, port=None):
    """Build an adapter from a config [[engine]] table (a plain dict of
    type/url/...); `port` overrides the table's url port (discovery)."""
    etype = eng.get("type") or "custom"
    cls = ADAPTERS.get(etype, CustomAdapter)
    url = eng.get("url") or ""
    if port is not None and url:
        p = urllib.request.urlparse(url)
        url = p._replace(netloc=(p.hostname or "127.0.0.1") + ":" + str(port)
                         ).geturl()
    a = cls(url=url or None, key=eng.get("name"), label=eng.get("name"),
            parent=eng.get("parent"), api_key_env=eng.get("api_key_env"))
    a.optional = bool(eng.get("optional"))   # down is normal: no alert (speculum.toml)
    if isinstance(a, CustomAdapter):
        a.health = eng.get("health")
        a.models = eng.get("models")
        a.metrics = eng.get("metrics")
        a.slots = eng.get("slots")
        a.map = eng.get("map") or {}
    if not eng.get("name"):
        a.label = TYPE_LABELS.get(etype, etype)
    return a


class Scheduler:
    """One thread polls every engine in turn. Each engine carries next_due
    and a backoff: up and busy 1 s, up and idle 5 s, down backs off
    5, 15, 60, 300 s. Discovery runs once at start and every 60 s on the
    default localhost ports, by fingerprint, skipping ports already claimed
    by a configured engine or by the legacy threads.

    on_result(adapter, rec) receives each poll result; claimed_ports() is
    a callable returning the ports no discovery probe may touch."""

    TICK = 0.25

    def __init__(self, adapters, discovery_enabled=True, claimed_ports=None,
                 on_result=None, on_event=None, on_discover=None,
                 discovery_hosts=None):
        self.discovery_enabled = discovery_enabled
        self.claimed_ports = claimed_ports
        self.discovery_hosts = list(discovery_hosts or ())
        self.on_result = on_result
        self.on_event = on_event
        self.on_discover = on_discover
        self.clock = time.time      # injectable in tests
        self._next_disc = None
        self.engines = {}
        for a in adapters:
            self.add(a)
        self._stop = False

    def add(self, a):
        a._prev_up = None
        self.engines[a.key] = {
            "adapter": a, "next_due": 0.0, "backoff": 0, "up": None,
        }
        return a

    def stop(self):
        self._stop = True

    # -- discovery -----------------------------------------------------------

    def _claimed(self):
        """(host, port) pairs discovery must not touch: every configured
        engine's own endpoint, plus the legacy localhost ports handed over by
        claimed_ports(). Keying on host AND port is what lets a remote 8080 be
        discovered while a local 8080 is already claimed."""
        pairs = set()
        for a in list(self.engines.values()):
            u = a["adapter"].url
            try:
                p = urllib.request.urlparse(u)
                pairs.add((p.hostname or LOCAL_HOST, p.port or 0))
            except Exception:
                pass
        if self.claimed_ports is not None:
            try:
                # legacy callback: bare localhost ports
                pairs.update((LOCAL_HOST, p) for p in self.claimed_ports())
            except Exception:
                pass
        return pairs

    def discovery(self):
        claimed = self._claimed()
        found = []
        for host, port, types in discovery_targets(self.discovery_hosts):
            if (host, port) in claimed:
                continue
            base = "http://%s:%d" % (host, port)
            # Strata has its own poller and passes the llama.cpp and OpenAI fingerprints; at startup it
            # may not be claimed yet (its poller has not answered), so ask the server who it is.
            if host == LOCAL_HOST and _is_strata(base):
                continue
            for tname in types:
                try:
                    ok = ADAPTERS[tname].fingerprint(base)
                except Exception:
                    ok = False
                if ok:
                    a = make_adapter({"type": tname, "url": base}, port=port)
                    self.add(a)
                    found.append((base, a))
                    break
        for base, a in found:
            if self.on_discover is not None:
                self.on_discover(a)
            self._poll(a, first=True)

    # -- polling ---------------------------------------------------------------

    def _poll(self, a, first=False):
        try:
            rec = a.poll()
        except Exception:
            rec = {"up": False, "state": "error"}
            a._busy = False
            self._finish_rec(a, rec)
            if self.on_event and not first:
                self.on_event("err", "%s unreachable" % a.label)
        else:
            self._finish_rec(a, rec)
            if self.on_result is not None:
                try:
                    self.on_result(a, rec)
                except Exception:
                    pass
        if not first:
            prev, now_up = a._prev_up, bool(rec.get("up"))
            a._prev_up = now_up
            if self.on_event and prev is not None and prev != now_up:
                self.on_event("ok" if now_up else "warn",
                              "%s %s" % (a.label, "up" if now_up else "down"))

    def _finish_rec(self, a, rec):
        rec.setdefault("key", a.key)
        rec.setdefault("label", a.label)
        rec.setdefault("type", a.type)
        rec.setdefault("url", a.url)
        rec.setdefault("state", "error" if not rec.get("up") else "idle")
        rec.setdefault("caps", a._caps(rec))
        st = self.engines.get(a.key)
        if st is not None:
            st["up"] = bool(rec.get("up"))

    # -- the one loop ---------------------------------------------------------

    def _step(self):
        """Poll every engine whose next_due has come; set the next one."""
        t = self.clock()
        due = [e for e in self.engines.values() if e["next_due"] <= t]
        due.sort(key=lambda e: e["next_due"])
        for e in due:
            a = e["adapter"]
            self._poll(a)
            if e["up"]:
                e["backoff"] = 0
                e["next_due"] = self.clock() + (1.0 if a._busy else 5.0)
            else:
                e["next_due"] = self.clock() + DOWN_BACKOFF[e["backoff"]]
                e["backoff"] = min(e["backoff"] + 1, len(DOWN_BACKOFF) - 1)
        if self._next_disc is not None and t >= self._next_disc:
            self.discovery()
            self._next_disc = t + DISCOVERY_EVERY

    def run(self):
        if self.discovery_enabled:
            self.discovery()
            self._next_disc = self.clock() + DISCOVERY_EVERY
        else:
            self._next_disc = None
        while not self._stop:
            self._step()
            time.sleep(self.TICK)


if __name__ == "__main__":
    # quick manual probe: python3 engines.py [url ...]
    import sys
    urls = ["http://127.0.0.1:11434"] + sys.argv[1:]
    for base in urls:
        for tname, cls in ADAPTERS.items():
            if tname == "custom":
                continue
            try:
                ok = cls.fingerprint(base)
            except Exception:
                ok = False
            print("%-8s %s %s" % (tname, base, "match" if ok else "-"))
