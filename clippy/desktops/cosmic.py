"""COSMIC (Pop!_OS) integration.

Shortcuts are a RON map at
``~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom``; the action
for a command is ``Spawn("…")``. We edit that file surgically (only our own
``Spawn`` entries) and keep a one-time backup, so existing shortcuts are safe.

The theme lives alongside it: COSMIC records the mode in
``…/CosmicTheme.Mode/v1/is_dark`` and the palette in
``…/CosmicTheme.{Dark,Light}/v1/<key>`` files (RON structs whose
``base: (red, green, blue, alpha)`` floats we parse).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .base import Shortcut

_HOME = Path.home()
_CONFIG = _HOME / ".config" / "cosmic"
CUSTOM = _CONFIG / "com.system76.CosmicSettings.Shortcuts" / "v1" / "custom"
_MODE_FILE = _CONFIG / "com.system76.CosmicTheme.Mode" / "v1" / "is_dark"

# Matches a whole binding whose action spawns a command containing "clippy".
_CLIPPY_ENTRY = re.compile(
    r'\n?[ \t]*\(\s*modifiers\s*:\s*\[[^\]]*\]\s*,?\s*'
    r'key\s*:\s*"[^"]*"\s*,?\s*\)\s*:\s*'
    r'Spawn\(\s*"[^"]*clippy[^"]*"\s*\)\s*,?',
    re.DOTALL,
)
_CLIPPY_READ = re.compile(
    r'\(\s*modifiers\s*:\s*\[([^\]]*)\]\s*,?\s*'
    r'key\s*:\s*"([^"]*)"\s*,?\s*\)\s*:\s*'
    r'Spawn\(\s*"[^"]*clippy[^"]*"\s*\)',
    re.DOTALL,
)

_BASE_RE = re.compile(
    r"base:\s*\(\s*red:\s*([0-9.]+),\s*green:\s*([0-9.]+),"
    r"\s*blue:\s*([0-9.]+),\s*alpha:\s*([0-9.]+)",
    re.DOTALL,
)


def _first_base(path: Path) -> Optional[Tuple[float, float, float]]:
    try:
        m = _BASE_RE.search(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not m:
        return None
    return tuple(float(x) for x in m.groups()[:3])  # type: ignore[return-value]


class CosmicDesktop:
    name = "cosmic"
    label = "COSMIC"

    def detected(self) -> bool:
        if "cosmic" in (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower():
            return True
        return _CONFIG.is_dir()

    # -- global shortcut --------------------------------------------------
    def can_bind(self) -> bool:
        return True

    def read_shortcut(self) -> Optional[Shortcut]:
        try:
            content = CUSTOM.read_text(encoding="utf-8")
        except OSError:
            return None
        m = _CLIPPY_READ.search(content)
        if not m:
            return None
        mods = [tok.strip() for tok in m.group(1).split(",") if tok.strip()]
        return mods, m.group(2)

    def read_unmanaged_shortcut(self) -> Optional[Shortcut]:
        # COSMIC keeps custom bindings in one file we already read whole, so a
        # hand-written Clippy binding is found by read_shortcut or not at all.
        return None

    def set_shortcut(self, modifiers: List[str], key: str, command: str) -> bool:
        CUSTOM.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = CUSTOM.read_text(encoding="utf-8")
        except OSError:
            content = "{\n}\n"

        # One-time backup of the user's original file.
        backup = CUSTOM.with_name("custom.clippy.bak")
        if not backup.exists():
            try:
                backup.write_text(content, encoding="utf-8")
            except OSError:
                pass

        content = _CLIPPY_ENTRY.sub("", content)
        mod_list = ", ".join(modifiers)
        entry = (
            f"\n    (\n        modifiers: [{mod_list}],\n"
            f'        key: "{key}",\n    ): Spawn("{command}"),\n'
        )
        brace = content.find("{")
        if brace == -1:
            content = "{" + entry + "}\n"
        else:
            content = content[: brace + 1] + entry + content[brace + 1:]

        try:
            CUSTOM.write_text(content, encoding="utf-8")
            return True
        except OSError:
            return False

    def remove_shortcut(self) -> bool:
        try:
            content = CUSTOM.read_text(encoding="utf-8")
        except OSError:
            return False
        try:
            CUSTOM.write_text(_CLIPPY_ENTRY.sub("", content), encoding="utf-8")
            return True
        except OSError:
            return False

    def shortcut_help(self, command: str) -> str:
        return (
            "In COSMIC:\n"
            "  Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts → + Add\n"
            f"    Command:  {command}\n"
            "    Key:      e.g. Super + V\n\n"
            f"COSMIC stores it in:\n    {CUSTOM}\n"
        )

    # -- clipboard quirks -------------------------------------------------
    def x11_owner_serves_wayland(self) -> bool:
        # cosmic-comp only mirrors regular -> data-control, so the X11 owner is
        # how a recovered clip reaches native-Wayland apps. See x11clip.
        return True

    # -- panel quirks -----------------------------------------------------
    def focus_out_means_click_away(self) -> bool:
        # Yes: cosmic-comp moves keyboard focus when the user clicks, and only
        # then, so the panel can stay a non-modal strip and read click-away off
        # focus. It won't hand focus to a layer surface mapped from a menu
        # though, so the panel grabs the keyboard as it opens (see _grab_keyboard).
        return True

    # -- theme ------------------------------------------------------------
    def is_dark(self) -> bool:
        try:
            return _MODE_FILE.read_text(encoding="utf-8").strip().lower() != "false"
        except OSError:
            return True

    def palette(self, dark: bool) -> dict:
        base = _CONFIG / f"com.system76.CosmicTheme.{'Dark' if dark else 'Light'}" / "v1"
        # COSMIC has no distinct "warning" role and derives its own text color
        # from the background, which is what theme.py does too -- so neither is
        # reported here.
        found = {
            "background": _first_base(base / "background"),
            "surface": _first_base(base / "primary"),
            "accent": _first_base(base / "accent"),
            "danger": _first_base(base / "destructive"),
        }
        return {k: v for k, v in found.items() if v is not None}

    def theme_paths(self) -> List[Path]:
        return [_MODE_FILE, _CONFIG]
