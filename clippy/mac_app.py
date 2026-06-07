"""macOS menubar app for Clippy clipboard sync (no CLI, no GTK).

A status-bar item that runs the sync engine + the macOS clipboard backend and
exposes pairing entirely through the menu — show a code, enter a code, see your
paired devices. This is the macOS counterpart to the Linux GTK Settings → Sync
section. Bundled into Clippy.app via packaging/macos.

Requires: rumps, pynacl, zeroconf, pyobjc (see packaging/macos/build-app.sh).
Untested on Linux CI — validate on a Mac.
"""
from __future__ import annotations

import threading

from . import settings, sync
from .capture import capture_current


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

    # Sync is the whole point of the Mac app — enable it by default.
    if not settings.get("sync_enabled"):
        settings.set_value("sync_enabled", True)

    engine = sync.SyncEngine()
    engine.start()

    def on_change():
        try:
            if capture_current():
                engine.broadcast_latest()
        except Exception:
            pass

    # Snapshot what's already on the pasteboard, then poll for changes.
    from . import clipboard
    capture_current()
    clipboard.start_watch(on_change)

    class ClippyApp(rumps.App):
        def __init__(self):
            super().__init__("Clippy", quit_button="Quit Clippy")
            self.menu = [
                "Show pairing code",
                "Enter code…",
                None,
                "Paired devices",
            ]
            self.menu["Paired devices"].set_callback(None)  # header, not clickable
            self._refresh()
            rumps.Timer(self._tick, 5).start()

        # -- pairing -----------------------------------------------------
        @rumps.clicked("Show pairing code")
        def show_code(self, _):
            code = engine.enter_pairing()
            rumps.alert(
                title="Clippy pairing code",
                message=f"Enter this code on the other device within 2 minutes:\n\n"
                        f"        {code}",
            )

        @rumps.clicked("Enter code…")
        def enter_code(self, _):
            resp = rumps.Window(
                title="Pair a device",
                message="Enter the code shown on the other device:",
                dimensions=(120, 22),
                ok="Pair", cancel="Cancel",
            ).run()
            if not resp.clicked:
                return
            code = resp.text.strip()
            if not code:
                return

            def work():
                res = engine.join_pairing(code)
                msg = (f"Paired with {res.get('name', 'device')}."
                       if res.get("ok") else
                       f"Pairing failed: {res.get('error', 'unknown error')}")
                rumps.alert("Clippy", msg)
                self._refresh()

            threading.Thread(target=work, daemon=True).start()

        # -- status ------------------------------------------------------
        def _tick(self, _timer):
            self._refresh()

        def _refresh(self):
            st = engine.status()
            header = self.menu["Paired devices"]
            header.title = f"Paired devices ({len(st['peers'])})"
            # Drop previously-added peer rows, then re-add the current set.
            for key in [k for k in list(self.menu.keys()) if k.startswith("  ")]:
                del self.menu[key]
            if not st["peers"]:
                self.menu.insert_after("Paired devices", "  (none yet)")
            else:
                anchor = "Paired devices"
                for p in st["peers"]:
                    label = f"  {'●' if p['online'] else '○'} {p['name']}"
                    self.menu.insert_after(anchor, label)
                    self.menu[label].set_callback(None)
                    anchor = label

    ClippyApp().run()
    engine.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
