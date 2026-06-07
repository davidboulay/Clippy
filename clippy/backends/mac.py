"""macOS clipboard backend via PyObjC NSPasteboard.

macOS has no background push for clipboard changes, so ``start_watch`` polls
``changeCount`` on a timer (the same approach Maccy/Flycut/Crosspaste use).
We translate between our MIME vocabulary and NSPasteboard UTIs so the portable
capture path is unchanged. Requires ``pyobjc-framework-Cocoa``.

NOTE: untested on Linux CI — validate on a Mac.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from .base import ClipboardError

_POLL_SECONDS = 0.4

# MIME <-> NSPasteboard UTI
_TEXT = "public.utf8-plain-text"
_HTML = "public.html"
_PNG = "public.png"
_TIFF = "public.tiff"


class MacBackend:
    def __init__(self):
        try:
            import AppKit  # noqa: F401
        except Exception as exc:  # pragma: no cover - mac only
            raise ClipboardError(
                "macOS clipboard support needs PyObjC:\n"
                "    pip install pyobjc-framework-Cocoa"
            ) from exc
        from AppKit import NSPasteboard
        self._NSPasteboard = NSPasteboard
        self._pb = NSPasteboard.generalPasteboard()
        self._last_change = self._pb.changeCount()
        self._watch_thread: Optional[threading.Thread] = None
        self._stop = False

    def require_tools(self) -> None:
        return None  # import check happened in __init__

    # -- type discovery --------------------------------------------------
    def list_types(self) -> List[str]:
        types = list(self._pb.types() or [])
        out: List[str] = []
        if _PNG in types or _TIFF in types:
            out.append("image/png")
        if _TEXT in types or self._pb.stringForType_(_TEXT):
            out.append("text/plain")
        if _HTML in types:
            out.append("text/html")
        return out

    def pick_image_type(self, types: List[str]) -> Optional[str]:
        return "image/png" if "image/png" in types else None

    def pick_text_type(self, types: List[str]) -> Optional[str]:
        return "text/plain" if "text/plain" in types else None

    def pick_html_type(self, types: List[str]) -> Optional[str]:
        return "text/html" if "text/html" in types else None

    # -- read ------------------------------------------------------------
    def read_text(self, mime: Optional[str] = None) -> str:
        uti = _HTML if (mime and "html" in mime) else _TEXT
        return self._pb.stringForType_(uti) or ""

    def read_bytes(self, mime: str) -> bytes:
        order = (_PNG, _TIFF) if "png" in mime else (_TIFF, _PNG)
        for uti in order:                       # whichever rep the app provided
            data = self._pb.dataForType_(uti)
            if data is not None:
                return bytes(data)
        return b""

    # -- write -----------------------------------------------------------
    def copy_text(self, text: str) -> None:
        self._pb.clearContents()
        self._pb.setString_forType_(text, _TEXT)
        self._last_change = self._pb.changeCount()

    def copy_html(self, html: str) -> None:
        self._pb.clearContents()
        self._pb.setString_forType_(html, _HTML)
        self._last_change = self._pb.changeCount()

    def copy_image(self, data: bytes, mime: str) -> None:
        from AppKit import NSData
        uti = _PNG if "png" in mime else _TIFF
        nsdata = NSData.dataWithBytes_length_(data, len(data))
        self._pb.clearContents()
        self._pb.setData_forType_(nsdata, uti)
        self._last_change = self._pb.changeCount()

    # -- files ----------------------------------------------------------
    def read_file_paths(self, types: List[str]) -> List[str]:
        import os
        out = []
        # 1) Legacy Finder file list — the most reliable for "copy" in Finder.
        try:
            fl = self._pb.propertyListForType_("NSFilenamesPboardType")
            if fl:
                for p in fl:
                    p = str(p)
                    if os.path.isfile(p):
                        out.append(p)
        except Exception:
            pass
        if out:
            return out
        # 2) Modern file URLs (public.file-url).
        try:
            from AppKit import NSURL
            urls = self._pb.readObjectsForClasses_options_([NSURL], None)
            for u in (urls or []):
                try:
                    if u.isFileURL():
                        p = str(u.path())
                        if os.path.isfile(p):
                            out.append(p)
                except Exception:
                    pass
        except Exception:
            pass
        return out

    def copy_file(self, path: str) -> None:
        from AppKit import NSURL
        url = NSURL.fileURLWithPath_(path)
        self._pb.clearContents()
        self._pb.writeObjects_([url])
        self._last_change = self._pb.changeCount()

    # -- watch (poll changeCount) ---------------------------------------
    def start_watch(self, on_change: Callable[[], None]) -> None:
        if self._watch_thread is not None:
            return

        def loop():
            while not self._stop:
                time.sleep(_POLL_SECONDS)
                try:
                    cur = self._pb.changeCount()
                except Exception:
                    continue
                if cur != self._last_change:
                    self._last_change = cur
                    try:
                        on_change()
                    except Exception:
                        pass

        self._watch_thread = threading.Thread(target=loop, daemon=True)
        self._watch_thread.start()

    def stop_watch(self) -> None:
        self._stop = True
