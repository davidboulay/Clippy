"""Desktop-environment integration, selected at runtime.

Mirrors ``clippy/backends/``: one module per environment behind a common
interface (see ``base.py``), chosen once per process. The order below is the
detection order — most specific first, ``generic`` last, which always matches.
"""
from __future__ import annotations

import sys

from .base import MODIFIERS, SEMANTIC_COLORS, Desktop, Shortcut, parse_hex  # noqa: F401

_desktop = None


def _detect() -> "Desktop":
    if sys.platform == "darwin":       # macOS binds its own hotkey in mac_app
        from .generic import GenericDesktop
        return GenericDesktop()

    from .cosmic import CosmicDesktop
    from .hyprland import HyprlandDesktop
    for cls in (CosmicDesktop, HyprlandDesktop):
        try:
            candidate = cls()
            if candidate.detected():
                return candidate
        except Exception:
            continue

    from .generic import GenericDesktop
    return GenericDesktop()


def get_desktop() -> "Desktop":
    """The singleton desktop integration for this session."""
    global _desktop
    if _desktop is None:
        _desktop = _detect()
    return _desktop
