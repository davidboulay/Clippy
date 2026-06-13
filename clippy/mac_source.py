"""Mac-only: remember which app each clip was copied from.

A tiny JSON map of entry id -> source app bundle identifier, recorded at capture
time (the frontmost app when the clipboard changed). The panel shows that app's
icon in the tile header. macOS-only; shared core untouched.
"""
from __future__ import annotations

import json

from . import config

_PATH = config.DATA_DIR / "mac_sources.json"
_cache = {"mtime": None, "data": {}}


def _load() -> dict:
    try:
        d = json.loads(_PATH.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _data() -> dict:
    try:
        m = _PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != m:
        _cache["mtime"] = m
        _cache["data"] = _load()
    return _cache["data"]


def record(entry_id: int, bundle_id: str) -> None:
    if not bundle_id:
        return
    d = _load()
    d[str(entry_id)] = bundle_id
    if len(d) > 4000:                      # keep it from growing unbounded
        d = dict(list(d.items())[-2000:])
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(d))
        _cache["mtime"] = None             # invalidate
    except OSError:
        pass


def get(entry_id: int):
    return _data().get(str(entry_id))
