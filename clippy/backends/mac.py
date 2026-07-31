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
_FILE_URL = "public.file-url"
_FILENAMES = "NSFilenamesPboardType"


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
        # A copied file (Finder Cmd+C). Surface it first — Finder usually also
        # drops a TIFF preview + filename text, but a bare file-url copy (some
        # apps) would otherwise leave this list empty and make capture_current()
        # bail at `if not types` before read_file_paths is ever consulted.
        if _FILE_URL in types or _FILENAMES in types:
            out.append("text/uri-list")
        # PNG for either rep: a promise `read_bytes` keeps by transcoding the
        # TIFF-only case, so callers never get TIFF wearing a PNG label.
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
        """Image bytes in the flavor the caller actually asked for.

        ``list_types`` advertises ``image/png`` whenever the pasteboard holds any
        image, but macOS most often offers only ``public.tiff`` — Preview, Safari's
        Copy Image and screenshots all do. Returning those TIFF bytes under a PNG
        label is what filed uncompressed TIFF into history as an ``image/png``
        entry: a tile that renders broken and pastes as an empty image, here and
        on every device it syncs to. So honour the claim rather than mislabel it —
        transcode. (It also shrinks the payload a lot; the TIFF flavor is
        uncompressed, e.g. 1.59 MB for a 778x510 grab.)

        If the transcode fails we return nothing at all. Handing back TIFF under
        a PNG label is the bug this exists to prevent, and a dropped clip is the
        lesser failure."""
        want_png = "png" in mime
        order = (_PNG, _TIFF) if want_png else (_TIFF, _PNG)
        for uti in order:                       # whichever rep the app provided
            data = self._pb.dataForType_(uti)
            if data is None:
                continue
            raw = bytes(data)
            if not want_png or uti == _PNG:
                return raw
            return self._tiff_to_png(raw)
        return b""

    @staticmethod
    def _tiff_to_png(data: bytes) -> bytes:
        """`data` (a TIFF rep) re-encoded as PNG, or b"" if that isn't possible.

        Never raises and never returns the input unchanged: every caller is about
        to label the result ``image/png``."""
        try:
            from AppKit import NSBitmapImageRep, NSData
            try:
                from AppKit import NSBitmapImageFileTypePNG as png_type
            except ImportError:                 # PyObjC < 8 spelled it differently
                from AppKit import NSPNGFileType as png_type
        except Exception:
            return b""
        try:
            nsdata = NSData.dataWithBytes_length_(data, len(data))
            rep = NSBitmapImageRep.imageRepWithData_(nsdata)
            if rep is None:
                return b""
            out = rep.representationUsingType_properties_(png_type, {})
            return bytes(out) if out else b""
        except Exception:
            return b""

    # -- write -----------------------------------------------------------
    def copy_text(self, text: str) -> None:
        self._pb.clearContents()
        self._pb.setString_forType_(text, _TEXT)
        self._last_change = self._pb.changeCount()

    def copy_html(self, html: str, text: Optional[str] = None) -> None:
        # Offer BOTH flavors, like a native browser copy: apps that paste
        # plain text (VS Code, Slack — Electron reads public.utf8-plain-text)
        # get nothing from an html-only pasteboard.
        self._pb.clearContents()
        self._pb.setString_forType_(html, _HTML)
        if text:
            self._pb.setString_forType_(text, _TEXT)
        self._last_change = self._pb.changeCount()

    def copy_image(self, data: bytes, mime: str) -> None:
        from AppKit import NSData
        uti = _PNG if "png" in mime else _TIFF
        nsdata = NSData.dataWithBytes_length_(data, len(data))
        self._pb.clearContents()
        self._pb.setData_forType_(nsdata, uti)
        self._last_change = self._pb.changeCount()

    def mirror_to_x11(self, mime: str, data: bytes) -> None:
        return None  # X11 has no meaning on macOS

    # -- files ----------------------------------------------------------
    def read_file_paths(self, types: List[str]) -> List[str]:
        import os
        import urllib.parse

        def _from_url_str(s):
            try:
                p = urllib.parse.unquote(urllib.parse.urlparse(str(s)).path)
                return p if (p and os.path.isfile(p)) else None
            except Exception:
                return None

        out = []
        # 1) public.file-url per pasteboard item — the reliable path on modern
        #    macOS (Finder Cmd+C). Handles multi-file selections too.
        try:
            for it in (self._pb.pasteboardItems() or []):
                try:
                    s = it.stringForType_(_FILE_URL)
                except Exception:
                    s = None
                p = _from_url_str(s) if s else None
                if p:
                    out.append(p)
        except Exception:
            pass
        if out:
            return out
        # 2) NSURL objects from the pasteboard.
        try:
            from AppKit import NSURL
            for u in (self._pb.readObjectsForClasses_options_([NSURL], None) or []):
                try:
                    if u.isFileURL() and os.path.isfile(str(u.path())):
                        out.append(str(u.path()))
                except Exception:
                    pass
        except Exception:
            pass
        if out:
            return out
        # 3) Single public.file-url on the pasteboard, then legacy filenames.
        try:
            p = _from_url_str(self._pb.stringForType_(_FILE_URL))
            if p:
                out.append(p)
        except Exception:
            pass
        if not out:
            try:
                for p in (self._pb.propertyListForType_(_FILENAMES) or []):
                    if os.path.isfile(str(p)):
                        out.append(str(p))
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
