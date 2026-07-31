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
    # Which copy sound to play (see sound.SOUND_CHOICES).
    "sound_choice": "tap",
    "always_plain_text": False,
    # Offer a recovered image as a *file* as well as raw bytes. File-drop targets
    # (COSMIC Files, the VS Code Explorer) can only paste an image that way, but
    # chat apps that accept both make one attachment per flavor — Slack shows the
    # image plus empty duplicates. Pasting into chat is the common case, so this
    # is off by default.
    "image_file_flavors": False,
    # EXPERIMENTAL. After any app copies an image on Wayland, take the X11
    # selection and serve the bytes ourselves, so XWayland apps (Claude Desktop
    # runs --ozone-platform=x11) can paste it. Without this they get nothing:
    # Xwayland exports text selections to X11 but never image ones, and a
    # wlr-data-control clip (what CosmicShot's wl-copy writes) never reaches X11
    # at all. Recovering the same clip from the panel already works, because that
    # path makes us the selection owner — measured serving in 0.0s and still
    # doing so 85s later.
    #
    # KNOWN NOT TO WORK on cosmic-comp — leave it off. Doing this at capture time
    # was shipped in 1.4.21 and reverted in 1.4.22 (both channels dead ~35s after
    # a copy). Retried here on the theory that the collapse was two owners
    # contending rather than us owning X11, since the recover path holds fine at
    # t+85s — and tested on a real copy: still empty images. Three mechanisms have
    # failed at capture time and all three work on recover. Kept as the record of
    # the attempt; turning it on reproduces the failure rather than fixing it.
    "x11_image_takeover": False,
    "retention": "1m",
    # "system" follows COSMIC's light/dark; or force "dark" / "light".
    "theme_mode": "system",
    # Periodically check GitHub for a newer release.
    "auto_check_updates": True,
    # State (not a user-facing pref): unix time of the last automatic check.
    "last_update_check": 0,
    # Stored for display; the actual binding lives in COSMIC's config.
    "shortcut": {"modifiers": ["Super"], "key": "v"},
    # LAN clipboard sync.
    "sync_enabled": False,
    "device_name": "",   # empty => fall back to the hostname
    # Largest media payload to sync, in bytes (clamped to config.SYNC_MAX_CEILING).
    "sync_max_bytes": 512 * 1024 * 1024,
    # Show a transfer progress bar on the sender above this size (bytes).
    "progress_min_bytes": 5 * 1024 * 1024,
    # macOS: show the "allow incoming connections" firewall hint once.
    "mac_firewall_hint_shown": False,
    # macOS: launch at login (default on; installs a LaunchAgent).
    "start_at_login": True,
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
