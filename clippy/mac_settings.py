"""macOS Settings window (PyObjC / AppKit).

A compact native window: version, update check + auto-update, device pairing,
and Start-at-login. Opened from the menubar "Settings…" item. UI work that
starts on a worker thread is marshalled back to the main thread.

Untested on Linux CI — validated on a Mac.
"""
from __future__ import annotations

import threading

from . import config, settings, sound, updates

try:
    import objc
    from AppKit import (
        NSApp, NSButton, NSButtonTypeSwitch, NSBezelStyleRounded, NSColor,
        NSMakeRect, NSPopUpButton, NSTextField, NSView, NSWindow,
        NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskTitled, NSBackingStoreBuffered, NSFont,
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
        W, H = 440, 624
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
        view.addSubview_(_label(f"Version {updates.current_version()}", 20, y - 22, 300, 18, size=11))

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

        # -- Sound -------------------------------------------------------
        y -= 44
        view.addSubview_(_label("Sound", 20, y, 200, 20, bold=True, size=13))
        y -= 28
        self.sound_cb = _checkbox("Play a sound on copy", 20, y, 360, 20,
                                  bool(settings.get("sound_on_copy")),
                                  self, b"toggleSound:")
        view.addSubview_(self.sound_cb)
        y -= 36
        view.addSubview_(_label("Copy sound", 20, y + 2, 90, 20, size=12))
        self._sound_ids = [sid for sid, _ in sound.SOUND_CHOICES]
        self.sound_choice = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(110, y - 2, 160, 26), False)
        for _sid, slabel in sound.SOUND_CHOICES:
            self.sound_choice.addItemWithTitle_(slabel)
        cur = settings.get("sound_choice")
        if cur in self._sound_ids:
            self.sound_choice.selectItemAtIndex_(self._sound_ids.index(cur))
        elif sound.DEFAULT_SOUND in self._sound_ids:
            self.sound_choice.selectItemAtIndex_(self._sound_ids.index(sound.DEFAULT_SOUND))
        self.sound_choice.setTarget_(self)
        self.sound_choice.setAction_(b"soundChoiceChanged:")
        view.addSubview_(self.sound_choice)
        view.addSubview_(_button("Preview", 280, y - 2, 90, 28, self, b"previewSound:"))

        # -- History ------------------------------------------------------
        y -= 46
        view.addSubview_(_label("History", 20, y, 200, 20, bold=True, size=13))
        y -= 30
        view.addSubview_(_label("Keep history for", 20, y + 2, 120, 20, size=12))
        self._ret_keys = [k for k, _l, _s in config.RETENTION_OPTIONS]
        self.ret_choice = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(150, y - 2, 150, 26), False)
        for _k, lbl, _s in config.RETENTION_OPTIONS:
            self.ret_choice.addItemWithTitle_(lbl)
        rk = settings.get("retention")
        if rk in self._ret_keys:
            self.ret_choice.selectItemAtIndex_(self._ret_keys.index(rk))
        self.ret_choice.setTarget_(self)
        self.ret_choice.setAction_(b"retentionChanged:")
        view.addSubview_(self.ret_choice)
        y -= 38
        view.addSubview_(_button("Clear history now", 20, y, 180, 28,
                                 self, b"clearHistory:"))
        self.clear_status = _label("", 210, y + 4, 210, 18, size=11)
        view.addSubview_(self.clear_status)

        self.refreshPeers()

    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        try:
            NSApp().activateIgnoringOtherApps_(True)
        except Exception:
            pass

    # -- main-thread helpers (camelCase: PyObjC turns underscores into colons) --
    def setUpdateStatus_(self, text):
        self.update_status.setStringValue_(text)

    def setPairStatus_(self, text):
        self.pair_status.setStringValue_(text)

    def refreshPeers(self):
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
                       else f"Up to date ({updates.current_version()})")
            except Exception:
                msg = "Couldn't check (offline?)"
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"setUpdateStatus:", msg, False)

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
                b"setPairStatus:", msg, False)
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"refreshPeers", None, False)

        threading.Thread(target=work, daemon=True).start()

    def toggleLogin_(self, sender):
        from .mac_app import set_login_item
        on = bool(self.login_cb.state())
        settings.set_value("start_at_login", on)
        set_login_item(on)

    def toggleSound_(self, sender):
        settings.set_value("sound_on_copy", bool(self.sound_cb.state()))

    def _selected_sound(self):
        i = self.sound_choice.indexOfSelectedItem()
        return self._sound_ids[i] if 0 <= i < len(self._sound_ids) else None

    def soundChoiceChanged_(self, sender):
        sid = self._selected_sound()
        if sid:
            settings.set_value("sound_choice", sid)

    def previewSound_(self, sender):
        sound.play(self._selected_sound())

    def retentionChanged_(self, sender):
        i = self.ret_choice.indexOfSelectedItem()
        if 0 <= i < len(self._ret_keys):
            settings.set_value("retention", self._ret_keys[i])
            try:
                from . import storage
                storage.apply_retention()
            except Exception:
                pass

    def clearHistory_(self, sender):
        from AppKit import NSAlert
        from . import storage
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Clear clipboard history?")
        alert.setInformativeText_("Pinned clips and clips in tabs are kept.")
        alert.addButtonWithTitle_("Clear")
        alert.addButtonWithTitle_("Cancel")
        if alert.runModal() == 1000:
            storage.clear(include_pinned=False)
            self.clear_status.setStringValue_("History cleared.")
