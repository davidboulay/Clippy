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
        # First occurrence wins: some apps (e.g. Bitwarden) advertise the same
        # type in two cases — 'text/plain;charset=utf-8' AND ';charset=UTF-8' —
        # but wl-paste only serves the exact string it actually offered. Letting
        # a later case-variant clobber the earlier one made us request a phantom
        # type that reads back empty. setdefault keeps the first (servable) one.
        avail: dict = {}
        for t in available:
            avail.setdefault(t.lower(), t)
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
        # Offer the raw image bytes so chat/editor apps (Slack, WhatsApp, VS Code,
        # browsers) accept a direct paste — not just file managers. wl-copy serves
        # native-Wayland apps; the X11 mirror covers XWayland apps, which the
        # compositor does not reliably hand a data-control image selection.
        subprocess.run(["wl-copy", "--type", mime], input=data, timeout=15)
        self._x11_mirror(mime, data)

    def mirror_to_x11(self, mime: str, data: bytes) -> None:
        """Publish freshly-*captured* bytes to the X11 clipboard so XWayland apps
        can paste them too. The compositor already bridges text both ways, but not
        image (or file) selections — so this covers exactly that gap, which is why
        ``copy_text`` deliberately doesn't mirror while ``copy_image`` does.

        Idempotent: skipped when X11 already holds these exact bytes. That guard
        does double duty — it breaks the Wayland<->X11 echo loop (Xwayland
        re-publishes whatever we put on X11 back onto the Wayland selection, which
        fires ``wl-paste --watch`` and re-enters capture), and it avoids yanking
        the selection away from an app that copied on the X11 side to begin with.
        Best-effort: no $DISPLAY or no xclip → native-Wayland paste still works."""
        import os
        if not os.environ.get("DISPLAY") or shutil.which("xclip") is None:
            return
        if self._x11_has(mime, data):
            return
        self._x11_mirror(mime, data)

    @staticmethod
    def _x11_has(mime: str, data: bytes) -> bool:
        """True if the X11 clipboard already serves exactly `data` for `mime`."""
        try:
            cur = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", mime, "-o"],
                capture_output=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        return cur.returncode == 0 and cur.stdout == data

    @staticmethod
    def _x11_mirror(mime: str, data: bytes) -> None:
        """Also place `data` on the X11 (XWayland) clipboard via xclip, served by
        a detached process so it survives after this call. Best-effort: skipped if
        there's no X display or xclip isn't installed (native-Wayland paste still
        works via wl-copy)."""
        import os
        if not os.environ.get("DISPLAY") or shutil.which("xclip") is None:
            return
        try:
            p = subprocess.Popen(
                ["xclip", "-selection", "clipboard", "-t", mime],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            p.stdin.write(data)
            p.stdin.close()   # xclip drains stdin, then forks to serve the selection
        except OSError:
            pass

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
        # Mirror a uri-list to X11 so XWayland apps that accept a dropped/pasted
        # file (editors, some chat apps) see it too.
        self._x11_mirror("text/uri-list", f"file://{uri}\r\n".encode("utf-8"))

    def start_watch(self, on_change: Callable[[], None]) -> None:
        # No-op: the daemon spawns `wl-paste --watch ... _store`, which is the
        # capture trigger on Linux.
        return None
