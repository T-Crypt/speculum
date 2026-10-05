#!/usr/bin/env python3
"""Speculum collector — live data feed for the glass dashboard.

Serves the static dashboard from the repo root plus two API endpoints:

    GET /api/snapshot   one-shot full JSON snapshot (history + requests + state)
    GET /api/stream     SSE stream: `tick` at 1 Hz, plus push events
                        (`req`, `ev`, `alert`) when something happens

Sources (each optional, degrades cleanly):

    GPU      ONE long-lived nvidia-smi subprocess (-lms 1000), read line by
             line. The collector never opens a CUDA/NVML context itself.
    host     via hostinfo.py — Linux: /proc (CPU total/per-core, meminfo,
             loadavg, uptime); Windows: Win32 via ctypes — plus RSS of
             llama-server / ninfer-serve / strata processes.
    llama-swap  http://127.0.0.1:9090  /running, /v1/models,
             /api/metrics/activity, /api/events, /logs/stream/<model>
    NInfer backend  (port from /running "proxy", e.g. 127.0.0.1:5803)
             /metrics, /slots
    Strata  http://127.0.0.1:8080  /v1/models, /metrics, /slots (when up)

Stdlib only. Bounded buffers: 60 min @ 1 s for GPU/host, last 500 inference
requests, last 200 events. Target footprint < 40 MB RSS.
"""

import argparse
import collections
import csv
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config as spec_config
import engines
import history
import hostinfo

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
PORT = 8792

LLAMA_SWAP = "http://127.0.0.1:9090"
STRATA = "http://127.0.0.1:8080"

HIST_SECONDS = 60 * 60          # ring buffer depth: 60 min at 1 s
REqs_MAX = 500                  # last ~500 inference requests
EVENTS_MAX = 200
PROBE_SLOW = 5.0                # timeout for sources that may be down
PROBE_FAST = 3.0                # timeout for sources that should be up
GPU_CMD = [
    "nvidia-smi",
    "--query-gpu=name,driver_version,temperature.gpu,utilization.gpu,"
    "power.draw,power.limit,memory.used,memory.total,clocks.sm,clocks.mem,"
    "fan.speed,pcie.link.gen.current,pcie.link.width.current",
    "--format=csv,noheader,nounits", "-lms", "1000",
]


KPI_KEYS = ["tps", "rpm", "p95", "ttft", "tpot", "vram", "cache", "queue", "reingest", "mtp"]

lock = threading.RLock()


class State:
    def __init__(self):
        self.t0 = time.time()
        self.gpu = {"present": None, "last": None,
                    "hist": collections.deque(maxlen=HIST_SECONDS)}
        self.host = {"last": None, "hist": collections.deque(maxlen=HIST_SECONDS)}
        self.kpi = {"last": {}, "hist": {k: collections.deque(maxlen=HIST_SECONDS)
                                         for k in KPI_KEYS}}
        self.models = []            # llama-swap /v1/models summary
        self.running = []           # llama-swap /running entries
        self._swap_models = []      # (engine key, model) pairs, for history spans
        self.engines = {}           # engine key -> engine state dict
        self.requests = collections.deque(maxlen=REqs_MAX)  # newest first
        self.events = collections.deque(maxlen=EVENTS_MAX)  # newest first
        self.strata = {"up": None, "models": [], "metrics": {}, "slots": []}
        self.alerts = []            # active alert strings
        self.eng_hist = {}          # engine key -> 1 Hz throughput deque
        self._req_seen = collections.OrderedDict()
        self._tick_gen = 0
        self._pend_ev = []          # events not yet shipped in a tick
        self._pend_req = []         # requests not yet shipped in a tick

    def eng_series(self, key):
        d = self.eng_hist.get(key)
        if d is None:
            d = collections.deque(maxlen=HIST_SECONDS)
            self.eng_hist[key] = d
        return d

    def engine(self, key, label, origin, window=None):
        with lock:
            e = self.engines.get(key)
            if e is None:
                e = {
                    "key": key, "label": label, "origin": origin,
                    "up": None, "latched": False, "backend": None,
                    "queue": None, "rates": None, "counters": None,
                    "slots": [], "sessions": [], "service_unavailable": 0,
                    "mtp": None, "window": window,
                }
                self.engines[key] = e
            else:
                e.update(label=label, origin=origin, window=window or e["window"])
            return e


S = State()
STARTED = time.time()    # this collector's start, so replayed request lists do not re-announce old requests
# History defaults to a no-op so imports and tests never touch the disk;
# main() swaps in a real History from cfg["history"]["retention_days"].
HIST = history._Noop()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def now():
    return time.time()


def jget(url, timeout=PROBE_FAST, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_get_text(url, timeout=PROBE_SLOW):
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(1 << 20).decode("utf-8", "replace")


def parse_num(s, default=None):
    if s is None:
        return default
    s = str(s)
    m = re.match(r"^([\d.]+)\s*([kKmM])?$", s.strip())
    if not m:
        return default
    v = float(m.group(1))
    if m.group(2) in ("k", "K"):
        v *= 1e3
    elif m.group(2) in ("m", "M"):
        v *= 1e6
    return v


def push_event(level, msg, ts=None):
    """level: info | ok | warn | err | alert"""
    with lock:
        ev = {"t": ts if ts is not None else now(), "level": level, "msg": msg}
        S.events.appendleft(ev)
        S._pend_ev.insert(0, ev)
        if len(S._pend_ev) > 200:
            S._pend_ev.pop()
    HIST.add_event(ev["t"], level, None, msg)   # cheap bounded append
    return ev


def add_request(rec):
    """Append one request record (newest first). Dedupe by id if present."""
    with lock:
        # The same request arrives from llama-swap activity and from the NInfer log:
        # merge on (prompt, output) within 120 s, filling fields the other source lacks.
        if rec.get("prompt") is not None and rec.get("output") is not None:
            for old in S.requests:
                if (old.get("prompt") == rec["prompt"] and old.get("output") == rec["output"]
                        and abs((old.get("t") or 0) - (rec.get("t") or 0)) < 120):
                    for k, v in rec.items():
                        if v is not None and old.get(k) is None:
                            old[k] = v
                    return False
        if "id" in rec:
            seen = S._req_seen
            if rec["id"] in seen:
                return False
            seen[rec["id"]] = None
            while len(seen) > REqs_MAX * 2:
                seen.popitem(last=False)
        S.requests.appendleft(rec)
        S._pend_req.insert(0, rec)
        if len(S._pend_req) > REqs_MAX:
            S._pend_req.pop()
    # only reached for new records (merged duplicates return False above);
    # History holds the dict reference and writes it after the 120 s merge
    # window closes, so fields merged in later are captured
    HIST.add_request(rec)
    return True


def add_alert(msg):
    with lock:
        if msg not in S.alerts:
            S.alerts.append(msg)
            push_event("alert", msg)


def clear_alert(msg):
    with lock:
        if msg in S.alerts:
            S.alerts.remove(msg)


# --------------------------------------------------------------------------
# GPU: one long-lived nvidia-smi subprocess, read line by line
# --------------------------------------------------------------------------

def gpu_line(sample_fields, vals):
    if len(vals) != len(sample_fields):
        return None
    row = dict(zip(sample_fields, vals))
    out = {}
    for k in ("name", "driver_version"):
        out[k] = row.get(k)
    for k in ("temperature.gpu", "utilization.gpu", "power.draw", "power.limit",
              "memory.used", "memory.total", "clocks.sm", "clocks.mem", "fan.speed"):
        v = parse_num(row.get(k))
        if v is None:
            return None
        out[k] = v
    gen = parse_num(row.get("pcie.link.gen.current"), 4)
    wid = parse_num(row.get("pcie.link.width.current"), 16)
    out["pcie"] = "PCIe %s x%s" % (int(gen or 4), int(wid or 16))
    out["memory.used_gb"] = out["memory.used"] / 1024.0
    out["memory.total_gb"] = out["memory.total"] / 1024.0
    out["fan_rpm"] = int(out["fan.speed"]) if out["fan.speed"] not in (0.0, -1.0) else None
    out["clock_sm_mhz"] = int(out["clocks.sm"])
    out["clock_mem_mhz"] = int(out["clocks.mem"])
    return out


def gpu_thread():
    fields = ["name", "driver_version", "temperature.gpu", "utilization.gpu",
              "power.draw", "power.limit", "memory.used", "memory.total",
              "clocks.sm", "clocks.mem", "fan.speed",
              "pcie.link.gen.current", "pcie.link.width.current"]
    while True:
        try:
            p = subprocess.Popen(GPU_CMD, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            with lock:
                S.gpu["present"] = False
                S.gpu["last"] = None
            push_event("warn", "nvidia-smi unavailable: %s" % e)
            return  # no point retrying on this boot
        with lock:
            S.gpu["present"] = True
        while True:
            try:
                line = p.stdout.readline()
            except Exception:
                break
            if not line:
                break
            parts = [x.strip() for x in line.strip().split(",")]
            if len(parts) < 8:
                continue
            try:
                row = gpu_line(fields, parts)
            except Exception:
                continue
            if row is None:
                continue
            row["t"] = now()
            with lock:
                S.gpu["last"] = row
                S.gpu["hist"].append(row)
        rc = p.wait()
        time.sleep(2.0)  # it died: restart the long-lived poller


# --------------------------------------------------------------------------
# host: sampling via hostinfo (Linux: /proc; Windows: Win32 ctypes)
# --------------------------------------------------------------------------

def host_thread():
    prev_total, prev_cores = None, []
    procs = []
    next_scan = 0.0
    while True:
        t = now()
        total, cores = hostinfo.cpu_times()
        sample = {}
        if total and prev_total:
            dt = total[0] - prev_total[0]
            if dt > 0:
                sample["cpu_total_pct"] = round(100.0 * (dt - (total[1] - prev_total[1])) / dt, 1)
                per = []
                for i, c in enumerate(cores):
                    if i < len(prev_cores):
                        pd = c[0] - prev_cores[i][0]
                        if pd > 0:
                            per.append(round(100.0 * (pd - (c[1] - prev_cores[i][1])) / pd, 1))
                        else:
                            per.append(0.0)
                sample["cpu_per_core"] = per
        prev_total, prev_cores = total, cores
        sample.update(hostinfo.memory())
        load = hostinfo.load()
        if load is not None:
            sample["load"] = load
        up = hostinfo.uptime_s()
        if up is not None:
            sample["uptime_s"] = up
        if t >= next_scan:
            procs = hostinfo.processes()
            next_scan = t + 15
        sample["procs"] = procs
        sample["t"] = t
        with lock:
            S.host["last"] = sample
            S.host["hist"].append(sample)
        time.sleep(1.0)


# --------------------------------------------------------------------------
# llama-swap: /running, /v1/models, /api/metrics/activity, /api/events
# --------------------------------------------------------------------------

def strata_engine():
    e = S.engine("strata", "Strata", "strata")
    e["up"] = S.strata["up"]
    return e


def llama_swap_poll():
    while True:
        # /running drives which backend the engine state follows
        try:
            running = jget(LLAMA_SWAP + "/running", timeout=PROBE_FAST).get("running", [])
        except Exception:
            running = None
        with lock:
            S.running = running if running is not None else []
        # llama-swap's engine is llama.cpp (llama-server): one llama.cpp card follows llama-swap. It names
        # the running model when there is one (the loop below), reads "llama.cpp" when idle, and says why
        # when llama-swap does not answer. NInfer, also started by llama-swap, keeps its own card.
        sw = S.engine("llama", "llama.cpp", "llama-swap")
        with lock:
            sw["up"] = running is not None
            sw["label"], sw["origin"] = "llama.cpp", "llama-swap"
            sw["backend"] = None      # the running model's proxy, set below
            sw["reason"] = None if running is not None else (      # the card's origin says llama-swap
                "not answering on %s" % LLAMA_SWAP.split("//", 1)[-1])
        if running is not None:
            # model spans for history: diff the running set (key, model)
            models_now = []
            for r in running:
                model = r.get("model") or "model"
                key = ("ninfer" if ("ninfer" in r.get("cmd", "")
                                     or "NInfer" in model) else "llama")
                if (key, model) not in models_now:
                    models_now.append((key, model))
            with lock:
                prev = set(S._swap_models)
                S._swap_models = models_now
            cur = set(models_now)
            if prev != cur:
                t = now()
                for key, model in cur - prev:
                    HIST.model_loaded(key, model, t)
                for key, model in prev - cur:
                    HIST.model_unloaded(key, model, t)
        if running is not None:
            for r in running:
                model = r.get("model") or "model"
                proxy = r.get("proxy") or ""
                backend = proxy.replace("localhost", "127.0.0.1") or None
                if "ninfer" in r.get("cmd", "") or "NInfer" in model:
                    key, label, origin, window = "ninfer", model, "ninfer", None
                else:
                    key, label, origin, window = "llama", model, "llama-swap", None
                e = S.engine(key, label, origin, window)
                e["up"] = True
                e["latched"] = False
                e["backend"] = backend
                clear_alert("llama-swap engine %s latched (service unavailable)" % label)
        # /v1/models
        try:
            data = jget(LLAMA_SWAP + "/v1/models", timeout=PROBE_FAST).get("data", [])
            with lock:
                S.models = [{
                    "id": m.get("id"),
                    "window": m.get("context_window") or m.get("context_length"),
                    "status": (m.get("status") or {}).get("value"),
                    "aliases": (m.get("meta", {}).get("llamaswap") or {}).get("aliases", []),
                    "vision": (m.get("architecture") or {}).get("input_modalities", []) and "image" in (m.get("architecture") or {}).get("input_modalities", []),
                } for m in data]
        except Exception:
            pass
        # /api/metrics/activity — per-request records, newest first
        try:
            recs = jget(LLAMA_SWAP + "/api/metrics/activity", timeout=PROBE_FAST).get("data", [])
            for r in recs:
                tok = r.get("tokens") or {}
                cache = tok.get("cache_tokens", 0) or 0
                inp = tok.get("input_tokens", 0) or 0
                out = tok.get("output_tokens", 0) or 0
                fresh = max(0, inp)
                total = cache + fresh
                rec = {
                    "id": "swap-%s" % r.get("id"),
                    "ts": r.get("timestamp"),
                    "t": ts_epoch(r.get("timestamp")),
                    "model": r.get("model"),
                    "origin": origin_for(r.get("model")),
                    "prompt": total, "cache": cache, "fresh": fresh,
                    "output": out,
                    "cache_pct": round(100.0 * cache / total, 1) if total else None,
                    "total_s": (r.get("duration_ms") or 0) / 1000.0,
                    "ttft_s": None, "queue_s": None,
                    "prefill_tps": None, "decode_tps": None,
                    "mtp_acc": None, "mtp_tot": None,
                    "window": window_for(r.get("model")),
                }
                ok = add_request(rec)
                if ok:
                    push_event("req", "req done · %s · ↑%s ↓%s · cache %s%% · %.1fs"
                               % (rec["model"], fmtk(total), fmtk(out),
                                  rec["cache_pct"] if rec["cache_pct"] is not None else "—",
                                  rec["total_s"]))
        except Exception:
            pass
        time.sleep(2.0)


def origin_for(model):
    if not model:
        return None
    for e in S.engines.values():
        if e.get("label") == model:
            return e["key"]
    return "ninfer" if "NInfer" in model else "swap"


def window_for(model):
    if not model:
        return None
    for m in S.models:
        if m.get("id") == model:
            return m.get("window")
    for e in S.engines.values():
        if e.get("label") == model and e.get("window"):
            return e["window"]
    return None


def ts_epoch(s):
    if not s:
        return None
    try:
        # "2026-10-02T23:08:34-06:00"
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def fmtk(n):
    if n is None:
        return "?"
    n = float(n)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fk" % (n / 1e3)
    return str(int(n))


# --------------------------------------------------------------------------
# llama-swap /api/events — SSE logData relay (cheap, always up with the box)
# --------------------------------------------------------------------------

def sse_relay():
    while True:
        try:
            req = urllib.request.Request(LLAMA_SWAP + "/api/events",
                                         headers={"Accept": "text/event-stream"})
            r = urllib.request.urlopen(req, timeout=PROBE_SLOW)
            while True:
                line = r.readline()
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except Exception:
                    continue
                data = payload.get("data", "")
                if not isinstance(data, str) or not data.strip():
                    continue
                # the data field is a multi-line raw log; a frame can end mid
                # line, so only trust lines that carry their own [LEVEL] stamp
                for line in data.splitlines():
                    m = re.match(r"^\S+\s+\[(\w+)\]\s+(.*)$", line.strip())
                    if not m:
                        continue
                    lv = {"ERROR": "err", "WARN": "warn"}.get(m.group(1), "info")
                    if lv == "info":
                        low = m.group(2).lower()
                        if not any(k in low for k in
                                   ("engine", "loading", "weights", "swap",
                                    "unload", "latch", "fail", "ready",
                                    "starting", "unavailable", "selecting")):
                            continue
                    push_event(lv, m.group(2)[:300])
        except Exception:
            pass
        time.sleep(3.0)


# --------------------------------------------------------------------------
# NInfer backend: /metrics, /slots, upstream log stream
# --------------------------------------------------------------------------

METRIC_KEYS = [
    "llamacpp:prompt_tokens_total",
    "llamacpp:prompt_seconds_total",
    "llamacpp:tokens_predicted_total",
    "llamacpp:tokens_predicted_seconds_total",
    "llamacpp:requests_processing",
    "llamacpp:requests_deferred",
    "ninfer:requests_total",
    "ninfer:prefix_cache_hit_tokens_total",
    "ninfer:draft_tokens_total",
    "ninfer:draft_accepted_tokens_total",
]


def parse_metrics(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parse_num(parts[1])
    return out


# NInfer upstream log stream: per-request done lines, WARN/ERROR, latch detect
REQ_RE = re.compile(r"req#(\d+) done \|")
NUM_FIELDS = {
    "prompt": r"prompt ([\d,]+)",
    "output": r"output ([\d,]+)",
    "cache": r"cache ([\d,]+)",
    "ttft": r"TTFT ([\d.]+ ?(ms|s|m|h)?)",
    "total": r"total ([\d.]+ ?(ms|s|m|h)?)",
    "queue": r"queue ([\d.]+ ?(ms|s|m|h)?)",
    "prefill": r"prefill ([\d.]+k?) tok/s",
    "decode": r"decode ([\d.]+k?) tok/s",
    "mtp_acc": r"mtp accepted (\d+)/(\d+)",
    "thinking": r"thinking (\d+)/",
}


def parse_dur(s):
    s = s.strip()
    m = re.match(r"^([\d.]+)\s*(ms|s|m|h)?$", s)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2) or "s"
    if unit == "ms":
        v /= 1000.0
    elif unit == "m":
        v *= 60
    elif unit == "h":
        v *= 3600
    return v


LOG_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d+)")


def log_line(line, e):
    m = REQ_RE.search(line)
    if m and e is not None and e.get("origin") == "ninfer":
        rid = int(m.group(1))
        lt = LOG_TS_RE.match(line)
        t = now()
        if lt:
            try:
                t = time.mktime(time.strptime(lt.group(1), "%Y-%m-%d %H:%M:%S")) + float("0." + lt.group(2))
            except ValueError:
                pass
        # req# restarts at 1 on every NInfer load, so the log timestamp is part of the id
        rec = {"id": "req#%d@%d" % (rid, int(t)), "t": t, "model": e.get("label"),
               "origin": "ninfer", "prompt": None, "cache": None, "fresh": None,
               "output": None, "cache_pct": None, "total_s": None, "ttft_s": None,
               "queue_s": None, "prefill_tps": None, "decode_tps": None,
               "mtp_acc": None, "mtp_tot": None, "window": e.get("window"),
               "detail": line.strip()[:300]}
        for k, pat in NUM_FIELDS.items():
            mm = re.search(pat, line)
            if not mm:
                continue
            if k == "mtp_acc":
                rec["mtp_acc"] = int(mm.group(1).replace(",", ""))
                rec["mtp_tot"] = int(mm.group(2).replace(",", ""))
            elif k in ("ttft", "total", "queue"):
                rec[k + "_s"] = parse_dur(mm.group(1))
            elif k in ("prefill", "decode"):
                rec[k + "_tps"] = parse_num(mm.group(1))
            elif k == "thinking":
                rec["thinking"] = int(mm.group(1).replace(",", ""))
            else:
                v = int(mm.group(1).replace(",", ""))
                if k == "cache":
                    rec["cache"] = v
                elif k == "prompt":
                    rec["prompt"] = v
                elif k == "output":
                    rec["output"] = v
        if rec["prompt"] is not None and rec.get("cache") is not None:
            rec["fresh"] = max(0, rec["prompt"] - rec["cache"])
            if rec["prompt"]:
                rec["cache_pct"] = round(100.0 * rec["cache"] / rec["prompt"], 1)
        ok = add_request(rec)
        if ok:
            lvl = "ok" if (rec.get("cache_pct") or 0) and rec["cache_pct"] >= 90 else "info"
            push_event(lvl, "req#%d done · %s · cache %s%% (%s) · ↑%s ↓%s%s · %.1fs%s"
                       % (rid, e.get("label"), rec.get("cache_pct") if rec.get("cache_pct") is not None else "—",
                          "replay" if "response replay" in line else "prefix",
                          fmtk(rec.get("prompt")), fmtk(rec.get("output")),
                          (" · mtp %s/%s" % (rec["mtp_acc"], rec["mtp_tot"])) if rec.get("mtp_acc") is not None else "",
                          rec.get("total_s") or 0,
                          " · queue %.1fs" % rec["queue_s"] if rec.get("queue_s") and rec["queue_s"] > 0.5 else ""),
                       ts=rec["t"])
        return
    up = line.lower()
    if "service unavailable" in up or "unavailable" in up:
        with lock:
            n = e["service_unavailable"] + 1 if e else 1
            if e:
                e["service_unavailable"] = n
        if e and n >= 3:
            e["latched"] = True
            msg = "NInfer engine latched — service unavailable x%d" % n
            add_alert(msg)
        elif e and n == 1:
            push_event("warn", "NInfer: service unavailable (x1)")
        return
    if e:
        with lock:
            if e.get("service_unavailable"):
                e["service_unavailable"] = 0
                if e.get("latched"):
                    e["latched"] = False
                    S.alerts = [a for a in S.alerts
                                if not a.startswith("NInfer engine latched")]
                    push_event("ok", "NInfer engine recovered")
    if "ERROR" in line:
        push_event("err", line.strip()[:300])
    elif "WARN" in line:
        push_event("warn", line.strip()[:300])


def logs_thread():
    """Follow llama-swap /logs/stream/<running-model> for the active NInfer engine."""
    cur_model = None
    while True:
        running = []
        try:
            running = jget(LLAMA_SWAP + "/running", timeout=PROBE_FAST).get("running", [])
        except Exception:
            time.sleep(3.0)
            continue
        model = None
        for r in running:
            if "ninfer" in (r.get("cmd") or "") or "NInfer" in (r.get("model") or ""):
                model = r.get("model")
                break
        if model != cur_model:
            cur_model = model
            if model:
                push_event("info", "following NInfer logs · %s" % model)
        if not model:
            time.sleep(3.0)
            continue
        e = S.engine("ninfer", model, "ninfer", 262144)
        try:
            # /logs/stream/<model> is a plain text tail, not SSE frames
            req = urllib.request.Request("http://127.0.0.1:9090/logs/stream/%s" % model,
                                         headers={"Accept": "text/plain"})
            r = urllib.request.urlopen(req, timeout=PROBE_SLOW)
            while True:
                line = r.readline()
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                line = line.strip()
                if line:
                    log_line(line, e)
        except Exception:
            pass
        time.sleep(3.0)


# --------------------------------------------------------------------------
# Strata (by hand, may be down): /v1/models, /metrics, /slots
# --------------------------------------------------------------------------

def strata_json_into(e, sj):
    """Strata's JSON /metrics onto the engine card's fields (call with `lock` held)."""
    eng, live, tot = sj.get("engine") or {}, sj.get("live") or {}, sj.get("totals") or {}
    e["window"] = eng.get("max_context") or e.get("window")
    e["queue"] = live.get("queued")
    e["rates"] = {"decode_tps": live.get("tok_s"), "prefill_tps": live.get("prefill_tok_s_mean"),
                  "state": live.get("state")}
    offered = tot.get("drafts_offered") or 0
    e["mtp"] = round(100.0 * (tot.get("drafts_accepted") or 0) / offered, 1) if offered else None
    e["counters"] = {k: tot.get(k) for k in ("requests", "prompt_tokens", "reused", "output_tokens")}
    e["counters"].update(kv=eng.get("kv"), kv_resident=eng.get("kv_resident"),
                         expert_slots=eng.get("expert_slots"), vram_free_mib=eng.get("vram_free_mib"),
                         engine_version=eng.get("version"))


def strata_requests(sj):
    """Strata's finished requests (JSON /metrics) as Speculum request records; add_request dedupes by id."""
    eng = sj.get("engine") or {}
    out = []
    for r in sj.get("requests") or []:
        t = r.get("time")
        if not t:
            continue
        # cached = prompt - read: Strata's `reused` can exceed prompt_tokens (a reply that is reused as
        # well), which made cache hit read 106%; prompt_read is the tokens actually read.
        prompt, prompt_ms = r.get("prompt_tokens") or 0, r.get("prompt_ms")
        read = r.get("prompt_read")
        fresh = min(prompt, read) if read is not None else max(0, prompt - (r.get("reused") or 0))
        cache = prompt - fresh
        out.append({
            "id": "strata-%.3f" % t, "ts": None, "t": t, "model": eng.get("model") or "strata",
            "origin": "strata", "prompt": prompt, "cache": cache, "fresh": fresh,
            "output": r.get("output_tokens") or 0,
            "cache_pct": round(100.0 * cache / prompt, 1) if prompt else None,
            "total_s": r.get("duration_s"),
            "ttft_s": prompt_ms / 1000.0 if prompt_ms is not None else None,   # prompt read time
            "queue_s": None,
            "prefill_tps": round(fresh / (prompt_ms / 1000.0), 1) if prompt_ms and fresh else None,
            "decode_tps": r.get("decode_tok_s"),
            "mtp_acc": r.get("drafts_accepted"), "mtp_tot": r.get("drafts_offered"),
            "window": eng.get("max_context"),
        })
    return out


def strata_thread():
    span = None             # the model of Strata's open load span (the timeline), or None
    while True:
        up = True
        models, metrics, slots, sj = [], {}, [], None
        for path, sink in (("/v1/models", "models"), ("/metrics", "metrics"), ("/slots", "slots")):
            try:
                if path == "/v1/models":
                    models = jget(STRATA + path, timeout=PROBE_SLOW).get("data", [])
                elif path == "/metrics":
                    raw = http_get_text(STRATA + path, timeout=PROBE_SLOW)
                    # Strata 0.1.38+ answers one JSON document (engine, live, totals, requests);
                    # older ones Prometheus-style text. Line-splitting the JSON made "{"engine":"
                    # a counter name and left the window at a guess.
                    if raw.lstrip().startswith("{"):
                        sj = json.loads(raw)
                    else:
                        metrics = parse_metrics(raw)
                else:
                    slots = json.loads(http_get_text(STRATA + path, timeout=PROBE_SLOW))
            except Exception:
                up = False
                break
        e = S.engine("strata", "Strata", "strata")
        with lock:
            S.strata["up"] = up
            S.strata["models"] = [{
                "id": m.get("id"),
                "window": m.get("context_window") or m.get("context_length"),
            } for m in models]
            S.strata["metrics"] = metrics
            S.strata["slots"] = slots
            e["up"] = up
            ses = []
            for s in slots:
                if isinstance(s, dict):
                    ses.append({
                        "id": "s%d" % s.get("id", 0),
                        "session": (s.get("session_digest") or s.get("session_id") or "")[:8] or "strata",
                        "window": s.get("n_ctx"),
                        "used": s.get("n_prompt_tokens"),
                        "cached": s.get("n_prompt_tokens_cache"),
                        "processing": bool(s.get("is_processing")),
                    })
            e["slots"] = ses
            e["sessions"] = ses
            if metrics:
                e["counters"] = metrics
                for m in models:
                    if (m.get("context_window") or 0) > (e.get("window") or 0):
                        e["window"] = m.get("context_window")
            if sj:
                strata_json_into(e, sj)
        if sj:
            for rec in reversed(strata_requests(sj)):     # oldest first, so the feed stays newest-first
                # Strata lists its recent requests on every poll, so a collector restart sees them all
                # again: history dedupes them, and only ones that finished since this start get an event
                if add_request(rec) and rec["t"] >= STARTED - 5:
                    push_event("req", "req done · strata · ↑%s ↓%s · cache %s%% · %.1fs"
                               % (fmtk(rec["prompt"]), fmtk(rec["output"]),
                                  rec["cache_pct"] if rec["cache_pct"] is not None else "—",
                                  rec["total_s"] or 0))
        # load spans for the timeline: Strata is started by hand, so up/down is its load/unload
        model = ((sj or {}).get("engine") or {}).get("model") or (models[0].get("id") if models else None)
        if up and model and span != model:
            if span:
                HIST.model_unloaded("strata", span, now())
            HIST.model_loaded("strata", model, now())
            span = model
        elif not up and span:
            HIST.model_unloaded("strata", span, now())
            span = None
        if not up:
            e.pop("counters", None)
            with lock:
                e["rates"] = e["queue"] = e["mtp"] = None
        time.sleep(5.0)


# --------------------------------------------------------------------------
# KPI assembly (1 Hz)
# --------------------------------------------------------------------------

def pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return vals[f] + (vals[c] - vals[f]) * (k - f)


ALERTS = {}          # cfg["alerts"], set by main()


def gpu_processes():
    """[(pid, name, MiB)] of every compute process on the GPU (nvidia-smi; no CUDA context here)."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return []
    procs = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 3 and parts[0].isdigit():
            procs.append((int(parts[0]), os.path.basename(parts[1]), parse_num(parts[2]) or 0))
    return procs


def threshold_alerts(cfg, gpu, engines_, procs):
    """{message: True} for every threshold crossed right now (pure: the thread holds and fires them)."""
    out = {}
    if gpu:
        if (gpu.get("temperature.gpu") or 0) > cfg["gpu_temp_c"]:
            out["GPU temperature over %d °C" % cfg["gpu_temp_c"]] = True
        tot = gpu.get("memory.total") or 0
        if tot and 100.0 * (gpu.get("memory.used") or 0) / tot > cfg["vram_pct"]:
            out["VRAM over %d%%" % cfg["vram_pct"]] = True
    for e in engines_:
        if (e.get("queue") or 0) > cfg["queue"]:
            out["queue over %d on %s" % (cfg["queue"], e.get("label") or e.get("key"))] = True
    known = [n.lower() for n in cfg["engine_processes"]]
    for pid, name, mib in procs:
        if mib >= cfg["foreign_vram_mib"] and not any(k in name.lower() for k in known):
            out["foreign VRAM: %s (pid %d) holds %d MiB" % (name, pid, mib)] = True
    return out


# GPU process name -> engine key, for the idle-VRAM flag
PROC_ENGINE = (("llama-server", "llama"), ("ninfer-serve", "ninfer"), ("strata", "strata"), ("ollama", "ollama"))


def idle_vram(procs, last_req, t, idle_s, started):
    """{engine key: {"mib", "idle_s"}} for engine processes holding >= 1 GiB of VRAM whose engine has
    served nothing for idle_s (counted from the collector's start when it has served nothing since).
    Pure; idle_vram_pass writes it onto the cards."""
    out = {}
    for pid, name, mib in procs:
        key = next((k for pat, k in PROC_ENGINE if pat in name.lower()), None)
        if key is None or mib < 1024:
            continue
        idle = t - max(last_req.get(key) or 0, started)
        if idle >= idle_s:
            out[key] = {"mib": int(mib) + int((out.get(key) or {}).get("mib", 0)), "idle_s": int(idle)}
    return out


def idle_vram_pass(procs, t):
    last = {}
    with lock:
        for r in S.requests:
            k = r.get("origin")
            k = {"swap": "llama"}.get(k, k)
            if r.get("t") and (r["t"] > (last.get(k) or 0)):
                last[k] = r["t"]
    flags = idle_vram(procs, last, t, 60 * ALERTS.get("idle_vram_min", 30), STARTED)
    with lock:
        for k, e in S.engines.items():
            e["idle_vram"] = flags.get(k)


def alerts_thread():
    """Threshold alerts: a condition must hold for hold_s before it is raised, and clears when it ends."""
    since, raised, procs, last_procs = {}, set(), [], 0.0
    while True:
        t = time.time()
        if t - last_procs >= 15:
            procs, last_procs = gpu_processes(), t
        with lock:
            gpu = dict(S.gpu.get("last") or {})
            engs = [dict(e) for e in S.engines.values()]
        idle_vram_pass(procs, t)
        now_on = threshold_alerts(ALERTS, gpu, engs, procs)
        for msg in now_on:
            since.setdefault(msg, t)
            if msg not in raised and t - since[msg] >= ALERTS["hold_s"]:
                add_alert(msg)
                raised.add(msg)
        for msg in list(since):
            if msg not in now_on:
                since.pop(msg)
                if msg in raised:
                    clear_alert(msg)
                    raised.discard(msg)
        time.sleep(5.0)


def kpi_thread():
    while True:
        try:
            kpi_pass()
        except Exception as e:
            # one bad pass must not kill the KPI feed: log and continue
            push_event("err", "kpi: %s" % e)
        time.sleep(1.0)


def kpi_pass():
    t = now()
    t15 = t - 900
    with lock:
        reqs = [r for r in S.requests if (r.get("t") or 0) >= t15]
        engines = {k: dict(v) for k, v in S.engines.items()}
        gpu_last = S.gpu.get("last")
        host_last = S.host.get("last")

        # tps: prefer engine decode rates, fall back to window estimate
        tps = 0.0
        have_rate = False
        for e in engines.values():
            r = e.get("rates") or {}
            if r.get("decode_tps") is not None:
                tps += r["decode_tps"]
                have_rate = True
        if not have_rate and reqs:
            span = sum(r.get("total_s") or 0 for r in reqs)
            out = sum(r.get("output") or 0 for r in reqs)
            if span > 0:
                tps = out / min(span * 2, 900.0)

        rpm = sum(1 for r in S.requests if (r.get("t") or 0) >= t - 60)
        recent = [r for r in S.requests if (r.get("t") or 0) >= t - 300]
        totals = [r["total_s"] for r in recent if r.get("total_s")]
        ttfts = [r["ttft_s"] for r in recent if r.get("ttft_s") is not None]
        cache = fresh = out = 0
        mtp_acc = mtp_tot = 0
        for r in reqs:
            cache += r.get("cache") or 0
            fresh += r.get("fresh") or 0
            out += r.get("output") or 0
            mtp_acc += r.get("mtp_acc") or 0
            mtp_tot += r.get("mtp_tot") or 0
        cache_pct = round(100.0 * cache / (cache + fresh), 1) if (cache + fresh) else None
        reingest = round(100.0 * fresh / (cache + fresh), 1) if (cache + fresh) else None
        mtp = round(100.0 * mtp_acc / mtp_tot, 1) if mtp_tot else None
        queue = sum((e.get("queue") or 0) for e in engines.values())
        # TPOT: per-token generation time from recent requests
        tpot = None
        if reqs:
            dt = sum((r.get("total_s") or 0) for r in reqs)
            if out > 0:
                tpot = round((dt - sum(r.get("ttft_s") or 0 for r in reqs) if any(r.get("ttft_s") for r in reqs) else dt) / out * 1000.0, 1)

        last = {
            "t": t, "tps": round(tps, 1), "rpm": rpm,
            "p95": round(pct(totals, 95) * 1000.0, 0) if totals else None,
            "ttft": round(1000.0 * sum(ttfts) / len(ttfts), 0) if ttfts else None,
            "tpot": tpot,
            "vram": round(gpu_last["memory.used_gb"], 1) if gpu_last else None,
            "vram_total": round(gpu_last["memory.total_gb"], 1) if gpu_last else None,
            "cache": cache_pct, "reingest": reingest, "mtp": mtp,
            "queue": queue,
        }
        S.kpi["last"] = last
        for k in KPI_KEYS:
            v = last.get(k)
            S.kpi["hist"][k].append(v if v is not None else 0)
        for key, e in S.engines.items():
            r = e.get("rates") or {}
            v = r.get("decode_tps")
            if v is None:
                v = r.get("gen_tps_inst")
            S.eng_series(key).append(v if v is not None else 0)
    # history sample outside the State lock: `engines` and `gpu_last`
    # were copied to plain values inside the lock above
    HIST.sample(t, engines, dict(gpu_last) if gpu_last else None)


# --------------------------------------------------------------------------
# snapshot + SSE
# --------------------------------------------------------------------------

def history_series(deque, field):
    out = []
    for row in deque:
        v = row.get(field) if isinstance(row, dict) else row
        out.append(v)
    return out


def snapshot():
    with lock:
        gpu = S.gpu
        host = S.host["last"] or {}
        kpi = dict(S.kpi["last"])
        kpi_hist = {k: list(h) for k, h in S.kpi["hist"].items()}
        snap = {
            "t": now(),
            "collector": {
                "uptime_s": int(now() - S.t0),
                "port": PORT,
                "requests_buffered": len(S.requests),
            },
            "gpu": {
                "present": gpu["present"],
                "last": gpu.get("last"),
                "hist": {
                    "t": [r["t"] for r in gpu["hist"]],
                    "temp": history_series(gpu["hist"], "temperature.gpu"),
                    "util": history_series(gpu["hist"], "utilization.gpu"),
                    "power": history_series(gpu["hist"], "power.draw"),
                    "vram_gb": history_series(gpu["hist"], "memory.used_gb"),
                } if gpu["hist"] else None,
            },
            "host": {
                **{k: host.get(k) for k in
                   ("cpu_total_pct", "cpu_per_core", "ram_used_gb", "ram_total_gb",
                    "swap_used_gb", "swap_total_gb", "load", "uptime_s")},
                "procs": host.get("procs", []),
                "hist": {
                    "t": [r["t"] for r in S.host["hist"]],
                    "cpu": history_series(S.host["hist"], "cpu_total_pct"),
                    "ram_gb": history_series(S.host["hist"], "ram_used_gb"),
                } if S.host["hist"] else None,
            },
            "kpi": {
                "last": kpi,
                "hist": {k: hist[-HIST_SECONDS:] for k, hist in kpi_hist.items()},
            },
            "models": S.models,
            "running": S.running,
            "engines": {k: {
                "key": e["key"], "label": e["label"], "origin": e["origin"],
                "up": e.get("up"), "latched": bool(e.get("latched")),
                "backend": e.get("backend"), "queue": e.get("queue"),
                "rates": e.get("rates"), "mtp": e.get("mtp"),
                "window": e.get("window"), "sessions": e.get("sessions", []),
                "service_unavailable": e.get("service_unavailable", 0),
                "counters": e.get("counters"), "reason": e.get("reason"),
                "idle_vram": e.get("idle_vram"),
                "hist": list(S.eng_hist.get(k, ()))[-HIST_SECONDS:],
            } for k, e in S.engines.items()},
            "strata": {
                "up": S.strata["up"], "models": S.strata["models"],
                "slots": S.strata["slots"],
            },
            "requests": list(S.requests)[:200],
            "events": list(S.events)[:100],
            "alerts": list(S.alerts),
            "gen": S._tick_gen,
        }
    return snap


def tick():
    with lock:
        pend_ev = list(S._pend_ev)
        pend_req = list(S._pend_req)
        S._pend_ev.clear()
        S._pend_req.clear()
        S._tick_gen += 1
        gen = S._tick_gen
        gpu_last = S.gpu.get("last")
        host_last = S.host["last"] or {}
        kpi = dict(S.kpi["last"])
        # 1 Hz hot values for the tick
        kpi_hot = {k: (S.kpi["hist"][k][-1] if S.kpi["hist"][k] else None) for k in KPI_KEYS}
        data = {
            "t": now(),
            "gpu": {k: gpu_last.get(k) for k in
                    ("name", "driver_version", "temperature.gpu", "utilization.gpu",
                     "power.draw", "power.limit", "memory.used_gb", "memory.total_gb",
                     "clock_sm_mhz", "clock_mem_mhz", "fan_rpm", "pcie")} if gpu_last else None,
            "host": {k: host_last.get(k) for k in
                     ("cpu_total_pct", "cpu_per_core", "ram_used_gb", "ram_total_gb",
                      "load", "uptime_s", "procs")},
            "kpi": kpi_hot,
            "engines": {k: {
                "up": e.get("up"), "latched": bool(e.get("latched")),
                "queue": e.get("queue"), "rates": e.get("rates"),
                "mtp": e.get("mtp"), "sessions": e.get("sessions", []),
                "counters": e.get("counters"), "reason": e.get("reason"),
                "idle_vram": e.get("idle_vram"),
                "rate": ((S.eng_hist.get(k) or [None])[-1]
                         if S.eng_hist.get(k) else None),
            } for k, e in S.engines.items()},
            "strata_up": S.strata["up"],
            "alerts": list(S.alerts),
            "new_events": pend_ev,
            "new_requests": pend_req,
            "gen": gen,
        }
    return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "speculum/1.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/snapshot":
            try:
                self._json(snapshot())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif path == "/api/history":
            self._history(query)
        elif path == "/api/requests":
            self._requests_api(query)
        elif path == "/api/spans":
            self._spans(query)
        elif path == "/api/leaderboard":
            self._leaderboard(query)
        elif path == "/api/storage":
            self._storage()
        elif path == "/api/export":
            self._export(query)
        elif path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"event: tick\ndata: "
                                     + json.dumps(tick()).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(1.0)
            except Exception:
                pass
        else:
            self._static(path)

    # -- history APIs ------------------------------------------------------------

    def _history(self, query):
        q = urllib.parse.parse_qs(query)
        range_ = (q.get("range") or [""])[0]
        engine = (q.get("engine") or [None])[0]
        try:
            rows = HIST.history(range_, engine)
        except ValueError as e:          # unknown range -> 400
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"range": range_, "engine": engine, "rows": rows})

    def _leaderboard(self, query):
        range_ = (urllib.parse.parse_qs(query).get("range") or ["24h"])[0]
        try:
            rows = HIST.leaderboard(range_)
        except ValueError as e:          # unknown range -> 400
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"range": range_, "rows": rows})

    def do_POST(self):
        """POST /api/prune: apply the history retention now. Only with a JSON content type, so a form
        on another site cannot trigger it from a browser; it deletes nothing newer than retention."""
        path = self.path.partition("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(min(n, 4096))
        if path != "/api/prune":
            self._json({"error": "not found"}, 404)
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json({"error": "Content-Type must be application/json"}, 415)
            return
        try:
            done = HIST.prune_now()
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        if done:
            push_event("info", "history pruned to %d days" % HIST.retention_days)
        self._json({"pruned": done, "storage": HIST.storage()})

    def _spans(self, query):
        range_ = (urllib.parse.parse_qs(query).get("range") or ["24h"])[0]
        try:
            rows = HIST.spans(range_)
        except ValueError as e:          # unknown range -> 400
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"range": range_, "spans": rows})

    def _requests_api(self, query):
        q = urllib.parse.parse_qs(query)
        since_raw = (q.get("since") or [None])[0]
        limit_raw = (q.get("limit") or ["1000"])[0]
        try:
            since = int(float(since_raw)) if since_raw is not None else 0
            limit = int(limit_raw)
            if limit < 1:
                raise ValueError
        except (ValueError, TypeError):
            self._json({"error": "since and limit must be integers"}, 400)
            return
        try:
            rows = HIST.requests(since, limit)
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"rows": rows})

    def _storage(self):
        try:
            self._json(HIST.storage())
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _export(self, query):
        q = urllib.parse.parse_qs(query)
        fmt = (q.get("format") or ["csv"])[0]
        range_ = (q.get("range") or ["24h"])[0]
        if fmt not in ("csv", "json") or range_ not in history.RANGES:
            self._json({"error": "format must be csv|json, "
                                 "range one of %s" % sorted(history.RANGES)}, 400)
            return
        try:
            data, truncated = HIST.export(fmt, range_)
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        if fmt == "csv":
            body = data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="speculum-requests.csv"')
            if truncated:
                self.send_header("X-Speculum-Truncated", "1")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"rows": data, "truncated": truncated})

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        f = (ROOT / path.lstrip("/")).resolve()
        if ROOT not in f.parents and f != ROOT:
            self.send_error(403)
            return
        if not f.is_file():
            self.send_error(404)
            return
        data = f.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".json": "application/json",
            ".md": "text/plain; charset=utf-8",
        }.get(f.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def _port_of(u):
    """Best-effort port from a proxy string ("http://h:p" or "h:p")."""
    if not u:
        return None
    try:
        p = urllib.request.urlparse(u if "//" in u else "http://" + u).port
        if p:
            return p
    except Exception:
        pass
    if ":" in u:
        tail = u.rsplit(":", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


def claimed_ports():
    """Ports the discovery probes must skip: llama-swap, Strata when up,
    and the backend of any running entry (e.g. the NInfer proxy port)."""
    ports = {9090}
    with lock:
        if S.strata.get("up"):
            ports.add(8080)
        for r in S.running or []:
            p = _port_of(r.get("proxy") or "")
            if p:
                ports.add(p)
    return ports


def on_engine_result(a, rec):
    """Write one scheduler poll into the shared engine state so the
    existing UI renders it as an engine card (and the KPI pass appends
    its decode_tps to the 1 Hz series like every other engine)."""
    e = S.engine(a.key, a.label, a.type)
    with lock:
        if rec.get("up"):
            e["up"] = True
            e["state"] = rec.get("state")
            if rec.get("parent"):
                e["parent"] = rec["parent"]
            ctxs = [m.get("ctx") for m in rec.get("models") or [] if m.get("ctx")]
            if ctxs:
                e["window"] = max(ctxs)
            if "queue" in rec:
                e["queue"] = rec["queue"]
            if rec.get("sessions"):
                e["sessions"] = rec["sessions"]
                e["slots"] = rec["sessions"]
            e["mtp"] = None
            if rec.get("rates"):
                e["rates"] = rec["rates"]
            if rec.get("counters"):
                e["counters"] = rec["counters"]
            if rec.get("models"):
                e["models"] = rec["models"]
            e["reason"] = rec.get("reason")        # e.g. Unsloth: an API key is needed for models
            clear_alert("engine %s down" % a.label)
        else:
            e["up"] = False
            e["state"] = rec.get("state") or "error"
            e["rates"] = None
            e["counters"] = None
            e["queue"] = None
            e["sessions"] = []
            e["mtp"] = None
            e["reason"] = rec.get("reason") or "not running on %s" % a.url.split("//", 1)[-1]
            # optional = true (an app started now and then, e.g. LM Studio): a card, no alert
            if not getattr(a, "optional", False):
                add_alert("engine %s down" % a.label)


def start_scheduler(cfg):
    adapters = [engines.make_adapter(e) for e in cfg.get("engine") or []]
    sched = engines.Scheduler(
        adapters,
        discovery_enabled=bool(cfg.get("discovery", {}).get("enabled", True)),
        claimed_ports=claimed_ports,
        on_result=on_engine_result,
        on_event=lambda lv, m: push_event(lv, m),
        on_discover=lambda a: push_event(
            "info", "discovered %s · %s" % (a.label, a.url)),
    )
    threading.Thread(target=sched.run, daemon=True).start()
    return sched


def main():
    global PORT, HIST
    if sys.stdout is None:       # pythonw / no console: a print would crash
        sys.stdout = sys.stderr = open(os.devnull, "w")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=None,
                    help="speculum.toml path (default: repo root, then "
                         "~/.config/speculum/speculum.toml)")
    args = ap.parse_args()

    try:
        cfg, cfg_src = spec_config.load(args.config)
    except FileNotFoundError as exc:
        print("speculum: %s" % exc, file=sys.stderr)
        sys.exit(2)
    if args.port is not None:          # CLI wins over the config file
        PORT = args.port
    elif cfg.get("server", {}).get("port"):
        PORT = int(cfg["server"]["port"])
    host = cfg.get("server", {}).get("host") or HOST

    # SQLite history. retention_days 0 -> no-op object.
    retention = int((cfg.get("history") or {}).get("retention_days", 30) or 0)
    if retention > 0:
        HIST = history.History(retention_days=retention,
                               log=lambda m: push_event("warn", "history: %s" % m))
        push_event("info", "history db · %s · keep %dd"
                   % (HIST.path, retention))
    else:
        HIST = history._Noop()
        push_event("info", "history off (retention_days = 0)")

    push_event("info", "collector up · port %d%s"
               % (PORT, " · config %s" % cfg_src if cfg_src else ""))
    for fn in (gpu_thread, host_thread, llama_swap_poll, sse_relay,
              logs_thread, strata_thread, kpi_thread):
        threading.Thread(target=fn, daemon=True).start()
    # NInfer backend poller starts once /running reports a proxy; run one
    # generic poller that follows the active NInfer entry.
    threading.Thread(target=ninfer_backend_poll, daemon=True).start()
    ALERTS.update(cfg.get("alerts") or spec_config.DEFAULTS["alerts"])
    threading.Thread(target=alerts_thread, daemon=True).start()
    # Adapter scheduler beside the legacy threads.
    start_scheduler(cfg)

    srv = ThreadingHTTPServer((host, PORT), Handler)
    srv.daemon_threads = True
    print("speculum collector on http://%s:%d" % (host, PORT), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        HIST.close()                   # flush pending rows, close the db


def ninfer_serve_port():
    """The port of a running ninfer-serve process (started by hand, not by llama-swap), or None.
    ninfer-serve listens on --port, default 8080 (its serve_options). On
    Windows the process argv is not available, so a found process resolves
    to the default port."""
    for p in hostinfo.processes():
        if p["name"] != "ninfer-serve":
            continue
        port = ninfer_port_of(p["cmd"].split())
        if port:
            return port
    return None


def ninfer_port_of(args):
    """ninfer-serve's listening port from its argv: --port N / --port=N, else its default 8080."""
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
        if a.startswith("--port=") and a.split("=", 1)[1].isdigit():
            return int(a.split("=", 1)[1])
    return 8080


def ninfer_backend():
    """(model, backend URL) of a live ninfer-serve: llama-swap's proxy for its NInfer entry, else a
    ninfer-serve process run by hand; (None, None) when there is none."""
    try:
        running = jget(LLAMA_SWAP + "/running", timeout=PROBE_FAST).get("running", [])
    except Exception:
        running = []
    for r in running:
        if "ninfer" in (r.get("cmd") or "") or "NInfer" in (r.get("model") or ""):
            return (r.get("model") or "NInfer",
                    (r.get("proxy") or "").replace("localhost", "127.0.0.1") or None)
    port = ninfer_serve_port()
    return ("NInfer", "http://127.0.0.1:%d" % port) if port else (None, None)


def drop_ninfer():
    """NInfer has a card only while ninfer-serve answers /metrics (operator, 2026-10-03)."""
    with lock:
        S.engines.pop("ninfer", None)
        S.eng_hist.pop("ninfer", None)


def ninfer_backend_poll():
    """Poll /metrics + /slots of the live ninfer-serve, whether llama-swap or a person started it."""
    label = None
    prev = None
    prev_t = None
    while True:
        model, backend = ninfer_backend()
        if backend is None:
            drop_ninfer()
            label = prev = prev_t = None
            time.sleep(2.0)
            continue
        if model != label:
            label = model
            prev = prev_t = None
            push_event("info", "NInfer backend · %s · %s" % (model, backend))
        window = 262144
        counters = None
        slots = []
        rates = None
        t = now()
        try:
            counters = parse_metrics(http_get_text(backend + "/metrics", timeout=PROBE_FAST))
        except Exception:
            pass
        try:
            slots = json.loads(http_get_text(backend + "/slots", timeout=PROBE_FAST))
        except Exception:
            pass
        if counters is not None and prev is not None and prev_t is not None and t - prev_t > 0.5:
            dt = t - prev_t
            rates = {}
            dp_s = counters.get("llamacpp:prompt_seconds_total", 0) - prev.get("llamacpp:prompt_seconds_total", 0)
            dp = counters.get("llamacpp:prompt_tokens_total", 0) - prev.get("llamacpp:prompt_tokens_total", 0)
            dd_s = counters.get("llamacpp:tokens_predicted_seconds_total", 0) - prev.get("llamacpp:tokens_predicted_seconds_total", 0)
            dd = counters.get("llamacpp:tokens_predicted_total", 0) - prev.get("llamacpp:tokens_predicted_total", 0)
            if dp_s > 0:
                rates["prefill_tps"] = round(dp / dp_s, 1)
            if dd_s > 0:
                rates["decode_tps"] = round(dd / dd_s, 1)
            rates["gen_tps_inst"] = round(dd / dt, 1)
        ses = []
        for s in slots:
            if isinstance(s, dict):
                ses.append({
                    "id": "s%d" % s.get("id", 0),
                    "session": (s.get("session_digest") or "")[:8] or "slot",
                    "window": s.get("n_ctx"),
                    "used": s.get("n_prompt_tokens"),
                    "cached": s.get("n_prompt_tokens_cache"),
                    "processing": bool(s.get("is_processing")),
                    "retained": s.get("retained"),
                })
        if counters is None:              # not serving metrics (yet): no card, not a "down" one
            drop_ninfer()
            prev = prev_t = None
            time.sleep(2.0)
            continue
        e = S.engine("ninfer", model, "ninfer", window)
        with lock:
            e["backend"] = backend
            if counters is not None:
                e["up"] = True
                e["counters"] = counters
                e["queue"] = int(counters.get("llamacpp:requests_processing", 0)
                                 + counters.get("llamacpp:requests_deferred", 0))
                d = counters.get("ninfer:draft_tokens_total") or 0
                da = counters.get("ninfer:draft_accepted_tokens_total") or 0
                e["mtp"] = round(100.0 * da / d, 1) if d else None
                clear_alert("backend %s unreachable" % model)
            e["rates"] = rates
            e["slots"] = ses
            e["sessions"] = ses
        prev = counters
        prev_t = t
        time.sleep(1.0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    main()
