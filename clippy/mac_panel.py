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
    NSBackingStoreBuffered,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskRightMouseDown,
    NSFont,
    NSPanel,
    NSScreen,
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
from Foundation import NSMakeRect, NSObject, NSPoint

from . import config

# Carbon hot-key event constants.
_kEventClassKeyboard = 0x6B657962      # 'keyb'
_kEventHotKeyPressed = 6
_kVK_ANSI_V = 9                        # 'v' on the US layout
# Carbon modifier masks (Events.h)
_cmdKey = 0x0100
_shiftKey = 0x0200
_optionKey = 0x0800
_controlKey = 0x1000

_NS_WINDOW_LEVEL_STATUS = 25           # NSStatusWindowLevel

_DEBUG_LOG = "/tmp/clippy-panel.log"


def _log(msg: str) -> None:
    """Append a diagnostic line to a file (GUI apps swallow stdout)."""
    try:
        with open(_DEBUG_LOG, "a") as f:
            f.write(f"{_time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _status_level():
    try:
        from AppKit import NSStatusWindowLevel
        return NSStatusWindowLevel
    except Exception:
        return _NS_WINDOW_LEVEL_STATUS


def _visual_material():
    """A light/dark-adaptive material; name varies across macOS versions."""
    from AppKit import NSVisualEffectView as _VE  # noqa: N811
    for name in ("NSVisualEffectMaterialPopover", "NSVisualEffectMaterialHUDWindow",
                 "NSVisualEffectMaterialMenu", "NSVisualEffectMaterialWindowBackground"):
        val = getattr(__import__("AppKit"), name, None)
        if val is not None:
            return val
    return 6  # popover


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
        return self

    # -- building --------------------------------------------------------
    def _ensure_panel(self):
        if self._panel is not None:
            return
        rect = NSMakeRect(0, 0, 800, config.PANEL_HEIGHT)
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = ClippyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        panel.setLevel_(_status_level())
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setHidesOnDeactivate_(False)
        panel.setFloatingPanel_(True)
        panel.setWorksWhenModal_(True)
        panel.setBecomesKeyOnlyIfNeeded_(False)
        panel.setOpaque_(False)
        panel.setMovableByWindowBackground_(False)

        # Light/dark-adaptive blurred background with rounded corners.
        ve = NSVisualEffectView.alloc().initWithFrame_(rect)
        ve.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        ve.setState_(NSVisualEffectStateActive)
        ve.setMaterial_(_visual_material())
        ve.setWantsLayer_(True)
        ve.layer().setCornerRadius_(14.0)
        ve.layer().setMasksToBounds_(True)
        panel.setContentView_(ve)

        # Milestone-1 placeholder content (tiles replace this in milestone 2).
        label = NSTextField.labelWithString_(
            "Clippy — clipboard history\n(shell · ⌘⇧V or menu to toggle · Esc / click away to close)")
        label.setFont_(NSFont.systemFontOfSize_(15))
        label.setAlignment_(1)             # NSTextAlignmentCenter
        label.setFrame_(NSMakeRect(0, config.PANEL_HEIGHT / 2 - 24, 800, 48))
        label.setAutoresizingMask_(0x12)   # width-flexible + centered vertically-ish
        label.setMaximumNumberOfLines_(2)
        ve.addSubview_(label)

        self._panel = panel

    def _position(self):
        """Anchor a full-width strip to the bottom of the screen under the cursor."""
        screen = self._screen_under_cursor()
        vis = screen.visibleFrame()
        margin = 16.0
        width = vis.size.width - 2 * margin
        height = float(config.PANEL_HEIGHT)
        x = vis.origin.x + margin
        y = vis.origin.y + margin
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
        self._position()
        self._panel.makeKeyAndOrderFront_(None)
        self._panel.orderFrontRegardless()
        self._add_click_monitor()

    def hide(self):
        self._remove_click_monitor()
        if self._panel is not None:
            self._panel.orderOut_(None)

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
