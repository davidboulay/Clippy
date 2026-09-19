"""Fallback desktop: any Wayland session we don't have a backend for.

Binds nothing (we have no idea where this compositor keeps its shortcuts) and
reads dark/light from the cross-desktop sources: the XDG settings portal first,
then GSettings. Both are advisory — a compositor with neither still gets
Clippy's built-in dark palette.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional

from .base import Shortcut

# org.freedesktop.appearance color-scheme: 0 no preference, 1 dark, 2 light.
_PORTAL_DARK = 1


def _portal_color_scheme() -> Optional[int]:
    try:
        proc = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.Settings.ReadOne",
             "org.freedesktop.appearance", "color-scheme"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # "(<uint32 1>,)"
    digits = "".join(ch for ch in proc.stdout if ch.isdigit())
    return int(digits) if digits else None


def _gsettings_prefers_dark() -> Optional[bool]:
    try:
        proc = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip().strip("'\"")
    if value in ("prefer-dark", "default-dark"):
        return True
    if value == "prefer-light":
        return False
    return None


class GenericDesktop:
    name = "generic"
    label = os.environ.get("XDG_CURRENT_DESKTOP") or "this desktop"

    def detected(self) -> bool:
        return True  # the last resort always matches

    # -- global shortcut --------------------------------------------------
    def can_bind(self) -> bool:
        return False

    def read_shortcut(self) -> Optional[Shortcut]:
        return None

    def read_unmanaged_shortcut(self) -> Optional[Shortcut]:
        return None

    def set_shortcut(self, modifiers: List[str], key: str, command: str) -> bool:
        return False

    def remove_shortcut(self) -> bool:
        return False

    def shortcut_help(self, command: str) -> str:
        return (
            f"Clippy could not detect where {self.label} keeps its keyboard\n"
            "shortcuts, so bind one yourself in its settings:\n\n"
            f"    Command:  {command}\n"
            "    Key:      e.g. Super + Shift + V\n"
        )

    # -- clipboard quirks -------------------------------------------------
    def x11_owner_serves_wayland(self) -> bool:
        # The normal case: the compositor bridges its own selections, so
        # wl-copy is what reaches Wayland apps and the X11 owner is only for
        # XWayland. Hyprland inherits this.
        return False

    # -- panel quirks -----------------------------------------------------
    def focus_out_means_click_away(self) -> bool:
        # No: assume a wlroots-style compositor, where keyboard focus follows
        # the mouse and window activation rather than the user's intent, and
        # the panel is better off catching the click itself. Hyprland inherits
        # this.
        return False

    # -- theme ------------------------------------------------------------
    def is_dark(self) -> bool:
        scheme = _portal_color_scheme()
        if scheme is not None and scheme != 0:
            return scheme == _PORTAL_DARK
        prefers = _gsettings_prefers_dark()
        if prefers is not None:
            return prefers
        return True

    def palette(self, dark: bool) -> dict:
        return {}

    def theme_paths(self) -> List[Path]:
        return []
