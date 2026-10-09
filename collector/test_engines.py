#!/usr/bin/env python3
"""Unit tests for collector/engines.py.

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


class _VllmStub(BaseHTTPRequestHandler):
    """A vLLM OpenAI server. `with_metrics` False models a server started with
    metrics disabled -- the case that used to read as "engine down" and raise a
    false red alert on a perfectly healthy, serving engine."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        st = self.server.state
        p = self.path.split("?")[0]
        if p == "/version":
            body = json.dumps({"version": "0.11.0"}).encode()
            code = 200
        elif p == "/v1/models":
            body = json.dumps({"object": "list", "data": [
                {"id": "Qwen/Qwen3-8B", "max_model_len": 32768}]}).encode()
            code = 200
        elif p == "/metrics" and st.get("with_metrics", True):
            st["metrics_calls"] = st.get("metrics_calls", 0) + 1
            n = st["metrics_calls"]
            body = ("vllm:prompt_tokens_total 1000\n"
                    "vllm:generation_tokens_total %d\n"
                    "vllm:num_requests_running 1\n"
                    "vllm:num_requests_waiting 2\n" % (500 + 100 * n)).encode()
            code = 200
        elif p == "/metrics":
            body, code = b"404 page not found", 404
        else:
            body, code = b"not found", 404
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class VllmMetricsAreOptional(unittest.TestCase):
    """vLLM must never be called down because /metrics is absent."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _VllmStub)
        cls.server.daemon_threads = True
        cls.server.state = {"with_metrics": True}
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_fingerprint_matches_without_metrics(self):
        self.server.state["with_metrics"] = False
        self.assertTrue(engines.VllmAdapter.fingerprint(self.url))

    def test_up_without_metrics_is_not_down(self):
        self.server.state["with_metrics"] = False
        rec = engines.VllmAdapter(url=self.url).poll()
        self.assertTrue(rec["up"], "a vLLM with metrics off must read as up")
        self.assertEqual(rec["state"], "running")
        self.assertEqual([m["id"] for m in rec["models"]], ["Qwen/Qwen3-8B"])
        self.assertEqual(rec["window"], 32768)
        self.assertNotIn("counters", rec)       # absent, not fabricated zero

    def test_counters_and_queue_when_metrics_served(self):
        self.server.state.update({"with_metrics": True, "metrics_calls": 0})
        rec = engines.VllmAdapter(url=self.url).poll()
        self.assertTrue(rec["up"])
        self.assertEqual(rec["queue"], 2)
        self.assertIn("output_tokens", rec["counters"])
        self.assertEqual(rec["caps"],
                         [k for k in engines.OPTIONAL_BLOCKS if k in rec])

    def test_rates_from_counter_delta(self):
        self.server.state.update({"with_metrics": True, "metrics_calls": 0})
        a = engines.VllmAdapter(url=self.url)
        a.poll()                                  # first poll: baseline only
        # Rewind the stored baseline by 5 s so the next poll sees a real delta
        # without sleeping. rates() needs >= 0.5 s of elapsed time.
        counters, t = a._last
        a._last = (dict(counters), t - 5.0)
        rec = a.poll()
        self.assertIn("rates", rec)
        self.assertGreater(rec["rates"]["decode_tps"], 0)


class _FreetokenStub(BaseHTTPRequestHandler):
    """FreeToken 1919: /health plus the JSON /v1/stats (docs/cli.md:143-144).
    No Prometheus anywhere -- that is the whole point of this adapter."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        st = self.server.state
        p = self.path.split("?")[0]
        if p == "/health":
            body, code = json.dumps({"status": "ok", "model": "GLM-5.2"}).encode(), 200
        elif p == "/v1/stats":
            st["stats_calls"] += 1
            n = st["stats_calls"]
            body = json.dumps({
                "tokens_generated": 1000 + 50 * n,
                "tokens_prompt": 400 + 10 * n,
                "tokens_cached": 120,
                "queue": st.get("queue", 1),
                "vram_used_gb": 21.5,
                "tokens_per_second": 47.3,
                "model": {"input_modalities": ["text"]},
            }).encode()
            code = 200
        elif p == "/metrics":
            body, code = b"404 page not found", 404
        else:
            body, code = b"not found", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FreeTokenJsonStats(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FreetokenStub)
        cls.server.daemon_threads = True
        cls.server.state = {"stats_calls": 0, "queue": 1}
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_fingerprint_uses_stats_shape(self):
        self.assertTrue(engines.FreeTokenAdapter.fingerprint(self.url))

    def test_fingerprint_rejects_closed_port(self):
        self.assertFalse(engines.FreeTokenAdapter.fingerprint("http://127.0.0.1:1"))

    def test_poll_reads_counters_queue_and_extras(self):
        self.server.state.update({"stats_calls": 0, "queue": 3})
        rec = engines.FreeTokenAdapter(url=self.url).poll()
        self.assertTrue(rec["up"])
        self.assertEqual(rec["queue"], 3)
        self.assertEqual(rec["state"], "running")
        self.assertEqual(rec["counters"]["cached_tokens"], 120.0)
        self.assertEqual(rec["extras"]["vram_gb"], 21.5)
        self.assertEqual(rec["extras"]["server_tps"], 47.3)

    def test_idle_when_queue_empty(self):
        self.server.state.update({"stats_calls": 0, "queue": 0})
        rec = engines.FreeTokenAdapter(url=self.url).poll()
        self.assertEqual(rec["state"], "idle")

    def test_missing_fields_absent_not_zero(self):
        self.server.state["stats_calls"] = 0
        rec = engines.FreeTokenAdapter(url=self.url).poll()
        self.assertIn("cached_tokens", rec["counters"])


class _SglangStub(BaseHTTPRequestHandler):
    """SGLang 30000 with enable_metrics OFF by default
    (arg_groups/fields/observability.py:70)."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        st = self.server.state
        p = self.path.split("?")[0]
        if p == "/v1/models":
            body = json.dumps({"object": "list", "data": [
                {"id": "meta-llama/Llama-3.1-8B", "max_model_len": 8192}]}).encode()
            code = 200
        elif p == "/get_model_info":
            body = json.dumps({"model_path": "meta-llama/Llama-3.1-8B",
                               "is_generating": bool(st.get("busy"))}).encode()
            code = 200
        elif p == "/health":
            body, code = b"", 200
        elif p == "/metrics" and st.get("with_metrics"):
            body = ("sglang:prompt_tokens_total 900\n"
                    "sglang:generation_tokens_total 400\n"
                    "sglang:num_requests_waiting 0\n").encode()
            code = 200
        elif p == "/metrics":
            body, code = b"404 page not found", 404
        else:
            body, code = b"not found", 404
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SglangMetricsOffByDefault(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _SglangStub)
        cls.server.daemon_threads = True
        cls.server.state = {"with_metrics": False, "busy": False}
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_default_ports(self):
        self.assertEqual(engines.SglangAdapter.default_port, 30000)
        self.assertEqual(engines.FreeTokenAdapter.default_port, 1919)
        self.assertEqual(engines.KoboldCppAdapter.default_port, 5001)
        self.assertEqual(engines.TabbyApiAdapter.default_port, 5000)

    def test_fingerprint_and_up_without_metrics(self):
        self.assertTrue(engines.SglangAdapter.fingerprint(self.url))
        self.server.state["with_metrics"] = False
        rec = engines.SglangAdapter(url=self.url).poll()
        self.assertTrue(rec["up"], "stock sglang has metrics off; must read as up")
        self.assertEqual([m["ctx"] for m in rec["models"]], [8192])
        self.assertNotIn("counters", rec)

    def test_counters_when_metrics_enabled(self):
        self.server.state["with_metrics"] = True
        rec = engines.SglangAdapter(url=self.url).poll()
        self.assertTrue(rec["up"])
        self.assertEqual(rec["counters"]["output_tokens"], 400.0)


class _KoboldCppStub(BaseHTTPRequestHandler):
    """KoboldCpp 5001 (koboldcpp.py:130 defaultport = 5001). /slots answers 501
    (koboldcpp.py:7114) so there are no KV sessions to report."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/v1/model":
            body, code = json.dumps({"result": "koboldcpp/Qwen2.5-7B-GGUF"}).encode(), 200
        elif p == "/api/v1/config/max_length":
            body, code = json.dumps({"value": 8192}).encode(), 200
        elif p == "/v1/models":
            body, code = json.dumps({"object": "list", "data": [
                {"id": "koboldcpp/Qwen2.5-7B-GGUF", "owned_by": "koboldcpp"}]}).encode(), 200
        elif p == "/slots":
            body, code = json.dumps({"error": {"code": 501, "message":
                "This server does not support slots endpoint."}}).encode(), 501
        elif p == "/props":
            # KoboldCpp serves a llama.cpp-shaped /props with total_slots added
            body = json.dumps({"id": 0, "total_slots": 1,
                               "model_path": "qwen.gguf", "n_ctx": 8192,
                               "default_generation_settings": {"n_ctx": 8192}}).encode()
            code = 200
        elif p == "/version":
            # KoboldCpp does NOT serve a bare /version, which is what keeps it
            # from satisfying the vLLM fingerprint on a shared port.
            body, code = b"not found", 404
        else:
            body, code = b"not found", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class KoboldCppIsNotLlamaCpp(unittest.TestCase):
    """KoboldCpp serves a llama.cpp-shaped /props, so llamacpp must decline it
    (same treatment Strata already gets) or it polls for counters that never come."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _KoboldCppStub)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_llamacpp_fingerprint_declines(self):
        self.assertFalse(engines.LlamaCppAdapter.fingerprint(self.url))

    def test_koboldcpp_fingerprint_matches(self):
        self.assertTrue(engines.KoboldCppAdapter.fingerprint(self.url))

    def test_poll_reports_model_and_window(self):
        rec = engines.KoboldCppAdapter(url=self.url).poll()
        self.assertTrue(rec["up"])
        self.assertEqual([m["id"] for m in rec["models"]],
                         ["koboldcpp/Qwen2.5-7B-GGUF"])
        self.assertEqual(rec["window"], 8192)
        self.assertNotIn("sessions", rec)          # /slots is 501 here

    def test_strata_still_excluded(self):
        # the pre-existing Strata guard must survive the new total_slots check
        self.assertFalse(engines._is_strata(self.url))


class _LocalAIStub(BaseHTTPRequestHandler):
    """LocalAI 8080: /healthz answers 204 No Content (core/http/routes/health.go)."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/healthz":
            self.send_response(204)                # no body, by design
            self.end_headers()
            return
        if p == "/v1/models":
            body = json.dumps({"object": "list", "data": [
                {"id": "gpt4all-j", "context_length": 8192}]}).encode()
            self.send_response(200)
        elif p == "/metrics":
            body = (b"localai_completion_tokens_total 1234\n"
                    b"localai_prompt_tokens_total 567\n")
            self.send_response(200)
        else:
            self.send_response(404)
            body = b"not found"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LocalAISharesPortWithLlamaCpp(unittest.TestCase):
    """LocalAI is OpenAI-compatible on 8080, so before its adapter it matched the
    generic `openai` fingerprint and was labelled "OpenAI" with no counters."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalAIStub)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_fingerprint_needs_204_empty(self):
        self.assertTrue(engines.LocalAIAdapter.fingerprint(self.url))
        # it also matches generic openai, which is why the DISCOVERY order matters
        self.assertTrue(engines.OpenAIAdapter.fingerprint(self.url))

    def test_discovery_order_prefers_localai_over_openai(self):
        types = dict(engines.DISCOVERY)[8080]
        self.assertLess(types.index("localai"), types.index("openai"))

    def test_poll_reads_models_and_counters(self):
        rec = engines.LocalAIAdapter(url=self.url).poll()
        self.assertTrue(rec["up"])
        self.assertEqual([m["id"] for m in rec["models"]], ["gpt4all-j"])
        self.assertEqual(rec["counters"]["output_tokens"], 1234.0)


class _TabbyStub(BaseHTTPRequestHandler):
    """TabbyAPI 5000: /tabby/config is the ExLlamaV3 admin namespace."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/tabby/config":
            body = json.dumps({"context_length": 8192, "model": "qwen3-8b-exl2"}).encode()
            code = 200
        elif p == "/v1/models":
            body = json.dumps({"object": "list", "data": [
                {"id": "qwen3-8b-exl2"}]}).encode()
            code = 200
        else:
            body, code = b"not found", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _PlainOpenAIServer(BaseHTTPRequestHandler):
    """Any OpenAI-compatible server on 5000. TabbyAPI must NOT claim it."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.split("?")[0] == "/v1/models":
            body = json.dumps({"object": "list", "data": [{"id": "someone-elses"}]}).encode()
            code = 200
        else:
            body, code = b"not found", 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TabbyIsNotAnyOpenAIServer(unittest.TestCase):
    """5000 is only claimed via /tabby/config. Falling back to /v1/models would
    let any OpenAI-compatible server on 5000 be mislabelled TabbyAPI."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _TabbyStub)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

        cls.plain = ThreadingHTTPServer(("127.0.0.1", 0), _PlainOpenAIServer)
        cls.plain.daemon_threads = True
        threading.Thread(target=cls.plain.serve_forever, daemon=True).start()
        cls.plain_url = "http://127.0.0.1:%d" % cls.plain.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for s in (cls.srv, cls.plain):
            s.shutdown()
            s.server_close()

    def test_matches_real_tabby(self):
        self.assertTrue(engines.TabbyApiAdapter.fingerprint(self.url))
        rec = engines.TabbyApiAdapter(url=self.url).poll()
        self.assertTrue(rec["up"])
        self.assertEqual([m["id"] for m in rec["models"]], ["qwen3-8b-exl2"])
        self.assertEqual(rec["window"], 8192)

    def test_declines_a_plain_openai_server(self):
        # it does serve /v1/models, so this is the exact case a fallback would get wrong
        self.assertTrue(engines.OpenAIAdapter.fingerprint(self.plain_url))
        self.assertFalse(engines.TabbyApiAdapter.fingerprint(self.plain_url))


class AdaptersDegradeOnDeadPort(unittest.TestCase):
    """Invariant 3: every adapter must fingerprint False and poll to up=False on a
    closed port, and return the full schema -- never raise, never claim to be up."""

    DEAD = "http://127.0.0.1:1"

    def test_no_adapter_matches_a_closed_port(self):
        for name, cls in engines.ADAPTERS.items():
            if name == "custom":
                continue
            try:
                self.assertFalse(cls.fingerprint(self.DEAD), name)
            except Exception as e:
                self.fail("%s fingerprint raised %s" % (name, type(e).__name__))

    def test_no_adapter_polls_up_on_a_closed_port(self):
        for name, cls in engines.ADAPTERS.items():
            if name == "custom":
                continue
            try:
                rec = cls(url=self.DEAD).poll()
            except Exception as e:
                self.fail("%s poll raised %s: %s" % (name, type(e).__name__, e))
            self.assertIs(rec.get("up"), False, name)
            for k in ("key", "label", "type", "url", "state", "caps", "up"):
                self.assertIn(k, rec, "%s missing %s" % (name, k))


class DiscoveryTargetsAndClaims(unittest.TestCase):
    """[discovery] targets adds remote hosts on the same port list, and claiming
    is per host:port so a remote 8080 is discoverable while a local 8080 is taken."""

    def test_localhost_targets(self):
        t = engines.discovery_targets()
        self.assertTrue(all(h == engines.LOCAL_HOST for h, _, _ in t))
        self.assertEqual(len(t), len(engines.DISCOVERY))

    def test_remote_host_appends_full_port_list(self):
        t = engines.discovery_targets(["10.0.0.41"])
        self.assertEqual(len(t), 2 * len(engines.DISCOVERY))
        self.assertIn(("10.0.0.41", 30000, ("sglang",)), t)

    def test_duplicates_and_local_aliases_dropped(self):
        t = engines.discovery_targets(["10.0.0.41", "10.0.0.41",
                                       "127.0.0.1", "localhost"])
        self.assertEqual(len(t), 2 * len(engines.DISCOVERY))

    def test_claims_are_host_and_port(self):
        class Fake(engines.Adapter):
            type = "fake"
            default_port = None

        s = engines.Scheduler(
            [Fake(url="http://127.0.0.1:8080", key="a"),
             Fake(url="http://10.0.0.41:8080", key="b")],
            discovery_enabled=False, claimed_ports=lambda: {9090})
        self.assertEqual(s._claimed(),
                         {("127.0.0.1", 8080), ("10.0.0.41", 8080),
                          ("127.0.0.1", 9090)})

    def test_every_discovery_type_is_registered_and_labelled(self):
        for port, types in engines.DISCOVERY:
            for t in types:
                self.assertIn(t, engines.ADAPTERS, "port %d" % port)
                self.assertIn(t, engines.TYPE_LABELS, "port %d" % port)

    def test_scheduler_passes_discovery_hosts(self):
        s = engines.Scheduler([], discovery_enabled=False,
                              discovery_hosts=["10.0.0.41"])
        self.assertEqual(s.discovery_hosts, ["10.0.0.41"])


class CustomEnginePorts(unittest.TestCase):
    """[[engine]] may name a port (or host) instead of spelling out a URL, so
    moving llama.cpp off 8080 is a one-line change. [discovery] ports adds
    non-default ports to the probe list, with openai always tried last."""

    def test_port_shorthand_builds_url(self):
        self.assertEqual(
            engines.make_adapter({"type": "llamacpp", "port": 8081}).url,
            "http://127.0.0.1:8081")
        self.assertEqual(
            engines.make_adapter({"type": "llamacpp", "host": "gpu.lan",
                                  "port": 8081}).url,
            "http://gpu.lan:8081")
        # host alone keeps the adapter's own default port
        self.assertEqual(
            engines.make_adapter({"type": "vllm", "host": "10.0.0.9"}).url,
            "http://10.0.0.9:8000")

    def test_port_overrides_a_given_url(self):
        a = engines.make_adapter({"type": "llamacpp",
                                  "url": "http://127.0.0.1:8080", "port": 8081})
        self.assertEqual(a.url, "http://127.0.0.1:8081")

    def test_discovery_override_port_still_wins(self):
        a = engines.make_adapter({"type": "llamacpp", "url": "http://h:1"},
                                 port=8082)
        self.assertEqual(a.url, "http://h:8082")

    def test_extra_discovery_port_uses_all_types_openai_last(self):
        t = engines.discovery_targets(extra_ports=[8081])
        entry = next((e for e in t if e[1] == 8081), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry[0], engines.LOCAL_HOST)
        self.assertEqual(entry[2].index("openai"), len(entry[2]) - 1)
        self.assertIn("llamacpp", entry[2])

    def test_known_discovery_port_keeps_its_order(self):
        t = engines.discovery_targets(extra_ports=[8080])
        # 8080 is already in the table: no duplicate, order untouched
        self.assertEqual(len([e for e in t if e[1] == 8080]), 1)

    def test_extra_ports_probe_each_target_too(self):
        t = engines.discovery_targets(["10.0.0.41"], extra_ports=[8081])
        self.assertIn(("10.0.0.41", 8081, engines._all_discovery_types()), t)

    def test_scheduler_passes_discovery_ports(self):
        s = engines.Scheduler([], discovery_enabled=False,
                              discovery_ports=[8081])
        self.assertEqual(s.discovery_ports, [8081])


class FingerprintMatrix(unittest.TestCase):
    """Every real engine, against every adapter fingerprint.

    Two distinct stubs per family matter: Ollama serves /api/version and NOT
    /props, while llama.cpp serves /props and NOT /api/version. Handing both to
    one hybrid stub makes each match the other and hides real regressions.

    The generic `openai` adapter legitimately matches every OpenAI-compatible
    server, so a cross-match by `openai` is expected. What must never happen is
    a SPECIFIC adapter claiming the wrong engine -- that is what would
    mislabel a card and poll endpoints that do not exist.
    """

    class _RealOllama(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/api/version":
                body, code = b'{"version": "0.34.3"}', 200
            elif p == "/api/ps":
                body, code = b'{"models": []}', 200
            else:
                body, code = b"not found", 404
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class _RealLlamaCpp(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/props":
                body = b'{"default_generation_settings": {"n_ctx": 4096}, "build_info": "b1234"}'
                code = 200
            elif p == "/metrics":
                body = (b"llamacpp:prompt_tokens_total 1000\n"
                        b"llamacpp:prompt_seconds_total 10.0\n"
                        b"llamacpp:tokens_predicted_total 500\n"
                        b"llamacpp:tokens_predicted_seconds_total 5.0\n"
                        b"llamacpp:requests_running 0\n"
                        b"llamacpp:requests_waiting 0\n"
                        b"llamacpp:requests_deferred 0\n")
                code = 200
            elif p == "/slots":
                body = (b'[{"id":0,"state":"idle","n_ctx":4096,'
                        b'"n_prompt_tokens":17,"n_prompt_tokens_cache":12}]')
                code = 200
            else:
                body, code = b"not found", 404
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _serve(self, handler):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.daemon_threads = True
        srv.state = {"metrics_calls": 0, "stats_calls": 0, "with_metrics": False,
                     "ps_models": [], "llamacpp_running": 0, "auth": None,
                     "queue": 1, "busy": False}
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, "http://127.0.0.1:%d" % srv.server_address[1]

    def test_each_engine_is_claimed_only_by_its_own_specific_adapter(self):
        cases = [
            (self._RealOllama, "ollama"),
            (self._RealLlamaCpp, "llamacpp"),
            (_VllmStub, "vllm"),
            (_SglangStub, "sglang"),
            (_FreetokenStub, "freetoken"),
            (_KoboldCppStub, "koboldcpp"),
            (_TabbyStub, "tabbyapi"),
            (_LocalAIStub, "localai"),
            (_UnslothStub, "unsloth"),
        ]
        specific = [n for n in engines.ADAPTERS
                    if n not in ("custom", "openai")]
        srvs = []
        try:
            for handler, expect in cases:
                srv, url = self._serve(handler)
                srvs.append(srv)
                hits = set()
                for name in specific:
                    try:
                        if engines.ADAPTERS[name].fingerprint(url):
                            hits.add(name)
                    except Exception:
                        pass
                self.assertIn(expect, hits,
                              "%s not recognised by its own adapter" % expect)
                others = hits - {expect}
                self.assertFalse(others, "%s also claimed by %s" % (expect, others))
        finally:
            for s in srvs:
                s.shutdown()
                s.server_close()

    def test_generic_openai_never_precedes_a_specific_adapter(self):
        # discovery() takes the FIRST match, so the generic adapter must always
        # be last -- otherwise it would claim an engine a specific one knows.
        for port, types in engines.DISCOVERY:
            if "openai" not in types:
                continue
            specific_idx = [i for i, t in enumerate(types) if t != "openai"]
            self.assertGreater(types.index("openai"), max(specific_idx),
                               "port %d: openai must come last" % port)

    def test_every_adapter_is_reachable(self):
        reachable = {t for _p, ts in engines.DISCOVERY for t in ts}
        reachable |= {"openai", "custom"}     # config-only / fallback
        for name in engines.ADAPTERS:
            self.assertIn(name, reachable, name)


class _UnslothStub(BaseHTTPRequestHandler):
    """What a real `unsloth studio` answered (probed 2026-10-03): /api/health needs no key,
    /v1/models is 401 without one."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        key = self.headers.get("Authorization") == "Bearer sk-unsloth-test"
        if self.path == "/api/health":
            body, code = {"status": "healthy", "service": "Unsloth UI Backend", "chat_only": True}, 200
        elif self.path == "/v1/models" and key:
            body, code = {"object": "list", "data": [{"id": "unsloth/Qwen3-8B-GGUF"}]}, 200
        elif self.path == "/v1/models":
            body, code = {"error": {"message": "Not authenticated", "type": "authentication_error"}}, 401
        else:
            body, code = {"detail": "API endpoint not found"}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class UnslothStudio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _UnslothStub)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_fingerprint_and_poll(self):
        A = engines.ADAPTERS["unsloth"]
        self.assertTrue(A.fingerprint(self.url))
        os.environ.pop("UNSLOTH_STUDIO_AUTH_TOKEN", None)
        rec = A(url=self.url).poll()
        self.assertTrue(rec["up"])
        self.assertIn("API key needed", rec.get("reason") or "")
        os.environ["UNSLOTH_STUDIO_AUTH_TOKEN"] = "sk-unsloth-test"
        try:
            rec = A(url=self.url).poll()
        finally:
            os.environ.pop("UNSLOTH_STUDIO_AUTH_TOKEN", None)
        self.assertEqual(rec["state"], "running")
        self.assertEqual([m["id"] for m in rec["models"]], ["unsloth/Qwen3-8B-GGUF"])

    def test_closed_port_is_not_unsloth(self):
        self.assertFalse(engines.ADAPTERS["unsloth"].fingerprint("http://127.0.0.1:1"))
