"""
Config file management.  Loads/saves config.json from a well-known location.

Passwords are **not** written to disk. The web UI keeps the Navidrome password in the
Flask session only. Legacy ``server.password`` keys in an existing file are ignored
on load and removed on the next save.

Config schema:
{
  "server": {
    "host": "https://music.example.com",
    "username": "admin"
  },
  "device": {
    "mount_path": ""          // empty = auto-detect
  },
  "sync": {
    "transcode_format": "mp3",  // format name matching a Navidrome transcoding profile
    "default_quality": "transcode",  // "transcode" | "original" for new artist selections
    "sync_starred": true
  }
}
"""

import copy
import json
from pathlib import Path


# ============================================================================
# Defaults
# ============================================================================

_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "syncsonic" / "config.json"

_DEFAULTS: dict = {
    "server": {
        "host": "",
        "username": "",
    },
    "device": {
        "mount_path": "",
    },
    "sync": {
        "transcode_format": "mp3",
        "default_quality": "transcode",
        "sync_starred": True,
    },
}


# ============================================================================
# Load / save
# ============================================================================

def _merge(base: dict, override: dict) -> dict:
    """Deep merge override into base, returning a new dict."""
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def load(path: Path = _DEFAULT_CONFIG_PATH) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        merged = _merge(_DEFAULTS, on_disk)
    else:
        merged = _merge(_DEFAULTS, {})
    # Never expose a password from disk to callers (legacy files may still contain it).
    if "server" in merged and "password" in merged["server"]:
        merged = copy.deepcopy(merged)
        del merged["server"]["password"]
    return merged


def save(config: dict, path: Path = _DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    to_write = copy.deepcopy(config)
    if "server" in to_write and "password" in to_write["server"]:
        del to_write["server"]["password"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_write, f, indent=2)


def db_path(config_path: Path = _DEFAULT_CONFIG_PATH) -> Path:
    return config_path.parent / "manifest.db"
