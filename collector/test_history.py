#!/usr/bin/env python3
"""Unit tests for collector/history.py (DESIGN.md section 2, phase 2).

No network: the History writer is driven directly with H._cycle() against
an injected clock, and readers use the module's own query methods or a
read-only connection to the temp-dir database.

Run: python3 -m unittest test_history -v   (from collector/)
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import history  # noqa: E402


class FakeClock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = float(t)


def read_only(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def rows_of(h, sql, args=()):
    with h._db_lock:
        return h._db.execute(sql, args).fetchall()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="speculum-hist-test-")
        self.path = os.path.join(self.dir, "speculum.db")
        self.errors = []
        self.clock = FakeClock(1_000_000.0)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def mkhist(self, retention=30):
        # start_thread=False: the writer's _cycle() is driven manually with
        # the fake clock, so tests are deterministic
        return history.History(path=self.path, retention_days=retention,
                               log=self.errors.append, clock=self.clock,
                               start_thread=False)

    # ------------------------------------------------------------- schema

    def test_schema_pragmas_and_indexes(self):
        h = self.mkhist()
        try:
            conn = read_only(self.path)
            try:
                self.assertEqual(conn.execute(
                    "PRAGMA user_version").fetchone()[0], history.SCHEMA_VERSION)
                self.assertEqual(conn.execute(
                    "PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(conn.execute(
                    "PRAGMA auto_vacuum").fetchone()[0], 2)  # 2 = INCREMENTAL
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
                for t in ("requests", "rollup_1m", "rollup_1h",
                          "model_spans", "events"):
                    self.assertIn(t, tables)
                indexes = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'")}
                for ix in ("ix_requests_dedupe", "ix_requests_t",
                           "ix_requests_eng",
                           "ix_r1m_minute", "ix_r1m_engine",
                           "ix_r1m_key",
                           "ix_r1h_hour", "ix_r1h_engine",
                           "ix_r1h_key",
                           "ix_spans_loaded", "ix_spans_engine",
                           "ix_events_t"):
                    self.assertIn(ix, indexes)
                for name in ("ix_requests_dedupe", "ix_r1m_key",
                             "ix_r1h_key"):
                    sql = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE name = ?",
                        (name,)).fetchone()[0]
                    self.assertIn("UNIQUE", sql, name)
                cols = [r[1] for r in conn.execute("PRAGMA table_info(requests)")]
                self.assertEqual(cols, ["t", "engine", "model", "prompt",
                                        "cached", "fresh", "output",
                                        "ttft_ms", "total_ms", "queue_ms",
                                        "decode_tps", "prefill_tps",
                                        "mtp_acc", "mtp_tot", "status",
                                        "client"])
            finally:
                conn.close()
        finally:
            h.close()

    # --------------------------------------------------- merge window 130 s

    def test_request_written_only_after_merge_window(self):
        h = self.mkhist()
        t0 = self.clock.t
        rec = {"t": t0, "origin": "ninfer", "model": "M",
               "prompt": 100, "cache": 40, "fresh": 60, "output": 50,
               "total_s": 2.0, "ttft_s": 0.1, "queue_s": 0.05,
               "decode_tps": 45.0, "prefill_tps": 900.0}
        h.add_request(rec)
        # still inside the 120 s merge window (+10 s margin)
        self.clock.set(t0 + 120.0)
        h._cycle()
        self.assertEqual(rows_of(h, "SELECT COUNT(*) FROM requests")[0][0], 0)
        # just past it: written with seconds fields converted to ms
        self.clock.set(t0 + 131.0)
        h._cycle()
        (row,) = rows_of(h, "SELECT * FROM requests")
        self.assertEqual(row[0], t0)
        self.assertEqual(row[1], "ninfer")
        self.assertEqual(row[3], 100)      # prompt
        self.assertEqual(row[4], 40)       # cached
        self.assertAlmostEqual(row[7], 100.0)    # ttft_ms
        self.assertAlmostEqual(row[8], 2000.0)   # total_ms
        self.assertAlmostEqual(row[9], 50.0)     # queue_ms
        self.assertAlmostEqual(row[10], 45.0)    # decode_tps
        h.close()

    def test_merged_fields_captured_from_same_dict(self):
        h = self.mkhist()
        t0 = self.clock.t
        rec = {"t": t0, "origin": "ninfer", "model": "M",
               "prompt": 100, "output": 50}
        h.add_request(rec)
        # speculum.add_request merges a later duplicate into the same dict
        rec["decode_tps"] = 60.0
        rec["ttft_s"] = 0.2
        self.clock.set(t0 + 131.0)
        h._cycle()
        (row,) = rows_of(h, "SELECT * FROM requests")
        self.assertAlmostEqual(row[7], 200.0, places=3)   # ttft_ms merged in
        self.assertAlmostEqual(row[10], 60.0)              # decode_tps merged
        h.close()

    def test_flush_writes_records_inside_merge_window(self):
        h = self.mkhist()
        h.add_request({"t": self.clock.t, "origin": "llama", "model": "L",
                       "prompt": 10, "output": 5})
        h.flush()
        self.assertEqual(rows_of(h, "SELECT COUNT(*) FROM requests")[0][0], 1)
        h.close()

    # ------------------------------------------------ restart dedupe (v2)

    def test_requests_dedupe_across_restart(self):
        h1 = self.mkhist()
        h1.add_request({"t": self.clock.t, "origin": "ninfer", "model": "M",
                        "prompt": 100, "output": 50})
        h1.flush()
        h1.close()
        # restart: a fresh History on the same file replays the same record
        h2 = self.mkhist()
        h2.add_request({"t": self.clock.t, "origin": "ninfer", "model": "M",
                        "prompt": 100, "output": 50})
        h2.flush()
        self.assertEqual(rows_of(h2, "SELECT COUNT(*) FROM requests")[0][0], 1)
        h2.close()

    def test_migration_v1_dedupes_and_bumps_version(self):
        import re
        # v1 fixture: today's schema minus the unique dedupe index
        v1_schema = re.sub(
            r"CREATE UNIQUE INDEX IF NOT EXISTS ix_requests_dedupe.*?;\n",
            "", history._SCHEMA, flags=re.S)
        conn = sqlite3.connect(self.path)
        conn.executescript(v1_schema)
        conn.execute("PRAGMA user_version = 1")
        t = int(self.clock.t)
        conn.executemany(
            "INSERT INTO requests (t, engine, model, prompt, output) "
            "VALUES (?, 'ninfer', 'M', 100, 50)", [(t,), (t,), (t,)])
        conn.commit()
        conn.close()
        h = self.mkhist()                 # opening migrates v1 -> v2
        self.assertEqual(rows_of(h, "SELECT COUNT(*) FROM requests")[0][0], 1)
        self.assertEqual(rows_of(h, "PRAGMA user_version")[0][0],
                         history.SCHEMA_VERSION)
        self.assertTrue(rows_of(
            h, "SELECT name FROM sqlite_master WHERE name = "
               "'ix_requests_dedupe'"))
        h.close()

    def test_migration_v2_dedupes_rollups_and_bumps_version(self):
        import re
        # v2 fixture: today's schema minus the v3 unique rollup keys
        v2_schema = re.sub(
            r"CREATE UNIQUE INDEX IF NOT EXISTS ix_r1m_key.*?;\n"
            r"CREATE UNIQUE INDEX IF NOT EXISTS ix_r1h_key.*?;\n",
            "", history._SCHEMA, flags=re.S)
        conn = sqlite3.connect(self.path)
        conn.executescript(v2_schema)
        conn.execute("PRAGMA user_version = 2")
        m = int(self.clock.t) // 60 * 60
        # duplicate (minute, engine) rows the old plain-INSERT flush
        # accumulated, plus a duplicate (hour, engine) rollup_1h row
        for _ in range(3):
            conn.execute("INSERT INTO rollup_1m (minute, engine, requests) "
                         "VALUES (?, 'x', 1)", (m,))
        hh = int(self.clock.t) // 3600 * 3600
        for _ in range(2):
            conn.execute("INSERT INTO rollup_1h (hour, engine) "
                         "VALUES (?, 'x')", (hh,))
        conn.commit()
        conn.close()
        h = self.mkhist()                 # opening migrates v2 -> v3
        self.assertEqual(rows_of(h, "PRAGMA user_version")[0][0],
                         history.SCHEMA_VERSION)
        self.assertEqual(rows_of(h, "SELECT COUNT(*) FROM rollup_1m")[0][0], 1)
        self.assertEqual(rows_of(h, "SELECT COUNT(*) FROM rollup_1h")[0][0], 1)
        self.assertTrue(rows_of(
            h, "SELECT name FROM sqlite_master WHERE name = 'ix_r1m_key'"))
        self.assertTrue(rows_of(
            h, "SELECT name FROM sqlite_master WHERE name = 'ix_r1h_key'"))
        h.close()

    # -------------------------------------------------------- rollup_1m

    def _cadence_records(self, t0, steps, per_step=2):
        """Request records of the production cadence: `per_step` per 10 s
        step, at t + j*3 s within the step."""
        recs = []
        for i in range(steps):
            t = t0 + i * 10.0
            for j in range(per_step):
                recs.append({"t": t + j * 3.0, "origin": "ninfer",
                             "model": "M", "prompt": 10 + j,
                             "cache": 4 + j, "fresh": 6, "output": 5 + j})
        return recs

    def _feed_cadence(self, h, recs):
        """Drive the writer at the production cadence: one _cycle() per
        10 s step, the step's sample plus its requests arriving in between."""
        t0 = recs[0]["t"]
        per = 2
        for i in range(len(recs) // per):
            t = t0 + i * 10.0
            h.sample(t, {"ninfer": {"up": True,
                                    "rates": {"decode_tps": 40.0 + (i % 3) * 5.0},
                                    "queue": i % 4}},
                     {"power.draw": 300.0, "memory.used_gb": 20.0})
            for r in recs[i * per:(i + 1) * per]:
                h.add_request(r)
            self.clock.set(t + 10.0)
            h._cycle()

    def _expected_minutes(self, recs):
        """minute -> [requests, prompt, cached, output] over the given
        request records."""
        exp = {}
        for r in recs:
            m = int((r["t"] or 0) // 60.0) * 60
            e = exp.setdefault(m, [0, 0, 0, 0])
            e[0] += 1
            e[1] += r.get("prompt") or 0
            e[2] += r.get("cache") or 0
            e[3] += r.get("output") or 0
        return exp

    def test_minute_rollup_production_cadence(self):
        # production cadence: 10 s writer steps driven manually for six
        # minutes; requests only land 130 s after their minute, so the
        # rollup must be filled by the request upserts, not by a later
        # pass over the requests table
        t0 = 3_000_000.0
        self.clock.set(t0)
        recs = self._cadence_records(t0, 36)
        h = self.mkhist()
        self._feed_cadence(h, recs)
        now = self.clock.t
        exp = self._expected_minutes(recs)
        # every fully-settled minute (its last request is past the 130 s
        # merge hold) must equal the request rows of that minute
        settled = [m for m in exp
                   if m + 60 + history.MERGE_HOLD_S <= now]
        self.assertGreaterEqual(len(settled), 2)
        for m in settled:
            row = rows_of(h, "SELECT requests, prompt, cached, output, "
                              "decode_tps_avg, decode_tps_max, queue_max, "
                              "gpu_w_avg, vram_max "
                              "FROM rollup_1m WHERE minute = ? AND engine = ?",
                          (m, "ninfer"))
            self.assertEqual(list(row[0][:4]) if row else None, exp[m],
                             "minute %d" % m)
            if row:
                # sample-derived stats merged into the same (minute, engine)
                # row: six samples of 40/45/50 each twice per minute
                self.assertAlmostEqual(row[0][4], 45.0)
                self.assertAlmostEqual(row[0][5], 50.0)
                self.assertAlmostEqual(row[0][6], 3.0)
                self.assertAlmostEqual(row[0][7], 300.0)
                self.assertAlmostEqual(row[0][8], 20.0)
        # let the still-pending originals come due too, so that the
        # replay below is the only traffic
        self.clock.set(now + 140.0)
        h._cycle()
        # every written request row is counted exactly once in rollup_1m
        self.assertEqual(
            rows_of(h, "SELECT COALESCE(SUM(requests), 0), "
                       "COALESCE(SUM(output), 0) FROM rollup_1m")[0],
            rows_of(h, "SELECT COUNT(*), COALESCE(SUM(output), 0) "
                       "FROM requests")[0])
        self.assertEqual(rows_of(h, "SELECT COUNT(*) FROM requests")[0][0],
                         len(recs))
        # replay the same records (a restart feed-replay): the
        # INSERT OR IGNORE drops them and no rollup sum may move
        sums = rows_of(
            h, "SELECT COALESCE(SUM(requests), 0), "
               "COALESCE(SUM(output), 0) FROM rollup_1m")[0]
        for r in recs:
            h.add_request(r)
        self.clock.set(now + 280.0)
        h._cycle()
        self.assertEqual(
            rows_of(h, "SELECT COALESCE(SUM(requests), 0), "
                       "COALESCE(SUM(output), 0) FROM rollup_1m")[0], sums)
        self.assertEqual(rows_of(h, "SELECT COUNT(*) FROM requests")[0][0],
                         len(recs))
        # the settled-minute equality still holds after the replay
        for m in settled:
            row = rows_of(h, "SELECT requests, prompt, cached, output "
                          "FROM rollup_1m WHERE minute = ? AND engine = ?",
                          (m, "ninfer"))
            self.assertEqual(list(row[0][:4]) if row else None, exp[m],
                             "minute %d after replay" % m)
        h.close()

    # -------------------------------------------------------- rollup_1h

    def test_hourly_aggregation(self):
        # one full hour at the production cadence (10 s writer steps);
        # the hour's last requests land at H+3600+130, so the hour is
        # only eligible to aggregate 300 s after it closes
        h0 = 3_600_000.0
        self.clock.set(h0)
        recs = self._cadence_records(h0, 360, per_step=1)
        h = self.mkhist()
        per = 1
        for i in range(360):
            t = h0 + i * 10.0
            h.sample(t, {"ninfer": {"up": True,
                                    "rates": {"decode_tps": 10.0}}})
            h.add_request(recs[i])
            self.clock.set(t + 10.0)
            h._cycle()
        # at the hour boundary it is not eligible yet (its last requests
        # are still inside the merge window)
        self.clock.set(h0 + 3600 + 10.0)
        h._cycle()
        self.assertEqual(rows_of(h, "SELECT COUNT(*) FROM rollup_1h")[0][0],
                         0)
        # 300 s past the close: the recompute-upsert lands, and rollup_1h
        # must equal the sum of the hour's rollup_1m rows
        self.clock.set(h0 + 3600 + 300.0)
        h._cycle()
        (row,) = rows_of(h, "SELECT hour, engine, requests, prompt, "
                             "cached, output, decode_tps_avg "
                             "FROM rollup_1h")
        self.assertEqual((row[0], row[1]), (3_600_000, "ninfer"))
        self.assertEqual((row[2], row[3], row[4], row[5]),
                         (360, 3600, 1440, 1800))
        self.assertAlmostEqual(row[6], 10.0)
        m = rows_of(h, "SELECT COALESCE(SUM(requests), 0), "
                       "COALESCE(SUM(prompt), 0), "
                       "COALESCE(SUM(cached), 0), "
                       "COALESCE(SUM(output), 0) "
                       "FROM rollup_1m WHERE minute >= ? AND minute < ? "
                       "AND engine = 'ninfer'",
                       (3_600_000, 3_603_600))[0]
        self.assertEqual((row[2], row[3], row[4], row[5]), tuple(m))
        h.close()

    def test_hourly_aggregation_self_heals_after_gap(self):
        # three hours of per-minute data, then a five-hour suspend with
        # no cycles; the next pass must walk back from the last
        # aggregated hour and fill every remaining hour with data
        h0 = 3_600_000.0
        self.clock.set(h0)
        h = self.mkhist()
        for i in range(180):
            t = h0 + i * 60.0
            h.sample(t, {"ninfer": {"up": True,
                                    "rates": {"decode_tps": 10.0}}})
            h.add_request({"t": t, "origin": "ninfer", "model": "M",
                           "prompt": 10, "cache": 5, "fresh": 5, "output": 7})
            self.clock.set(t + 60.0)
            h._cycle()
        # during the feed, hours 0 and 1 already aggregated as they
        # became eligible; the suspend leaves hour 2 (and everything
        # after) unaggregated
        self.clock.set(h0 + 3 * 3600 + 5 * 3600)
        h._cycle()
        rows = rows_of(h, "SELECT hour, requests, output "
                          "FROM rollup_1h ORDER BY hour")
        self.assertEqual([r[0] for r in rows],
                         [3_600_000, 3_603_600, 3_607_200])
        self.assertTrue(all((r[1], r[2]) == (60, 420) for r in rows))
        # a later pass recomputes (upsert): no duplicate rows, sums stable
        self.clock.set(self.clock.t + 3600.0)
        h._cycle()
        self.assertEqual(
            rows_of(h, "SELECT hour, requests, output FROM rollup_1h "
                       "ORDER BY hour"), rows)
        h.close()

    # ------------------------------------------------------------- prune

    def test_prune_retention_and_event_cap(self):
        now = 5_000_000.0
        self.clock.set(now)
        h = self.mkhist(retention=1)
        old = now - 10 * 86400
        with h._db_lock, h._db:
            h._db.executemany(
                "INSERT INTO requests (t, engine) VALUES (?, 'x')",
                [(old,), (now - 3600,)])
            h._db.executemany(
                "INSERT INTO rollup_1m (minute, engine) VALUES (?, 'x')",
                [(int(old),), (int(now) - 3600,)])
            h._db.executemany(
                "INSERT INTO model_spans (engine, loaded_at) VALUES ('x', ?)",
                [(old,), (int(now) - 3600,)])
            h._db.executemany(
                "INSERT INTO rollup_1h (hour, engine) VALUES (?, 'x')",
                [(int(now) - 500 * 86400,), (int(now) - 100 * 86400,)])
            h._db.executemany(
                "INSERT INTO events (t) VALUES (?)",
                [(now - i,) for i in range(60_000)])
        # the one-off prune ~60 s after start fires on the next writer cycle
        self.clock.set(now + 61.0)
        h._cycle()
        self.assertEqual(
            rows_of(h, "SELECT COUNT(*) FROM requests WHERE t < ?",
                    (now - 86400,))[0][0], 0)
        self.assertEqual(
            rows_of(h, "SELECT COUNT(*) FROM requests WHERE t >= ?",
                    (now - 86400,))[0][0], 1)
        self.assertEqual(
            rows_of(h, "SELECT COUNT(*) FROM rollup_1m WHERE minute < ?",
                    (int(now) - 86400,))[0][0], 0)
        self.assertEqual(
            rows_of(h, "SELECT COUNT(*) FROM model_spans "
                       "WHERE loaded_at < ?", (int(now) - 86400,))[0][0], 0)
        cut_h = int(now) - history.HOUR_RETENTION_D * 86400
        self.assertEqual(
            rows_of(h, "SELECT COUNT(*) FROM rollup_1h WHERE hour < ?",
                    (cut_h,))[0][0], 0)          # 500-d-old row pruned
        self.assertEqual(
            rows_of(h, "SELECT COUNT(*) FROM rollup_1h WHERE hour = ?",
                    (int(now) - 100 * 86400,))[0][0], 1)   # 100-d row kept
        ev = rows_of(h, "SELECT COUNT(*), MIN(t) FROM events")[0]
        self.assertEqual(ev[0], history.EVENTS_CAP)
        # seeded t = now - i (i = 0..59999); the 10k oldest are dropped,
        # so the minimum that survives is now - 49999
        self.assertAlmostEqual(
            ev[1], now - (60_000 - 1 - (60_000 - history.EVENTS_CAP)))
        h.close()

    # ------------------------------------------------- retention_days == 0

    def test_retention_zero_is_noop_with_no_file(self):
        h = history.History(path=self.path, retention_days=0,
                            log=self.errors.append, clock=self.clock,
                            start_thread=False)
        h.add_request({"t": self.clock.t, "origin": "x"})
        h.sample(self.clock.t, {"x": {"up": True}})
        h.add_event(self.clock.t, "info", None, "hello")
        h.flush()
        self.assertFalse(Path(self.path).exists())
        self.assertFalse((Path(self.path).with_name("speculum.db-wal")).exists())
        st = h.storage()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["projection"], {})
        self.assertEqual(h.history("24h"), [])
        self.assertEqual(h.requests(0), [])
        h.close()
        self.assertEqual(self.errors, [])

    # ---------------------------------------------------------- storage()

    def test_storage_projection_keys_and_shape(self):
        h = self.mkhist()
        t0 = self.clock.t
        h.add_request({"t": t0, "origin": "ninfer", "model": "M",
                       "prompt": 10, "output": 5})
        h.sample(t0, {"ninfer": {"up": True}})
        h.flush()
        st = h.storage()
        for k in ("path", "bytes", "retention_days", "rows", "first_t",
                  "bytes_per_day", "basis", "projection"):
            self.assertIn(k, st)
        self.assertEqual(set(st["projection"]), {"30", "60", "90"})
        self.assertTrue(all(v > 0 for v in st["projection"].values()))
        self.assertGreater(st["bytes"], 0)
        self.assertEqual(st["retention_days"], 30)
        self.assertEqual(st["rows"]["requests"], 1)
        # oldest data is the minute-floor rollup row of the sample
        self.assertEqual(st["first_t"], int(t0 // 60.0) * 60)
        # under one hour of rollup coverage: raw DESIGN.md estimate
        self.assertEqual(st["basis"], "estimate")
        est = (history._EST_REQS_PER_DAY * history._EST_REQ_B
               + history._EST_MINUTES_UP * history._EST_ROLLUP_B)
        self.assertEqual(st["bytes_per_day"], est)
        h.close()

    def test_storage_basis_measured_after_one_day(self):
        now = 5_000_000.0
        self.clock.set(now)
        h = self.mkhist()
        with h._db_lock, h._db:
            # hourly rollup rows spanning exactly two days
            for m in range(-48, -1):
                h._db.execute(
                    "INSERT INTO rollup_1m (minute, engine) VALUES (?, 'x')",
                    (int(now) + m * 3600,))
            # a far older (replayed) request timestamp must not stretch
            # the observed span
            h._db.execute(
                "INSERT INTO requests (t, engine) VALUES (?, 'x')",
                (int(now) - 30 * 86400,))
        st = h.storage()
        self.assertEqual(st["basis"], "measured")
        conn = read_only(self.path)
        try:
            pc = conn.execute("PRAGMA page_count").fetchone()[0]
            ps = conn.execute("PRAGMA page_size").fetchone()[0]
            fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
        finally:
            conn.close()
        expected = (pc - fl) * ps / 2.0     # span is exactly two days
        self.assertGreater(expected, 0)
        self.assertAlmostEqual(st["bytes_per_day"], expected,
                               delta=expected * 0.1)
        h.close()

    def test_storage_basis_rate_scaled(self):
        now = 6_000_000.0
        self.clock.set(now)
        h = self.mkhist()
        with h._db_lock, h._db:
            # two hours of rollup coverage with a busy request rate
            for m in range(-120, -1):
                h._db.execute(
                    "INSERT INTO rollup_1m (minute, engine, requests) "
                    "VALUES (?, 'x', 100)", (int(now) + m * 60,))
        st = h.storage()
        self.assertEqual(st["basis"], "rate-scaled")
        est = (history._EST_REQS_PER_DAY * history._EST_REQ_B
               + history._EST_MINUTES_UP * history._EST_ROLLUP_B)
        # 12000 requests in 2 h = 144000/day >> the 500/day estimate
        self.assertGreater(st["bytes_per_day"], est)
        h.close()

    # ---------------------------------------------------- queue cap drops

    def test_queue_cap_drops_oldest(self):
        h = self.mkhist()
        n = history.ENQUEUE_CAP
        for i in range(n + 50):
            h.add_event(self.clock.t + i, "info", None, "e%d" % i)
        self.assertEqual(h.drops, 50)
        self.assertEqual(len(h._events), n)
        self.assertEqual(h._events[0][0], self.clock.t + 50)   # oldest kept
        self.assertEqual(h._events[-1][0], self.clock.t + n + 49)
        before = h.drops
        for i in range(n + 20):
            h.add_request({"t": self.clock.t + i, "origin": "x"})
        self.assertEqual(h.drops, before + 20)
        self.assertEqual(len(h._reqs), n)
        h.close()

    # --------------------------------------- spans: load/unload and startup

    def test_spans_lifecycle_and_startup_close(self):
        h = self.mkhist()
        h.model_loaded("llama", "model-a", 100.0)
        h.model_unloaded("llama", "model-a", 200.0)
        h.flush()
        h.close()
        (row,) = rows_of_closed(self.path,
                                "SELECT engine, model, loaded_at, unloaded_at "
                                "FROM model_spans")
        self.assertEqual(row, ("llama", "model-a", 100.0, 200.0))
        # a span left open is closed at its loaded_at on next start
        h = self.mkhist()
        h.model_loaded("llama", "model-b", 300.0)
        h.flush()
        h.close()
        h = self.mkhist()
        (row,) = rows_of_closed(self.path,
                                "SELECT model, loaded_at, unloaded_at "
                                "FROM model_spans WHERE model = 'model-b'")
        self.assertEqual(row, ("model-b", 300.0, 300.0))
        h.close()

    # ------------------------------------------------------------ export

    def test_export_json_and_csv(self):
        h = self.mkhist()
        h.add_request({"t": self.clock.t, "origin": "ninfer", "model": "M",
                       "prompt": 10, "output": 5})
        h.flush()
        js, truncated = h.export("json", "24h")
        self.assertFalse(truncated)
        self.assertEqual(len(js), 1)
        self.assertEqual(js[0]["engine"], "ninfer")
        csv_text, truncated = h.export("csv", "24h")
        self.assertFalse(truncated)
        self.assertTrue(csv_text.startswith("t,engine,model,"))
        self.assertIn("ninfer", csv_text)
        with self.assertRaises(ValueError):
            h.export("csv", "5x")
        h.close()

    def test_export_truncation_flag(self):
        # with EXPORT_CAP patched small, the export returns the cap and
        # reports that more rows of the range exist
        h = self.mkhist()
        t0 = self.clock.t
        for i in range(5):
            h.add_request({"t": t0, "origin": "ninfer", "model": "M",
                           "prompt": i, "output": i})
        h.flush()
        orig = history.EXPORT_CAP
        history.EXPORT_CAP = 3
        try:
            js, truncated = h.export("json", "24h")
            self.assertTrue(truncated)
            self.assertEqual(len(js), 3)
            csv_text, truncated = h.export("csv", "24h")
            self.assertTrue(truncated)
            self.assertEqual(len(csv_text.splitlines()), 4)  # header + 3
        finally:
            history.EXPORT_CAP = orig
        # under the cap: no truncation
        js, truncated = h.export("json", "24h")
        self.assertFalse(truncated)
        self.assertEqual(len(js), 5)
        h.close()

    # ------------------------------------------------- error survival

    def test_writer_survives_sqlite_errors(self):
        h = self.mkhist()
        h.add_event(self.clock.t, "info", None, "before")
        with h._db_lock:
            h._db.close()
        self.clock.set(self.clock.t + 20.0)
        h._run_once()       # sqlite error: logged, not raised, thread keeps going
        self.assertTrue(any("writer:" in m for m in self.errors),
                        "error was not logged: %r" % (self.errors,))
        # pending data is still queued for the next healthy cycle
        self.assertEqual(len(h._events), 1)
        h.close()


def rows_of_closed(path, sql, args=()):
    conn = read_only(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
