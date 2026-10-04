#!/usr/bin/env python3
"""Speculum config loading (DESIGN.md section 1, speculum.toml).

The file is optional: with none found, discovery is on and everything
else is default. Search order:

    1. --config argument (explicit path, error if it does not exist)
    2. <repo root>/speculum.toml          (next to collector/)
    3. ~/.config/speculum/speculum.toml   (Windows: %APPDATA%\\Speculum\\)

Only stdlib (tomllib on 3.11+).
"""

import os
import sys
import tomllib
from pathlib import Path

DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8792},
    "discovery": {"enabled": True},
    "history": {"retention_days": 30},   # 30, 60, 90 or 0 to turn off
    "engine": [],
}


def config_candidates():
    root = Path(__file__).resolve().parent.parent
    cands = [root / "speculum.toml"]
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        cands.append(Path(base) / "Speculum" / "speculum.toml")
    else:
        cands.append(Path(os.environ.get(
            "XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "speculum" / "speculum.toml")
    return cands


def _merge(dst, src):
    for k, v in (src or {}).items():
        dst[k] = v
    return dst


def _clean_engine(e):
    """Keep only the keys an engine table may carry; drop comment-only
    or unknown tables (a [[engine]] with no type is an error, not a
    discovery candidate)."""
    out = {}
    for k in ("name", "type", "url", "parent", "api_key_env", "optional",
              "health", "models", "metrics", "slots", "map"):
        if k in e:
            out[k] = e[k]
    return out


def load(path=None):
    """Return (cfg, source). `path` is the --config argument or None.
    source is the file that was read, or None for built-in defaults."""
    used = None
    if path is None:
        for c in config_candidates():
            if c.is_file():
                used = c
                break
    else:
        used = Path(path)
        if not used.is_file():
            raise FileNotFoundError("config file not found: %s" % used)
    cfg = {
        "server": dict(DEFAULTS["server"]),
        "discovery": dict(DEFAULTS["discovery"]),
        "history": dict(DEFAULTS["history"]),
        "engine": [],
    }
    if used is None:
        return cfg, None
    raw = tomllib.loads(used.read_text(encoding="utf-8"))
    _merge(cfg["server"], raw.get("server"))
    _merge(cfg["discovery"], raw.get("discovery"))
    _merge(cfg["history"], raw.get("history"))
    for e in raw.get("engine") or []:
        if isinstance(e, dict) and e.get("type"):
            cfg["engine"].append(_clean_engine(e))
    return cfg, used
