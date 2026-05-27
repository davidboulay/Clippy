"""User preferences, persisted as JSON at ``config.SETTINGS_PATH``.

GTK-free so the ``_store`` hook can read e.g. the sound / plain-text prefs.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from . import config

DEFAULTS: Dict[str, Any] = {
    "open_at_login": True,
    "sound_on_copy": False,
    "always_plain_text": False,
    "retention": "1m",
    # "system" follows COSMIC's light/dark; or force "dark" / "light".
    "theme_mode": "system",
    # Stored for display; the actual binding lives in COSMIC's config.
    "shortcut": {"modifiers": ["Super"], "key": "v"},
}


def load() -> Dict[str, Any]:
    data = dict(DEFAULTS)
    try:
        with open(config.SETTINGS_PATH, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            data.update({k: stored[k] for k in stored if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


def save(data: Dict[str, Any]) -> None:
    config.ensure_dirs()
    merged = dict(DEFAULTS)
    merged.update({k: data[k] for k in data if k in DEFAULTS})
    tmp = config.SETTINGS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
    tmp.replace(config.SETTINGS_PATH)


def get(key: str) -> Any:
    return load().get(key, DEFAULTS.get(key))


def set_value(key: str, value: Any) -> None:
    data = load()
    data[key] = value
    save(data)
