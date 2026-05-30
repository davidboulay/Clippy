"""Filesystem paths and tunable constants.

Persistent state lives under the XDG data dir; the IPC socket under the
runtime dir (cleaned up on logout). User-adjustable preferences live in
``settings.py``; this module holds only fixed paths and limits.
"""
from __future__ import annotations

import os
from pathlib import Path


def _xdg(env: str, default: Path) -> Path:
    val = os.environ.get(env)
    return Path(val) if val else default


HOME = Path.home()
DATA_DIR = _xdg("XDG_DATA_HOME", HOME / ".local" / "share") / "clippy"
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", HOME / ".config") / "clippy"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))

DB_PATH = DATA_DIR / "history.db"
IMAGE_DIR = DATA_DIR / "images"
SOCKET_PATH = RUNTIME_DIR / "clippy.sock"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SOUND_PATH = DATA_DIR / "copy.wav"
# Icon copied into a private theme dir so the tray can reference it by name.
ICON_THEME_DIR = DATA_DIR / "icons"
ICON_PATH = ICON_THEME_DIR / "clippy.png"

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
# Icon ships inside the package so it resolves under any install layout
# (source tree, .deb, Flatpak, AppImage).
BUNDLED_ICON = PACKAGE_ROOT / "icons" / "clippy.png"

# Hard safety cap regardless of the time-based retention setting.
MAX_HISTORY = 1000

# How many tiles to build into the panel at once. The strip is a horizontal
# row nobody scrolls hundreds of items through, and each tile (especially an
# image one) costs real time to construct, so we cap the rendered set to keep
# opening instant as history grows. Search still queries the whole history.
DISPLAY_LIMIT = 60

# Largest image (bytes) we will store. Bigger payloads are skipped.
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MiB

# Panel geometry (logical pixels).
PANEL_HEIGHT = 320
TILE_WIDTH = 230
TILE_HEIGHT = 250
# Fixed height of a tile's content area (text/image). Content beyond this is
# clipped so every tile is exactly the same height.
TILE_CONTENT_HEIGHT = TILE_HEIGHT - 78  # ~172px

# Preferred clipboard MIME types, in priority order.
IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/bmp", "image/tiff")
TEXT_TYPES = (
    "text/plain;charset=utf-8",
    "text/plain",
    "UTF8_STRING",
    "STRING",
)
HTML_TYPES = ("text/html", "text/html;charset=utf-8")

# History retention options: key -> (label, seconds or None for forever).
RETENTION_OPTIONS = [
    ("1d", "1 day", 86_400),
    ("1w", "1 week", 604_800),
    ("1m", "1 month", 2_592_000),
    ("1y", "1 year", 31_536_000),
    ("forever", "Forever", None),
]


def retention_seconds(key: str):
    for k, _label, secs in RETENTION_OPTIONS:
        if k == key:
            return secs
    return None


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ICON_THEME_DIR.mkdir(parents=True, exist_ok=True)
