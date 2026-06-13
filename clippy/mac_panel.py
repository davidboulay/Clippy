"""macOS clipboard-history panel (the Mac equivalent of clippy/panel.py).

A floating, non-activating NSPanel summoned by a global hotkey (⌘⇧V by default).
It shows recent clipboard entries as tiles; selecting one puts it back on the
clipboard (no auto-paste — the user hits ⌘V themselves), mirroring the Linux
panel's UX. The non-UI core (storage/clipboard/sync/capture) is shared and
unchanged — this module only READS storage and writes the clipboard.

Milestone 1: the panel *shell* — correct window level/space behavior, the
global hotkey + menu toggle, and Esc / click-away dismissal. Tiles, search,
selection, theming and thumbnails arrive in later milestones.

macOS only (PyObjC/AppKit). Owned by the Mac build; not imported on Linux.
"""
from __future__ import annotations

import ctypes
import time as _time

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivateIgnoringOtherApps,
    NSBackingStoreBuffered,
    NSColor,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskRightMouseDown,
    NSFont,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSLineBreakByTruncatingTail,
    NSPanel,
    NSScreen,
    NSScrollView,
    NSTextField,
    NSView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSMakeSize, NSObject

from . import config, storage

# Carbon hot-key event constants.
_kEventClassKeyboard = 0x6B657962      # 'keyb'
_kEventHotKeyPressed = 6
_kVK_ANSI_V = 9                        # 'v' on the US layout
# Carbon modifier masks (Events.h)
_cmdKey = 0x0100
_shiftKey = 0x0200
_optionKey = 0x0800
_controlKey = 0x1000

_NS_POPUP_MENU_LEVEL = 101             # NSPopUpMenuWindowLevel — above the Dock
                                       # *for the active app* (CrossPaste's level)
# CALayer corner masks (QuartzCore) — round only the top corners of the strip.
_TOP_CORNERS = 4 | 8                   # kCALayerMinXMaxYCorner | kCALayerMaxXMaxYCorner

_DEBUG_LOG = "/tmp/clippy-panel.log"


def _log(msg: str) -> None:
    """Append a diagnostic line to a file (GUI apps swallow stdout)."""
    try:
        with open(_DEBUG_LOG, "a") as f:
            f.write(f"{_time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _panel_level():
    """Window level above the Dock — but only takes effect over the Dock while
    OUR app is active (see show()'s activate dance). Matches CrossPaste."""
    try:
        from AppKit import NSPopUpMenuWindowLevel
        return NSPopUpMenuWindowLevel
    except Exception:
        return _NS_POPUP_MENU_LEVEL


def _visual_material():
    """A light/dark-adaptive material; name varies across macOS versions."""
    from AppKit import NSVisualEffectView as _VE  # noqa: N811
    for name in ("NSVisualEffectMaterialPopover", "NSVisualEffectMaterialHUDWindow",
                 "NSVisualEffectMaterialMenu", "NSVisualEffectMaterialWindowBackground"):
        val = getattr(__import__("AppKit"), name, None)
        if val is not None:
            return val
    return 6  # popover


# -- tile rendering (mirrors clippy/panel.py's Tile) ----------------------
_PAD = 16.0                 # outer padding inside the panel
_GAP = 12.0                 # gap between tiles
_TILE_PAD = 10.0            # inner padding within a tile
_NS_CENTER = 1              # NSTextAlignmentCenter
_NS_LEFT = 0                # NSTextAlignmentLeft

_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif",
               "heic", "heif", "avif", "ico", "svg"}
_VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "mkv", "avi", "wmv", "flv", "mpg", "mpeg"}


def _relative_time(ts: float) -> str:
    delta = max(0, int(_time.time() - ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} d ago"


def _ext(name: str) -> str:
    import os
    return os.path.splitext(name or "")[1].lstrip(".").lower()


def _meta_text(entry) -> str:
    when = _relative_time(entry.created_at)
    if entry.is_image or entry.is_file:
        sz = entry.size or 0
        human = (f"{sz / 1024 / 1024:.1f} MB" if sz >= 1024 * 1024
                 else f"{max(1, sz // 1024)} KB")
        return f"{when}  ·  {human}"
    text = entry.text or ""
    lines = text.count("\n") + 1
    if lines > 1:
        return f"{when}  ·  {len(text)} chars · {lines} lines"
    return f"{when}  ·  {len(text)} chars"


def _thumbnail_image(path, max_px):
    """Load a downscaled NSImage via ImageIO (loads only a ~max_px thumbnail,
    not the full-resolution bitmap — keeps memory + decode time small)."""
    try:
        from Foundation import NSURL
        from Quartz import (
            CGImageSourceCreateThumbnailAtIndex,
            CGImageSourceCreateWithURL,
            kCGImageSourceCreateThumbnailFromImageAlways,
            kCGImageSourceCreateThumbnailWithTransform,
            kCGImageSourceThumbnailMaxPixelSize,
        )
        src = CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
        if src is None:
            return None
        cg = CGImageSourceCreateThumbnailAtIndex(src, 0, {
            kCGImageSourceCreateThumbnailFromImageAlways: True,
            kCGImageSourceCreateThumbnailWithTransform: True,
            kCGImageSourceThumbnailMaxPixelSize: int(max_px),
        })
        if cg is None:
            return None
        return NSImage.alloc().initWithCGImage_size_(cg, NSMakeSize(0, 0))
    except Exception:
        return None


def _label(text, size, color, align=_NS_LEFT, bold=False):
    f = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    lbl = NSTextField.labelWithString_(text or "")
    lbl.setFont_(f)
    lbl.setTextColor_(color)
    lbl.setAlignment_(align)
    lbl.setLineBreakMode_(NSLineBreakByTruncatingTail)
    return lbl


# -- tile builders (module-level: no instance state) ----------------------
def _make_tile(entry):
    w, h = float(config.TILE_WIDTH), float(config.TILE_HEIGHT)
    tile = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    tile.setWantsLayer_(True)
    tile.layer().setCornerRadius_(10.0)
    tile.layer().setBackgroundColor_(
        NSColor.controlBackgroundColor().colorWithAlphaComponent_(0.85).CGColor())

    # header: type badge (+ rich badge), pin marker
    badge_txt, badge_col = (
        ("IMAGE", NSColor.systemTealColor()) if entry.is_image else
        ("FILE", NSColor.systemOrangeColor()) if entry.is_file else
        ("TEXT", NSColor.secondaryLabelColor()))
    badge = _label(badge_txt, 9, badge_col, bold=True)
    badge.setFrame_(NSMakeRect(_TILE_PAD, h - _TILE_PAD - 16, 70, 14))
    tile.addSubview_(badge)
    if entry.has_formatting:
        rich = _label("RICH", 9, NSColor.systemPurpleColor(), bold=True)
        rich.setFrame_(NSMakeRect(_TILE_PAD + 50, h - _TILE_PAD - 16, 40, 14))
        tile.addSubview_(rich)
    if entry.pinned:
        star = _label("★", 12, NSColor.systemYellowColor(), _NS_CENTER)
        star.setFrame_(NSMakeRect(w - _TILE_PAD - 18, h - _TILE_PAD - 17, 18, 16))
        tile.addSubview_(star)

    # footer: relative time + size/chars
    footer = _label(_meta_text(entry), 10, NSColor.secondaryLabelColor())
    footer.setFrame_(NSMakeRect(_TILE_PAD, _TILE_PAD, w - 2 * _TILE_PAD, 14))
    tile.addSubview_(footer)

    # content preview
    crect = NSMakeRect(_TILE_PAD, _TILE_PAD + 18,
                       w - 2 * _TILE_PAD, float(config.TILE_CONTENT_HEIGHT))
    tile.addSubview_(_build_preview(entry, crect))
    return tile

def _build_preview(entry, rect):
    path = entry.image_path
    mime = (entry.mime or "").lower()
    name = entry.filename or entry.text or ""
    ext = _ext(name)
    if path and (entry.is_image or mime.startswith("image/") or ext in _IMAGE_EXTS):
        # Downscaled thumbnail (cheap on memory); fall back to a full load.
        img = (_thumbnail_image(path, int(config.TILE_WIDTH * 2))
               or NSImage.alloc().initWithContentsOfFile_(path))
        if img is not None and img.isValid():
            iv = NSImageView.alloc().initWithFrame_(rect)
            iv.setImage_(img)
            iv.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            return iv
        if entry.is_image:
            return _centered(rect, [("[image unavailable]", 12,
                                          NSColor.secondaryLabelColor())])
    if path and (mime.startswith("video/") or ext in _VIDEO_EXTS):
        return _centered(rect, [("VIDEO", 11, NSColor.secondaryLabelColor(), True),
                                     (name or "video", 11, NSColor.labelColor())])
    if entry.is_file:
        return _centered(rect, [((ext.upper() or "FILE"), 11,
                                      NSColor.secondaryLabelColor(), True),
                                     (name or "file", 11, NSColor.labelColor())])
    # text snippet
    tf = NSTextField.wrappingLabelWithString_((entry.text or "").strip()[:800])
    tf.setFont_(NSFont.systemFontOfSize_(12))
    tf.setTextColor_(NSColor.labelColor())
    tf.setFrame_(rect)
    tf.setLineBreakMode_(NSLineBreakByTruncatingTail)
    tf.setMaximumNumberOfLines_(10)
    return tf

def _centered(rect, rows):
    """A box with centered stacked labels: rows = [(text, size, color[, bold])]."""
    box = NSView.alloc().initWithFrame_(rect)
    n = len(rows)
    total = n * 20
    top = rect.size.height / 2 + total / 2
    for i, row in enumerate(rows):
        text, size, color = row[0], row[1], row[2]
        bold = len(row) > 3 and row[3]
        lbl = _label(text, size, color, _NS_CENTER, bold=bold)
        lbl.setFrame_(NSMakeRect(0, top - (i + 1) * 20, rect.size.width, 18))
        box.addSubview_(lbl)
    return box


# -- Carbon global hotkey (no Accessibility permission required) ----------
class CarbonHotKey:
    """System-wide hotkey via Carbon RegisterEventHotKey.

    Unlike NSEvent global keyDown monitors, this needs no Accessibility /
    Input-Monitoring permission — the standard approach for a background
    (LSUIElement) app. The handler fires on the main run loop thread.
    """

    class _EventTypeSpec(ctypes.Structure):
        _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

    class _EventHotKeyID(ctypes.Structure):
        _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

    _HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self, keycode: int, modifiers: int, on_fire):
        self._on_fire = on_fire
        self._hotkey_ref = ctypes.c_void_p()
        self._handler_ref = ctypes.c_void_p()
        self._ok = False
        try:
            self._carbon = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/Carbon.framework/Carbon")
            self._install(keycode, modifiers)
            self._ok = True
        except Exception as exc:           # pragma: no cover - mac runtime
            _log(f"hotkey registration failed: {exc}")

    def _install(self, keycode, modifiers):
        carbon = self._carbon
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p, self._HANDLER, ctypes.c_uint32,
            ctypes.POINTER(self._EventTypeSpec), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        carbon.InstallEventHandler.restype = ctypes.c_int32
        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, self._EventHotKeyID,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32

        target = carbon.GetApplicationEventTarget()

        def _cb(_next, _event, _user):
            try:
                self._on_fire()
            except Exception as exc:
                _log(f"hotkey on_fire error: {exc}")
            return 0
        self._cb = self._HANDLER(_cb)      # keep a strong ref (avoid GC)

        spec = self._EventTypeSpec(_kEventClassKeyboard, _kEventHotKeyPressed)
        st1 = carbon.InstallEventHandler(target, self._cb, 1, ctypes.byref(spec),
                                         None, ctypes.byref(self._handler_ref))
        hk_id = self._EventHotKeyID(0x636C7079, 1)   # 'clpy'
        st2 = carbon.RegisterEventHotKey(ctypes.c_uint32(keycode),
                                         ctypes.c_uint32(modifiers), hk_id,
                                         target, 0, ctypes.byref(self._hotkey_ref))
        if st1 != 0 or st2 != 0:
            _log(f"hotkey register failed: InstallEventHandler={st1} "
                 f"RegisterEventHotKey={st2} keycode={keycode} mods={hex(modifiers)}")

    @property
    def ok(self) -> bool:
        return self._ok


def parse_shortcut(spec):
    """Map a stored shortcut to (keycode, carbon_modifiers).

    Accepts a string ('cmd+shift+v' / '⌘⇧V'), the Linux dict form
    ({'modifiers': [...], 'key': ...}), or None. Defaults to ⌘⇧V. Milestone 1
    supports the 'v' key only; a fuller key map arrives with the settings UI.
    """
    if isinstance(spec, dict):
        tokens = "+".join(spec.get("modifiers") or []).lower()
    elif isinstance(spec, str):
        tokens = spec.lower()
    else:
        tokens = ""
    mods = 0
    if "cmd" in tokens or "⌘" in tokens or "super" in tokens or "meta" in tokens:
        mods |= _cmdKey
    if "shift" in tokens or "⇧" in tokens:
        mods |= _shiftKey
    if "alt" in tokens or "opt" in tokens or "⌥" in tokens:
        mods |= _optionKey
    if "ctrl" in tokens or "control" in tokens or "⌃" in tokens:
        mods |= _controlKey
    if not mods:
        mods = _cmdKey | _shiftKey
    return _kVK_ANSI_V, mods


# -- the panel window -----------------------------------------------------
class ClippyPanel(NSPanel):
    """Borderless non-activating panel that can still take keyboard focus."""

    def canBecomeKeyWindow(self):       # noqa: N802 (Cocoa selector)
        return True

    def cancelOperation_(self, _sender):  # noqa: N802 — Esc in the responder chain
        d = self.delegate()
        if d is not None and d.respondsToSelector_(b"hide"):
            d.hide()                       # routes through the controller (restores focus)
        else:
            self.orderOut_(None)


# -- controller -----------------------------------------------------------
class PanelController(NSObject):
    """Owns the single panel instance and the show/hide lifecycle."""

    def init(self):
        self = objc.super(PanelController, self).init()
        if self is None:
            return None
        self._panel = None
        self._click_monitor = None
        self._prev_app = None      # app to re-activate on hide (so ⌘V targets it)
        return self

    # -- building --------------------------------------------------------
    def _ensure_panel(self):
        if self._panel is not None:
            return
        rect = NSMakeRect(0, 0, 800, config.PANEL_HEIGHT)
        # Borderless (activatable) NSPanel — we activate the app on show so it
        # floats over the Dock, then restore the previous app on hide.
        style = NSWindowStyleMaskBorderless
        panel = ClippyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setHidesOnDeactivate_(False)
        panel.setFloatingPanel_(True)
        panel.setWorksWhenModal_(True)
        panel.setBecomesKeyOnlyIfNeeded_(False)
        panel.setOpaque_(False)
        panel.setMovableByWindowBackground_(False)
        # IMPORTANT: set the level LAST — setFloatingPanel_(True) forces the
        # level to NSFloatingWindowLevel(3), which is BELOW the Dock(20). Setting
        # it here (popUpMenu=101) is what actually floats the panel over the Dock.
        panel.setLevel_(_panel_level())

        # Light/dark-adaptive blurred background with rounded corners.
        ve = NSVisualEffectView.alloc().initWithFrame_(rect)
        ve.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        ve.setState_(NSVisualEffectStateActive)
        ve.setMaterial_(_visual_material())
        ve.setWantsLayer_(True)
        ve.layer().setCornerRadius_(14.0)
        ve.layer().setMasksToBounds_(True)
        try:
            ve.layer().setMaskedCorners_(_TOP_CORNERS)   # round only the top edge
        except Exception:
            pass
        panel.setContentView_(ve)

        # Horizontal scroll of tiles, populated by reload().
        sframe = NSMakeRect(_PAD, _PAD, 800 - 2 * _PAD, config.PANEL_HEIGHT - 2 * _PAD)
        scroll = NSScrollView.alloc().initWithFrame_(sframe)
        scroll.setHasHorizontalScroller_(True)
        scroll.setHasVerticalScroller_(False)
        scroll.setDrawsBackground_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(0)               # NSNoBorder
        scroll.setAutoresizingMask_(2 | 16)    # width + height flexible
        doc = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, sframe.size.width, sframe.size.height))
        scroll.setDocumentView_(doc)
        ve.addSubview_(scroll)

        panel.setDelegate_(self)   # for cancelOperation_ (Esc) routing
        self._panel = panel
        self._scroll = scroll
        self._doc = doc

    # -- tiles -----------------------------------------------------------
    def reload(self):
        """Rebuild tiles from the current history (called each time we show)."""
        self._ensure_panel()
        for v in list(self._doc.subviews()):
            v.removeFromSuperview()
        vis_h = self._scroll.contentView().bounds().size.height
        vis_w = self._scroll.frame().size.width
        try:
            entries = storage.list_entries(limit=config.DISPLAY_LIMIT)
        except Exception:
            entries = []
        if not entries:
            msg = _label("No clipboard history yet.", 14,
                         NSColor.secondaryLabelColor(), _NS_CENTER)
            msg.setFrame_(NSMakeRect(0, vis_h / 2 - 12, vis_w, 24))
            self._doc.setFrameSize_(NSMakeSize(vis_w, vis_h))
            self._doc.addSubview_(msg)
            return
        th = float(config.TILE_HEIGHT)
        y = max(0.0, (vis_h - th) / 2)
        x = _GAP
        for e in entries:
            tile = _make_tile(e)
            tile.setFrame_(NSMakeRect(x, y, config.TILE_WIDTH, th))
            self._doc.addSubview_(tile)
            x += config.TILE_WIDTH + _GAP
        self._doc.setFrameSize_(NSMakeSize(max(x, vis_w), vis_h))

    def _position(self):
        """Anchor a full-width strip to the bottom of the screen, floating OVER
        the Dock (use frame(), not visibleFrame(); our NSStatusWindowLevel is
        above the Dock's window level so it draws on top)."""
        screen = self._screen_under_cursor()
        area = screen.frame()
        width = area.size.width                 # flush to both side edges
        height = float(config.PANEL_HEIGHT)
        x = area.origin.x
        y = area.origin.y                        # flush to the bottom edge
        self._panel.setFrame_display_(NSMakeRect(x, y, width, height), True)
        self._panel.contentView().setFrame_(NSMakeRect(0, 0, width, height))

    @staticmethod
    def _screen_under_cursor():
        try:
            from AppKit import NSMouseInRect
            loc = NSEvent.mouseLocation()
            for s in NSScreen.screens():
                if NSMouseInRect(loc, s.frame(), False):
                    return s
        except Exception:
            pass
        return NSScreen.mainScreen()

    # -- show / hide -----------------------------------------------------
    def toggle(self):
        self._ensure_panel()
        if self._panel.isVisible():
            self.hide()
        else:
            self.show()

    def show(self):
        self._ensure_panel()
        self._remember_frontmost()
        self._position()
        self.reload()
        # Activate our app so the popUpMenu-level panel draws OVER the Dock
        # (the Dock stays above background apps' windows). CrossPaste's approach.
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._panel.makeKeyAndOrderFront_(None)
        self._panel.orderFrontRegardless()
        self._panel.makeKeyWindow()
        self._add_click_monitor()

    def hide(self):
        self._remove_click_monitor()
        if self._panel is not None:
            self._panel.orderOut_(None)
        self._restore_frontmost()

    # -- focus hand-off (so ⌘V targets the app you were in) --------------
    def _remember_frontmost(self):
        try:
            from AppKit import NSRunningApplication, NSWorkspace
            cur = NSWorkspace.sharedWorkspace().frontmostApplication()
            me = NSRunningApplication.currentApplication()
            if cur is not None and cur.processIdentifier() != me.processIdentifier():
                self._prev_app = cur
        except Exception:
            self._prev_app = None

    def _restore_frontmost(self):
        app, self._prev_app = self._prev_app, None
        if app is not None:
            try:
                app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            except Exception:
                pass

    # -- click-away dismissal (clicks in OTHER apps; no permission needed) -
    def _add_click_monitor(self):
        if self._click_monitor is not None:
            return
        mask = NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown

        def _on_click(_event):
            self.hide()

        self._click_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, _on_click)

    def _remove_click_monitor(self):
        if self._click_monitor is not None:
            try:
                NSEvent.removeMonitor_(self._click_monitor)
            except Exception:
                pass
            self._click_monitor = None
