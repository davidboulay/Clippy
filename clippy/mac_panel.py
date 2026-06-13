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
import os
import threading
import time as _time

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivateIgnoringOtherApps,
    NSBackingStoreBuffered,
    NSBitmapImageRep,
    NSButton,
    NSColor,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskRightMouseDown,
    NSEventModifierFlagCommand,
    NSFont,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSLineBreakByTruncatingTail,
    NSPanel,
    NSScreen,
    NSScrollView,
    NSSearchField,
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

from . import clipboard, config, settings, sound, storage

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
_AUDIO_EXTS = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "aiff", "aif", "opus"}
_ARCHIVE_EXTS = {"zip", "tar", "gz", "tgz", "bz2", "tbz", "7z", "rar", "xz", "zst", "dmg"}


def _category(entry):
    """Badge label + color for a history entry — granular for file kinds."""
    if entry.kind == "text":
        return "TEXT", NSColor.secondaryLabelColor()
    if entry.is_image:                       # image DATA (Copy Image)
        return "IMAGE", NSColor.systemTealColor()
    mime = (entry.mime or "").lower()
    ext = _ext(entry.filename or entry.text or "")
    if mime.startswith("image/") or ext in _IMAGE_EXTS:
        return "IMAGE", NSColor.systemTealColor()
    if mime.startswith("video/") or ext in _VIDEO_EXTS:
        return "VIDEO", NSColor.systemPinkColor()
    if mime.startswith("audio/") or ext in _AUDIO_EXTS:
        return "AUDIO", NSColor.systemPurpleColor()
    if mime == "application/pdf" or ext == "pdf":
        return "PDF", NSColor.systemRedColor()
    if ext in _ARCHIVE_EXTS:
        return "ZIP", NSColor.systemBrownColor()
    if ext:
        return ext.upper()[:6], NSColor.systemOrangeColor()
    return "FILE", NSColor.systemOrangeColor()


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


def _action_button(title, size=13, color=None):
    """A small borderless text button for a tile (pin / delete). With a color,
    the title is drawn in that color (so it reads on a colored header band)."""
    b = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 20, 20))
    b.setBordered_(False)
    if color is not None:
        from AppKit import NSFontAttributeName, NSForegroundColorAttributeName
        from Foundation import NSAttributedString
        attrs = {NSForegroundColorAttributeName: color,
                 NSFontAttributeName: NSFont.systemFontOfSize_(size)}
        b.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(title, attrs))
    else:
        b.setTitle_(title)
        b.setFont_(NSFont.systemFontOfSize_(size))
    return b


def _load_entry(entry, mode="auto"):
    """Put a history entry back on the clipboard (mirrors panel.paste_entry).

    mode: 'auto' (respect always_plain_text), 'plain', or 'rich'. No auto-paste —
    the user presses ⌘V themselves.
    """
    from pathlib import Path
    try:
        if entry.is_file and entry.image_path:
            clipboard.copy_file(entry.image_path)
        elif entry.is_image and entry.image_path:
            clipboard.copy_image(Path(entry.image_path).read_bytes(),
                                 entry.mime or "image/png")
        else:
            always_plain = bool(settings.get("always_plain_text"))
            use_rich = (entry.html and mode != "plain"
                        and (mode == "rich" or not always_plain))
            if use_rich:
                clipboard.copy_html(entry.html)
            else:
                clipboard.copy_text(entry.text or "")
        # Recovering a clip is a copy action — play the copy sound if enabled
        # (the watcher skips our own clipboard writes, so do it explicitly).
        if settings.get("sound_on_copy"):
            sound.play()
        return True
    except Exception as exc:
        _log(f"load entry failed: {exc}")
        return False


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


_NS_PNG_FILETYPE = 4          # NSBitmapImageFileTypePNG
_ql_inflight = set()          # keys currently being generated (dedup)
_ql_lock = threading.Lock()


def _ql_cache_path(key):
    return config.THUMB_DIR / f"{key}.png"


def _write_png(nsimage, path):
    tiff = nsimage.TIFFRepresentation()
    if tiff is None:
        return False
    rep = NSBitmapImageRep.imageRepWithData_(tiff)
    if rep is None:
        return False
    data = rep.representationUsingType_properties_(_NS_PNG_FILETYPE, {})
    return bool(data and data.writeToFile_atomically_(path, True))


def _warm_ql_cache(blob, ext, key, px):
    """Generate a QuickLook thumbnail for a file and cache it as a PNG (run on a
    background thread). Our blobs are hash-named, so QuickLook can't infer the
    type — give it a correctly-suffixed HARDLINK to the blob (same inode, no
    data copy; QuickLook won't generate video frames through a *symlink*, but a
    hardlink reads as a real file). Fire-and-forget: the thumbnail shows the next
    time the panel opens (cache hit)."""
    cache = _ql_cache_path(key)
    if cache.exists():
        return
    with _ql_lock:
        if key in _ql_inflight:
            return
        _ql_inflight.add(key)
    link = None
    try:
        from Foundation import NSMakeSize, NSURL
        from QuickLookThumbnailing import (
            QLThumbnailGenerationRequest,
            QLThumbnailGenerationRequestRepresentationTypeLowQualityThumbnail,
            QLThumbnailGenerationRequestRepresentationTypeThumbnail,
            QLThumbnailGenerator,
        )
        # Request real thumbnails only (NOT icons): with the "all" mask QL returns
        # the always-available generic icon instead of the content frame. If a file
        # truly can't be previewed, QL fails and we keep the clean type card.
        reptypes = (QLThumbnailGenerationRequestRepresentationTypeThumbnail
                    | QLThumbnailGenerationRequestRepresentationTypeLowQualityThumbnail)
        config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        src = blob
        if ext:
            link = config.THUMB_DIR / f"ql-{key}{ext}"
            try:
                if not link.exists():
                    os.link(blob, link)        # hardlink (no copy; QL needs a real file)
                src = str(link)
            except OSError:
                link = None
        req = QLThumbnailGenerationRequest.alloc().initWithFileAtURL_size_scale_representationTypes_(
            NSURL.fileURLWithPath_(src), NSMakeSize(px, px), 2.0, reptypes)
        ev = threading.Event()
        box = {}

        def _handler(rep, _err):
            box["rep"] = rep
            ev.set()

        QLThumbnailGenerator.sharedGenerator().generateBestRepresentationForRequest_completionHandler_(
            req, _handler)
        ev.wait(6.0)
        rep = box.get("rep")
        img = rep.NSImage() if rep is not None else None
        if img is not None:
            _write_png(img, str(cache))
    except Exception as exc:
        _log(f"ql thumb failed for {key}: {exc}")
    finally:
        if link is not None:
            try:
                link.unlink()
            except OSError:
                pass
        with _ql_lock:
            _ql_inflight.discard(key)


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
    tile = TileView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    tile.setWantsLayer_(True)
    tile.layer().setCornerRadius_(10.0)
    tile.layer().setMasksToBounds_(True)   # clip the header band to rounded corners
    tile.layer().setBackgroundColor_(
        NSColor.controlBackgroundColor().colorWithAlphaComponent_(0.85).CGColor())

    # Colored header band — color + label vary by file type.
    badge_txt, badge_col = _category(entry)
    white = NSColor.whiteColor()
    bh = 26.0
    band = NSView.alloc().initWithFrame_(NSMakeRect(0, h - bh, w, bh))
    band.setWantsLayer_(True)
    band.layer().setBackgroundColor_(badge_col.CGColor())
    tile.addSubview_(band)

    title = badge_txt + ("  ·  RICH" if entry.has_formatting else "")
    lbl = _label(title, 11, white, bold=True)
    lbl.setFrame_(NSMakeRect(10, (bh - 16) / 2, w - 92, 16))
    band.addSubview_(lbl)

    # delete + pin on the band (white); target/action wired in reload()
    del_btn = _action_button("✕", 13, white)
    del_btn.setFrame_(NSMakeRect(w - 26, (bh - 20) / 2, 20, 20))
    del_btn.setTag_(entry.id)
    band.addSubview_(del_btn)
    tile._del_btn = del_btn

    pin_btn = _action_button("★" if entry.pinned else "☆", 14, white)
    pin_btn.setFrame_(NSMakeRect(w - 48, (bh - 20) / 2, 20, 20))
    pin_btn.setTag_(entry.id)
    band.addSubview_(pin_btn)
    tile._pin_btn = pin_btn

    # footer: relative time + size/chars
    footer = _label(_meta_text(entry), 10, NSColor.secondaryLabelColor())
    footer.setFrame_(NSMakeRect(_TILE_PAD, _TILE_PAD, w - 2 * _TILE_PAD, 14))
    tile.addSubview_(footer)

    # content preview — fills between the footer and the header band
    crect = NSMakeRect(_TILE_PAD, _TILE_PAD + 18,
                       w - 2 * _TILE_PAD, h - bh - 22 - _TILE_PAD)
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
    if path and entry.is_file:
        # Files & videos: a real QuickLook thumbnail, cached on disk. On a cache
        # miss, warm it in the background and show a type card for now.
        key = entry.hash or os.path.basename(path)
        cache = _ql_cache_path(key)
        if cache.exists():
            img = _thumbnail_image(str(cache), int(config.TILE_WIDTH * 2))
            if img is not None and img.isValid():
                iv = NSImageView.alloc().initWithFrame_(rect)
                iv.setImage_(img)
                iv.setImageScaling_(NSImageScaleProportionallyUpOrDown)
                return iv
        else:
            import mimetypes
            qext = ("." + ext) if ext else (mimetypes.guess_extension(mime) or "")
            threading.Thread(
                target=_warm_ql_cache,
                args=(path, qext, key, int(config.TILE_WIDTH * 2)),
                daemon=True).start()
        is_video = mime.startswith("video/") or ext in _VIDEO_EXTS
        label = "VIDEO" if is_video else (ext.upper() or "FILE")
        return _centered(rect, [(label, 11, NSColor.secondaryLabelColor(), True),
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


# -- a clickable tile card ------------------------------------------------
class TileView(NSView):
    """One history entry's card. The whole card is a single click target
    (subviews don't intercept); clicking loads the entry onto the clipboard."""

    def hitTest_(self, point):          # noqa: N802 — collapse hits to the card,
        hit = objc.super(TileView, self).hitTest_(point)   # except the action buttons
        if hit is None:
            return None
        if hit is getattr(self, "_pin_btn", None) or hit is getattr(self, "_del_btn", None):
            return hit
        return self

    def mouseDown_(self, _event):       # noqa: N802
        ctrl = getattr(self, "_controller", None)
        eid = getattr(self, "_entry_id", None)
        if ctrl is not None and eid is not None:
            ctrl.selectEntry_(eid)


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

    def performKeyEquivalent_(self, event):  # noqa: N802 — ⌘1–9 / ⌘P / ⌘⌫
        try:
            if event.modifierFlags() & NSEventModifierFlagCommand:
                ch = event.charactersIgnoringModifiers() or ""
                d = self.delegate()
                if d is None:
                    return objc.super(ClippyPanel, self).performKeyEquivalent_(event)
                if len(ch) == 1 and ch in "123456789":
                    d.quickSelect_(int(ch)); return True
                if ch in ("p", "P"):
                    d.pinSelected(); return True
                if ch == "\x7f":                 # ⌘+Delete → remove selected
                    d.deleteSelected(); return True
        except Exception:
            pass
        return objc.super(ClippyPanel, self).performKeyEquivalent_(event)


# -- controller -----------------------------------------------------------
class PanelController(NSObject):
    """Owns the single panel instance and the show/hide lifecycle."""

    def init(self):
        self = objc.super(PanelController, self).init()
        if self is None:
            return None
        self._panel = None
        self._search = None
        self._click_monitor = None
        self._prev_app = None      # app to re-activate on hide (so ⌘V targets it)
        self._query = ""
        self._tiles = []           # current TileViews, in display order
        self._sel = -1             # selected index for keyboard nav
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

        full_w = 800.0
        sh = 28.0
        sf_y = config.PANEL_HEIGHT - _PAD - sh
        # Search field at the top (type to filter).
        search = NSSearchField.alloc().initWithFrame_(
            NSMakeRect(_PAD, sf_y, full_w - 2 * _PAD, sh))
        search.setFont_(NSFont.systemFontOfSize_(13))
        search.setDelegate_(self)
        search.setAutoresizingMask_(2 | 8)     # width-flexible, pinned to top
        ve.addSubview_(search)

        # Horizontal scroll of tiles below the search field, populated by reload().
        scroll_h = sf_y - _PAD - 8
        sframe = NSMakeRect(_PAD, _PAD, full_w - 2 * _PAD, scroll_h)
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
        self._search = search
        self._scroll = scroll
        self._doc = doc

    # -- tiles -----------------------------------------------------------
    def reload(self):
        """Rebuild tiles from the current history (called each time we show)."""
        self._ensure_panel()
        for v in list(self._doc.subviews()):
            v.removeFromSuperview()
        self._tiles = []
        self._sel = -1
        vis_h = self._scroll.contentView().bounds().size.height
        vis_w = self._scroll.frame().size.width
        try:
            entries = storage.list_entries(query=self._query, limit=config.DISPLAY_LIMIT)
        except Exception:
            entries = []
        if not entries:
            empty = "No matches." if self._query else "No clipboard history yet."
            msg = _label(empty, 14, NSColor.secondaryLabelColor(), _NS_CENTER)
            msg.setFrame_(NSMakeRect(0, vis_h / 2 - 12, vis_w, 24))
            self._doc.setFrameSize_(NSMakeSize(vis_w, vis_h))
            self._doc.addSubview_(msg)
            return
        th = float(config.TILE_HEIGHT)
        y = max(0.0, (vis_h - th) / 2)
        x = _GAP
        for e in entries:
            tile = _make_tile(e)
            tile._entry_id = e.id          # for click → selectEntry_
            tile._controller = self
            tile._pin_btn.setTarget_(self)
            tile._pin_btn.setAction_("pinClicked:")
            tile._del_btn.setTarget_(self)
            tile._del_btn.setAction_("deleteClicked:")
            tile.setFrame_(NSMakeRect(x, y, config.TILE_WIDTH, th))
            self._doc.addSubview_(tile)
            self._tiles.append(tile)
            x += config.TILE_WIDTH + _GAP
        self._doc.setFrameSize_(NSMakeSize(max(x, vis_w), vis_h))
        if self._tiles:
            self._set_selection(0)

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
        self._query = ""
        self._search.setStringValue_("")
        self._position()
        self.reload()
        # Activate our app so the popUpMenu-level panel draws OVER the Dock
        # (the Dock stays above background apps' windows). CrossPaste's approach.
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._panel.makeKeyAndOrderFront_(None)
        self._panel.orderFrontRegardless()
        self._panel.makeKeyWindow()
        self._panel.makeFirstResponder_(self._search)   # type-to-search immediately
        self._add_click_monitor()

    # -- search + keyboard nav -------------------------------------------
    def controlTextDidChange_(self, _notification):
        self._query = self._search.stringValue() or ""
        self.reload()

    def control_textView_doCommandBySelector_(self, _control, _textview, sel):
        name = sel if isinstance(sel, str) else str(sel)
        if name == "moveLeft:":
            self._set_selection(self._sel - 1); return True
        if name == "moveRight:":
            self._set_selection(self._sel + 1); return True
        if name in ("insertNewline:", "insertLineBreak:"):
            self._copy_selected(); return True
        if name == "cancelOperation:":
            self.hide(); return True
        return False

    def _set_selection(self, idx):
        if not self._tiles:
            self._sel = -1
            return
        idx = max(0, min(int(idx), len(self._tiles) - 1))
        self._sel = idx
        accent = NSColor.controlAccentColor().CGColor()
        for i, t in enumerate(self._tiles):
            lay = t.layer()
            if lay is None:
                continue
            if i == idx:
                lay.setBorderWidth_(2.5)
                lay.setBorderColor_(accent)
            else:
                lay.setBorderWidth_(0.0)
        try:
            t = self._tiles[idx]
            t.scrollRectToVisible_(t.bounds())
        except Exception:
            pass

    def _copy_selected(self):
        if 0 <= self._sel < len(self._tiles):
            self.selectEntry_(self._tiles[self._sel]._entry_id)

    def quickSelect_(self, n):
        i = int(n) - 1
        if 0 <= i < len(self._tiles):
            self.selectEntry_(self._tiles[i]._entry_id)

    # -- pin / delete (buttons + ⌘P / ⌘⌫) --------------------------------
    def pinClicked_(self, sender):
        self._pin(int(sender.tag())); self.reload()

    def deleteClicked_(self, sender):
        self._delete(int(sender.tag())); self.reload()

    def pinSelected(self):
        if 0 <= self._sel < len(self._tiles):
            self._pin(int(self._tiles[self._sel]._entry_id))
            self.reload()

    def deleteSelected(self):
        if 0 <= self._sel < len(self._tiles):
            keep = self._sel
            self._delete(int(self._tiles[keep]._entry_id))
            self.reload()
            if self._tiles:
                self._set_selection(min(keep, len(self._tiles) - 1))

    @staticmethod
    def _pin(entry_id):
        try:
            storage.toggle_pin(entry_id)
        except Exception as exc:
            _log(f"pin failed: {exc}")

    @staticmethod
    def _delete(entry_id):
        try:
            storage.delete(entry_id)
        except Exception as exc:
            _log(f"delete failed: {exc}")

    def hide(self):
        self._remove_click_monitor()
        if self._panel is not None:
            self._panel.orderOut_(None)
        self._restore_frontmost()

    def selectEntry_(self, entry_id):
        """Load the clicked entry onto the clipboard and close (no auto-paste —
        focus returns to the previous app so the user's ⌘V lands there)."""
        try:
            e = storage.get(int(entry_id))
        except Exception:
            e = None
        if e is not None:
            _load_entry(e)
        self.hide()

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
