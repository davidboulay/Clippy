"""System-tray (StatusNotifierItem) icon via Ayatana AppIndicator.

COSMIC's status-area applet hosts SNI items, so the paperclip shows in the
panel — never the dock. If the AppIndicator bindings aren't present the daemon
still works (open the panel via the shortcut; settings via the panel's ⚙).
"""
from __future__ import annotations

import gi

from . import config

_IndicatorModule = None
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as _AppIndicator  # type: ignore # noqa
    _IndicatorModule = _AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as _AppIndicator  # type: ignore # noqa
        _IndicatorModule = _AppIndicator
    except (ValueError, ImportError):
        _IndicatorModule = None

from gi.repository import Gtk  # noqa: E402


def available() -> bool:
    return _IndicatorModule is not None


class Tray:
    def __init__(self, controller):
        self._controller = controller
        AI = _IndicatorModule
        self._indicator = AI.Indicator.new(
            "clippy", "clippy", AI.IndicatorCategory.APPLICATION_STATUS
        )
        self._indicator.set_status(AI.IndicatorStatus.ACTIVE)
        # Reference the staged icon by name out of our private theme dir.
        if config.ICON_THEME_DIR.exists():
            self._indicator.set_icon_theme_path(str(config.ICON_THEME_DIR))
        self._indicator.set_icon_full("clippy", "Clippy")
        self._indicator.set_title("Clippy")
        self._indicator.set_menu(self._build_menu())

    def _build_menu(self) -> Gtk.Menu:
        menu = Gtk.Menu()

        def add(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda _m: cb())
            menu.append(mi)
            return mi

        add("Open Clippy", self._controller.open_panel)
        add("Settings…", self._controller.open_settings)
        menu.append(Gtk.SeparatorMenuItem())
        add("Quit", self._controller.quit)
        menu.show_all()
        return menu


def create(controller):
    """Return a Tray, or None if AppIndicator support is unavailable."""
    if not available():
        return None
    try:
        return Tray(controller)
    except Exception:
        return None
