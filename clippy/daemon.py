"""The long-running Clippy daemon.

One process hosts: the clipboard watcher (wl-paste --watch), the IPC server,
the tray icon, the overlay panel, and the settings window. Launch with
``clippy daemon`` (typically from autostart). A second launch detects the
running one and exits.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from typing import Optional

from . import clipboard, config, ipc, settings, setup, sound, theme
from .capture import capture_current

_RETENTION_INTERVAL_SECONDS = 1800  # re-check every 30 min


def _install_icon() -> None:
    try:
        config.ensure_dirs()
        if not config.ICON_PATH.exists() and config.BUNDLED_ICON.exists():
            shutil.copyfile(config.BUNDLED_ICON, config.ICON_PATH)
    except OSError:
        pass
    # Populate the hicolor theme so the tray host resolves 'clippy' by name.
    try:
        setup.install_icons()
    except Exception:
        pass


def _start_watcher() -> Optional[subprocess.Popen]:
    env = os.environ.copy()
    root = str(config.PROJECT_ROOT)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    try:
        return subprocess.Popen(
            ["wl-paste", "--watch", sys.executable, "-m", "clippy", "_store"],
            env=env,
        )
    except OSError as exc:
        print(f"clippy: failed to start clipboard watcher: {exc}", file=sys.stderr)
        return None


class AppController:
    """Owns the GTK objects and routes IPC commands to them."""

    def __init__(self):
        from gi.repository import Gdk, Gtk

        self._gtk = Gtk
        self._css = Gtk.CssProvider()

        # Build the panel first: creating its window initializes GTK so a real
        # GdkScreen exists. Attaching the provider before that silently no-ops
        # (Gdk.Screen.get_default() is None pre-init), leaving us unstyled.
        from .panel import Panel
        self.panel = Panel(self)

        screen = self.panel.window.get_screen() or Gdk.Screen.get_default()
        Gtk.StyleContext.add_provider_for_screen(
            screen, self._css, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        self.refresh_theme()

        from . import tray
        self.tray = tray.create(self)
        if self.tray is None:
            print("clippy: tray unavailable (no AppIndicator); use the shortcut "
                  "and the panel's ⚙ for settings.", file=sys.stderr)

        self._settings_window = None
        self._sync_autostart()

    # -- services the UI calls back into ----------------------------------
    def refresh_theme(self) -> None:
        dark = theme.resolve_dark()
        # Flip the default GTK theme (menus, combos, dialogs) to match.
        gsettings = self._gtk.Settings.get_default()
        if gsettings is not None:
            gsettings.set_property("gtk-application-prefer-dark-theme", dark)
        self._css.load_from_data(theme.build_css(dark).encode("utf-8"))

    def open_panel(self) -> None:
        self.panel.show()

    def open_settings(self) -> None:
        if self._settings_window is None:
            from .settings_window import SettingsWindow
            self._settings_window = SettingsWindow(self)
        self._settings_window.show()

    def settings_changed(self) -> None:
        self.refresh_theme()
        self._sync_autostart()
        if self.panel._visible:
            self.panel.reload()

    def quit(self) -> None:
        self._gtk.main_quit()

    # -- helpers ----------------------------------------------------------
    def _sync_autostart(self) -> None:
        want = bool(settings.get("open_at_login"))
        if want and not setup.autostart_installed():
            setup.install_autostart()
        elif not want and setup.autostart_installed():
            setup.remove_autostart()

    # -- IPC --------------------------------------------------------------
    def handle_command(self, command: str) -> bool:
        if command in ("toggle", "show", "hide", "refresh"):
            self.panel.handle_command(command)
        elif command == "open-settings":
            self.open_settings()
        elif command == "reload-settings":
            self.settings_changed()
        elif command == "quit":
            self.quit()
        return False


def run_daemon() -> int:
    try:
        clipboard.require_tools()
    except clipboard.ClipboardError as exc:
        print(f"clippy: {exc}", file=sys.stderr)
        return 1

    config.ensure_dirs()
    if ipc.daemon_running():
        print("clippy: daemon already running.")
        return 0

    _install_icon()
    sound.ensure()

    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk  # noqa: E402

    controller = AppController()

    server = ipc.Server(
        handler=lambda cmd: GLib.idle_add(controller.handle_command, cmd)
    )
    server.start()

    watcher = _start_watcher()

    # Capture whatever is already on the clipboard, then enforce retention.
    def _startup_work():
        capture_current()
        storage_apply_retention_safe()
    threading.Thread(target=_startup_work, daemon=True).start()

    # Periodic retention sweep.
    GLib.timeout_add_seconds(
        _RETENTION_INTERVAL_SECONDS, lambda: (storage_apply_retention_safe(), True)[1]
    )

    print("clippy: daemon started.")
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        if watcher is not None:
            watcher.terminate()
            try:
                watcher.wait(timeout=2)
            except subprocess.TimeoutExpired:
                watcher.kill()
    return 0


def storage_apply_retention_safe() -> None:
    from . import storage
    try:
        storage.apply_retention()
    except Exception:
        pass
