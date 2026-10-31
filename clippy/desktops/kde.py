"""KDE Plasma integration.

Plasma keeps global shortcuts in kglobalaccel (in-process in KWin on Plasma 6),
and an application's launch shortcut is the ``_launch`` action of a component
named after its desktop entry: System Settings → Shortcuts → Add Application
writes exactly that, as ``[services][clippy.desktop] _launch=Meta+Shift+V`` in
``kglobalshortcutsrc``.

So Clippy binds the desktop entry it already ships (whose ``Exec`` is
``clippy toggle``) through kglobalaccel's D-Bus API. Nothing is written to a
config file by hand, the change is live immediately, and the binding is the
same one System Settings shows: either side can change it, and there is never
a second, duplicate binding to find later.

kglobalaccel encodes a key as a Qt key combination (modifier flags OR a
``Qt::Key``); the tables below translate to and from Clippy's
``(modifiers, gdk keyval name)`` vocabulary.

The theme comes from the active color scheme in ``kdeglobals`` (``r,g,b``
triplets per ``[Colors:*]`` group), and dark/light from its window background,
so Clippy matches the scheme rather than only the portal's binary hint. The
clipboard/panel quirks are the generic ones: KWin bridges its own selections.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from .. import config
from .base import Shortcut
from .generic import GenericDesktop

_DEST = "org.kde.kglobalaccel"
_PATH = "/kglobalaccel"
_IFACE = "org.kde.KGlobalAccel"
_ACTION = "_launch"

# KGlobalAccel::SetShortcutFlag. SetPresent is what makes KWin act on the key:
# a shortcut set without it is stored, listed by System Settings and even fires
# from invokeShortcut, but a real key press is ignored. NoAutoloading keeps
# kglobalaccel from replacing our keys with a stored older value.
_SET_PRESENT = 0x2
_NO_AUTOLOADING = 0x4

# Qt::KeyboardModifier flags, in Clippy's modifier vocabulary.
_QT_MODS = {
    "Shift": 0x02000000,
    "Ctrl": 0x04000000,
    "Alt": 0x08000000,
    "Super": 0x10000000,   # Qt calls it Meta
}
_MOD_MASK = 0xFE000000

# gdk keyval names (lowercase as captured) -> Qt::Key, for keys whose Qt code is
# not simply their uppercase character.
_QT_KEYS = {
    "escape": 0x01000000, "tab": 0x01000001, "backspace": 0x01000003,
    "return": 0x01000004, "kp_enter": 0x01000005, "insert": 0x01000006,
    "delete": 0x01000007, "pause": 0x01000008, "print": 0x01000009,
    "home": 0x01000010, "end": 0x01000011, "left": 0x01000012,
    "up": 0x01000013, "right": 0x01000014, "down": 0x01000015,
    "page_up": 0x01000016, "page_down": 0x01000017, "menu": 0x01000055,
    "space": 0x20, "apostrophe": 0x27, "comma": 0x2C, "minus": 0x2D,
    "period": 0x2E, "slash": 0x2F, "semicolon": 0x3B, "equal": 0x3D,
    "bracketleft": 0x5B, "backslash": 0x5C, "bracketright": 0x5D,
    "grave": 0x60,
}
_QT_F1 = 0x01000030


def _key_to_qt(key: str) -> Optional[int]:
    name = (key or "").strip()
    low = name.lower()
    if low in _QT_KEYS:
        return _QT_KEYS[low]
    m = re.fullmatch(r"f([1-9]|[1-2][0-9]|3[0-5])", low)
    if m:
        return _QT_F1 + int(m.group(1)) - 1
    if len(name) == 1 and 0x20 < ord(name) < 0x7F:
        return ord(name.upper())
    return None


def _qt_to_key(code: int) -> Optional[str]:
    for name, value in _QT_KEYS.items():
        if value == code:
            # Report the gdk spelling the capture would have produced.
            return {"page_up": "Page_Up", "page_down": "Page_Down",
                    "backspace": "BackSpace", "return": "Return",
                    "kp_enter": "KP_Enter", "escape": "Escape", "tab": "Tab",
                    "insert": "Insert", "delete": "Delete", "pause": "Pause",
                    "print": "Print", "home": "Home", "end": "End",
                    "left": "Left", "up": "Up", "right": "Right",
                    "down": "Down", "menu": "Menu"}.get(name, name)
    if _QT_F1 <= code < _QT_F1 + 35:
        return f"F{code - _QT_F1 + 1}"
    if 0x20 < code < 0x7F:
        return chr(code).lower()
    return None


def combo_to_qt(modifiers: Sequence[str], key: str) -> Optional[int]:
    """Clippy's ``(["Super", "Shift"], "v")`` as a Qt key combination int."""
    code = _key_to_qt(key)
    if code is None:
        return None
    for mod in modifiers:
        if mod not in _QT_MODS:
            return None
        code |= _QT_MODS[mod]
    return code


def qt_to_combo(value: int) -> Optional[Shortcut]:
    key = _qt_to_key(value & ~_MOD_MASK)
    if key is None:
        return None
    # Same order the settings-window capture produces.
    mods = [m for m in ("Super", "Ctrl", "Alt", "Shift") if value & _QT_MODS[m]]
    return mods, key


def _kdeglobals() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "kdeglobals"


def _gvariant_strv(items: Sequence[str]) -> str:
    return "[" + ", ".join("'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
                           for s in items) + "]"


class KdeDesktop(GenericDesktop):
    name = "kde"
    label = "KDE Plasma"

    def detected(self) -> bool:
        desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
        if desktop:
            return "kde" in desktop.split(":")
        return os.environ.get("KDE_FULL_SESSION", "").lower() == "true"

    # -- D-Bus plumbing (overridden in tests) ------------------------------
    def _call(self, method: str, *args: str, path: str = _PATH,
              iface: str = _IFACE) -> Optional[str]:
        """``gdbus call`` on kglobalaccel; stdout, or None on any failure."""
        try:
            proc = subprocess.run(
                ["gdbus", "call", "--session", "--dest", _DEST,
                 "--object-path", path, "--method", f"{iface}.{method}", *args],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    @property
    def component(self) -> str:
        """The desktop entry whose launch action we bind: the Flatpak's own
        exported entry inside the sandbox, clippy.desktop everywhere else."""
        flatpak_id = os.environ.get("FLATPAK_ID")
        return f"{flatpak_id}.desktop" if flatpak_id else "clippy.desktop"

    def _action_id(self) -> str:
        return _gvariant_strv([self.component, _ACTION, "Clippy",
                               "Show clipboard history"])

    def _entry_installed(self) -> bool:
        if os.environ.get("FLATPAK_ID"):
            return True  # flatpak exports its own entry to the host
        data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
        dirs = [data_home] + (os.environ.get("XDG_DATA_DIRS")
                              or "/usr/local/share:/usr/share").split(":")
        return any((Path(d) / "applications" / self.component).is_file()
                   for d in dirs if d)

    def _install_entry(self) -> bool:
        """Write ~/.local/share/applications/clippy.desktop for an install that
        has none (a source checkout with the launcher in ~/.local/bin)."""
        from .. import setup  # late: setup imports this package
        try:
            return setup.install_desktop_entry() == 0
        except OSError:
            return False

    # -- global shortcut --------------------------------------------------
    def can_bind(self) -> bool:
        return True

    def read_shortcut(self) -> Optional[Shortcut]:
        out = self._call("shortcutKeys", self._action_id())
        if not out:
            return None
        # "([([301989974, 0, 0, 0],)],)" -> the first key of the first sequence
        m = re.search(r"\[\s*(-?\d+)", out)
        if not m or int(m.group(1)) == 0:
            return None
        return qt_to_combo(int(m.group(1)) & 0xFFFFFFFF)

    def read_unmanaged_shortcut(self) -> Optional[Shortcut]:
        # The binding is kglobalaccel's, whichever UI set it, and read_shortcut
        # already reads it. There is no second place a hand-made one could be.
        return None

    def set_shortcut(self, modifiers: List[str], key: str, command: str) -> bool:
        code = combo_to_qt(modifiers, key)
        if code is None:
            return False
        if not self._entry_installed() and not self._install_entry():
            return False
        action = self._action_id()
        if self._call("doRegister", action) is None:
            return False
        # Not setForeignShortcutKeys: it stores the keys without marking the
        # shortcut present, and KWin then ignores the key press (found live on
        # Plasma 6.8: listed, invokable over D-Bus, dead on the keyboard).
        if self._call("setShortcutKeys", action, f"[([{code}, 0, 0, 0],)]",
                      str(_SET_PRESENT | _NO_AUTOLOADING)) is None:
            return False
        return self.read_shortcut() is not None

    def remove_shortcut(self) -> bool:
        action = self._action_id()
        if self._call("setShortcutKeys", action, "[]",
                      str(_SET_PRESENT | _NO_AUTOLOADING)) is None:
            return False
        self._call("unregister", self.component, _ACTION)
        return True

    # -- capture ----------------------------------------------------------
    _BLOCK_MARKER = config.RUNTIME_DIR / "clippy-kde-shortcuts-blocked"

    def begin_capture(self) -> None:
        """Suspend global shortcuts while the settings window records a key,
        as System Settings does -- otherwise KWin acts on an already-bound
        combo (toggling the panel) and the window never sees the press.

        The suspension is global and outlives this process, so a marker in
        the runtime dir records that we hold it; recover_capture() lifts it
        if Clippy quit or crashed mid-capture."""
        try:
            self._BLOCK_MARKER.touch()
        except OSError:
            pass
        self._call("blockGlobalShortcuts", "true")

    def end_capture(self) -> None:
        self._call("blockGlobalShortcuts", "false")
        try:
            self._BLOCK_MARKER.unlink()
        except OSError:
            pass

    def recover_capture(self) -> None:
        """Daemon start/exit: undo a suspension a previous capture left on."""
        if self._BLOCK_MARKER.exists():
            self.end_capture()

    # -- theme ------------------------------------------------------------
    def _scheme(self) -> dict:
        """``{(group, key): (r, g, b)}`` from kdeglobals: the user's file over
        the system default, as KConfig cascades them."""
        out: dict = {}
        files = [Path("/etc/xdg/kdeglobals"), _kdeglobals()]
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            group = ""
            for raw in text.splitlines():
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    group = line[1:-1]
                    continue
                if not group.startswith("Colors:") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                parts = value.strip().split(",")
                if len(parts) < 3:
                    continue
                try:
                    rgb = tuple(max(0, min(255, int(p))) / 255.0 for p in parts[:3])
                except ValueError:
                    continue
                out[(group[len("Colors:"):], key.strip())] = rgb
        return out

    def is_dark(self) -> bool:
        bg = self._scheme().get(("Window", "BackgroundNormal"))
        if bg is not None:
            r, g, b = bg
            return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5
        return super().is_dark()

    def palette(self, dark: bool) -> dict:
        scheme = self._scheme()
        bg = scheme.get(("Window", "BackgroundNormal"))
        # A color scheme is one mode. Asked for the other, let the built-in
        # palette answer rather than paint a light panel with dark colors.
        if bg is None or self.is_dark() != dark:
            return {}
        wanted = {
            "background": ("Window", "BackgroundNormal"),
            "surface": ("Button", "BackgroundNormal"),
            "foreground": ("Window", "ForegroundNormal"),
            "accent": ("Selection", "BackgroundNormal"),
            "danger": ("Window", "ForegroundNegative"),
            "warning": ("Window", "ForegroundNeutral"),
        }
        return {name: scheme[k] for name, k in wanted.items() if k in scheme}

    def theme_paths(self) -> List[Path]:
        path = _kdeglobals()
        return [path] if path.exists() else []

    def shortcut_help(self, command: str) -> str:
        return (
            "Easiest: open Clippy's Settings and use the shortcut picker — it\n"
            "registers the shortcut with Plasma directly.\n\n"
            "By hand:\n"
            "  System Settings → Keyboard → Shortcuts → Add New → Application…\n"
            "  → Clippy, then record e.g. Meta + Shift + V.\n\n"
            "Both are the same binding, so change it in either place.\n"
        )
