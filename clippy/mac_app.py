"""macOS menubar app for Clippy clipboard sync (no CLI, no GTK).

A template menubar icon (auto light/dark) that runs the sync engine + the macOS
clipboard backend. Pairing, updates, and Start-at-login live in a native
Settings window (clippy/mac_settings.py); quick pairing is also in the menu.
Survives sleep/wake by restarting discovery on NSWorkspaceDidWake.

Requires: rumps, pynacl, zeroconf, pyobjc (see packaging/macos/build-app.sh).
Untested on Linux CI — validate on a Mac.
"""
from __future__ import annotations

import os
import threading

from . import config, settings, sync
from .capture import capture_current

_LOGIN_LABEL = "io.github.davidboulay.Clippy"
_LOGIN_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{_LOGIN_LABEL}.plist")


# -- Start at login (LaunchAgent) ---------------------------------------
def login_installed() -> bool:
    return os.path.exists(_LOGIN_PLIST)


def set_login_item(enabled: bool) -> None:
    import plistlib
    import shlex
    if not enabled:
        os.system(f"launchctl unload {shlex.quote(_LOGIN_PLIST)} 2>/dev/null")
        try:
            os.unlink(_LOGIN_PLIST)
        except OSError:
            pass
        return
    try:
        from Foundation import NSBundle
        bundle = str(NSBundle.mainBundle().bundlePath())
    except Exception:
        bundle = "/Applications/Clippy.app"
    os.makedirs(os.path.dirname(_LOGIN_PLIST), exist_ok=True)
    data = {"Label": _LOGIN_LABEL,
            "ProgramArguments": ["/usr/bin/open", bundle],
            "RunAtLoad": True}
    with open(_LOGIN_PLIST, "wb") as f:
        plistlib.dump(data, f)
    os.system(f"launchctl load {shlex.quote(_LOGIN_PLIST)} 2>/dev/null")


def _menubar_icon_path():
    """Resolve the template icon — package dir, else the bundle's Resources."""
    if config.MAC_MENUBAR_ICON.exists():
        return str(config.MAC_MENUBAR_ICON)
    try:
        from Foundation import NSBundle
        rp = NSBundle.mainBundle().resourcePath()
        if rp:
            p = os.path.join(str(rp), "clippy-menubar.png")
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return None


def _install_wake_observer(engine):
    """Restart discovery/sockets when the Mac wakes (mDNS + TCP go stale)."""
    try:
        from AppKit import NSWorkspace
        from Foundation import NSObject

        class _Waker(NSObject):
            def wake_(self, _note):
                engine.restart_network()

        waker = _Waker.alloc().init()
        nc = NSWorkspace.sharedWorkspace().notificationCenter()
        nc.addObserver_selector_name_object_(
            waker, b"wake:", "NSWorkspaceDidWakeNotification", None)
        return waker  # caller keeps a reference so it isn't GC'd
    except Exception:
        return None


def run() -> int:
    try:
        import rumps
    except Exception:
        print("clippy: the macOS app needs 'rumps' (pip install rumps).")
        return 1

    if not sync.sync_available():
        rumps.alert("Clippy — sync unavailable",
                    "The sync libraries failed to load inside the app:\n\n"
                    + (sync.import_error() or "pynacl / zeroconf missing"))
        return 1

    if not settings.get("sync_enabled"):
        settings.set_value("sync_enabled", True)

    engine = sync.SyncEngine()

    def _on_received_clip():
        # macOS's changeCount watcher is suppressed for our own clipboard writes,
        # so capture_current (which plays the copy sound) never runs for received
        # clips — play it here instead, honoring the setting.
        try:
            from . import sound
            if settings.get("sound_on_copy"):
                sound.play(settings.get("sound_choice"))
        except Exception:
            pass
    engine._on_received = _on_received_clip

    class ClippyApp(rumps.App):
        def __init__(self):
            icon = _menubar_icon_path()
            super().__init__("Clippy", title=None, icon=icon, template=True,
                             quit_button="Quit Clippy")
            self.menu = ["Sync status", None,
                         "Show Clipboard History", None,
                         "Settings…", None,
                         "Show pairing code", "Enter code…", None,
                         "Clipboard types (debug)"]
            self.menu["Sync status"].set_callback(None)   # info line, not clickable
            self._settings = None
            self._prog = None
            self._icon_path = icon
            self._panel_ctrl = None
            self._hotkey = None
            rumps.Timer(self._tick_progress, 0.4).start()
            rumps.Timer(self._tick_status, 5).start()
            rumps.Timer(self._fix_retina_icon, 1).start()  # one-shot (stops itself)
            rumps.Timer(self._setup_panel, 0.3).start()    # one-shot (stops itself)
            self._tick_status(None)

        def _setup_panel(self, timer):
            # Build the history panel + register the global hotkey from inside the
            # running NSApplication loop (AppKit/Carbon need the app event target).
            timer.stop()
            try:
                from .mac_panel import CarbonHotKey, PanelController, parse_shortcut
                self._panel_ctrl = PanelController.alloc().init()

                def _open_settings():
                    from .mac_settings import SettingsController
                    if self._settings is None:
                        self._settings = SettingsController.alloc().initWithEngine_(engine)
                    self._settings.show()
                self._panel_ctrl._open_settings = _open_settings   # cog button → Settings
                # Mac-specific key (the shared "shortcut" is a Linux dict and
                # Super+V maps to ⌘V, which collides with paste). Default ⌘⇧V.
                keycode, mods = parse_shortcut(settings.get("mac_shortcut"))
                self._hotkey = CarbonHotKey(keycode, mods, self._toggle_panel)
            except Exception as exc:
                import traceback
                from .mac_panel import _log
                _log(f"panel setup failed: {exc}\n{traceback.format_exc()}")

        def _toggle_panel(self):
            # Hotkey callback fires on the main run loop thread, so this is safe.
            if self._panel_ctrl is not None:
                self._panel_ctrl.toggle()

        @rumps.clicked("Show Clipboard History")
        def show_panel(self, _):
            self._toggle_panel()

        def _fix_retina_icon(self, timer):
            # Use the native SF Symbol "paperclip" — vector, crisp at any density,
            # and a proper template (solid white/black per menubar theme). Falls
            # back to the bundled @2x PNG on older macOS.
            timer.stop()
            try:
                from AppKit import NSImage
                img = None
                try:
                    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                        "paperclip", "Clippy")
                    if img is not None:
                        try:
                            from AppKit import NSImageSymbolConfiguration
                            cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                                15.0, 0.0)
                            img = img.imageByApplyingSymbolConfiguration_(cfg) or img
                        except Exception:
                            pass
                except Exception:
                    img = None
                if img is None and self._icon_path:
                    from AppKit import NSMakeSize
                    img = NSImage.alloc().initByReferencingFile_(self._icon_path)
                    img.setSize_(NSMakeSize(18.0, 18.0))
                if img is not None:
                    img.setTemplate_(True)
                    self._nsapp.nsstatusitem.button().setImage_(img)
            except Exception:
                pass

        def _tick_status(self, _timer):
            try:
                peers = engine.status().get("peers", [])
            except Exception:
                peers = []
            online = [p for p in peers if p["online"]]
            if not peers:
                txt = "Not paired with any device"
            else:
                txt = f"Paired: {len(peers)} ({len(online)} online)"
            self.menu["Sync status"].title = txt

        @rumps.clicked("Settings…")
        def open_settings(self, _):
            try:
                from .mac_settings import SettingsController
                if self._settings is None:
                    self._settings = SettingsController.alloc().initWithEngine_(engine)
                self._settings.show()
            except Exception as exc:
                rumps.alert("Clippy", f"Couldn't open Settings: {exc}")

        @rumps.clicked("Clipboard types (debug)")
        def debug_types(self, _):
            from .backends import get_backend
            be = get_backend()
            try:
                raw = [str(t) for t in (be._pb.types() or [])]
            except Exception as exc:
                raw = [f"(error: {exc})"]
            try:
                files = be.read_file_paths(raw)
            except Exception as exc:
                files = [f"(error: {exc})"]
            rumps.alert("Clipboard debug",
                        "Types on the clipboard:\n  " + "\n  ".join(raw)
                        + "\n\nDetected file paths:\n  "
                        + ("\n  ".join(files) if files else "(none)"))

        @rumps.clicked("Show pairing code")
        def show_code(self, _):
            code = engine.enter_pairing()
            rumps.alert("Clippy pairing code",
                        f"Enter this on the other device within 2 minutes:\n\n        {code}")

        @rumps.clicked("Enter code…")
        def enter_code(self, _):
            resp = rumps.Window(title="Pair a device",
                                message="Enter the code shown on the other device:",
                                dimensions=(120, 22), ok="Pair", cancel="Cancel").run()
            if not resp.clicked or not resp.text.strip():
                return
            code = resp.text.strip()

            def work():
                res = engine.join_pairing(code)
                rumps.alert("Clippy", f"Paired with {res.get('name', 'device')}."
                            if res.get("ok") else
                            f"Pairing failed: {res.get('error', 'unknown error')}")
            threading.Thread(target=work, daemon=True).start()

        # progress shown in the menubar title (set from a worker thread, applied
        # on the main thread by this timer)
        def on_progress(self, name, sent, total, done):
            self._prog = None if done else (int(sent / total * 100) if total else 0)

        def _tick_progress(self, _timer):
            self.title = f"⬆ {self._prog}%" if self._prog is not None else None

    app = ClippyApp()
    engine._on_progress = app.on_progress
    engine.start()

    # First run: honor Start-at-login (default on).
    if settings.get("start_at_login") and not login_installed():
        try:
            set_login_item(True)
        except Exception:
            pass

    # Firewall reminder once (unsigned apps don't get a reliable prompt).
    if not settings.get("mac_firewall_hint_shown"):
        try:
            rumps.alert("Allow Clippy through the firewall",
                        "If macOS asks, click “Allow” so devices can reach Clippy.\n\n"
                        "If pairing fails: System Settings → Network → Firewall →\n"
                        "Options → allow Clippy to accept incoming connections.")
        finally:
            settings.set_value("mac_firewall_hint_shown", True)

    def _frontmost_bundle_id():
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            bid = app.bundleIdentifier() if app else None
            return str(bid) if bid else None
        except Exception:
            return None

    def on_change():
        try:
            src = _frontmost_bundle_id()   # the app the user copied from
            eid = capture_current()
            if eid:
                if src:
                    try:
                        from . import mac_source
                        mac_source.record(eid, src)
                    except Exception:
                        pass
                engine.broadcast_id(eid)
        except Exception:
            pass

    capture_current()
    from . import clipboard
    clipboard.start_watch(on_change)
    run._waker = _install_wake_observer(engine)   # keep a strong ref

    app.run()
    engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
