#!/usr/bin/env python3
"""Config tests: per-engine host/port keys survive cleaning, the built-in
endpoint tables have defaults, and a TOML file overrides them."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

import config


class TestCleanEngine(unittest.TestCase):
    def test_host_and_port_kept(self):
        e = config._clean_engine(
            {"type": "llamacpp", "host": "10.0.0.9", "port": 8081,
             "url": "http://127.0.0.1:8080", "unknown": "drop me"})
        self.assertEqual(e, {"type": "llamacpp", "host": "10.0.0.9",
                             "port": 8081, "url": "http://127.0.0.1:8080"})

    def test_defaults_carry_endpoint_tables(self):
        self.assertEqual(config.DEFAULTS["llama_swap"]["url"],
                         "http://127.0.0.1:9090")
        self.assertEqual(config.DEFAULTS["strata"]["url"],
                         "http://127.0.0.1:8080")
        self.assertEqual(config.DEFAULTS["discovery"]["ports"], [])


@unittest.skipUnless(importlib.util.find_spec("tomllib"),
                     "tomllib needs Python 3.11+")
class TestLoadToml(unittest.TestCase):
    def _load(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "speculum.toml"
            p.write_text(text, encoding="utf-8")
            return config.load(str(p))[0]

    def test_engine_port_survives(self):
        cfg = self._load(
            '[[engine]]\nname = "llama"\ntype = "llamacpp"\nport = 8081\n')
        self.assertEqual(cfg["engine"][0]["port"], 8081)

    def test_discovery_ports_and_builtin_urls(self):
        cfg = self._load(
            '[discovery]\nports = [8081, 9091]\n'
            '[llama_swap]\nport = 9091\n'
            '[strata]\nurl = "http://127.0.0.1:8090"\n')
        self.assertEqual(cfg["discovery"]["ports"], [8081, 9091])
        self.assertEqual(cfg["llama_swap"]["port"], 9091)
        self.assertEqual(cfg["strata"]["url"], "http://127.0.0.1:8090")


if __name__ == "__main__":
    unittest.main(verbosity=2)
