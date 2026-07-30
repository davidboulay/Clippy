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

from . import clipboard, config, ipc, settings, setup, sound, theme, x11clip
from .capture import capture_current

_RETENTION_INTERVAL_SECONDS = 1800  # re-check every 30 min
_UPDATE_TICK_SECONDS = 6 * 3600     # wake every 6h; act only if 24h elapsed
_UPDATE_MIN_INTERVAL = 24 * 3600    # at most one network check per day


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

    def __init__(self, engine=None):
        self.sync = engine  # SyncEngine or None; used by the settings Sync UI
        self._progress = None
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

    def progress_update(self, name, sent, total, done) -> bool:
        """Sender-side transfer progress (called via GLib.idle_add)."""
        if self._progress is None:
            from .progress import ProgressManager
            self._progress = ProgressManager()
        self._progress.update(name, sent, total, done)
        return False

    def quit(self) -> None:
        self._gtk.main_quit()

    def restart_for_update(self) -> None:
        """After a new package is installed, relaunch the daemon so the new
        code takes over, then quit this (old) process. A short sleep lets us
        release the IPC socket before the replacement binds it."""
        from gi.repository import GLib

        exe = shutil.which("clippy") or "/usr/bin/clippy"
        try:
            subprocess.Popen(
                ["sh", "-c", f"sleep 1.5; exec '{exe}' daemon"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass
        GLib.timeout_add(400, lambda: (self.quit(), False)[1])

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


def _make_engine():
    """Create + start the sync engine if available and enabled, else None."""
    from . import sync
    if not sync.sync_available() or not settings.get("sync_enabled"):
        return None
    try:
        engine = sync.SyncEngine()
        engine.start()
        print("clippy: clipboard sync enabled.")
        return engine
    except Exception as exc:
        print(f"clippy: sync disabled ({exc})", file=sys.stderr)
        return None


def _release_clipboard(entry_id: str) -> None:
    """On a clipboard change, give up the X11 selection unless the clip is ours.

    The persistent owner shadows the selection for every XWayland app for as long
    as it holds it, so after another app copies we have to hand it back — otherwise
    those apps keep pasting the clip Clippy last recovered. Keyed on the entry's
    content hash, which for images is the same sha256 the owner recorded when it
    published, so our own publish echoing back as a capture is recognised and kept.

    Runs on the IPC server thread (x11clip is lock-guarded); best-effort."""
    from . import storage
    digest = None
    try:
        entry = storage.get(int(entry_id))
        if entry is not None:
            digest = entry.hash
    except (TypeError, ValueError, OSError):
        pass
    try:
        x11clip.release_unless_ours(digest)
    except OSError:
        pass


def _sync_query(engine):
    """IPC query handler for sync commands (runs on the IPC server thread)."""
    import json

    def query(cmd, arg):
        # _release is handled before the sync check: it has nothing to do with
        # sync and must work when sync is disabled.
        if cmd == "_release":
            _release_clipboard(arg.strip())
            return "ok"
        if engine is None:
            return "err sync is disabled"
        if cmd == "_broadcast":
            if arg.strip():
                engine.broadcast_id(arg.strip())
            else:
                engine.broadcast_latest()
            return "ok"
        if cmd in ("peers", "sync-status"):
            return json.dumps(engine.status())
        if cmd == "pair":
            if arg:
                parts = arg.split()
                code = parts[0]
                host = parts[1] if len(parts) > 1 else None
                return json.dumps(engine.join_pairing(code, host))
            return json.dumps({"code": engine.enter_pairing()})
        return "err"

    return query


def _run_headless(engine) -> int:
    """macOS path: no GTK. Poll the pasteboard, capture, broadcast; serve IPC."""
    import signal
    import threading
    from . import clipboard
    from .capture import capture_current

    server = ipc.Server(handler=lambda cmd: None, query=_sync_query(engine))
    server.start()

    def on_change():
        try:
            eid = capture_current()
            if eid and engine is not None:
                engine.broadcast_id(eid)
        except Exception:
            pass

    capture_current()           # snapshot whatever's already there
    clipboard.start_watch(on_change)
    print("clippy: daemon started (headless).")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:
            pass
    try:
        stop.wait()
    finally:
        server.stop()
        if engine is not None:
            engine.stop()
    return 0


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

    engine = _make_engine()

    if sys.platform == "darwin":
        return _run_headless(engine)

    _install_icon()
    sound.ensure()

    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk  # noqa: E402

    controller = AppController(engine)

    # Sender-side transfer progress (big media): hop onto the GTK thread.
    if engine is not None:
        engine._on_progress = (
            lambda name, sent, total, done:
            GLib.idle_add(controller.progress_update, name, sent, total, done))
        # Keep our mDNS presence fresh so peers re-discover us after blips.
        GLib.timeout_add_seconds(180, lambda: (engine.readvertise(), True)[1])

    server = ipc.Server(
        handler=lambda cmd: GLib.idle_add(controller.handle_command, cmd),
        query=_sync_query(engine),
    )
    server.start()

    watcher = _start_watcher()

    # Persistent X11 (XWayland) clipboard owner: serves recovered clips to X11
    # apps (Claude Desktop, etc.) directly and keeps Xwayland from cycling. Start
    # it now and re-check on a slow tick so it's respawned if it ever dies.
    x11clip.start()
    GLib.timeout_add_seconds(30, lambda: (x11clip.start(), True)[1])

    # Capture whatever is already on the clipboard, then enforce retention.
    def _startup_work():
        capture_current()
        storage_apply_retention_safe()
    threading.Thread(target=_startup_work, daemon=True).start()

    # Periodic retention sweep.
    GLib.timeout_add_seconds(
        _RETENTION_INTERVAL_SECONDS, lambda: (storage_apply_retention_safe(), True)[1]
    )

    # Automatic update check: once shortly after startup, then on a slow tick.
    # Each tick only hits the network if enabled and >24h since the last check.
    GLib.timeout_add_seconds(45, lambda: (auto_update_check(controller), False)[1])
    GLib.timeout_add_seconds(
        _UPDATE_TICK_SECONDS, lambda: (auto_update_check(controller), True)[1]
    )

    print("clippy: daemon started.")
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        x11clip.stop()
        if engine is not None:
            engine.stop()
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


def _simple_note(body: str) -> None:
    """Fire-and-forget desktop note for update progress (best-effort)."""
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.Popen(
            ["notify-send", "--app-name", "Clippy", body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _do_update(controller, result) -> None:
    """Handle the notification's 'Update now': download the .deb, install it via
    pkexec (which shows its own password dialog), then relaunch. Runs on the
    notification worker thread; progress is reported with follow-up notes."""
    import os

    from . import updates
    deb_url = getattr(result, "deb_url", None)
    if not deb_url:
        return
    _simple_note(f"Downloading Clippy {result.latest}…")
    try:
        path = updates.download_deb(deb_url)
    except Exception:
        _simple_note("Update download failed. Try Settings → Check for updates.")
        return
    _simple_note("Installing update — enter your password if prompted.")
    ok, msg = updates.install_deb(path)
    try:
        os.remove(path)
    except OSError:
        pass
    if ok:
        _simple_note("Clippy updated — restarting.")
        if controller is not None:
            from gi.repository import GLib
            GLib.idle_add(controller.restart_for_update)
    else:
        _simple_note(f"Update failed: {msg}")


def auto_update_check(controller=None) -> None:
    """If enabled and >24h since the last check, query GitHub on a background
    thread and notify if a newer release exists. Never blocks the main loop.
    The notification offers a one-click 'Update now' when a controller is
    available (so it can relaunch after installing)."""
    def worker():
        from . import updates
        result = updates.auto_check()
        if result and result.update_available and result.latest:
            on_update = (
                (lambda r: _do_update(controller, r))
                if controller is not None else None
            )
            updates.notify(result, on_update=on_update)

    threading.Thread(target=worker, daemon=True).start()
