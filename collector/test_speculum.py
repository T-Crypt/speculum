#!/usr/bin/env python3
"""Tests for KPI model attribution: engine_model_name, and the tps_models field
kpi_pass emits so the UI can name the model behind the decode rate."""

import unittest

import speculum


def _reset_kpi_state():
    S = speculum.S
    S.engines.clear()
    S._swap_models = []
    S.strata["models"] = []
    S.requests.clear()
    S.gpu["last"] = None
    S.host["last"] = None


class TestEngineModelName(unittest.TestCase):
    def test_by_origin(self):
        swap = [("llama", "llama-3.3-70b"), ("ninfer", "NInfer-Qwen3-8B")]
        strata = ["strata-model"]
        e = {
            "llama":  {"key": "llama", "origin": "llama-swap", "up": True, "label": "llama-3.3-70b"},
            "ninfer": {"key": "ninfer", "origin": "ninfer", "up": True, "label": "NInfer-Qwen3-8B"},
            "strata": {"key": "strata", "origin": "strata", "up": True, "label": "Strata"},
            "ollama": {"key": "ollama", "origin": "ollama", "up": True, "label": "Ollama",
                       "models": [{"id": "qwen3:8b"}]},
            "down":   {"key": "vllm", "origin": "vllm", "up": False, "label": "vLLM",
                       "models": [{"id": "m"}]},
        }
        self.assertEqual(speculum.engine_model_name("llama", e["llama"], swap, strata), "llama-3.3-70b")
        self.assertEqual(speculum.engine_model_name("ninfer", e["ninfer"], swap, strata), "NInfer-Qwen3-8B")
        self.assertEqual(speculum.engine_model_name("strata", e["strata"], swap, strata), "strata-model")
        self.assertEqual(speculum.engine_model_name("ollama", e["ollama"], swap, strata), "qwen3:8b")
        self.assertIsNone(speculum.engine_model_name("vllm", e["down"], swap, strata))
        # idle llama-swap engine (absent from the running set) attributes no model
        self.assertIsNone(speculum.engine_model_name("llama", e["llama"], [], strata))

    def test_discovered_engine_without_models(self):
        e = {"key": "sglang", "origin": "sglang", "up": True, "label": "SGLang"}
        self.assertIsNone(speculum.engine_model_name("sglang", e, [], []))


class TestKpiPassTpsModels(unittest.TestCase):
    def setUp(self):
        _reset_kpi_state()

    def test_ninfer_decode_rate_names_model(self):
        S = speculum.S
        e = S.engine("ninfer", "NInfer-Qwen3-8B", "ninfer")
        e["up"] = True
        e["rates"] = {"decode_tps": 72.0}
        speculum.kpi_pass()
        self.assertEqual(S.kpi["last"]["tps_models"], ["NInfer-Qwen3-8B"])

    def test_llama_swap_model_from_running_set(self):
        S = speculum.S
        e = S.engine("llama", "llama-3.3-70b", "llama-swap")
        e["up"] = True
        e["rates"] = {"decode_tps": 45.0}
        S._swap_models = [("llama", "llama-3.3-70b")]
        speculum.kpi_pass()
        self.assertEqual(S.kpi["last"]["tps_models"], ["llama-3.3-70b"])

    def test_idle_engine_attributes_no_model(self):
        S = speculum.S
        e = S.engine("ninfer", "NInfer-Qwen3-8B", "ninfer")
        e["up"] = True
        e["rates"] = None            # up but not decoding: no model to attribute
        speculum.kpi_pass()
        self.assertEqual(S.kpi["last"]["tps_models"], [])


class TestBuiltinEndpoints(unittest.TestCase):
    """llama-swap and Strata can be moved off their stock ports, by url or by
    the host/port shorthand, since the built-in threads are not discovery."""

    def test_default_when_unset(self):
        self.assertEqual(speculum.endpoint_url({}, "http://127.0.0.1:9090"),
                         "http://127.0.0.1:9090")
        self.assertEqual(speculum.endpoint_url(None, "http://127.0.0.1:9090"),
                         "http://127.0.0.1:9090")

    def test_url_override(self):
        self.assertEqual(
            speculum.endpoint_url({"url": "http://127.0.0.1:9091/"},
                                  "http://127.0.0.1:9090"),
            "http://127.0.0.1:9091")

    def test_port_shorthand_overrides_default(self):
        self.assertEqual(
            speculum.endpoint_url({"port": 9091}, "http://127.0.0.1:9090"),
            "http://127.0.0.1:9091")

    def test_host_shorthand(self):
        self.assertEqual(
            speculum.endpoint_url({"host": "gpu.lan"},
                                  "http://127.0.0.1:8080"),
            "http://gpu.lan:8080")


if __name__ == "__main__":
    unittest.main()
