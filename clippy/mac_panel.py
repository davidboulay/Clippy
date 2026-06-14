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
    NSMenu,
    NSMenuItem,
    NSPanel,
    NSScreen,
    NSBezierPath,
    NSScroller,
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
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize, NSObject

from . import (clip_types, clipboard, config, mac_source, mac_tabs, settings,
               sound, storage)

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
_TAB_H = 26.0               # tab-bar row height
# Mac-local tile size — slightly landscape (more rectangular than the Linux
# square-ish tile), and a panel height tuned to fit it snugly.
_TILE_W = 264.0
_TILE_H = 222.0
_PANEL_H = 300.0
_BAND_H = 40.0              # tile header band — tall enough for a large app icon
_SCROLLER_H = 13.0         # reserved strip for the always-visible horizontal bar


def _color_from_hex(hexstr, alpha=1.0):
    try:
        h = (hexstr or "").lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)
    except Exception:
        return NSColor.systemGrayColor()

# Classification (which bucket / what label / freedesktop icon) lives in the
# shared clip_types module so the Mac and GTK panels never diverge. The ext/mime
# sets are aliased here under their original names for the thumbnail-detection
# call sites below; macOS maps the shared bucket keys to its own SF Symbols and
# NSColors locally (rendering only).
_IMAGE_EXTS = clip_types.IMAGE_EXTS
_VIDEO_EXTS = clip_types.VIDEO_EXTS
_AUDIO_EXTS = clip_types.AUDIO_EXTS
_ARCHIVE_EXTS = clip_types.ARCHIVE_EXTS
_SHEET_EXTS = clip_types.SHEET_EXTS
_SHEET_MIMES = clip_types.SHEET_MIMES

_entry_type_key = clip_types.type_key


def _key_color(key):
    return {
        "text": _color_from_hex("#1E40AF"),       # deep blue
        "image": NSColor.systemTealColor(),
        "video": NSColor.systemPinkColor(),
        "audio": NSColor.systemPurpleColor(),
        "pdf": NSColor.systemRedColor(),
        "sheet": NSColor.systemGreenColor(),
        "archive": NSColor.systemBrownColor(),
        "file": NSColor.systemOrangeColor(),
    }.get(key, NSColor.systemOrangeColor())


# Bucket key -> SF Symbol (the macOS-local rendering of clip_types' icons).
_KEY_SYMBOL = {
    "text": "text.alignleft",
    "image": "photo",
    "video": "film",
    "audio": "music.note",
    "pdf": "doc.richtext",
    "sheet": "tablecells",
    "archive": "archivebox",
    "file": "doc",
}


def _category(entry):
    """(label, NSColor, SF-symbol name) for a history entry — granular for
    files. Label/bucket come from the shared classifier; colour + symbol are
    the macOS-local rendering of the bucket."""
    label, key, _icon = clip_types.category(entry)
    return label, _key_color(key), _KEY_SYMBOL[key]


# Type filter menu: keys/labels/order from the shared classifier, SF symbols
# mapped locally (key -> menu label + SF symbol).
_TYPE_FILTERS = [(k, lbl, _KEY_SYMBOL[k]) for k, lbl, _icon in clip_types.TYPE_FILTERS]


_app_icon_cache = {}


def _app_icon(bundle_id):
    """The icon for an app bundle id (cached), or None."""
    if not bundle_id:
        return None
    if bundle_id in _app_icon_cache:
        return _app_icon_cache[bundle_id]
    img = None
    try:
        from AppKit import NSWorkspace
        ws = NSWorkspace.sharedWorkspace()
        url = ws.URLForApplicationWithBundleIdentifier_(bundle_id)
        if url is not None:
            img = ws.iconForFile_(url.path())
    except Exception:
        img = None
    _app_icon_cache[bundle_id] = img
    return img


def _symbol_view(name, color, px):
    """An NSImageView showing a tinted SF Symbol (or empty if unavailable)."""
    iv = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, px, px))
    try:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img is not None:
            img.setTemplate_(True)
            iv.setImage_(img)
            iv.setContentTintColor_(color)
            iv.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    except Exception:
        pass
    return iv


def _relative_time(ts: float) -> str:
    delta = max(0, int(_time.time() - ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} d ago"


_ext = clip_types.ext


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


def _attr_label(text, size, color, bold=False, tracking=0.0):
    """A label with letter-spacing (tracking) for crisp badge/caption text."""
    from AppKit import (
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSKernAttributeName,
    )
    from Foundation import NSAttributedString
    f = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    lbl = NSTextField.labelWithString_("")
    lbl.setLineBreakMode_(NSLineBreakByTruncatingTail)
    lbl.setAttributedStringValue_(NSAttributedString.alloc().initWithString_attributes_(
        text or "", {NSForegroundColorAttributeName: color,
                     NSFontAttributeName: f, NSKernAttributeName: tracking}))
    return lbl


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


def _hide_alert_icon(alert):
    """Hide the app icon on an NSAlert (we don't want the Python/app icon)."""
    try:
        alert.setIcon_(NSImage.alloc().initWithSize_(NSMakeSize(1, 1)))
    except Exception:
        pass


def _confirm(message, info):
    """Modal OK/Cancel confirmation. Returns True on OK."""
    from AppKit import NSAlert
    alert = NSAlert.alloc().init()
    alert.setMessageText_(message)
    if info:
        alert.setInformativeText_(info)
    _hide_alert_icon(alert)
    alert.addButtonWithTitle_("OK")
    alert.addButtonWithTitle_("Cancel")
    return alert.runModal() == 1000


def _text_dialog(title, default):
    """Modal text prompt. Returns the entered string (stripped) or None."""
    from AppKit import NSAlert
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    _hide_alert_icon(alert)
    alert.addButtonWithTitle_("OK")
    alert.addButtonWithTitle_("Cancel")
    tf = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 240, 22))
    tf.setStringValue_(default or "")
    alert.setAccessoryView_(tf)
    alert.window().setInitialFirstResponder_(tf)
    if alert.runModal() == 1000:
        return (tf.stringValue() or "").strip() or None
    return None


# -- tile builders (module-level: no instance state) ----------------------
def _make_tile(entry):
    w, h = _TILE_W, _TILE_H
    tile = TileView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    tile.setWantsLayer_(True)
    tile.layer().setCornerRadius_(12.0)
    tile.layer().setMasksToBounds_(True)   # clip the header band to rounded corners
    tile.layer().setBackgroundColor_(
        NSColor.controlBackgroundColor().colorWithAlphaComponent_(0.92).CGColor())
    tile.layer().setBorderWidth_(0.5)
    tile.layer().setBorderColor_(NSColor.separatorColor().CGColor())

    # Colored header band — color + file-type icon + label vary by file type.
    badge_txt, badge_col, symbol = _category(entry)
    white = NSColor.whiteColor()
    white90 = white.colorWithAlphaComponent_(0.92)
    bh = _BAND_H
    band = NSView.alloc().initWithFrame_(NSMakeRect(0, h - bh, w, bh))
    band.setWantsLayer_(True)
    band.layer().setBackgroundColor_(badge_col.CGColor())
    tile.addSubview_(band)

    icon = _symbol_view(symbol, white, 18)
    icon.setFrame_(NSMakeRect(12, (bh - 18) / 2, 18, 18))
    band.addSubview_(icon)

    # delete + pin, flush right on the band (white); wired in reload()
    del_btn = _action_button("✕", 13, white90)
    del_btn.setFrame_(NSMakeRect(w - 26, (bh - 22) / 2, 22, 22))
    del_btn.setTag_(entry.id)
    band.addSubview_(del_btn)
    tile._del_btn = del_btn

    pin_btn = _action_button("★" if entry.pinned else "☆", 14, white90)
    pin_btn.setFrame_(NSMakeRect(w - 50, (bh - 22) / 2, 22, 22))
    pin_btn.setTag_(entry.id)
    band.addSubview_(pin_btn)
    tile._pin_btn = pin_btn

    # Source-app icon (the app the clip was copied from) — large, left of pin.
    src_img = _app_icon(mac_source.get(entry.id))
    icon_sz = 28.0
    src_right = w - 56                     # just left of the pin button
    if src_img is not None:
        siv = NSImageView.alloc().initWithFrame_(
            NSMakeRect(src_right - icon_sz, (bh - icon_sz) / 2, icon_sz, icon_sz))
        siv.setImage_(src_img)
        siv.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        band.addSubview_(siv)
        src_right -= icon_sz + 6

    lbl = _attr_label(badge_txt, 11, white, bold=True, tracking=0.8)
    lbl.setFrame_(NSMakeRect(36, (bh - 16) / 2, max(40.0, src_right - 36 - 36), 16))
    band.addSubview_(lbl)
    if entry.has_formatting:
        rich = _attr_label("RICH", 8, white90, bold=True, tracking=0.6)
        rich.setFrame_(NSMakeRect(src_right - 34, (bh - 13) / 2, 34, 13))
        band.addSubview_(rich)

    # footer: relative time + size/chars
    footer = _label(_meta_text(entry), 10, NSColor.tertiaryLabelColor())
    footer.setFrame_(NSMakeRect(_TILE_PAD + 2, _TILE_PAD - 2, w - 2 * _TILE_PAD - 2, 14))
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
        img = (_thumbnail_image(path, int(_TILE_W * 2))
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
            img = _thumbnail_image(str(cache), int(_TILE_W * 2))
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
                args=(path, qext, key, int(_TILE_W * 2)),
                daemon=True).start()
        is_video = mime.startswith("video/") or ext in _VIDEO_EXTS
        label = "VIDEO" if is_video else (ext.upper() or "FILE")
        return _centered(rect, [(label, 11, NSColor.secondaryLabelColor(), True),
                                (name or "file", 11, NSColor.labelColor())])
    # text snippet — larger, more readable
    tf = NSTextField.wrappingLabelWithString_((entry.text or "").strip()[:800])
    tf.setFont_(NSFont.systemFontOfSize_(14))
    tf.setTextColor_(NSColor.labelColor())
    tf.setFrame_(rect)
    tf.setLineBreakMode_(NSLineBreakByTruncatingTail)
    tf.setMaximumNumberOfLines_(8)
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


# -- Custom thin horizontal scroller --------------------------------------
class ClippyScroller(NSScroller):
    """A slim, always-visible horizontal bar: a faint track + a rounded knob
    that picks up the system accent, replacing the default chunky legacy bar."""

    def initWithFrame_(self, frame):
        self = objc.super(ClippyScroller, self).initWithFrame_(frame)
        if self is not None:
            self.setScrollerStyle_(0)          # legacy (reserves layout space)
        return self

    def drawKnobSlotInRect_highlight_(self, rect, flag):
        # subtle pill track, inset so it reads as a thin line
        inset = NSMakeRect(rect.origin.x + 2, rect.origin.y + 4,
                           rect.size.width - 4, max(3.0, rect.size.height - 8))
        r = inset.size.height / 2.0
        NSColor.tertiaryLabelColor().colorWithAlphaComponent_(0.18).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inset, r, r).fill()

    def drawKnob(self):
        rect = self.rectForPart_(2)            # NSScrollerKnob
        inset = NSMakeRect(rect.origin.x + 1, rect.origin.y + 4,
                           rect.size.width - 2, max(3.0, rect.size.height - 8))
        r = inset.size.height / 2.0
        NSColor.secondaryLabelColor().colorWithAlphaComponent_(0.55).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(inset, r, r).fill()


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
        self._type_filter = None   # None = all types; else a _TYPE_FILTERS key
        self._filter_btn = None
        self._tiles = []           # current TileViews, in display order
        self._sel = -1             # selected index for keyboard nav
        self._tab = "recent"       # "recent" | "pinned" | <custom tab name>
        self._tabbar = None        # container view rebuilt by _build_tabbar
        self._tabids = []          # tag index -> tab id
        self._open_settings = None  # callback set by mac_app (cog button)
        self._tab_dlg = None       # the New-tab popup while open
        self._tab_dlg_monitor = None
        self._last_sig = None      # (tab,query,count,newest) of the last rebuild
        return self

    def _history_sig(self):
        try:
            es = storage.list_entries(limit=1)
            newest = es[0].id if es else 0
            return (self._tab, self._query, self._type_filter, storage.count(), newest)
        except Exception:
            return None

    # -- building --------------------------------------------------------
    def _ensure_panel(self):
        if self._panel is not None:
            return
        rect = NSMakeRect(0, 0, 800, _PANEL_H)
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

        # Light/dark-adaptive blurred background — square corners (flush bar).
        ve = NSVisualEffectView.alloc().initWithFrame_(rect)
        ve.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        ve.setState_(NSVisualEffectStateActive)
        ve.setMaterial_(_visual_material())
        panel.setContentView_(ve)

        full_w = 800.0
        sh = 28.0
        row_y = _PANEL_H - _PAD - sh
        # Top row: a narrow search field on the LEFT, a type-filter button just
        # to its right, the tab bar CENTERED.
        sw = 200.0
        search = NSSearchField.alloc().initWithFrame_(
            NSMakeRect(_PAD, row_y, sw, sh))
        search.setFont_(NSFont.systemFontOfSize_(13))
        search.setDelegate_(self)
        search.setAutoresizingMask_(4 | 8)     # pinned top-left, fixed size
        ve.addSubview_(search)

        fbtn = NSButton.alloc().initWithFrame_(
            NSMakeRect(_PAD + sw + 6, row_y + (sh - 26) / 2, 26, 26))
        fbtn.setBordered_(False)
        fbtn.setTitle_("")
        fbtn.setTarget_(self)
        fbtn.setAction_(b"showTypeFilter:")
        fbtn.setAutoresizingMask_(4 | 8)       # pinned top-left
        ve.addSubview_(fbtn)
        self._filter_btn = fbtn
        # Left edge of the centered tab cluster must clear search + filter btn.
        self._tabbar_min_x = _PAD + sw + 6 + 26 + 8
        self._update_filter_btn()

        # Settings cog — top-right, level with the search/tab row.
        cog = NSButton.alloc().initWithFrame_(
            NSMakeRect(full_w - _PAD - 22, row_y + (sh - 22) / 2, 22, 22))
        cog.setBordered_(False)
        cog.setTitle_("")
        try:
            gear = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "gearshape", None)
            if gear is not None:
                gear.setTemplate_(True)
                cog.setImage_(gear)
                cog.setContentTintColor_(NSColor.secondaryLabelColor())
        except Exception:
            cog.setTitle_("⚙")
        cog.setTarget_(self)
        cog.setAction_(b"openSettings:")
        cog.setAutoresizingMask_(1 | 8)        # pinned top-right
        ve.addSubview_(cog)

        # Tab bar — full-width container; buttons are centered by _build_tabbar().
        tabbar = NSView.alloc().initWithFrame_(
            NSMakeRect(0, row_y + (sh - _TAB_H) / 2, full_w, _TAB_H))
        tabbar.setAutoresizingMask_(2 | 8)     # width-flexible, pinned to top
        ve.addSubview_(tabbar)
        self._tabbar = tabbar

        # Horizontal scroll of tiles below the top row, populated by reload().
        # Pulled close to the bottom edge; the scrollbar is always visible.
        # (Legacy style reserves the scroller strip *inside* this frame, so the
        # tiles' clip area sits just above the always-on bar near the bottom.)
        scroll_h = row_y - 6 - 6
        sframe = NSMakeRect(_PAD, 6, full_w - 2 * _PAD, scroll_h)
        scroll = NSScrollView.alloc().initWithFrame_(sframe)
        scroll.setHasHorizontalScroller_(True)
        scroll.setHasVerticalScroller_(False)
        scroll.setDrawsBackground_(False)
        scroll.setAutohidesScrollers_(False)   # the bar stays put — always visible
        scroll.setScrollerStyle_(0)            # NSScrollerStyleLegacy — reserves its strip
        scroll.setBorderType_(0)               # NSNoBorder
        scroll.setAutoresizingMask_(2 | 16)    # width + height flexible
        # Don't let AppKit inject safe-area/title insets — keeps doc x=0 flush
        # with the search field's left edge (both at _PAD).
        try:
            scroll.setAutomaticallyAdjustsContentInsets_(False)
        except Exception:
            pass
        try:
            sc = ClippyScroller.alloc().initWithFrame_(
                NSMakeRect(0, 0, sframe.size.width, _SCROLLER_H))
            sc.setControlSize_(1)              # NSControlSizeSmall
            scroll.setHorizontalScroller_(sc)
        except Exception:
            pass
        doc = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, sframe.size.width, sframe.size.height))
        scroll.setDocumentView_(doc)
        ve.addSubview_(scroll)

        panel.setDelegate_(self)   # for cancelOperation_ (Esc) routing
        self._panel = panel
        self._search = search
        self._scroll = scroll
        self._doc = doc
        self._build_tabbar()

    # -- tiles -----------------------------------------------------------
    def reload(self):
        """Rebuild tiles from the current history (called each time we show)."""
        self._ensure_panel()
        self._last_sig = self._history_sig()
        for v in list(self._doc.subviews()):
            v.removeFromSuperview()
        self._tiles = []
        self._sel = -1
        vis_h = self._scroll.contentView().bounds().size.height
        vis_w = self._scroll.frame().size.width
        entries = self._entries_for_tab()
        if not entries:
            if self._type_filter is not None:
                tlabel = next((l for k, l, _s in _TYPE_FILTERS
                               if k == self._type_filter), "that type")
                empty = f"No {tlabel.lower()} clips here."
            elif self._query:
                empty = "No matches."
            elif self._tab == "pinned":
                empty = "No pinned clips. Press ☆ on a clip to pin it."
            elif self._tab not in ("recent", "pinned"):
                empty = f"Nothing in “{self._tab}” yet. Press ☆ on a clip to add it."
            else:
                empty = "No clipboard history yet."
            msg = _label(empty, 14, NSColor.secondaryLabelColor(), _NS_CENTER)
            msg.setFrame_(NSMakeRect(0, vis_h / 2 - 12, vis_w, 24))
            self._doc.setFrameSize_(NSMakeSize(vis_w, vis_h))
            self._doc.addSubview_(msg)
            return
        th = _TILE_H
        y = 2.0                       # sit close to the bottom edge (just above the bar)
        x = 0.0                       # first tile's left edge aligns with the search field
        for e in entries:
            tile = _make_tile(e)
            tile._entry_id = e.id          # for click → selectEntry_
            tile._controller = self
            tile._pin_btn.setTarget_(self)
            tile._pin_btn.setAction_("pinClicked:")
            tile._del_btn.setTarget_(self)
            tile._del_btn.setAction_("deleteClicked:")
            if e.kind == "text" and e.html:        # right-click → plain-text copy
                ctx = NSMenu.alloc().init()
                mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    "Copy as Plain Text", b"selectEntryPlain:", "")
                mi.setTarget_(self)
                mi.setTag_(e.id)
                ctx.addItem_(mi)
                tile.setMenu_(ctx)
            tile.setFrame_(NSMakeRect(x, y, _TILE_W, th))
            self._doc.addSubview_(tile)
            self._tiles.append(tile)
            x += _TILE_W + _GAP
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
        height = _PANEL_H
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
        self._build_tabbar()        # re-center for the actual (full-screen) width
        # Snappy reopen: only rebuild tiles when the history actually changed.
        if not self._tiles or self._history_sig() != self._last_sig:
            self.reload()
        elif self._tiles:
            self._set_selection(0)
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

    # -- tabs ------------------------------------------------------------
    def _entries_for_tab(self):
        q = self._query
        tf = self._type_filter
        # Over-fetch when a type filter is active so a full screen survives the
        # post-filter (the SQL query has no type column).
        lim = config.DISPLAY_LIMIT if tf is None else max(config.DISPLAY_LIMIT * 8, 200)
        try:
            if self._tab == "recent":
                ents = storage.list_entries(query=q, limit=lim, pinned=False)
            elif self._tab == "pinned":
                members = mac_tabs.all_member_ids()
                ents = [e for e in storage.list_entries(query=q, limit=lim, pinned=True)
                        if e.id not in members]
            else:  # custom tab
                ents = [storage.get(i) for i in mac_tabs.member_ids(self._tab)]
                ents = [e for e in ents if e is not None]
                if q:
                    ql = q.lower()
                    ents = [e for e in ents
                            if ql in (e.text or "").lower()
                            or ql in (e.filename or "").lower()]
                ents.sort(key=lambda e: e.created_at, reverse=True)
            if tf is not None:
                ents = [e for e in ents if _entry_type_key(e) == tf]
            return ents[:config.DISPLAY_LIMIT]
        except Exception as exc:
            _log(f"entries_for_tab failed: {exc}")
            return []

    # -- type filter -----------------------------------------------------
    def _update_filter_btn(self):
        btn = self._filter_btn
        if btn is None:
            return
        tf = self._type_filter
        sym = "line.3.horizontal.decrease.circle"
        tint = NSColor.secondaryLabelColor()
        if tf is not None:
            sym = next((s for k, _l, s in _TYPE_FILTERS if k == tf), sym + ".fill")
            tint = NSColor.controlAccentColor()
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(sym, None)
            if img is not None:
                img.setTemplate_(True)
                btn.setImage_(img)
                btn.setContentTintColor_(tint)
            else:
                btn.setTitle_("⌄")
        except Exception:
            btn.setTitle_("⌄")
        label = next((l for k, l, _s in _TYPE_FILTERS if k == tf), "All types")
        btn.setToolTip_("Filter: " + (label if tf is not None else "All types"))

    def showTypeFilter_(self, sender):
        menu = NSMenu.alloc().init()
        allit = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "All types", b"filterPicked:", "")
        allit.setTarget_(self)
        allit.setRepresentedObject_("")           # "" => clear filter
        if self._type_filter is None:
            allit.setState_(1)
        menu.addItem_(allit)
        menu.addItem_(NSMenuItem.separatorItem())
        for key, label, sym in _TYPE_FILTERS:
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label, b"filterPicked:", "")
            it.setTarget_(self)
            it.setRepresentedObject_(key)
            try:
                img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(sym, None)
                if img is not None:
                    img.setTemplate_(True)
                    it.setImage_(img)
            except Exception:
                pass
            if self._type_filter == key:
                it.setState_(1)
            menu.addItem_(it)
        # Drop the menu directly underneath the filter button.
        menu.popUpMenuPositioningItem_atLocation_inView_(None, NSMakePoint(0, 0), sender)

    def filterPicked_(self, sender):
        k = sender.representedObject()
        self._type_filter = str(k) if k else None
        self._update_filter_btn()
        self.reload()

    def _build_tabbar(self):
        if self._tabbar is None:
            return
        for v in list(self._tabbar.subviews()):
            v.removeFromSuperview()
        self._tabids = []
        rows = [("recent", "Recent", NSColor.labelColor()),
                ("pinned", "★ Pinned", NSColor.labelColor())]
        for t in mac_tabs.tabs():
            rows.append((t["name"], "● " + t["name"], _color_from_hex(t.get("color"))))
        # Build the buttons first (to measure), then lay them out centered.
        btns = []
        for tab_id, label, color in rows:
            tag = len(self._tabids)
            self._tabids.append(tab_id)
            b = self._tab_button(label, color, self._tab == tab_id, tag)
            if tab_id not in ("recent", "pinned"):
                b.setMenu_(self._tab_mgmt_menu(tab_id))   # right-click to manage
            btns.append(b)
        plus = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 26, _TAB_H))
        plus.setBordered_(False)
        plus.setFont_(NSFont.boldSystemFontOfSize_(16))
        plus.setTitle_("+")
        plus.setTarget_(self)
        plus.setAction_(b"createTab:")
        gap = 8.0
        widths = [b.frame().size.width + 18 for b in btns] + [26.0]
        total = sum(widths) + gap * (len(widths) - 1)
        cw = self._tabbar.frame().size.width
        # Centered, but never under the left search field + filter button.
        min_x = getattr(self, "_tabbar_min_x", 236.0)
        x = max(min_x + gap, (cw - total) / 2.0)
        for b, w in zip(btns + [plus], widths):
            b.setFrame_(NSMakeRect(x, 0, w, _TAB_H))
            self._tabbar.addSubview_(b)
            x += w + gap

    def _tab_button(self, label, color, active, tag):
        from AppKit import NSFontAttributeName, NSForegroundColorAttributeName
        from Foundation import NSAttributedString
        font = (NSFont.boldSystemFontOfSize_(12) if active
                else NSFont.systemFontOfSize_(12))
        col = color if active else color.colorWithAlphaComponent_(0.5)
        b = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 10, _TAB_H))
        b.setBordered_(False)
        b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            label, {NSForegroundColorAttributeName: col, NSFontAttributeName: font}))
        b.setTag_(tag)
        b.setTarget_(self)
        b.setAction_(b"selectTab:")
        b.sizeToFit()
        if active:                              # subtle pill behind the active tab
            b.setWantsLayer_(True)
            b.layer().setBackgroundColor_(
                NSColor.labelColor().colorWithAlphaComponent_(0.10).CGColor())
            b.layer().setCornerRadius_(_TAB_H / 2.0 - 2.0)
        return b

    def selectTab_(self, sender):
        i = sender.tag()
        if not (0 <= i < len(self._tabids)):
            return
        self._tab = self._tabids[i]
        self._build_tabbar()
        self.reload()

    def _tab_mgmt_menu(self, name):
        """Right-click menu for a custom tab: rename / recolor / delete."""
        from AppKit import NSFontAttributeName, NSForegroundColorAttributeName
        from Foundation import NSAttributedString
        menu = NSMenu.alloc().init()
        rn = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Rename…", b"renameTab:", "")
        rn.setTarget_(self); rn.setRepresentedObject_({"tab": name})
        menu.addItem_(rn)
        color_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Color", None, "")
        submenu = NSMenu.alloc().init()
        for hexc in mac_tabs.PALETTE:
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("color", b"recolorTab:", "")
            it.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
                "●●●●●●", {NSForegroundColorAttributeName: _color_from_hex(hexc),
                           NSFontAttributeName: NSFont.systemFontOfSize_(13)}))
            it.setTarget_(self); it.setRepresentedObject_({"tab": name, "color": hexc})
            submenu.addItem_(it)
        color_item.setSubmenu_(submenu)
        menu.addItem_(color_item)
        menu.addItem_(NSMenuItem.separatorItem())
        dl = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Delete “{name}”", b"deleteTab:", "")
        dl.setTarget_(self); dl.setRepresentedObject_({"tab": name})
        menu.addItem_(dl)
        return menu

    def renameTab_(self, sender):
        old = sender.representedObject()["tab"]
        new = _text_dialog("Rename tab", old)
        if new and mac_tabs.rename_tab(old, new):
            if self._tab == old:
                self._tab = new
            self._build_tabbar()
            self.reload()

    def recolorTab_(self, sender):
        info = sender.representedObject()
        mac_tabs.set_color(info["tab"], info["color"])
        self._build_tabbar()

    def deleteTab_(self, sender):
        name = sender.representedObject()["tab"]
        if not _confirm(f"Delete the “{name}” tab?",
                             "The clips stay in your history; only the tab is removed."):
            return
        mac_tabs.remove_tab(name)
        if self._tab == name:
            self._tab = "recent"
        self._build_tabbar()
        self.reload()

    def createTab_(self, _sender):
        # A compact, non-modal popup: clicking outside cancels (#4).
        if self._tab_dlg is not None:
            return
        from AppKit import NSWindowStyleMaskBorderless
        w, h = 340.0, 178.0
        dlg = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        dlg.setLevel_(_panel_level() + 1)
        dlg.setOpaque_(False)
        dlg.setBackgroundColor_(NSColor.clearColor())
        dlg.setBecomesKeyOnlyIfNeeded_(False)
        dlg.setHasShadow_(True)
        bg = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        bg.setMaterial_(_visual_material())
        bg.setState_(NSVisualEffectStateActive)
        bg.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        bg.setWantsLayer_(True)
        bg.layer().setCornerRadius_(16.0)
        bg.layer().setMasksToBounds_(True)
        dlg.setContentView_(bg)

        title = _label("New tab", 14, NSColor.labelColor(), _NS_CENTER, bold=True)
        title.setFrame_(NSMakeRect(0, h - 34, w, 18))
        bg.addSubview_(title)

        name = NSTextField.alloc().initWithFrame_(NSMakeRect(24, h - 74, w - 48, 26))
        name.setPlaceholderString_("Tab name")
        name.setFont_(NSFont.systemFontOfSize_(13))
        bg.addSubview_(name)
        self._tab_dlg_name = name

        self._pending_color = mac_tabs.PALETTE[0]
        self._swatches = []
        sw, gap = 28.0, 8.0
        total = len(mac_tabs.PALETTE) * sw + (len(mac_tabs.PALETTE) - 1) * gap
        x0 = (w - total) / 2
        for i, hexc in enumerate(mac_tabs.PALETTE):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(x0 + i * (sw + gap), h - 112, sw, sw))
            b.setBordered_(False)
            b.setTitle_("")
            b.setWantsLayer_(True)
            b.layer().setBackgroundColor_(_color_from_hex(hexc).CGColor())
            b.layer().setCornerRadius_(7.0)
            b.setTag_(i)
            b.setTarget_(self)
            b.setAction_(b"swatchPicked:")
            bg.addSubview_(b)
            self._swatches.append(b)
        self._mark_swatch(0)

        cancel = NSButton.alloc().initWithFrame_(NSMakeRect(w / 2 - 144, 18, 132, 30))
        cancel.setBezelStyle_(1)
        cancel.setTitle_("Cancel")
        cancel.setTarget_(self)
        cancel.setAction_(b"tabDlgCancel:")
        cancel.setKeyEquivalent_("\x1b")
        bg.addSubview_(cancel)
        create = NSButton.alloc().initWithFrame_(NSMakeRect(w / 2 + 12, 18, 132, 30))
        create.setBezelStyle_(1)
        create.setTitle_("Create")
        create.setTarget_(self)
        create.setAction_(b"tabDlgCreate:")
        create.setKeyEquivalent_("\r")
        bg.addSubview_(create)

        if self._panel is not None:
            pf = self._panel.frame()
            dlg.setFrameOrigin_(NSMakePoint(pf.origin.x + (pf.size.width - w) / 2,
                                            pf.origin.y + (pf.size.height - h) / 2 + 60))
        dlg.setDelegate_(self)             # cancel when it loses key (click elsewhere)
        dlg.makeKeyAndOrderFront_(None)
        dlg.makeFirstResponder_(name)
        self._tab_dlg = dlg

        # Outside-click cancels; pause the panel's own click monitor meanwhile.
        self._remove_click_monitor()
        mask = NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown

        def _outside(_e):
            try:
                from AppKit import NSMouseInRect
                if NSMouseInRect(NSEvent.mouseLocation(), dlg.frame(), False):
                    return
            except Exception:
                pass
            self._closeTabDlg()

        self._tab_dlg_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, _outside)

    def tabDlgCreate_(self, _sender):
        name = (self._tab_dlg_name.stringValue() or "").strip()
        self._closeTabDlg()
        if name and mac_tabs.add_tab(name, self._pending_color):
            self._tab = name
            self._build_tabbar()
            self.reload()

    def tabDlgCancel_(self, _sender):
        self._closeTabDlg()

    def _closeTabDlg(self):
        if self._tab_dlg_monitor is not None:
            try:
                NSEvent.removeMonitor_(self._tab_dlg_monitor)
            except Exception:
                pass
            self._tab_dlg_monitor = None
        if self._tab_dlg is not None:
            self._tab_dlg.orderOut_(None)
            self._tab_dlg = None
        if self._panel is not None and self._panel.isVisible():
            self._add_click_monitor()

    def windowDidResignKey_(self, note):   # noqa: N802 — cancel the New-tab popup
        if self._tab_dlg is not None and note.object() is self._tab_dlg:
            self._closeTabDlg()

    def pickedTab_(self, sender):
        self._add_to(int(sender.tag()), str(sender.representedObject()))
        self.reload()

    # -- pin / add-to-tab / delete (buttons + ⌘P / ⌘⌫) -------------------
    def pinClicked_(self, sender):
        self._toggle_membership(int(sender.tag()), sender)

    def _toggle_membership(self, eid, anchor):
        # Remove if it's already in the tab we're viewing (with confirmation).
        if self._tab == "pinned" and eid not in mac_tabs.all_member_ids():
            if _confirm("Unpin this clip?", ""):
                self._set_pinned(eid, False); self.reload()
            return
        if self._tab not in ("recent", "pinned") and self._tab in mac_tabs.tabs_for(eid):
            if _confirm(f"Remove this clip from “{self._tab}”?", ""):
                mac_tabs.unassign(eid, self._tab); self.reload()
            return
        # Otherwise add: pick a destination if custom tabs exist, else Pinned.
        if mac_tabs.tab_names() and anchor is not None:
            self._show_tab_picker(anchor, eid)
        else:
            self._add_to(eid, "pinned"); self.reload()

    def _add_to(self, eid, dest):
        self._set_pinned(eid, True)
        if dest != "pinned":
            mac_tabs.assign(eid, dest)

    def _show_tab_picker(self, anchor, eid):
        menu = NSMenu.alloc().init()
        items = ([("pinned", "★ Pinned")]
                 + [(t["name"], "● " + t["name"]) for t in mac_tabs.tabs()])
        for dest, label in items:
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, b"pickedTab:", "")
            it.setTarget_(self)
            it.setTag_(eid)
            it.setRepresentedObject_(dest)
            menu.addItem_(it)
        # Drop the picker directly underneath the ☆ button.
        menu.popUpMenuPositioningItem_atLocation_inView_(None, NSMakePoint(0, 0), anchor)

    @staticmethod
    def _set_pinned(eid, flag):
        try:
            e = storage.get(eid)
            if e is not None and bool(e.pinned) != bool(flag):
                storage.toggle_pin(eid)
        except Exception as exc:
            _log(f"set_pinned failed: {exc}")

    def deleteClicked_(self, sender):
        if not _confirm("Delete this clip?", "It will be removed from your history."):
            return
        self._delete(int(sender.tag())); self.reload()

    def pinSelected(self):
        if 0 <= self._sel < len(self._tiles):
            self._toggle_membership(int(self._tiles[self._sel]._entry_id),
                                    self._tiles[self._sel]._pin_btn)

    def deleteSelected(self):
        if 0 <= self._sel < len(self._tiles):
            keep = self._sel
            if not _confirm("Delete this clip?",
                                 "It will be removed from your history."):
                return
            self._delete(int(self._tiles[keep]._entry_id))
            self.reload()
            if self._tiles:
                self._set_selection(min(keep, len(self._tiles) - 1))

    @staticmethod
    def _delete(entry_id):
        try:
            storage.delete(entry_id)
        except Exception as exc:
            _log(f"delete failed: {exc}")

    # -- create-tab dialog (name + fixed color palette) ------------------
    def swatchPicked_(self, sender):
        i = int(sender.tag())
        self._pending_color = mac_tabs.PALETTE[i]
        self._mark_swatch(i)

    def _mark_swatch(self, idx):
        for i, b in enumerate(getattr(self, "_swatches", [])):
            b.layer().setBorderWidth_(3.0 if i == idx else 0.0)
            b.layer().setBorderColor_(NSColor.labelColor().CGColor())

    def hide(self):
        self._remove_click_monitor()
        if self._panel is not None:
            self._panel.orderOut_(None)
        self._restore_frontmost()

    def selectEntry_(self, entry_id):
        """Load the clicked entry onto the clipboard and close (no auto-paste —
        focus returns to the previous app so the user's ⌘V lands there)."""
        self._recover_clip(int(entry_id), "auto")

    def selectEntryPlain_(self, sender):
        """Right-click → recover the plain-text version of a styled clip."""
        self._recover_clip(int(sender.tag()), "plain")

    def _recover_clip(self, entry_id, mode):
        try:
            e = storage.get(entry_id)
        except Exception:
            e = None
        if e is not None:
            _load_entry(e, mode)
            try:
                storage.touch(entry_id)        # recovered clip jumps to the front
            except Exception:
                pass
        self.hide()

    def openSettings_(self, _sender):
        # Close the panel but DON'T bounce focus back to the previous app —
        # otherwise it competes with the Settings window coming to the front.
        self._remove_click_monitor()
        self._prev_app = None
        if self._panel is not None:
            self._panel.orderOut_(None)
        if self._open_settings is not None:
            try:
                self._open_settings()
            except Exception as exc:
                _log(f"open settings failed: {exc}")

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
            # Only dismiss for clicks OUTSIDE the panel. (When our app isn't the
            # active one, clicks inside the panel are also seen by this global
            # monitor, which would otherwise close the panel on any click.)
            try:
                from AppKit import NSMouseInRect
                loc = NSEvent.mouseLocation()
                if (self._panel is not None
                        and NSMouseInRect(loc, self._panel.frame(), False)):
                    return
            except Exception:
                pass
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
