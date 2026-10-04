#!/usr/bin/env python3
"""Engine adapters and scheduler for Speculum (DESIGN.md section 1).

Stdlib only. An Adapter knows how to fingerprint its engine (by response,
never by port) and poll() the common schema:

    key, label, type, url, up, state, [parent], [models[]], [counters{}],
    [rates{}], [queue], [window], [sessions[]], [extras{}], caps[]

Missing optional blocks are simply absent; caps lists which are present.
The Scheduler is the ONE thread that polls every engine in turn, applying
the DESIGN.md cadence: up and busy 1 s, up and idle 5 s, down backs off
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
TIMEOUT_LOCAL = 0.3       # DESIGN: connect timeout on localhost
TIMEOUT_REMOTE = 2.0
DISCOVERY_EVERY = 60.0
DOWN_BACKOFF = (5, 15, 60, 300)
OPTIONAL_BLOCKS = ("models", "counters", "rates", "queue", "window",
                   "sessions", "extras")

# port -> adapters to try there, in order (8080 is ambiguous: llama.cpp,
# OpenAI-compatible servers and Strata all listen there)
DISCOVERY = [
    (11434, ("ollama",)),
    (8080, ("llamacpp", "openai")),
    (8000, ("vllm",)),
    (1234, ("lmstudio",)),
]


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
    type = "vllm"
    default_port = 8000

    RATES = {"decode_tps": ("vllm:generation_tokens_total", None),
             "prefill_tps": ("vllm:prompt_tokens_total", None)}

    @classmethod
    def fingerprint(cls, url):
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url + "/metrics"),
                timeout=TIMEOUT_LOCAL if _is_local(url) else TIMEOUT_REMOTE)
            return "vllm:" in r.read(1 << 20).decode("utf-8", "replace")
        except Exception:
            return False

    def poll(self):
        t = time.time()
        try:
            counters = parse_prom(self.get_text("/metrics"))
        except Exception:
            return self._finish({"up": False, "state": "error"})
        if not any(k.startswith("vllm:") for k in counters):
            return self._finish({"up": False, "state": "error"})
        rec = {
            "up": True,
            "state": "running" if counters.get("vllm:num_requests_running", 0)
                    else "idle",
            "counters": {"prompt_tokens": counters.get("vllm:prompt_tokens_total"),
                         "output_tokens": counters.get("vllm:generation_tokens_total")},
            "queue": int(counters.get("vllm:num_requests_waiting", 0) or 0),
            "rates": self.rates(counters, t, self.RATES),
        }
        if not rec["rates"]:
            rec.pop("rates")
        return self._finish(rec, counters=counters)


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
    "lmstudio": LmStudioAdapter,
    "openai": OpenAIAdapter,
    "custom": CustomAdapter,
}

# card labels for engines found by discovery (configured engines use
# their [[engine]] name)
TYPE_LABELS = {"ollama": "Ollama", "llamacpp": "llama.cpp", "vllm": "vLLM",
               "lmstudio": "LM Studio", "openai": "OpenAI"}


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
                 on_result=None, on_event=None, on_discover=None):
        self.discovery_enabled = discovery_enabled
        self.claimed_ports = claimed_ports
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
        ports = set()
        for a in list(self.engines.values()):
            u = a["adapter"].url
            try:
                ports.add(urllib.request.urlparse(u).port or 0)
            except Exception:
                pass
        if self.claimed_ports is not None:
            try:
                ports.update(self.claimed_ports())
            except Exception:
                pass
        return ports

    def discovery(self):
        claimed = self._claimed()
        found = []
        for port, types in DISCOVERY:
            if port in claimed:
                continue
            base = "http://127.0.0.1:%d" % port
            # Strata has its own poller and passes the llama.cpp and OpenAI fingerprints; at startup it
            # may not be claimed yet (its poller has not answered), so ask the server who it is.
            if _is_strata(base):
                continue
            for tname in types:
                try:
                    ok = ADAPTERS[tname].fingerprint(base)
                except Exception:
                    ok = False
                if ok:
                    a = make_adapter({"type": tname, "url": base}, port=port)
                    self.add(a)
                    found.append((port, a))
                    break
        for port, a in found:
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
