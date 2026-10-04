#!/usr/bin/env python3
"""Unit tests for collector/engines.py (DESIGN.md phase 1).

A stub http.server on 127.0.0.1 serves canned Ollama (/api/version,
/api/ps), llama.cpp (/props, /metrics, /slots) and small custom-engine
endpoints. Run: python3 -m unittest collector.test_engines -v
(or from the collector/ dir: python3 -m unittest test_engines -v).
"""

import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engines  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        st = self.server.state
        st["auth"] = self.headers.get("Authorization")
        p = self.path.split("?")[0]
        if p == "/api/version":
            self._send(200, json.dumps({"version": "0.34.3"}).encode(),
                       "application/json")
        elif p == "/api/ps":
            self._send(200,
                       json.dumps({"models": st.get("ps_models", [])}).encode(),
                       "application/json")
        elif p == "/health" and st.get("health_service"):
            self._send(200, json.dumps({"status": "ok", "service": st["health_service"]}).encode(),
                       "application/json")
        elif p == "/props":
            props = {"default_generation_settings": {"n_ctx": 4096}}
            if st.get("props_build_info"):
                props["build_info"] = st["props_build_info"]
            self._send(200, json.dumps(props).encode(), "application/json")
        elif p == "/metrics":
            st["metrics_calls"] += 1
            n = st["metrics_calls"]
            text = ("llamacpp:prompt_tokens_total 1000\n"
                    "llamacpp:prompt_seconds_total 10.0\n"
                    "llamacpp:tokens_predicted_total %d\n"
                    "llamacpp:tokens_predicted_seconds_total %.1f\n"
                    "llamacpp:requests_running %d\n"
                    "llamacpp:requests_waiting 0\n"
                    "llamacpp:requests_deferred 0\n") % (
                        100 + 10 * n, 10.0 + n, st.get("llamacpp_running", 0))
            self._send(200, text.encode(), "text/plain")
        elif p == "/slots":
            self._send(200, json.dumps([
                {"id": 0, "state": "idle", "n_ctx": 4096,
                 "n_prompt_tokens": 17, "n_prompt_tokens_cache": 12},
            ]).encode(), "application/json")
        elif p == "/health":
            self._send(200, b"ok", "text/plain")
        elif p == "/m":
            self._send(200,
                       b"myengine_generated_tokens_total 41\n"
                       b"myengine_prompt_tokens_total 11\n"
                       b"myengine_requests_waiting 2\n",
                       "text/plain")
        else:
            self._send(404, b"no such endpoint", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StubServer(unittest.TestCase):
    """Mixin: starts the stub in setUpClass, base URL on self.url."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.server.daemon_threads = True
        cls.server.state = {"metrics_calls": 0, "ps_models": [],
                            "llamacpp_running": 0, "auth": None}
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def reset_stub(self, **kw):
        self.server.state.update({
            "metrics_calls": 0, "ps_models": [],
            "llamacpp_running": 0, "auth": None})
        self.server.state.update(kw)


class TestFingerprint(StubServer):
    def test_ollama_positive(self):
        self.assertTrue(engines.OllamaAdapter.fingerprint(self.url))

    def test_ollama_negative_closed_port(self):
        self.assertFalse(
            engines.OllamaAdapter.fingerprint(
                "http://127.0.0.1:%d" % _free_port()))

    def test_ollama_negative_wrong_shape(self):
        # the stub also answers /props; /api/ps exists but /api/version
        # drives the fingerprint, so a metrics-only server must not match
        self.assertFalse(engines.VllmAdapter.fingerprint(self.url))

    def test_llamacpp_positive(self):
        self.assertTrue(engines.LlamaCppAdapter.fingerprint(self.url))

    def test_openai_negative(self):
        # no /v1/models on the stub
        self.assertFalse(engines.OpenAIAdapter.fingerprint(self.url))


class TestOllamaPoll(StubServer):
    def test_schema_idle(self):
        self.reset_stub()
        a = engines.OllamaAdapter(url=self.url)
        rec = a.poll()
        for k in ("key", "label", "type", "url", "up", "state", "caps"):
            self.assertIn(k, rec)
        self.assertTrue(rec["up"])
        self.assertEqual(rec["state"], "idle")          # no model loaded
        self.assertEqual(rec["models"], [])
        self.assertIn("models", rec["caps"])
        self.assertEqual(rec["type"], "ollama")
        self.assertEqual(rec["url"], self.url)

    def test_schema_running(self):
        self.reset_stub(ps_models=[{
            "name": "qwen3:8b", "size": 4936015360, "size_vram": 4936015360,
            "expires_at": "2026-10-03T12:00:00Z"}])
        rec = engines.OllamaAdapter(url=self.url).poll()
        self.assertEqual(rec["state"], "running")
        m = rec["models"][0]
        self.assertEqual(m["id"], "qwen3:8b")
        self.assertTrue(m["loaded"])
        self.assertEqual(m["size_bytes"], 4936015360)
        self.assertEqual(m["vram_bytes"], 4936015360)
        self.assertEqual(m["expires"], "2026-10-03T12:00:00Z")

    def test_down(self):
        a = engines.OllamaAdapter(url="http://127.0.0.1:%d" % _free_port())
        rec = a.poll()
        self.assertFalse(rec["up"])
        self.assertEqual(rec["state"], "error")


class TestLlamaCppPoll(StubServer):
    def test_schema_and_rates(self):
        self.reset_stub()
        a = engines.LlamaCppAdapter(url=self.url)
        rec = a.poll()
        self.assertTrue(rec["up"])
        self.assertEqual(rec["state"], "idle")       # requests_running 0
        self.assertEqual(rec["queue"], 0)
        self.assertEqual(rec["counters"]["prompt_tokens"], 1000)
        self.assertEqual(rec["counters"]["output_tokens"], 110)
        self.assertEqual(len(rec["sessions"]), 1)
        s = rec["sessions"][0]
        self.assertEqual(s["id"], "s0")
        self.assertEqual(s["window"], 4096)
        self.assertEqual(s["used"], 17)
        self.assertEqual(s["cached"], 12)
        self.assertIn("sessions", rec["caps"])
        self.assertNotIn("rates", rec)               # first poll has no delta

        time.sleep(0.6)                              # pass the 0.5 s gate
        self.server.state["llamacpp_running"] = 1
        rec = a.poll()
        self.assertEqual(rec["state"], "running")
        # stub advanced 10 predicted tokens over 1.0 predicted-seconds
        self.assertAlmostEqual(rec["rates"]["decode_tps"], 10.0, delta=0.5)
        self.assertIn("rates", rec["caps"])

    def test_down(self):
        a = engines.LlamaCppAdapter(url="http://127.0.0.1:%d" % _free_port())
        rec = a.poll()
        self.assertFalse(rec["up"])
        self.assertEqual(rec["state"], "error")


class TestCustomAdapter(StubServer):
    def test_poll_with_map(self):
        a = engines.make_adapter({
            "name": "my-engine", "type": "custom", "url": self.url,
            "health": "/health",
            "metrics": "/m",
            "map": {"output_tokens": "myengine_generated_tokens_total",
                    "prompt_tokens": "myengine_prompt_tokens_total",
                    "queue": "myengine_requests_waiting"},
        })
        rec = a.poll()
        self.assertTrue(rec["up"])
        self.assertEqual(rec["label"], "my-engine")
        self.assertEqual(rec["queue"], 2)
        self.assertEqual(rec["counters"]["output_tokens"], 41)
        self.assertEqual(rec["counters"]["prompt_tokens"], 11)

    def test_bearer_from_env(self):
        os.environ["SPECULUM_TEST_KEY"] = "sekrit"
        try:
            a = engines.make_adapter({
                "name": "guarded", "type": "custom", "url": self.url,
                "health": "/health", "api_key_env": "SPECULUM_TEST_KEY"})
            a.poll()
            self.assertEqual(self.server.state["auth"], "Bearer sekrit")
        finally:
            del os.environ["SPECULUM_TEST_KEY"]


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class FakeAdapter(engines.Adapter):
    type = "fake"
    default_port = None

    def __init__(self):
        super().__init__(url="http://127.0.0.1:1")
        self.up = False
        self.state = "error"

    def poll(self):
        # mirrors Adapter._finish: busy means up and actively running
        self._busy = bool(self.up and self.state == "running")
        return {"up": self.up, "state": self.state}


class TestSchedulerBackoff(unittest.TestCase):
    def make_sched(self, clock=None):
        clock = clock or FakeClock()
        self.results, self.events = [], []
        a = FakeAdapter()
        sched = engines.Scheduler(
            [a], discovery_enabled=False,
            on_result=lambda ad, r: self.results.append((ad, r)),
            on_event=lambda lv, m: self.events.append((lv, m)),
        )
        sched.clock = clock
        return sched, a

    def test_down_backoff_5_15_60_300(self):
        clock = FakeClock()
        sched, a = self.make_sched(clock)
        st = sched.engines[a.key]
        gaps = []
        for expected in (5, 15, 60, 300, 300):
            base = clock.t
            sched._step()
            self.assertFalse(st["up"])
            gap = st["next_due"] - base
            self.assertAlmostEqual(gap, expected, delta=1e-6)
            gaps.append(expected)
            clock.advance(expected)
        self.assertEqual(gaps, [5, 15, 60, 300, 300])
        self.assertEqual(st["backoff"], 3)            # capped at the last one

    def test_up_idle_then_busy(self):
        clock = FakeClock()
        sched, a = self.make_sched(clock)
        a.up, a.state = True, "idle"
        sched._step()
        self.assertTrue(sched.engines[a.key]["up"])
        self.assertAlmostEqual(sched.engines[a.key]["next_due"],
                               clock.t + 5.0, delta=1e-6)
        clock.advance(5)
        a.state = "running"                           # busy
        sched._step()
        self.assertAlmostEqual(sched.engines[a.key]["next_due"],
                               clock.t + 1.0, delta=1e-6)

    def test_transitions_emit_events(self):
        clock = FakeClock()
        sched, a = self.make_sched(clock)
        sched._step()                                 # down at start: no event
        self.assertEqual(self.events, [])
        a.up, a.state = True, "idle"
        clock.advance(5)
        sched._step()
        self.assertEqual(self.events[-1], ("ok", "fake up"))
        a.up, a.state = False, "error"
        clock.advance(5)
        sched._step()
        self.assertEqual(self.events[-1], ("warn", "fake down"))

    def test_on_result_receives_down_too(self):
        clock = FakeClock()
        sched, a = self.make_sched(clock)
        sched._step()
        self.assertEqual(len(self.results), 1)
        self.assertFalse(self.results[0][1]["up"])



class StrataIsNotLlamaCpp(StubServer):
    """Strata serves a llama.cpp-shaped /props (build_info "Strata 0.1.38"); discovery must not take it
    for llama.cpp, or a false "llama.cpp down" card and alert appear beside Strata's own."""

    def test_fingerprint(self):
        self.reset_stub()
        self.assertTrue(engines.ADAPTERS["llamacpp"].fingerprint(self.url))
        self.server.state["props_build_info"] = "b11115-abc"
        self.assertTrue(engines.ADAPTERS["llamacpp"].fingerprint(self.url))
        self.server.state["props_build_info"] = "Strata 0.1.38"
        self.assertFalse(engines.ADAPTERS["llamacpp"].fingerprint(self.url))
        self.server.state.pop("props_build_info")

    def test_discovery_identity(self):
        self.reset_stub()
        self.assertFalse(engines._is_strata(self.url))
        self.server.state["health_service"] = "strata"
        self.assertTrue(engines._is_strata(self.url))
        self.server.state.pop("health_service")


if __name__ == "__main__":
    unittest.main(verbosity=2)
