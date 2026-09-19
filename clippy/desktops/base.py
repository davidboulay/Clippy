"""Desktop-environment integration interface.

A *clipboard backend* (``clippy/backends/``) is the OS-specific surface for
clipboard I/O. A *desktop* is the narrower, per-DE surface for the two things
that are not clipboard at all but still differ per environment: where a global
shortcut is registered, and where the light/dark theme and its palette live.

Everything here is best-effort. A desktop we don't recognise still gets a
working Clippy — it just prints instructions for binding the shortcut by hand
and falls back to the built-in palette.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Protocol, Tuple, runtime_checkable

# A shortcut is (modifiers, key), with modifiers drawn from this vocabulary and
# the key a lowercase GDK keyval name ("v", "grave", "F5"). The settings-window
# capture produces exactly this; each desktop translates it to its own syntax.
MODIFIERS = ("Super", "Ctrl", "Alt", "Shift")

Shortcut = Tuple[List[str], str]

#: The desktop-theme colors ``theme.py`` knows how to use. Every one is
#: optional: a desktop reports what it can read and omits the rest.
SEMANTIC_COLORS = (
    "background",   # the window/panel ground -- also decides text contrast
    "surface",      # a raised element on that ground (Clippy's tiles)
    "foreground",   # body text on the background
    "accent",       # the selection/highlight color
    "danger",       # destructive actions ("Clear history")
    "warning",      # the second badge hue (image/media clips)
)


def parse_hex(value: str) -> Optional[Tuple[float, float, float]]:
    """``"#1e1e2e"`` (or ``"1e1e2e"``, or ``"#fff"``) as 0..1 floats, else None."""
    text = (value or "").strip().strip('"\'').lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


@runtime_checkable
class Desktop(Protocol):
    #: Stable identifier used in diagnostics (`clippy status`).
    name: str
    #: Human-readable name for the UI ("COSMIC", "Hyprland").
    label: str

    def detected(self) -> bool:
        """Whether we are running under this desktop right now."""
        ...

    # -- global shortcut --------------------------------------------------
    def can_bind(self) -> bool:
        """Whether :meth:`set_shortcut` can actually write a binding. False on
        desktops we can only read or advise about, which makes the settings
        window show manual instructions instead of a capture button."""
        ...

    def read_shortcut(self) -> Optional[Shortcut]:
        """The binding currently registered for Clippy, or None."""
        ...

    def read_unmanaged_shortcut(self) -> Optional[Shortcut]:
        """A Clippy binding the user wrote by hand, outside anything we
        manage, or None. Read-only: we never rewrite a line we did not write,
        so this exists to *report* rather than to take ownership."""
        ...

    def set_shortcut(self, modifiers: List[str], key: str, command: str) -> bool:
        """Register/replace Clippy's toggle shortcut, spawning ``command``.
        Returns success."""
        ...

    def remove_shortcut(self) -> bool:
        """Remove any binding we registered. Returns success."""
        ...

    def shortcut_help(self, command: str) -> str:
        """Instructions for binding ``command`` by hand, for `clippy
        setup-shortcut` and the settings window's fallback text."""
        ...

    # -- clipboard quirks -------------------------------------------------
    def x11_owner_serves_wayland(self) -> bool:
        """Whether owning the X11 selection also serves native-Wayland apps.

        True only on cosmic-comp, which mirrors the regular ``wl_data_device``
        selection into wlr-data-control but not back out — so a ``wl-copy``
        (data-control) clip is invisible to GUI apps, and Xwayland re-exposing
        the X11 selection *as* the regular one is the only bridge that works.

        Everywhere else this is False and must stay False: assuming otherwise
        means Clippy publishes to the X11 owner, returns satisfied, and never
        writes the Wayland selection at all — the clip reaches nothing.
        """
        ...

    # -- panel quirks -----------------------------------------------------
    def focus_out_means_click_away(self) -> bool:
        """Whether the panel losing keyboard focus means the user clicked away.

        This one answer decides how the panel opens *and* how it closes.

        True (cosmic-comp): the panel is a bottom strip that leaves the rest of
        the screen alone, grabs the keyboard as it maps -- cosmic-comp won't
        hand focus to a layer surface mapped from a menu -- relaxes that grab a
        moment later, and hides when focus goes elsewhere. The click that
        dismisses it still reaches the window you clicked.

        False (wlroots, so Hyprland): focus there says nothing about intent.
        With focus-follows-mouse it leaves as the pointer crosses a window and
        the panel vanishes untouched; a click on the window that already had
        focus moves none at all, so the panel never goes away. Re-setting the
        interactivity to relax a grab loses the focus we had on map, on top of
        it. So the panel covers the screen instead and reads the click-away off
        its own transparent surface, and that click is consumed rather than
        passed on.
        """
        ...

    # -- theme ------------------------------------------------------------
    def is_dark(self) -> bool:
        """The desktop's current dark/light state."""
        ...

    def palette(self, dark: bool) -> dict:
        """Semantic colors pulled from the desktop theme.

        Keys are drawn from :data:`SEMANTIC_COLORS` and values are ``(r, g, b)``
        floats in 0..1 (see :func:`parse_hex`). Any subset is fine, and a
        missing or unparseable color is simply left out — ``theme.py`` derives
        the full CSS palette from whatever arrives and falls back to the
        built-ins for the rest. Never raises.
        """
        ...

    def theme_paths(self) -> List[Path]:
        """Files/directories whose change means the theme changed. The daemon
        watches these so a theme switch re-styles an already-running Clippy."""
        ...
