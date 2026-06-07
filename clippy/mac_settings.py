"""macOS Settings window (PyObjC / AppKit).

A compact native window: version, update check + auto-update, device pairing,
and Start-at-login. Opened from the menubar "Settings…" item. UI work that
starts on a worker thread is marshalled back to the main thread.

Untested on Linux CI — validated on a Mac.
"""
from __future__ import annotations

import threading

from . import config, settings, updates

try:
    import objc
    from AppKit import (
        NSApp, NSButton, NSButtonTypeSwitch, NSBezelStyleRounded, NSColor,
        NSMakeRect, NSTextField, NSView, NSWindow, NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskTitled,
        NSBackingStoreBuffered, NSFont,
    )
    from Foundation import NSObject
    _HAVE_APPKIT = True
except Exception:  # pragma: no cover
    _HAVE_APPKIT = False
    NSObject = object


def _label(text, x, y, w, h, bold=False, size=13):
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
               else NSFont.systemFontOfSize_(size))
    return f


def _button(title, x, y, w, h, target, action):
    b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    b.setTitle_(title)
    b.setBezelStyle_(NSBezelStyleRounded)
    b.setTarget_(target)
    b.setAction_(action)
    return b


def _checkbox(title, x, y, w, h, on, target, action):
    c = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    c.setButtonType_(NSButtonTypeSwitch)
    c.setTitle_(title)
    c.setState_(1 if on else 0)
    c.setTarget_(target)
    c.setAction_(action)
    return c


class SettingsController(NSObject):
    # Created via SettingsController.alloc().initWithEngine_(engine)
    def initWithEngine_(self, engine):
        self = objc.super(SettingsController, self).init()
        if self is None:
            return None
        self.engine = engine
        self._build()
        return self

    def _build(self):
        W, H = 440, 430
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        win.setTitle_("Clippy Settings")
        win.center()
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        win.setContentView_(view)
        self.window = win

        y = H - 40
        view.addSubview_(_label("Clippy", 20, y, 200, 22, bold=True, size=16))
        view.addSubview_(_label(f"Version {config.VERSION}", 20, y - 22, 300, 18, size=11))

        y -= 64
        view.addSubview_(_button("Check for updates", 20, y, 180, 28,
                                 self, b"checkUpdates:"))
        self.update_status = _label("", 210, y + 4, 210, 18, size=11)
        view.addSubview_(self.update_status)

        y -= 34
        self.auto_cb = _checkbox("Automatically check for updates", 20, y, 360, 20,
                                 bool(settings.get("auto_check_updates")),
                                 self, b"toggleAuto:")
        view.addSubview_(self.auto_cb)

        y -= 44
        view.addSubview_(_label("Device sync", 20, y, 200, 20, bold=True, size=13))
        y -= 28
        view.addSubview_(_button("Show pairing code", 20, y, 180, 28,
                                 self, b"showCode:"))
        y -= 36
        self.code_field = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, 150, 24))
        self.code_field.setPlaceholderString_("Enter code")
        view.addSubview_(self.code_field)
        view.addSubview_(_button("Pair", 178, y - 2, 80, 28, self, b"pairNow:"))
        y -= 30
        self.pair_status = _label("", 20, y, 400, 18, size=11)
        view.addSubview_(self.pair_status)
        y -= 26
        self.peers_label = _label("No paired devices yet.", 20, y, 400, 18, size=11)
        view.addSubview_(self.peers_label)

        y -= 50
        self.login_cb = _checkbox("Start Clippy at login", 20, y, 360, 20,
                                  bool(settings.get("start_at_login")),
                                  self, b"toggleLogin:")
        view.addSubview_(self.login_cb)

        self._refresh_peers()

    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        try:
            NSApp().activateIgnoringOtherApps_(True)
        except Exception:
            pass

    # -- main-thread helpers --------------------------------------------
    def _set_update_status_(self, text):
        self.update_status.setStringValue_(text)

    def _set_pair_status_(self, text):
        self.pair_status.setStringValue_(text)

    def _refresh_peers(self):
        try:
            peers = self.engine.status().get("peers", []) if self.engine else []
        except Exception:
            peers = []
        if not peers:
            self.peers_label.setStringValue_("No paired devices yet.")
        else:
            self.peers_label.setStringValue_(
                "Paired: " + ", ".join(
                    ("● " if p["online"] else "○ ") + p["name"] for p in peers))

    # -- actions --------------------------------------------------------
    def checkUpdates_(self, sender):
        self.update_status.setStringValue_("Checking…")

        def work():
            try:
                res = updates.check()
                msg = (f"Update available: {res.latest}" if res.update_available
                       else f"Up to date ({config.VERSION})")
            except Exception:
                msg = "Couldn't check (offline?)"
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"_set_update_status_:", msg, False)

        threading.Thread(target=work, daemon=True).start()

    def toggleAuto_(self, sender):
        settings.set_value("auto_check_updates", bool(self.auto_cb.state()))

    def showCode_(self, sender):
        if not self.engine:
            return
        code = self.engine.enter_pairing()
        self.pair_status.setStringValue_(
            f"Pairing code: {code} — enter it on the other device (2 min).")

    def pairNow_(self, sender):
        if not self.engine:
            return
        code = str(self.code_field.stringValue()).strip()
        if not code:
            return
        self.pair_status.setStringValue_("Pairing…")

        def work():
            res = self.engine.join_pairing(code)
            msg = (f"Paired with {res.get('name', 'device')}." if res.get("ok")
                   else f"Pairing failed: {res.get('error', 'unknown')}")
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"_set_pair_status_:", msg, False)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"_refresh_peers", None, False)

        threading.Thread(target=work, daemon=True).start()

    def toggleLogin_(self, sender):
        from .mac_app import set_login_item
        on = bool(self.login_cb.state())
        settings.set_value("start_at_login", on)
        set_login_item(on)
