#!/usr/bin/env python3
"""Speculum collector — live data feed for the glass dashboard.

Serves the static dashboard from the repo root plus two API endpoints:

    GET /api/snapshot   one-shot full JSON snapshot (history + requests + state)
    GET /api/stream     SSE stream: `tick` at 1 Hz, plus push events
                        (`req`, `ev`, `alert`) when something happens

Sources (each optional, degrades cleanly):

    GPU      ONE long-lived nvidia-smi subprocess (-lms 1000), read line by
             line. The collector never opens a CUDA/NVML context itself.
    host     /proc/stat (total + per core), /proc/meminfo, /proc/loadavg,
             /proc/uptime, VmRSS of llama-server / ninfer-serve / strata.
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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

PROC_PATTERNS = ("llama-server", "ninfer-serve", "strata")

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
    return ev


def add_request(rec):
    """Append one request record (newest first). Dedupe by id if present."""
    with lock:
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
# host: /proc sampling
# --------------------------------------------------------------------------

def read_cpu_times():
    cores, total = [], None
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu"):
                    parts = line.split()
                    if not parts[1:]:
                        continue
                    idle = int(parts[4]) + (int(parts[5]) if len(parts) > 5 else 0)
                    total_v = sum(int(x) for x in parts[1:])
                    vals = (total_v, idle)
                    if line.startswith("cpu "):
                        total = vals
                    else:
                        cores.append(vals)
    except OSError:
        pass
    return total, cores


def read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
    except OSError:
        pass
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return {
        "ram_total_gb": total / 1e6,
        "ram_used_gb": max(0.0, (total - avail)) / 1e6 if total else 0.0,
        "swap_total_gb": info.get("SwapTotal", 0) / 1e6,
        "swap_used_gb": (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1e6,
    }


def scan_procs():
    found = []
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read(512).replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        name = None
        if "llama-server" in cmd:
            name = "llama-server"
        elif "ninfer-serve" in cmd:
            name = "ninfer-serve"
        elif "server.py" in cmd and "strata" in cmd:
            name = "strata"
        if name is None:
            continue
        rss_kb = None
        try:
            with open("/proc/%s/status" % pid) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
        except OSError:
            pass
        found.append({"name": name, "pid": int(pid),
                      "rss_gb": round(rss_kb / 1e6, 2) if rss_kb else None,
                      "cmd": cmd[:120]})
    return found


def host_thread():
    prev_total, prev_cores = None, []
    procs = []
    next_scan = 0.0
    while True:
        t = now()
        total, cores = read_cpu_times()
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
        sample.update(read_meminfo())
        try:
            sample["load"] = [float(x) for x in open("/proc/loadavg").read().split()[:3]]
        except OSError:
            pass
        try:
            sample["uptime_s"] = float(open("/proc/uptime").read().split()[0])
        except OSError:
            pass
        if t >= next_scan:
            procs = scan_procs()
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
    e = S.engine("strata", "Strata", "strata", 131072)
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
        if running is not None:
            for r in running:
                model = r.get("model") or "model"
                proxy = r.get("proxy") or ""
                backend = proxy.replace("localhost", "127.0.0.1") or None
                if "ninfer" in r.get("cmd", "") or "NInfer" in model:
                    key, label, origin, window = "ninfer", model, "ninfer", None
                else:
                    key, label, origin, window = "llama", model, "llama", None
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


def log_line(line, e):
    m = REQ_RE.search(line)
    if m and e is not None and e.get("origin") == "ninfer":
        rid = int(m.group(1))
        rec = {"id": "req#%d" % rid, "t": now(), "model": e.get("label"),
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

def strata_thread():
    while True:
        up = True
        models, metrics, slots = [], {}, []
        for path, sink in (("/v1/models", "models"), ("/metrics", "metrics"), ("/slots", "slots")):
            try:
                if path == "/v1/models":
                    models = jget(STRATA + path, timeout=PROBE_SLOW).get("data", [])
                elif path == "/metrics":
                    metrics = parse_metrics(http_get_text(STRATA + path, timeout=PROBE_SLOW))
                else:
                    slots = json.loads(http_get_text(STRATA + path, timeout=PROBE_SLOW))
            except Exception:
                up = False
                break
        e = S.engine("strata", "Strata", "strata", 131072)
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
        if not up:
            e.pop("counters", None)
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


def kpi_thread():
    while True:
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
        time.sleep(1.0)


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
                "counters": e.get("counters"),
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
                "counters": e.get("counters"),
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
        path = self.path.split("?", 1)[0]
        if path == "/api/snapshot":
            try:
                self._json(snapshot())
            except Exception as e:
                self._json({"error": str(e)}, 500)
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


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    PORT = args.port

    push_event("info", "collector up · port %d" % args.port)
    for fn in (gpu_thread, host_thread, llama_swap_poll, sse_relay,
              logs_thread, strata_thread, kpi_thread):
        threading.Thread(target=fn, daemon=True).start()
    # NInfer backend poller starts once /running reports a proxy; run one
    # generic poller that follows the active NInfer entry.
    threading.Thread(target=ninfer_backend_poll, daemon=True).start()

    srv = ThreadingHTTPServer((HOST, args.port), Handler)
    srv.daemon_threads = True
    print("speculum collector on http://%s:%d" % (HOST, args.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def ninfer_backend_poll():
    """Poll /metrics + /slots of whichever NInfer backend llama-swap is proxying."""
    label = None
    prev = None
    prev_t = None
    while True:
        running = []
        try:
            running = jget(LLAMA_SWAP + "/running", timeout=PROBE_FAST).get("running", [])
        except Exception:
            time.sleep(2.0)
            continue
        entry = None
        for r in running:
            if "ninfer" in (r.get("cmd") or "") or "NInfer" in (r.get("model") or ""):
                entry = r
                break
        if entry is None:
            e = S.engine("ninfer", "NInfer", "ninfer")
            with lock:
                e["up"] = False
                e["backend"] = None
                e["rates"] = None
                e["sessions"] = []
            time.sleep(2.0)
            continue
        model = entry.get("model") or "NInfer"
        if model != label:
            label = model
            prev = prev_t = None
            push_event("info", "NInfer backend · %s · %s" % (model, entry.get("proxy")))
        backend = (entry.get("proxy") or "").replace("localhost", "127.0.0.1")
        window = 262144
        e = S.engine("ninfer", model, "ninfer", window)
        e["backend"] = backend or None
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
        with lock:
            if counters is not None:
                e["up"] = True
                e["counters"] = counters
                e["queue"] = int(counters.get("llamacpp:requests_processing", 0)
                                 + counters.get("llamacpp:requests_deferred", 0))
                d = counters.get("ninfer:draft_tokens_total") or 0
                da = counters.get("ninfer:draft_accepted_tokens_total") or 0
                e["mtp"] = round(100.0 * da / d, 1) if d else None
                clear_alert("backend %s unreachable" % model)
            else:
                e["up"] = False
                add_alert("backend %s unreachable" % model)
            e["rates"] = rates
            e["slots"] = ses
            e["sessions"] = ses
        prev = counters
        prev_t = t
        time.sleep(1.0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    main()
