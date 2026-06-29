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
        NSImage, NSImageScaleProportionallyUpOrDown, NSImageView,
        NSMakeRect, NSMakeSize, NSPopover, NSPopUpButton, NSTextField,
        NSTrackingArea, NSView, NSViewController, NSWindow,
        NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskTitled, NSBackingStoreBuffered, NSFont,
    )
    from Foundation import NSObject
    _HAVE_APPKIT = True
except Exception:  # pragma: no cover
    _HAVE_APPKIT = False
    NSObject = object
    NSView = object


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


class HelpMarker(NSView):
    """A '?' in a circle that pops a small explanatory bubble on hover.

    Tooltips render unreliably (empty) in this background/agent app, so we use
    an NSPopover driven by a tracking area instead — same hover UX, full
    control over the content."""

    def initWithText_frame_(self, text, frame):
        self = objc.super(HelpMarker, self).initWithFrame_(frame)
        if self is None:
            return None
        self._text = text
        self._popover = None
        iv = NSImageView.alloc().initWithFrame_(
            NSMakeRect(0, 0, frame.size.width, frame.size.height))
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "questionmark.circle", "Help")
            if img is not None:
                img.setTemplate_(True)
                iv.setImage_(img)
                iv.setImageScaling_(NSImageScaleProportionallyUpOrDown)
                iv.setContentTintColor_(NSColor.secondaryLabelColor())
        except Exception:
            pass
        self.addSubview_(iv)
        return self

    def updateTrackingAreas(self):
        objc.super(HelpMarker, self).updateTrackingAreas()
        for ta in list(self.trackingAreas()):
            self.removeTrackingArea_(ta)
        opts = 0x01 | 0x80    # NSTrackingMouseEnteredAndExited | ActiveAlways
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None))

    def mouseEntered_(self, _ev):
        if self._popover is not None:
            return
        pad, w = 12.0, 300.0
        tf = NSTextField.wrappingLabelWithString_(self._text)
        tf.setFont_(NSFont.systemFontOfSize_(12))
        tf.setPreferredMaxLayoutWidth_(w - 2 * pad)
        h = tf.fittingSize().height
        tf.setFrame_(NSMakeRect(pad, pad, w - 2 * pad, h))
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h + 2 * pad))
        content.addSubview_(tf)
        vc = NSViewController.alloc().init()
        vc.setView_(content)
        pop = NSPopover.alloc().init()
        pop.setContentViewController_(vc)
        pop.setContentSize_(NSMakeSize(w, h + 2 * pad))
        pop.setBehavior_(0)            # ApplicationDefined — we close it on exit,
                                       # so a click elsewhere won't dismiss it
        pop.showRelativeToRect_ofView_preferredEdge_(self.bounds(), self, 1)  # below
        self._popover = pop

    def mouseExited_(self, _ev):
        if self._popover is not None:
            self._popover.close()
            self._popover = None


def _help_marker(tip, x, y, sz=15):
    """A '?'-in-a-circle hover marker (see HelpMarker)."""
    return HelpMarker.alloc().initWithText_frame_(tip, NSMakeRect(x, y, sz, sz))


def _alert_icon(symbol, color=None):
    """A themed SF-Symbol icon for an NSAlert — replaces the generic app/Python
    icon AND fills NSAlert's reserved icon slot (so it isn't an empty void)."""
    try:
        from AppKit import NSImageSymbolConfiguration
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
        if img is None:
            return None
        cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(40.0, 0.0)
        if color is not None:
            try:
                ccfg = NSImageSymbolConfiguration.configurationWithHierarchicalColor_(color)
                cfg = cfg.configurationByApplyingConfiguration_(ccfg)
            except Exception:
                pass
        return img.imageByApplyingSymbolConfiguration_(cfg) or img
    except Exception:
        return None


def _checkbox(title, x, y, w, h, on, target, action):
    c = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    c.setButtonType_(NSButtonTypeSwitch)
    c.setTitle_(title)
    c.setState_(1 if on else 0)
    c.setTarget_(target)
    c.setAction_(action)
    return c


# Keep created controllers (and their windows) alive — without a strong ref the
# NSWindow can be released, leaving a dangling self.window that crashes on show().
_ALIVE = []


class SettingsController(NSObject):
    # Created via SettingsController.alloc().initWithEngine_(engine)
    def initWithEngine_(self, engine):
        self = objc.super(SettingsController, self).init()
        if self is None:
            return None
        self.engine = engine
        self._build()
        _ALIVE.append(self)
        return self

    def _build(self):
        W, H = 440, 624
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        win.setReleasedWhenClosed_(False)      # we keep the controller; don't free on close
        win.setDelegate_(self)                 # close on click-away (windowDidResignKey_)
        win.setTitle_("Clippy Settings")
        win.center()
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        win.setContentView_(view)
        self.window = win

        y = H - 56
        icon = self._app_icon_image()
        if icon is not None:
            iv = NSImageView.alloc().initWithFrame_(NSMakeRect(20, y, 44, 44))
            iv.setImage_(icon)
            iv.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            view.addSubview_(iv)
        tx = 76 if icon is not None else 20
        view.addSubview_(_label("Clippy", tx, y + 22, 200, 22, bold=True, size=16))
        view.addSubview_(_label(f"Version {updates.current_version()}", tx, y, 300, 18, size=11))
        y = H - 40   # keep the rest of the layout exactly where it was

        y -= 64
        self._update_res = None        # last UpdateResult (set by checkUpdates_)
        self.update_btn = _button("Check for updates", 20, y, 180, 28,
                                   self, b"checkUpdates:")
        view.addSubview_(self.update_btn)
        self.update_status = _label("", 210, y + 4, 210, 18, size=11)
        view.addSubview_(self.update_status)

        y -= 34
        self.auto_cb = _checkbox("Automatically check for updates", 20, y, 360, 20,
                                 bool(settings.get("auto_check_updates")),
                                 self, b"toggleAuto:")
        view.addSubview_(self.auto_cb)

        y -= 44
        view.addSubview_(_label("Device sync", 20, y, 100, 20, bold=True, size=13))
        view.addSubview_(_help_marker(
            "Not needed between two Apple devices on the same Apple ID — macOS "
            "Universal Clipboard (Continuity) already shares the clipboard.\n\n"
            "Device sync is handy when the devices use different Apple IDs "
            "(e.g. a work and a personal Mac), or between an Apple device and a "
            "Linux PC — Clippy runs on both.",
            106, y + 3))
        y -= 28
        view.addSubview_(_button("Show pairing code", 20, y, 180, 28,
                                 self, b"showCode:"))
        y -= 36
        self.code_field = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, 150, 24))
        self.code_field.setPlaceholderString_("Enter code")
        view.addSubview_(self.code_field)
        view.addSubview_(_button("Pair", 178, y - 2, 80, 28, self, b"pairNow:"))
        y -= 24
        self.pair_status = _label("", 20, y, 400, 16, size=11)
        view.addSubview_(self.pair_status)
        # Paired-devices list (name + status dot + Unpair), rebuilt by refreshPeers.
        peers_h = 56
        y -= peers_h + 4
        self.peers_box = NSView.alloc().initWithFrame_(
            NSMakeRect(18, y, W - 36, peers_h))
        view.addSubview_(self.peers_box)

        y -= 30
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

    def _app_icon_image(self):
        """The Clippy logo: bundled PNG if present, else the app's own icon."""
        try:
            p = config.BUNDLED_ICON
            if p.exists():
                img = NSImage.alloc().initByReferencingFile_(str(p))
                if img is not None and img.isValid():
                    return img
        except Exception:
            pass
        try:
            return NSImage.imageNamed_("NSApplicationIcon")
        except Exception:
            return None

    def show(self):
        try:
            NSApp().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        self.window.makeKeyAndOrderFront_(None)
        self.window.orderFrontRegardless()

    def windowDidResignKey_(self, note):
        # Close when the user clicks AWAY from our app. An in-app NSAlert
        # (Clear history, etc.) also resigns key, so only dismiss when the
        # whole app is no longer active — otherwise our own confirms would
        # close Settings out from under the dialog.
        try:
            if not NSApp().isActive():
                self.window.orderOut_(None)
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
        box = self.peers_box
        for v in list(box.subviews()):
            v.removeFromSuperview()
        bw = box.frame().size.width
        bh = box.frame().size.height
        if not peers:
            box.addSubview_(_label("No paired devices yet.", 2, bh / 2 - 9,
                                   bw - 4, 18, size=11))
            return
        rowh, gap = 26.0, 2.0
        for i, p in enumerate(peers[:2]):
            ry = bh - (i + 1) * rowh - i * gap
            dot = "● " if p["online"] else "○ "
            lbl = _label(dot + p["name"], 2, ry + 3, bw - 96, 18, size=12)
            lbl.sizeToFit()
            box.addSubview_(lbl)
            # Unpair sits immediately after the device name (not pinned far right).
            bx = min(2 + lbl.frame().size.width + 10, bw - 88)
            btn = _button("Unpair", bx, ry, 84, 26, self, b"unpairClicked:")
            btn.setFont_(NSFont.systemFontOfSize_(11))
            btn.setIdentifier_(str(p["id"]))
            box.addSubview_(btn)

    def unpairClicked_(self, sender):
        from AppKit import NSAlert, NSImage, NSMakeSize
        if not self.engine:
            return
        pid = str(sender.identifier() or "")
        if not pid:
            return
        name = pid
        try:
            for p in self.engine.status().get("peers", []):
                if str(p["id"]) == pid:
                    name = p["name"]
        except Exception:
            pass
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"Unpair “{name}”?")
        alert.setInformativeText_(
            "This Mac will stop syncing clipboard items with it. You can pair "
            "again anytime with a new code.")
        ic = _alert_icon("minus.circle", NSColor.systemRedColor())
        if ic is not None:
            alert.setIcon_(ic)
        up = alert.addButtonWithTitle_("Unpair")
        cancel = alert.addButtonWithTitle_("Cancel")
        try:
            up.setKeyEquivalent_("")
            up.setHasDestructiveAction_(True)
            cancel.setKeyEquivalent_("\r")
        except Exception:
            pass
        if alert.runModal() == 1000:
            try:
                self.engine.unpair(pid)
            except Exception:
                pass
            self.refreshPeers()

    # -- actions --------------------------------------------------------
    def checkUpdates_(self, sender):
        self.update_status.setStringValue_("Checking…")

        def work():
            try:
                self._update_res = updates.check()
            except Exception:
                self._update_res = None
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"applyUpdate", None, False)

        threading.Thread(target=work, daemon=True).start()

    def applyUpdate(self):
        """Reflect the check result: if an update exists, turn the button into a
        one-click 'Download <ver>' (no auto-installer on macOS — the .dmg can't
        self-replace a running app; we fetch it and open it for the user)."""
        res = self._update_res
        if res is None:
            self.update_status.setStringValue_("Couldn't check (offline?)")
            return
        if res.update_available:
            self.update_status.setStringValue_(f"Update available: {res.latest}")
            self.update_btn.setTitle_(f"Download {res.latest}")
            self.update_btn.setAction_(b"installUpdate:")
        else:
            self.update_status.setStringValue_(f"Up to date ({updates.current_version()})")
            self.update_btn.setTitle_("Check for updates")
            self.update_btn.setAction_(b"checkUpdates:")

    def installUpdate_(self, sender):
        res = self._update_res
        if res is None:
            return
        if not getattr(res, "dmg_url", None):
            self._open_url(res.url)        # no .dmg asset — open the release page
            return
        self.update_status.setStringValue_("Downloading update…")
        self.update_btn.setEnabled_(False)

        def work():
            try:
                self._dmg_path = updates.download_dmg(res.dmg_url)
            except Exception:
                self._dmg_path = None
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"dmgReady", None, False)

        threading.Thread(target=work, daemon=True).start()

    def dmgReady(self):
        path = getattr(self, "_dmg_path", None)
        if not path:
            self.update_btn.setEnabled_(True)
            self.update_status.setStringValue_("Download failed — opening release page")
            self._open_url(self._update_res.url)
            return
        # Stage + spawn the swap/relaunch helper on a thread (hdiutil/ditto are
        # slow); then quit so the helper can replace the running bundle.
        self.update_status.setStringValue_("Installing update…")

        def work():
            try:
                from . import mac_update
                self._install_ok, _ = mac_update.install(path)
            except Exception:
                self._install_ok = False
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                b"installFinished", None, False)

        threading.Thread(target=work, daemon=True).start()

    def installFinished(self):
        if getattr(self, "_install_ok", False):
            self.update_status.setStringValue_("Updating — Clippy will relaunch…")
            from AppKit import NSApp
            NSApp().performSelector_withObject_afterDelay_(b"terminate:", None, 1.2)
            return
        # Couldn't self-update (e.g. running from source) — open the .dmg so the
        # user can install it manually.
        self.update_btn.setEnabled_(True)
        self.update_status.setStringValue_("Opening installer — drag Clippy to Applications.")
        import subprocess
        try:
            subprocess.Popen(["/usr/bin/open", self._dmg_path])
        except Exception:
            self._open_url(self._update_res.url)

    def _open_url(self, url):
        try:
            from AppKit import NSWorkspace
            from Foundation import NSURL
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
        except Exception:
            pass

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
        from AppKit import NSAlert, NSImage, NSMakeSize
        from . import storage
        n = 0
        try:
            n = storage.count(pinned=False)
        except Exception:
            pass
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Clear clipboard history?")
        alert.setInformativeText_(
            (f"This removes {n} unpinned clip{'s' if n != 1 else ''}. "
             if n else "")
            + "Pinned clips and clips in tabs are kept. This can’t be undone.")
        ic = _alert_icon("trash", NSColor.systemRedColor())   # not the Python icon
        if ic is not None:
            alert.setIcon_(ic)
        clear_btn = alert.addButtonWithTitle_("Clear")
        cancel_btn = alert.addButtonWithTitle_("Cancel")
        # Make Cancel the safe default (Return), and style Clear as destructive
        # so the data-wiping action isn't the highlighted blue button.
        try:
            clear_btn.setKeyEquivalent_("")
            clear_btn.setHasDestructiveAction_(True)
            cancel_btn.setKeyEquivalent_("\r")
        except Exception:
            pass
        if alert.runModal() == 1000:          # 1000 == first button (Clear)
            storage.clear(include_pinned=False)
            self.clear_status.setStringValue_("History cleared.")
