#!/usr/bin/env python3
"""Unit tests for collector/hostinfo.py.

The /proc backend is tested against the real Linux filesystem, including a
spawned fake "llama-server" executable so `processes()` has a known match;
the ctypes backend runs against the real OS on Windows. The module import
itself is safe on both: Win32 handles are created only when
`sys.platform == "win32"`, so importing on Linux never touches Win32.

Run: python3 -m pytest -q  (or python3 -m unittest test_hostinfo -v)
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hostinfo  # noqa: E402

IS_WINDOWS = hostinfo.IS_WINDOWS


class TestBackendSelection(unittest.TestCase):
    def test_module_is_import_safe_and_dispatched(self):
        for fn in ("cpu_times", "memory", "processes", "load", "uptime_s"):
            self.assertTrue(callable(getattr(hostinfo, fn)))
        if IS_WINDOWS:
            # pure helper: the per-core record layout is 96 bytes
            self.assertEqual(hostinfo._PROC_PERF_STRIDE, 96)

    def test_ninfer_serve_port_never_raises(self):
        # C1 regression: the old /proc scan raised OSError on Windows and
        # killed the NInfer poller thread; the new path must not raise.
        import speculum
        port = speculum.ninfer_serve_port()
        self.assertTrue(port is None or 0 < port < 65536)


@unittest.skipIf(IS_WINDOWS, "Linux /proc backend")
class TestLinuxBackend(unittest.TestCase):
    def test_memory_shape(self):
        m = hostinfo.memory()
        self.assertEqual(set(m), {"ram_total_gb", "ram_used_gb",
                                  "swap_total_gb", "swap_used_gb"})
        self.assertGreater(m["ram_total_gb"], 0)
        self.assertTrue(0 <= m["ram_used_gb"] <= m["ram_total_gb"])

    def test_cpu_times_shape(self):
        total, cores = hostinfo.cpu_times()
        self.assertIsNotNone(total)
        self.assertTrue(total[0] >= total[1] >= 0)
        self.assertEqual(len(cores), os.cpu_count())
        for all_c, idle_c in cores:
            self.assertTrue(all_c >= idle_c >= 0)

    def test_cpu_counters_are_monotonic(self):
        t1, _ = hostinfo.cpu_times()
        time.sleep(0.2)
        t2, _ = hostinfo.cpu_times()
        self.assertGreaterEqual(t2[0], t1[0])
        self.assertGreaterEqual(t2[1], t1[1])

    def test_load_and_uptime(self):
        load = hostinfo.load()
        self.assertTrue(load is None or
                        (isinstance(load, list) and len(load) == 3))
        up = hostinfo.uptime_s()
        self.assertTrue(up is None or up > 0)

    def test_processes_shape(self):
        for p in hostinfo.processes():
            self.assertEqual(set(p), {"name", "pid", "rss_gb", "cmd"})
            self.assertIn(p["name"], ("llama-server", "ninfer-serve", "strata"))
            self.assertGreater(p["pid"], 0)
            self.assertTrue(p["rss_gb"] is None or p["rss_gb"] > 0)
            self.assertLessEqual(len(p["cmd"]), 120)

    def test_processes_matches_fake_engine(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "llama-server-fake")
            with open(exe, "w") as f:
                f.write("#!/bin/sh\nsleep 30\n")
            os.chmod(exe, 0o755)
            proc = subprocess.Popen([exe])
            try:
                hit = None
                deadline = time.time() + 5
                while time.time() < deadline:
                    hit = [p for p in hostinfo.processes()
                           if p["pid"] == proc.pid]
                    if hit:
                        break
                    time.sleep(0.1)
                self.assertTrue(hit,
                                "fake llama-server not seen by hostinfo.processes()")
                p = hit[0]
                self.assertEqual(p["name"], "llama-server")
                self.assertIn("llama-server", p["cmd"])
                self.assertTrue(p["rss_gb"] is None or p["rss_gb"] > 0)
            finally:
                proc.kill()
                proc.wait()


@unittest.skipIf(not IS_WINDOWS, "Windows ctypes backend")
class TestWindowsBackend(unittest.TestCase):
    def test_memory_shape(self):
        m = hostinfo.memory()
        self.assertEqual(set(m), {"ram_total_gb", "ram_used_gb",
                                  "swap_total_gb", "swap_used_gb"})
        self.assertGreater(m["ram_total_gb"], 0)
        self.assertTrue(0 <= m["ram_used_gb"] <= m["ram_total_gb"])

    def test_cpu_times_shape(self):
        total, cores = hostinfo.cpu_times()
        self.assertIsNotNone(total)
        self.assertTrue(total[0] >= total[1] >= 0)
        self.assertEqual(len(cores), os.cpu_count())
        for all_c, idle_c in cores:
            self.assertTrue(all_c >= idle_c >= 0)

    def test_load_uptime_processes(self):
        self.assertIsNone(hostinfo.load())   # no load average on Windows
        self.assertGreater(hostinfo.uptime_s(), 0)
        for p in hostinfo.processes():
            self.assertEqual(set(p), {"name", "pid", "rss_gb", "cmd"})
            self.assertIn(p["name"], ("llama-server", "ninfer-serve", "strata"))
            self.assertGreater(p["pid"], 0)
            self.assertTrue(p["rss_gb"] is None or p["rss_gb"] > 0)
            self.assertLessEqual(len(p["cmd"]), 120)
