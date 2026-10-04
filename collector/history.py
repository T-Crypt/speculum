#!/usr/bin/env python3
"""Speculum history — SQLite persistence (DESIGN.md section 2, phase 2).

Stdlib only. One daemon writer thread commits every 10 s in a single
transaction; producer threads (pollers, KPI pass) only append to small
bounded in-memory lists under one small lock and return immediately.

File: ~/.local/share/speculum/speculum.db on Linux (XDG_DATA_HOME honoured),
%LOCALAPPDATA%\\Speculum\\speculum.db on Windows. Overridable by `path`
(tests use a temp dir).

On create the file gets PRAGMA auto_vacuum=INCREMENTAL (before the first
table), journal_mode=WAL, synchronous=NORMAL and PRAGMA user_version = 3.

Tables (time columns are INTEGER epoch seconds, rates are REAL):

    requests      t, engine, model, prompt, cached, fresh, output, ttft_ms,
                  total_ms, queue_ms, decode_tps, prefill_tps, mtp_acc,
                  mtp_tot, status, client
    rollup_1m     minute, engine, requests, prompt, cached, output,
                  decode_tps_avg, decode_tps_max, queue_max, gpu_w_avg,
                  vram_max
    rollup_1h     hour, engine, same sums as rollup_1m   (kept 400 days)
    model_spans   engine, model, loaded_at, unloaded_at
    events        t, level, engine, message    (capped at 50,000 rows)

Pruning runs once ~60 s after start and then once a day: rows older than
retention_days are dropped from requests / rollup_1m / model_spans /
events, rollup_1h older than 400 days, then PRAGMA incremental_vacuum.

requests carries a UNIQUE index on (t, engine, model, prompt, output)
(schema v2) and rows are INSERT OR IGNORE: the engine's activity feed
replays its recent history on every collector restart, and the in-memory
dedupe does not survive that, so duplicates are dropped at the table
level. rollup_1m / rollup_1h carry UNIQUE keys on (minute|hour, engine)
(schema v3): token sums ride the INSERT OR IGNORE of the request rows
(upserting into the minute's row only when rowcount == 1, so a replayed
duplicate never re-counts) and sample stats are upserted into the same
row when the minute closes. An hour aggregates only once its last
request row could have arrived (closed + 3600+130 s, +300 s margin), as
a recompute-upsert, so a long suspend self-heals on the next pass.
Existing v1/v2 databases are migrated on open (duplicates collapsed to
the lowest rowid, indexes added, user_version bumped).

Documented choices the spec left open:

* model_spans open at collector start (unloaded_at IS NULL) are closed at
  their loaded_at on startup — the load instant is the last time that span
  was known, so a span never appears to cover the collector's downtime.
  A span still open at the current moment therefore has zero duration.
* A request row is written only once its record is older than the 120 s
  merge window of speculum.add_request (+10 s margin = 130 s): the
  in-memory record is mutated in place when a duplicate source arrives,
  so holding the dict reference and reading it after the window closes
  captures the merged fields. Pending records are written anyway at
  shutdown / flush(), so a short-lived collector still keeps every row.
* Per-minute rollup accumulators are cleared when the minute's rows are
  written, so memory stays flat. The writer survives sqlite errors: they
  are reported through the log callback and the cycle is retried after a
  2..60 s backoff. Readers open their own connection per call
  (check_same_thread off, WAL) and never touch the writer's.
"""

import csv
import io
import os
import platform
import sqlite3
import sys
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 3   # v3: unique (minute|hour, engine) rollup keys + upserts
COMMIT_S = 10.0            # writer commits every 10 s (DESIGN.md §2)
MERGE_HOLD_S = 130.0       # 120 s speculum merge window + margin
HOUR_AGG_DELAY_S = 300.0   # an hour's last request lands 3600+130 s in;
                           # aggregate it only this long after the hour closes
EXPORT_CAP = 50_000        # export row cap; truncation is reported, not silent
ENQUEUE_CAP = 5000         # bounded enqueue lists; drop oldest, count drops
EVENTS_CAP = 50_000        # events table cap, enforced during prune
HOUR_RETENTION_D = 400     # rollup_1h kept 400 days (DESIGN.md §2)
PRUNE_S = 86_400.0         # then prune once a day
PRUNE_DELAY_S = 60.0       # and once ~60 s after start
_EST_REQ_B = 250           # first-estimate bytes per request row (DESIGN.md)
_EST_ROLLUP_B = 100        # and per rollup row, until a day is measured
_EST_REQS_PER_DAY = 500    # DESIGN.md §2 sizing example
_EST_MINUTES_UP = 3 * 1440  # three engines up all day

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    t INTEGER NOT NULL, engine TEXT, model TEXT,
    prompt INTEGER, cached INTEGER, fresh INTEGER, output INTEGER,
    ttft_ms REAL, total_ms REAL, queue_ms REAL,
    decode_tps REAL, prefill_tps REAL,
    mtp_acc INTEGER, mtp_tot INTEGER,
    status TEXT, client TEXT
);
CREATE TABLE IF NOT EXISTS rollup_1m (
    minute INTEGER NOT NULL, engine TEXT NOT NULL,
    requests INTEGER, prompt INTEGER, cached INTEGER, output INTEGER,
    decode_tps_avg REAL, decode_tps_max REAL,
    queue_max REAL, gpu_w_avg REAL, vram_max REAL
);
CREATE TABLE IF NOT EXISTS rollup_1h (
    hour INTEGER NOT NULL, engine TEXT NOT NULL,
    requests INTEGER, prompt INTEGER, cached INTEGER, output INTEGER,
    decode_tps_avg REAL, decode_tps_max REAL,
    queue_max REAL, gpu_w_avg REAL, vram_max REAL
);
CREATE TABLE IF NOT EXISTS model_spans (
    id INTEGER PRIMARY KEY, engine TEXT NOT NULL, model TEXT,
    loaded_at INTEGER NOT NULL, unloaded_at INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    t INTEGER NOT NULL, level TEXT, engine TEXT, message TEXT
);
-- restarts replay the activity feed, so (t, engine, model, prompt, output)
-- must be unique; COALESCE pins NULLs so the UNIQUE constraint still bites
CREATE UNIQUE INDEX IF NOT EXISTS ix_requests_dedupe ON requests (
    t, COALESCE(engine, ''), COALESCE(model, ''),
    COALESCE(prompt, 0), COALESCE(output, 0)
);
CREATE INDEX IF NOT EXISTS ix_requests_t   ON requests (t);
CREATE INDEX IF NOT EXISTS ix_requests_eng ON requests (engine);
CREATE INDEX IF NOT EXISTS ix_r1m_minute   ON rollup_1m (minute);
CREATE INDEX IF NOT EXISTS ix_r1m_engine   ON rollup_1m (engine);
CREATE INDEX IF NOT EXISTS ix_r1h_hour     ON rollup_1h (hour);
CREATE INDEX IF NOT EXISTS ix_r1h_engine   ON rollup_1h (engine);
-- v3: rollups are upserted on (minute|hour, engine), so those must be unique
CREATE UNIQUE INDEX IF NOT EXISTS ix_r1m_key ON rollup_1m (minute, engine);
CREATE UNIQUE INDEX IF NOT EXISTS ix_r1h_key ON rollup_1h (hour, engine);
CREATE INDEX IF NOT EXISTS ix_spans_loaded ON model_spans (loaded_at);
CREATE INDEX IF NOT EXISTS ix_spans_engine ON model_spans (engine);
CREATE INDEX IF NOT EXISTS ix_events_t     ON events (t);
"""

_REQ_COLS = ("engine", "model", "prompt", "cached", "fresh", "output",
             "ttft_ms", "total_ms", "queue_ms", "decode_tps", "prefill_tps",
             "mtp_acc", "mtp_tot", "status", "client")
_R1M_COLS = ("requests", "prompt", "cached", "output", "decode_tps_avg",
             "decode_tps_max", "queue_max", "gpu_w_avg", "vram_max")
_R1H_COLS = _R1M_COLS
_R1H_AGG = {  # rollup_1m column -> SQL expression over the hour's minutes
    "requests": "SUM(requests)", "prompt": "SUM(prompt)",
    "cached": "SUM(cached)", "output": "SUM(output)",
    "decode_tps_avg": "ROUND(AVG(decode_tps_avg), 1)",
    "decode_tps_max": "MAX(decode_tps_max)",
    "queue_max": "MAX(queue_max)", "gpu_w_avg": "ROUND(AVG(gpu_w_avg), 1)",
    "vram_max": "MAX(vram_max)",
}

RANGES = {
    "6h": 6 * 3600, "24h": 24 * 3600,
    "7d": 7 * 86400, "30d": 30 * 86400,
}


def default_db_path():
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Speculum" / "speculum.db"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "speculum" / "speculum.db"


def _row_to_dict(cols, row):
    return {k: v for k, v in zip(cols, row) if v is not None}


def _new_acc():
    return {"requests": 0, "prompt": 0, "cached": 0, "output": 0,
            "dec_sum": 0.0, "dec_n": 0, "dec_max": None,
            "queue_max": None, "gw_sum": 0.0, "gw_n": 0,
            "vram_max": None, "up": False}


class _Noop:
    """retention_days == 0 (history off): same methods, nothing written,
    no thread, no file created."""

    retention_days = 0
    drops = 0
    path = None
    _enabled = False

    def add_request(self, rec):
        pass

    def sample(self, t, engines, gpu_last=None):
        pass

    def model_loaded(self, engine, model, t):
        pass

    def model_unloaded(self, engine, model, t):
        pass

    def add_event(self, t, level, engine=None, msg=""):
        pass

    def flush(self):
        pass

    def close(self):
        pass

    def history(self, range_, engine=None):
        return []

    def requests(self, since, limit=1000):
        return []

    def spans(self, range_):
        return []

    def leaderboard(self, range_):
        return []

    def prune_now(self):
        return False

    def storage(self):
        return {"enabled": False, "path": None, "bytes": 0,
                "retention_days": 0, "rows": {},
                "first_t": None, "bytes_per_day": None,
                "basis": None, "projection": {}}

    def export(self, fmt, range_):
        if range_ not in RANGES:
            raise ValueError("range must be one of %s" % sorted(RANGES))
        return [], False


class History:
    """One SQLite writer thread + cheap enqueue methods. See module doc.

    start_thread=False (tests) skips the daemon writer so the commit cycle
    can be driven deterministically with _cycle() / _run_once()."""

    def __init__(self, path=None, retention_days=30, log=None, clock=None,
                 start_thread=True):
        self._clock = clock or time.time
        self._log_cb = log or (lambda m: print("speculum history: " + m,
                                               file=sys.stderr))
        self.retention_days = int(retention_days or 0)
        self.drops = 0
        self._stop = threading.Event()
        self._wake = threading.Event()     # lets close() end the writer sleep
        self._db_lock = threading.Lock()   # serialises writer vs flush()
        self._reqs = []                    # dict refs, held 130 s
        self._spans = []                   # (kind, engine, model, t)
        self._events = []                  # (t, level, engine, msg)
        self._acc = {}                     # minute -> {engine -> acc}
        self._last_minute = None
        if self.retention_days <= 0:
            self._enabled = False
            self.path = None
            return
        self._enabled = True
        self._lock = threading.Lock()
        self.path = Path(path).expanduser() if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._next_prune = self._clock() + PRUNE_DELAY_S
        self._last_err = None

        self._db = self._open_db()
        self._close_open_spans()

        self._thread = None
        if start_thread:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.name = "speculum-history"
            self._thread.start()

    # ------------------------------------------------------------- db setup

    def _run_once(self):
        """One writer iteration without the sleep: used by the writer
        thread and by tests. Never raises: errors are logged, the next
        iteration retries (the thread backs off, a direct call does not)."""
        try:
            self._cycle()
        except Exception as e:
            if self._stop.is_set():
                return
            self._last_err = str(e)
            self._log_cb("history: writer: %s" % e)

    def _open_db(self):
        new = not self.path.exists()
        conn = sqlite3.connect(str(self.path), timeout=10.0,
                               check_same_thread=False)
        try:
            if new:
                # auto_vacuum must be set before the first table exists
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            if new:
                conn.executescript(_SCHEMA)
                conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            else:
                ver = conn.execute(
                    "PRAGMA user_version").fetchone()[0]
                if ver < SCHEMA_VERSION:
                    self._migrate(conn, ver)
            conn.commit()
        except BaseException:
            conn.close()
            raise
        return conn

    def _migrate(self, conn, ver):
        """Bring an existing database up to the current schema. v1 -> v2
        drops the duplicates that restart feed-replays accumulated (keeping
        the lowest rowid of each dedupe key) and adds the unique index so
        future INSERT OR IGNORE calls silently drop replayed rows. v2 -> v3
        collapses the rollup duplicates the old plain-INSERT flush
        accumulated (keeping the lowest rowid of each (minute|hour, engine))
        so the new UNIQUE key indexes can be created. The deletes run
        first: creating a UNIQUE index over duplicate rows would fail."""
        if ver < 2:
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'requests'").fetchone():
                conn.execute(
                    "DELETE FROM requests WHERE rowid NOT IN "
                    "(SELECT MIN(rowid) FROM requests GROUP BY t, "
                    "COALESCE(engine, ''), COALESCE(model, ''), "
                    "COALESCE(prompt, 0), COALESCE(output, 0))")
        if ver < 3:
            for table, key in (("rollup_1m", "minute"),
                               ("rollup_1h", "hour")):
                if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = ?", (table,)).fetchone():
                    conn.execute(
                        "DELETE FROM %s WHERE rowid NOT IN "
                        "(SELECT MIN(rowid) FROM %s GROUP BY %s, engine)"
                        % (table, table, key))
        conn.executescript(_SCHEMA)   # idempotent: all IF NOT EXISTS
        conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

    def _close_open_spans(self):
        """Spans left open by a previous run: close at the previous run's last
        sign of life, the end of its newest minute rollup (written every minute,
        idle ones too), or at loaded_at when there is none - never inside the
        collector's own downtime. Closing at loaded_at alone turned every load
        that outlived a collector restart into a zero-length span."""
        try:
            with self._db:
                self._db.execute(
                    "UPDATE model_spans SET unloaded_at = MAX(loaded_at, COALESCE("
                    "(SELECT MAX(minute) + 60 FROM rollup_1m), loaded_at)) "
                    "WHERE unloaded_at IS NULL")
        except sqlite3.Error as e:
            self._log_cb("history: close-open-spans: %s" % e)

    # ---------------------------------------------------------- producer API
    # Cheap: one small append under one small lock, then return.

    def add_request(self, rec):
        """Hold a reference; the row is written once rec['t'] is older than
        the 120 s merge window (+margin), so fields merged into the same
        dict later by speculum.add_request are captured."""
        if not self._enabled or rec is None:
            return
        with self._lock:
            self._reqs.append(rec)
            if len(self._reqs) > ENQUEUE_CAP:
                self.drops += 1
                self._reqs.pop(0)

    def sample(self, t, engines, gpu_last=None):
        """1 Hz, outside the State lock: `engines` is {key: plain dict},
        `gpu_last` a plain dict or None. Folds into the current minute's
        per-engine accumulators (rate/gpu stats); request token sums come
        from add_request rows via the writer cycle."""
        if not self._enabled:
            return
        minute = int(t // 60.0) * 60
        gpu_w = vram = None
        if gpu_last:
            gpu_w = gpu_last.get("power.draw")
            vram = gpu_last.get("memory.used_gb")
        try:
            with self._lock:
                m = self._acc.setdefault(minute, {})
                for key, e in (engines or {}).items():
                    if not isinstance(e, dict) or not e.get("up"):
                        continue
                    a = m.setdefault(key, _new_acc())
                    a["up"] = True
                    rates = e.get("rates") or {}
                    v = rates.get("decode_tps")
                    if v is None:
                        v = rates.get("gen_tps_inst")
                    if v is not None and v > 0:
                        a["dec_sum"] += v
                        a["dec_n"] += 1
                        if a["dec_max"] is None or v > a["dec_max"]:
                            a["dec_max"] = v
                    q = e.get("queue") or 0
                    if q and (a["queue_max"] is None or q > a["queue_max"]):
                        a["queue_max"] = float(q)
                    if gpu_w is not None:
                        a["gw_sum"] += gpu_w
                        a["gw_n"] += 1
                    if vram is not None and (a["vram_max"] is None
                                             or vram > a["vram_max"]):
                        a["vram_max"] = vram
                # bound the accumulator map: keep the current minute plus
                # at most four unclosed ones (the writer closes them ~10 s
                # after the minute boundary)
                keys = sorted(self._acc)
                while len(keys) > 5:
                    k = keys.pop(0)
                    if k >= minute:
                        break
                    del self._acc[k]
        except Exception as e:  # never take the feed down
            self._log_cb("history: sample: %s" % e)

    def model_loaded(self, engine, model, t):
        if self._enabled:
            with self._lock:
                self._spans.append(("load", engine, model, t))
                if len(self._spans) > ENQUEUE_CAP:
                    self.drops += 1
                    self._spans.pop(0)

    def model_unloaded(self, engine, model, t):
        if self._enabled:
            with self._lock:
                self._spans.append(("unload", engine, model, t))
                if len(self._spans) > ENQUEUE_CAP:
                    self.drops += 1
                    self._spans.pop(0)

    def add_event(self, t, level, engine=None, msg=""):
        if self._enabled:
            with self._lock:
                self._events.append((t, level, engine, str(msg)[:500]))
                if len(self._events) > ENQUEUE_CAP:
                    self.drops += 1
                    self._events.pop(0)

    # --------------------------------------------------------- writer thread

    def _run(self):
        backoff = COMMIT_S
        while True:
            if self._stop.is_set():
                return
            try:
                self._cycle()
                backoff = COMMIT_S
            except Exception as e:  # sqlite or not: keep running
                if self._stop.is_set():
                    return
                self._last_err = str(e)
                backoff = min(backoff * 2, 60.0)
                self._log_cb("history: writer: %s (backoff %.0fs)"
                             % (e, backoff))
            self._wake.wait(backoff)
            self._wake.clear()

    def _cycle(self):
        """One commit: pending requests past the merge window, closed
        minutes, spans and events, then hourly aggregation and (once a
        day) prune. All data writes happen in one transaction; if it
        fails, the data goes back to the queues and the cycle raises."""
        now = self._clock()
        cutoff = now - MERGE_HOLD_S
        with self._lock:
            due = [r for r in self._reqs
                   if (r.get("t") or 0) is not None
                   and (r.get("t") or 0) <= cutoff]
            if due:
                self._reqs = [r for r in self._reqs if r not in due]
            spans, self._spans = self._spans, []
            events, self._events = self._events, []
            # the still-open minute's accumulator carries over: it is
            # flushed exactly once, on the first cycle past its end, so
            # its samples are never dropped and never double-merged
            last_minute = self._last_minute
            self._last_minute = int(now // 60.0) * 60
            acc_all, self._acc = self._acc, {}
            acc = {m: e for m, e in acc_all.items()
                   if m < self._last_minute}
            keep = {m: e for m, e in acc_all.items()
                    if m >= self._last_minute}
            if keep:
                self._acc = keep
            next_prune, self._next_prune = self._next_prune, self._next_prune + PRUNE_S
        if due or spans or events or acc:
            try:
                with self._db_lock, self._db:  # one transaction per commit
                    self._write_rows(due, spans, events)
                    self._flush_minutes(acc)
            except sqlite3.Error:
                # uncommitted: put the data back and let the next cycle retry
                with self._lock:
                    self._reqs = due + self._reqs
                    self._spans = spans + self._spans
                    self._events = events + self._events
                    self._acc = acc_all
                    self._last_minute = last_minute
                raise
        # best-effort, never fails the cycle
        try:
            self._aggregate_hours()
        except Exception as e:
            self._log_cb("history: hourly aggregate: %s" % e)
        if now >= next_prune:
            try:
                self._prune()
            except Exception as e:
                self._log_cb("history: prune: %s" % e)

    def _write_rows(self, reqs, spans, events):
        for r in reqs or ():
            # per-row (not executemany): cursor.rowcount says whether this
            # OR IGNORE really inserted; rows a restart replayed from the
            # engine's activity feed are ignored and must NOT re-count
            # into the rollup
            ins = self._db.execute(
                "INSERT OR IGNORE INTO requests (t, %s) VALUES (?, %s)"
                % (", ".join(_REQ_COLS),
                   ", ".join("?" * len(_REQ_COLS))),
                (r.get("t"),) + self._req_values(r))
            if ins.rowcount != 1:
                continue
            engine = r.get("origin")
            if engine is None:
                continue  # rollup_1m.engine is NOT NULL: row stays in requests only
            t = r.get("t") or 0
            # token sums ride the INSERT: the row lands 130 s after its
            # minute closed, far after the sample-derived row was written
            self._db.execute(
                "INSERT INTO rollup_1m "
                "(minute, engine, requests, prompt, cached, output) "
                "VALUES (?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(minute, engine) DO UPDATE SET "
                "requests = COALESCE(rollup_1m.requests, 0) + 1, "
                "prompt = COALESCE(rollup_1m.prompt, 0) "
                "         + COALESCE(excluded.prompt, 0), "
                "cached = COALESCE(rollup_1m.cached, 0) "
                "         + COALESCE(excluded.cached, 0), "
                "output = COALESCE(rollup_1m.output, 0) "
                "         + COALESCE(excluded.output, 0)",
                (int(t // 60.0) * 60, engine,
                 r.get("prompt"), r.get("cache"), r.get("output")))
        for kind, engine, model, t in spans:
            if kind == "load":
                self._db.execute(
                    "INSERT INTO model_spans (engine, model, loaded_at) "
                    "VALUES (?, ?, ?)", (engine, model, t))
            else:
                # SQLite UPDATE has no ORDER BY/LIMIT: target the newest
                # still-open span of (engine, model) via a rowid subquery
                self._db.execute(
                    "UPDATE model_spans SET unloaded_at = ? "
                    "WHERE rowid IN (SELECT rowid FROM model_spans "
                    "WHERE engine = ? AND model IS ? AND unloaded_at IS NULL "
                    "ORDER BY loaded_at DESC LIMIT 1)",
                    (t, engine, model))
        if events:
            self._db.executemany(
                "INSERT INTO events (t, level, engine, message) "
                "VALUES (?, ?, ?, ?)", events)

    @staticmethod
    def _req_values(r):
        def ms(v):
            return v * 1000.0 if v is not None else None
        return (r.get("origin"), r.get("model"), r.get("prompt"),
                r.get("cache"), r.get("fresh"), r.get("output"),
                ms(r.get("ttft_s")), ms(r.get("total_s")),
                ms(r.get("queue_s")),
                r.get("decode_tps"), r.get("prefill_tps"),
                r.get("mtp_acc"), r.get("mtp_tot"),
                r.get("status"), r.get("client"))

    def _flush_minutes(self, acc, partial=False):
        """Upsert the closed minutes' rollup_1m rows with the sample-derived
        rate, queue and GPU stats, combining with any row the request
        upserts already wrote: maxima merge with MAX, averages keep the
        new value if the old one is NULL, else average the two. The token
        columns are never touched here — they ride the request-row
        upserts in _write_rows. A minute is flushed exactly once: on the
        first cycle whose last_minute has moved past it (re-flushing the
        still-open minute every cycle would merge partial accumulators
        and bias the averages). Only engines that were up in the minute
        get a row; the still-open minute is written only at shutdown
        (partial=True), so a short-lived collector does not lose it."""
        for minute in sorted(acc):
            if minute >= self._last_minute and not (
                    partial and minute == self._last_minute):
                continue          # current partial minute stays in memory
            rows = []
            for engine in sorted(acc[minute]):
                a = acc[minute][engine]
                if not a["up"]:
                    continue
                rows.append((minute, engine,
                             round(a["dec_sum"] / a["dec_n"], 1)
                             if a["dec_n"] else None,
                             a["dec_max"], a["queue_max"],
                             round(a["gw_sum"] / a["gw_n"], 1)
                             if a["gw_n"] else None,
                             a["vram_max"]))
            if rows:
                self._db.executemany(
                    "INSERT INTO rollup_1m "
                    "(minute, engine, decode_tps_avg, decode_tps_max, "
                    "queue_max, gpu_w_avg, vram_max) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(minute, engine) DO UPDATE SET "
                    "decode_tps_avg = CASE "
                    "  WHEN rollup_1m.decode_tps_avg IS NULL "
                    "  THEN excluded.decode_tps_avg "
                    "  WHEN excluded.decode_tps_avg IS NULL "
                    "  THEN rollup_1m.decode_tps_avg "
                    "  ELSE ROUND((rollup_1m.decode_tps_avg "
                    "             + excluded.decode_tps_avg) / 2.0, 1) END, "
                    "decode_tps_max = MAX(rollup_1m.decode_tps_max, "
                    "                    excluded.decode_tps_max), "
                    "queue_max = MAX(rollup_1m.queue_max, excluded.queue_max), "
                    "gpu_w_avg = CASE "
                    "  WHEN rollup_1m.gpu_w_avg IS NULL "
                    "  THEN excluded.gpu_w_avg "
                    "  WHEN excluded.gpu_w_avg IS NULL "
                    "  THEN rollup_1m.gpu_w_avg "
                    "  ELSE ROUND((rollup_1m.gpu_w_avg + excluded.gpu_w_avg) "
                    "              / 2.0, 1) END, "
                    "vram_max = MAX(rollup_1m.vram_max, excluded.vram_max)",
                    rows)

    def _aggregate_hours(self):
        """Aggregate closed hours' rollup_1m rows into rollup_1h with
        SQL, as recompute-upserts on (hour, engine): a repeat recomputes
        the same sums, and a late minute-row still lands. An hour is only
        eligible once its last request row could have arrived (the hour
        closed 3600 s ago + the 130 s merge hold, +HOUR_AGG_DELAY_S
        margin), so the hour still in progress is never cut short.
        After a suspend the pass self-heals: it walks back from the last
        aggregated hour (or the oldest rollup_1m minute, if rollup_1h is
        empty) to the latest eligible hour, capped at 400 days of hours."""
        now = self._clock()
        latest = int((now - 3600.0 - HOUR_AGG_DELAY_S) // 3600.0) * 3600
        with self._db_lock:
            m = self._db.execute(
                "SELECT MAX(hour) FROM rollup_1h").fetchone()[0]
            if m is not None:
                earliest = m - 3600
            else:
                m = self._db.execute(
                    "SELECT MIN(minute) FROM rollup_1m").fetchone()[0]
                if m is None:
                    return
                earliest = int(m // 3600.0) * 3600
            if latest < earliest or earliest < 0:
                return
            # cap a big gap (e.g. a long suspend) at 400 days of hours
            earliest = max(earliest,
                           latest - (HOUR_RETENTION_D * 24 - 1) * 3600)
            with self._db:
                for h in range(earliest, latest + 1, 3600):
                    self._db.execute(
                        "INSERT INTO rollup_1h (hour, engine, %s) "
                        "SELECT ?, engine, %s FROM rollup_1m "
                        "WHERE minute >= ? AND minute < ? "
                        "GROUP BY engine "
                        "ON CONFLICT(hour, engine) DO UPDATE SET %s"
                        % (", ".join(_R1H_COLS),
                           ", ".join(_R1H_AGG[c] for c in _R1H_COLS),
                           ", ".join("%s = excluded.%s"
                                     % (c, c) for c in _R1H_COLS)),
                        (h, h, h + 3600))

    def _prune(self):
        cut = int(self._clock() - self.retention_days * 86400.0)
        cut_h = int(self._clock() - HOUR_RETENTION_D * 86400.0)
        with self._db_lock:
            with self._db:
                for table, col in (("requests", "t"),
                                   ("rollup_1m", "minute"),
                                   ("model_spans", "loaded_at"),
                                   ("events", "t")):
                    self._db.execute(
                        "DELETE FROM %s WHERE %s < ?" % (table, col), (cut,))
                self._db.execute("DELETE FROM rollup_1h WHERE hour < ?",
                                 (cut_h,))
                over = (self._db.execute("SELECT COUNT(*) FROM events")
                        .fetchone()[0] - EVENTS_CAP)
                if over > 0:
                    self._db.execute(
                        "DELETE FROM events WHERE rowid IN "
                        "(SELECT rowid FROM events ORDER BY t LIMIT ?)",
                        (over,))
                try:
                    self._db.execute("PRAGMA incremental_vacuum")
                except sqlite3.Error:
                    pass

    # -------------------------------------------------------- explicit flush

    def flush(self):
        """Write everything pending right now — used at shutdown and in
        tests. Also writes request records still inside the 130 s merge
        window (a short-lived collector must not lose rows)."""
        if not self._enabled:
            return
        with self._lock:
            reqs, self._reqs = self._reqs, []
            spans, self._spans = self._spans, []
            events, self._events = self._events, []
            acc, self._acc = self._acc, {}
            self._last_minute = int(self._clock() // 60.0) * 60
        try:
            with self._db_lock, self._db:
                self._write_rows(reqs, spans, events)
                self._flush_minutes(acc, partial=True)
            self._aggregate_hours()
        except sqlite3.Error as e:
            with self._lock:
                self._reqs = reqs + self._reqs
                self._spans = spans + self._spans
                self._events = events + self._events
                self._acc = acc
            self._log_cb("history: flush: %s" % e)

    def close(self):
        if not self._enabled:
            return
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.flush()
        try:
            self._db.close()
        except Exception:
            pass
        self._enabled = False

    # --------------------------------------------------------------- queries
    # A read-only connection per call — the writer's connection is never
    # shared across threads.

    def _reader(self):
        return sqlite3.connect("file:%s?mode=ro" % self.path, uri=True,
                               timeout=5.0, check_same_thread=False)

    def history(self, range_, engine=None):
        """Rollups for the range: 6h/24h from rollup_1m, 7d/30d from
        rollup_1h. Optional engine-key filter. The current partial minute
        is not included (it is not a closed rollup yet)."""
        if not self._enabled:
            return []
        if range_ not in RANGES:
            raise ValueError("range must be one of %s" % sorted(RANGES))
        cutoff = int(self._clock() - RANGES[range_])
        table, tcol = (("rollup_1m", "minute") if range_ in ("6h", "24h")
                       else ("rollup_1h", "hour"))
        cols = [tcol, "engine"] + list(_R1M_COLS)
        q = "SELECT %s FROM %s WHERE %s >= ?" % (
            ", ".join(cols), table, tcol)
        args = [cutoff]
        if engine:
            q += " AND engine = ?"
            args.append(engine)
        q += " ORDER BY %s, engine" % tcol
        conn = self._reader()
        try:
            return [_row_to_dict(cols, r)
                    for r in conn.execute(q, args)]
        finally:
            conn.close()

    def spans(self, range_):
        """Model load spans that overlap the range (the load/unload timeline), oldest first. A span
        still loaded has unloaded_at None."""
        if not self._enabled:
            return []
        if range_ not in RANGES:
            raise ValueError("range must be one of %s" % sorted(RANGES))
        cutoff = int(self._clock() - RANGES[range_])
        conn = self._reader()
        try:
            rows = conn.execute(
                "SELECT engine, model, loaded_at, unloaded_at FROM model_spans "
                "WHERE unloaded_at IS NULL OR unloaded_at >= ? ORDER BY loaded_at",
                (cutoff,)).fetchall()
        finally:
            conn.close()
        # explicit keys (not _row_to_dict, which drops None): an open span says unloaded_at: null
        return [dict(zip(("engine", "model", "loaded_at", "unloaded_at"), r)) for r in rows]

    def leaderboard(self, range_):
        """Per (engine, model) over the range, from the request rows: requests, tokens, median decode
        tok/s, cache hit, MTP acceptance; plus the engine's tokens per watt-hour from the minute
        rollups (GPU power x 60 s over the minutes it served requests; whole-card power, idle draw
        included, so it measures this box, not the model alone). Busiest first."""
        if not self._enabled:
            return []
        if range_ not in RANGES:
            raise ValueError("range must be one of %s" % sorted(RANGES))
        cutoff = int(self._clock() - RANGES[range_])
        conn = self._reader()
        try:
            reqs = conn.execute(
                "SELECT engine, model, prompt, cached, output, decode_tps, mtp_acc, mtp_tot "
                "FROM requests WHERE t >= ?", (cutoff,)).fetchall()
            energy = dict(((e, (wh, out)) for e, wh, out in conn.execute(
                "SELECT engine, SUM(gpu_w_avg) / 60.0, SUM(output) FROM rollup_1m "
                "WHERE minute >= ? AND requests > 0 AND gpu_w_avg IS NOT NULL GROUP BY engine",
                (cutoff,))))
        finally:
            conn.close()
        groups = {}
        for eng, model, prompt, cached, out, dtps, acc, tot in reqs:
            g = groups.setdefault((eng or "?", model or "?"), {
                "requests": 0, "prompt": 0, "cached": 0, "output": 0, "tps": [], "acc": 0, "tot": 0})
            g["requests"] += 1
            g["prompt"] += prompt or 0
            g["cached"] += cached or 0
            g["output"] += out or 0
            if dtps:
                g["tps"].append(dtps)
            g["acc"] += acc or 0
            g["tot"] += tot or 0
        rows = []
        for (eng, model), g in groups.items():
            tps = sorted(g["tps"])
            med = None
            if tps:
                m = len(tps) // 2
                med = tps[m] if len(tps) % 2 else (tps[m - 1] + tps[m]) / 2.0
            wh, eout = energy.get(eng, (None, None))
            rows.append({
                "engine": eng, "model": model, "requests": g["requests"], "prompt": g["prompt"],
                "output": g["output"],
                "decode_tps_median": round(med, 1) if med is not None else None,
                "cache_pct": round(100.0 * g["cached"] / g["prompt"], 1) if g["prompt"] else None,
                "mtp_pct": round(100.0 * g["acc"] / g["tot"], 1) if g["tot"] else None,
                "tok_per_wh": round(eout / wh, 1) if wh and eout else None,
            })
        rows.sort(key=lambda r: (-r["output"], -r["requests"]))
        return rows

    def prune_now(self):
        """Apply the retention now instead of at the daily run (the storage panel's prune button).
        Deletes nothing newer than retention_days."""
        if not self._enabled:
            return False
        self._prune()
        return True

    def _request_rows(self, since, limit):
        """Unclamped fetch (export uses it with EXPORT_CAP); requests()
        clamps to its own 1000-row API cap before delegating here."""
        conn = self._reader()
        try:
            rows = conn.execute(
                "SELECT t, %s FROM requests WHERE t >= ? "
                "ORDER BY t DESC LIMIT ?" % ", ".join(_REQ_COLS),
                (int(since), int(limit))).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(["t"] + list(_REQ_COLS), r) for r in rows]

    def requests(self, since, limit=1000):
        if not self._enabled:
            return []
        limit = max(1, min(int(limit), 1000))
        return self._request_rows(since, limit)

    def storage(self):
        """Disk usage and projection. `bytes` is the real on-disk total
        (main file + WAL/SHM sidecars) for display; bytes_per_day is
        computed from the main file's used pages only and from how long
        the COLLECTOR has been observing — MIN(rollup_1m.minute), not
        request timestamps, which the engine's replayed activity feed can
        stretch into its past:

            span >= 1 day            -> measured (db bytes / span days)
            1 h <= span < 1 day      -> DESIGN.md estimate with its request
                component scaled by the measured requests/day from
                rollup_1m sums          ("rate-scaled")
            span < 1 h               -> raw DESIGN.md §2 first estimate
        `basis` says which one a projection was built on."""
        if not self._enabled:
            return {"enabled": False, "path": None, "bytes": 0,
                    "retention_days": 0, "rows": {},
                    "first_t": None, "bytes_per_day": None,
                    "basis": None, "projection": {}}
        path = str(self.path)
        size = os.path.getsize(path) if self.path.exists() else 0
        for suf in ("-wal", "-shm"):
            p = path + suf
            if os.path.exists(p):
                size += os.path.getsize(p)
        conn = self._reader()
        try:
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            freelist = conn.execute(
                "PRAGMA freelist_count").fetchone()[0]
            rows = {}
            for t in ("requests", "rollup_1m", "rollup_1h",
                      "model_spans", "events"):
                rows[t] = conn.execute(
                    "SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            f_req = conn.execute("SELECT MIN(t) FROM requests").fetchone()[0]
            f_r1m = conn.execute(
                "SELECT MIN(minute) FROM rollup_1m").fetchone()[0]
            f_sp = conn.execute(
                "SELECT MIN(loaded_at) FROM model_spans").fetchone()[0]
            ev = conn.execute(
                "SELECT MIN(t), MAX(t) FROM events").fetchone()
            r1m_reqs = conn.execute(
                "SELECT COALESCE(SUM(requests), 0) FROM rollup_1m"
            ).fetchone()[0]
        finally:
            conn.close()
        firsts = [x for x in (f_req, f_r1m, f_sp, ev[0]) if x is not None]
        first_t = min(firsts) if firsts else None
        now = self._clock()
        # main-file data footprint, excluding WAL sidecars and free pages
        db_bytes = max(0, (page_count - freelist) * page_size)
        span_days = ((now - f_r1m) / 86400.0
                     if f_r1m is not None else 0.0)
        est_req = _EST_REQS_PER_DAY * _EST_REQ_B
        est_base = est_req + _EST_MINUTES_UP * _EST_ROLLUP_B
        if span_days >= 1.0:
            bytes_per_day = db_bytes / span_days
            basis = "measured"
        elif span_days >= 1.0 / 24.0 and r1m_reqs:
            reqs_per_day = r1m_reqs / span_days
            bytes_per_day = (est_base - est_req
                             + est_req * (reqs_per_day / _EST_REQS_PER_DAY))
            basis = "rate-scaled"
        else:
            bytes_per_day = est_base
            basis = "estimate"
        return {
            "enabled": True,
            "path": path,
            "bytes": size,
            "retention_days": self.retention_days,
            "rows": rows,
            "first_t": first_t,
            "bytes_per_day": int(bytes_per_day),
            "basis": basis,
            "projection": {str(d): int(bytes_per_day * d)
                           for d in (30, 60, 90)},
        }

    def export(self, fmt, range_):
        """Request rows of the range, capped at EXPORT_CAP (independent of
        the /api/requests 1000-row cap). Returns (rows, truncated) where
        rows is a csv string (fmt='csv') or a list of dicts (fmt='json')
        and truncated says more rows of the range exist than were
        returned — callers must surface the flag instead of dropping it."""
        if range_ not in RANGES:
            raise ValueError("range must be one of %s" % sorted(RANGES))
        since = int(self._clock() - RANGES[range_])
        conn = self._reader()
        try:
            raw = conn.execute(
                "SELECT t, %s FROM requests WHERE t >= ? "
                "ORDER BY t DESC LIMIT ?" % ", ".join(_REQ_COLS),
                (since, EXPORT_CAP)).fetchall()
            truncated = (len(raw) >= EXPORT_CAP and conn.execute(
                "SELECT 1 FROM requests WHERE t >= ? LIMIT 1",
                (since,)).fetchone() is not None)
        finally:
            conn.close()
        rows = [_row_to_dict(["t"] + list(_REQ_COLS), r) for r in raw]
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(["t"] + list(_REQ_COLS))
            for r in rows:
                w.writerow([r.get(c) for c in ["t"] + list(_REQ_COLS)])
            return buf.getvalue(), truncated
        return rows, truncated
