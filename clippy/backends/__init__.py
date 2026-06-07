"""Clipboard backend selection by platform."""
from __future__ import annotations

import sys

from .base import ClipboardBackend, ClipboardError  # noqa: F401

_backend = None


def get_backend() -> "ClipboardBackend":
    """Return the singleton clipboard backend for this OS."""
    global _backend
    if _backend is None:
        if sys.platform == "darwin":
            from .mac import MacBackend
            _backend = MacBackend()
        else:
            from .wayland import WaylandBackend
            _backend = WaylandBackend()
    return _backend
