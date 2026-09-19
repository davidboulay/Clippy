"""Hyprland integration, including Omarchy.

Two config dialects share one compositor, so this backend writes whichever the
session actually uses:

* **Omarchy** configures Hyprland in Lua and loads ``~/.config/hypr/bindings.lua``
  after its own defaults, so a binding appended there wins. We keep ours inside
  a marked block and rewrite only that block.
* **Plain Hyprland** uses ``hyprland.conf``. Rather than edit the user's main
  config we own a whole file, ``~/.config/hypr/clippy.conf``, and add a single
  ``source =`` line to hyprland.conf pointing at it.

Either way the compositor is asked to reload afterwards, so a shortcut set from
the settings window works immediately.

Themes: Omarchy publishes the active theme as a symlink at
``~/.local/state/omarchy/current/theme`` whose ``colors.toml`` carries the mode
and the palette. Plain Hyprland has no theme of its own, so we fall back to the
cross-desktop portal/GSettings reading in ``generic``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from .base import Shortcut, parse_hex
from .generic import GenericDesktop

_HOME = Path.home()
_HYPR = _HOME / ".config" / "hypr"
BINDINGS_LUA = _HYPR / "bindings.lua"
HYPRLAND_CONF = _HYPR / "hyprland.conf"
CLIPPY_CONF = _HYPR / "clippy.conf"

OMARCHY_THEME = _HOME / ".local" / "state" / "omarchy" / "current" / "theme"

_BEGIN = "-- >>> clippy (managed) >>>"
_END = "-- <<< clippy (managed) <<<"
_CONF_BEGIN = "# >>> clippy (managed) >>>"
_CONF_END = "# <<< clippy (managed) <<<"

# `o.bind("SUPER + SHIFT + V", "Clipboard history", "…")`
_LUA_BIND = re.compile(r'o\.bind\(\s*"([^"]*)"', re.DOTALL)
# `bind = SUPER SHIFT, V, exec, …`
_CONF_BIND = re.compile(r'^\s*bind\s*=\s*([^,]*),\s*([^,]+),', re.MULTILINE)

# The same two forms, but capturing the command as well, so a binding that runs
# Clippy can be told apart from the dozens that do not. The Lua description
# argument is a string or `nil` (Omarchy's own examples use both).
_LUA_BIND_CMD = re.compile(
    r'o\.bind\(\s*"([^"]*)"\s*,\s*(?:"[^"]*"|nil)\s*,\s*"([^"]*)"')
_CONF_BIND_CMD = re.compile(
    r'^\s*bind\s*=\s*([^,]*),\s*([^,]+),\s*exec\s*,\s*(.+)$', re.MULTILINE)

# Clippy's modifier vocabulary -> Hyprland's.
_TO_HYPR = {"Super": "SUPER", "Ctrl": "CTRL", "Alt": "ALT", "Shift": "SHIFT"}
_FROM_HYPR = {
    "SUPER": "Super", "MOD4": "Super", "WIN": "Super", "LOGO": "Super",
    "CTRL": "Ctrl", "CONTROL": "Ctrl",
    "ALT": "Alt", "MOD1": "Alt",
    "SHIFT": "Shift",
}
#: Order the settings window shows modifiers in, so a round-trip is stable.
_MOD_ORDER = ("Super", "Ctrl", "Alt", "Shift")


def _hypr_key(key: str) -> str:
    """A GDK keyval name as Hyprland spells it. Single letters are upper-cased;
    everything else (``grave``, ``F5``, ``bracketleft``) is an xkb keysym name
    already, which is exactly what Hyprland wants."""
    return key.upper() if len(key) == 1 else key


def _combo(modifiers: List[str], key: str) -> str:
    mods = [_TO_HYPR[m] for m in modifiers if m in _TO_HYPR]
    return " + ".join(mods + [_hypr_key(key)])


# A command that opens Clippy's panel. Anchored on the executable name and the
# subcommand together: matching "clippy" loose would claim any binding whose
# path merely contains the word (a checkout under ~/src/clippy/, a wrapper
# named clippy-debug), and claiming someone else's binding is worse than
# missing our own.
_CLIPPY_CMD = re.compile(r'(?:^|[/\s])clippy\s+(?:toggle|show|hide)\b')


def _runs_clippy(command: str) -> bool:
    """Whether a bound command opens Clippy's panel."""
    return bool(_CLIPPY_CMD.search(command))


def _parse_combo(text: str) -> Optional[Shortcut]:
    """``"SUPER + SHIFT + V"`` (or ``"SUPER SHIFT"`` + a separate key) back into
    Clippy's (modifiers, key)."""
    tokens = [t for t in re.split(r"[+\s]+", text.strip()) if t]
    if not tokens:
        return None
    mods, key = [], ""
    for tok in tokens:
        mapped = _FROM_HYPR.get(tok.upper())
        if mapped:
            if mapped not in mods:
                mods.append(mapped)
        else:
            key = tok
    if not key:
        return None
    mods.sort(key=_MOD_ORDER.index)
    return mods, key.lower() if len(key) == 1 else key


class HyprlandDesktop(GenericDesktop):
    name = "hyprland"
    label = "Hyprland"

    def __init__(self) -> None:
        # Omarchy ships the Lua config; its presence picks the dialect.
        self.omarchy = BINDINGS_LUA.is_file()
        if self.omarchy:
            self.label = "Omarchy"

    def _reload(self) -> None:
        """Ask Hyprland to re-read its config, so a shortcut set from the
        settings window works immediately. Best-effort: a failure here only
        means the binding waits for the next reload or login. Overridden in
        tests, which must not poke the developer's live compositor."""
        if not shutil.which("hyprctl"):
            return
        try:
            subprocess.run(["hyprctl", "reload"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def detected(self) -> bool:
        desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
        if "hyprland" in desktop or "omarchy" in desktop:
            return True
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return True
        return BINDINGS_LUA.is_file() or HYPRLAND_CONF.is_file()

    # -- global shortcut --------------------------------------------------
    def can_bind(self) -> bool:
        return True

    @property
    def _target(self) -> Path:
        return BINDINGS_LUA if self.omarchy else CLIPPY_CONF

    def read_shortcut(self) -> Optional[Shortcut]:
        block = self._read_block()
        if block is None:
            return None
        if self.omarchy:
            m = _LUA_BIND.search(block)
            return _parse_combo(m.group(1)) if m else None
        m = _CONF_BIND.search(block)
        return _parse_combo(f"{m.group(1)} {m.group(2)}") if m else None

    def read_unmanaged_shortcut(self) -> Optional[Shortcut]:
        """A Clippy binding the user wrote themselves, outside our block.

        :meth:`read_shortcut` deliberately sees only the managed block, which is
        right for writing -- we must never rewrite a line we did not write. But
        reporting "not set" for a binding that plainly works is wrong twice
        over: `clippy status` and `setup-shortcut` keep telling the user to do
        something they have already done, and the settings picker will write a
        second binding for the same key without noticing the first.

        Omarchy loads bindings.lua after its defaults, so a hand-written bind
        lives there beside ours. Plain Hyprland gets a file of our own, so the
        user's own binding is in hyprland.conf instead -- different file, same
        question.
        """
        target = BINDINGS_LUA if self.omarchy else HYPRLAND_CONF
        try:
            content = target.read_text(encoding="utf-8")
        except OSError:
            return None
        # Drop our own block first, or we would report ourselves as unmanaged.
        begin, end = self._markers()
        content = re.compile(
            re.escape(begin) + r".*?" + re.escape(end), re.DOTALL).sub("", content)

        if self.omarchy:
            for combo, command in _LUA_BIND_CMD.findall(content):
                if _runs_clippy(command):
                    return _parse_combo(combo)
            return None
        for mods, key, command in _CONF_BIND_CMD.findall(content):
            if _runs_clippy(command):
                return _parse_combo(f"{mods} {key}")
        return None

    def set_shortcut(self, modifiers: List[str], key: str, command: str) -> bool:
        combo = _combo(modifiers, key)
        if self.omarchy:
            body = (
                f'{_BEGIN}\n'
                "-- Written by Clippy's settings window; edit there, or delete\n"
                "-- this block to unbind. `clippy setup-shortcut` explains it.\n"
                f'hl.unbind("{combo}")\n'
                f'o.bind("{combo}", "Clipboard history", "{command}")\n'
                f'{_END}\n'
            )
        else:
            mods = " ".join(_TO_HYPR[m] for m in modifiers if m in _TO_HYPR)
            body = (
                f'{_CONF_BEGIN}\n'
                "# Written by Clippy's settings window; delete to unbind.\n"
                f'bind = {mods}, {_hypr_key(key)}, exec, {command}\n'
                f'{_CONF_END}\n'
            )
        if not self._write_block(body):
            return False
        if not self.omarchy and not self._ensure_sourced():
            return False
        self._reload()
        return True

    def remove_shortcut(self) -> bool:
        ok = self._write_block("")
        if ok:
            self._reload()
        return ok

    def shortcut_help(self, command: str) -> str:
        if self.omarchy:
            return (
                "Easiest: open Clippy's Settings and use the shortcut picker — it\n"
                f"writes a managed block into {BINDINGS_LUA}.\n\n"
                "By hand, add to that same file:\n\n"
                '    o.bind("SUPER + SHIFT + V", "Clipboard history", '
                f'"{command}")\n\n'
                "Then `hyprctl reload`. Check the key is free first with:\n"
                "    omarchy menu keybindings --print\n"
                "and unbind it with `hl.unbind(\"…\")` above your bind if it isn't.\n"
            )
        return (
            "Easiest: open Clippy's Settings and use the shortcut picker — it\n"
            f"writes {CLIPPY_CONF} and sources it from hyprland.conf.\n\n"
            "By hand, add to ~/.config/hypr/hyprland.conf:\n\n"
            f"    bind = SUPER SHIFT, V, exec, {command}\n\n"
            "Then `hyprctl reload`.\n"
        )

    # -- managed-block plumbing -------------------------------------------
    def _markers(self):
        return (_BEGIN, _END) if self.omarchy else (_CONF_BEGIN, _CONF_END)

    def _read_block(self) -> Optional[str]:
        begin, end = self._markers()
        try:
            content = self._target.read_text(encoding="utf-8")
        except OSError:
            return None
        pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
        m = pattern.search(content)
        return m.group(0) if m else None

    def _write_block(self, body: str) -> bool:
        """Replace our managed block with ``body`` (empty removes it), leaving
        every other line of the file untouched."""
        begin, end = self._markers()
        pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
        target = self._target
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            if not body:
                return True
            content = ""
        except OSError:
            return False

        if pattern.search(content):
            content = pattern.sub(body, content, count=1)
        elif body:
            if content and not content.endswith("\n"):
                content += "\n"
            content += "\n" + body

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return True
        except OSError:
            return False

    def _ensure_sourced(self) -> bool:
        """Plain Hyprland only: make hyprland.conf pull in our file, once."""
        line = f"source = {CLIPPY_CONF}"
        try:
            content = HYPRLAND_CONF.read_text(encoding="utf-8")
        except OSError:
            return False
        if line in content:
            return True
        if not content.endswith("\n"):
            content += "\n"
        try:
            HYPRLAND_CONF.write_text(f"{content}\n{line}\n", encoding="utf-8")
            return True
        except OSError:
            return False

    # -- theme ------------------------------------------------------------
    def _colors(self) -> dict:
        """The active Omarchy theme's colors.toml as a flat dict, or {}."""
        try:
            text = (OMARCHY_THEME / "colors.toml").read_text(encoding="utf-8")
        except OSError:
            return {}
        out = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if value[:1] in ("'", '"'):
                # Quoted, and the value itself is a "#rrggbb" -- so read to the
                # closing quote rather than treating that # as a comment.
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end > 0 else value[1:]
            else:
                value = value.split("#", 1)[0].strip()
            out[key.strip()] = value
        return out

    def is_dark(self) -> bool:
        mode = self._colors().get("mode", "").lower()
        if mode in ("dark", "light"):
            return mode == "dark"
        return super().is_dark()

    def palette(self, dark: bool) -> dict:
        colors = self._colors()
        # colors.toml describes one theme, and that theme *is* the mode. Asked
        # for the other one, we have nothing honest to say -- let the built-in
        # palette answer instead of recolouring a light panel with dark hexes.
        if not colors or (colors.get("mode", "").lower() == "dark") != dark:
            return {}
        wanted = {
            "background": "background",
            "surface": "lighter_background",
            "foreground": "foreground",
            "accent": "accent",
            "danger": "red",
            "warning": "orange",
        }
        found = {name: parse_hex(colors.get(key, "")) for name, key in wanted.items()}
        return {k: v for k, v in found.items() if v is not None}

    def theme_paths(self) -> List[Path]:
        # The symlink's parent is what changes when `omarchy theme set` runs.
        return [OMARCHY_THEME.parent] if OMARCHY_THEME.parent.is_dir() else []
