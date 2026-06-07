"""Wayland clipboard backend — wraps wl-clipboard (wl-paste / wl-copy).

GTK-free so it can be imported by the lightweight ``_store`` subprocess that
``wl-paste --watch`` spawns on every clipboard change. Capture on Linux is
driven by that external subprocess (see daemon._start_watcher), so
``start_watch`` here is a no-op.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Callable, List, Optional

from .. import config
from .base import ClipboardError


class WaylandBackend:
    def require_tools(self) -> None:
        missing = [t for t in ("wl-paste", "wl-copy") if shutil.which(t) is None]
        if missing:
            raise ClipboardError(
                "Missing required tools: %s. Install with:\n"
                "    sudo apt install wl-clipboard" % ", ".join(missing)
            )

    def list_types(self) -> List[str]:
        try:
            out = subprocess.run(
                ["wl-paste", "--list-types"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    @staticmethod
    def _pick(available: List[str], preferred) -> Optional[str]:
        avail = {t.lower(): t for t in available}
        for want in preferred:
            if want.lower() in avail:
                return avail[want.lower()]
        return None

    def pick_image_type(self, types: List[str]) -> Optional[str]:
        hit = self._pick(types, config.IMAGE_TYPES)
        if hit:
            return hit
        for t in types:
            if t.lower().startswith("image/"):
                return t
        return None

    def pick_text_type(self, types: List[str]) -> Optional[str]:
        hit = self._pick(types, config.TEXT_TYPES)
        if hit:
            return hit
        for t in types:
            low = t.lower()
            if low.startswith("text/") and not low.startswith("text/html"):
                return t
        return None

    def pick_html_type(self, types: List[str]) -> Optional[str]:
        return self._pick(types, config.HTML_TYPES)

    def read_bytes(self, mime: str) -> bytes:
        try:
            return subprocess.run(
                ["wl-paste", "-t", mime], capture_output=True, timeout=15,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            return b""

    def read_text(self, mime: Optional[str] = None) -> str:
        cmd = ["wl-paste", "--no-newline"]
        if mime:
            cmd += ["-t", mime]
        try:
            raw = subprocess.run(cmd, capture_output=True, timeout=15).stdout
        except (subprocess.SubprocessError, OSError):
            return ""
        return raw.decode("utf-8", "replace")

    def copy_text(self, text: str) -> None:
        subprocess.run(["wl-copy"], input=text.encode("utf-8"), timeout=10)

    def copy_html(self, html: str) -> None:
        subprocess.run(
            ["wl-copy", "--type", "text/html"],
            input=html.encode("utf-8"), timeout=10,
        )

    def copy_image(self, data: bytes, mime: str) -> None:
        subprocess.run(["wl-copy", "--type", mime], input=data, timeout=15)

    # -- files ----------------------------------------------------------
    _FILE_TYPES = ("x-special/gnome-copied-files", "text/uri-list")

    def read_file_paths(self, types: List[str]) -> List[str]:
        import urllib.parse
        low = {t.lower(): t for t in types}
        for want in self._FILE_TYPES:
            if want in low:
                raw = self.read_text(low[want])
                paths = []
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith("file://"):
                        p = urllib.parse.unquote(urllib.parse.urlparse(line).path)
                        import os
                        if os.path.isfile(p):
                            paths.append(p)
                if paths:
                    return paths
        return []

    def copy_file(self, path: str) -> None:
        # Offer the file the way file managers expect: a gnome-copied-files list
        # plus a uri-list, so pasting in Files/Nautilus drops the actual file.
        import urllib.request
        uri = urllib.request.pathname2url(path)
        payload = f"copy\nfile://{uri}".encode("utf-8")
        subprocess.run(["wl-copy", "--type", "x-special/gnome-copied-files"],
                       input=payload, timeout=15)

    def start_watch(self, on_change: Callable[[], None]) -> None:
        # No-op: the daemon spawns `wl-paste --watch ... _store`, which is the
        # capture trigger on Linux.
        return None
