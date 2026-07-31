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

from .. import config, settings, x11clip
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
            # text/uri-list is a *file* flavor that merely starts with "text/".
            # Falling back to it filed a file clip as a text entry holding the
            # URI string — which a file-only offer (no text flavor) hits every
            # time.
            if (low.startswith("text/") and not low.startswith("text/html")
                    and low != "text/uri-list"):
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
        data = text.encode("utf-8")
        subprocess.run(["wl-copy"], input=data, timeout=10)
        # wl-copy only sets the wlr-data-control selection. cosmic-comp bridges
        # the *regular* wl_data_device selection into data-control (so wl-paste
        # sees app copies) but NOT reliably the other way — a data-control
        # selection is invisible to GUI apps that read the regular selection
        # (Chromium/Brave, GTK, every XWayland app), so a recovered clip would
        # sit unpasteable there. Hand it to the persistent X11 owner, which
        # serves XWayland apps directly and keeps Xwayland alive; fall back to a
        # one-shot xclip mirror when that owner isn't available.
        if not x11clip.publish(data):
            self.mirror_to_x11(None, data)

    def copy_html(self, html: str, text: Optional[str] = None) -> None:
        # ``text`` (the plain flavor) can't be offered alongside html here:
        # wl-copy serves a single MIME type per invocation, and a second
        # wl-copy would steal the selection and drop the html.
        data = html.encode("utf-8")
        subprocess.run(["wl-copy", "--type", "text/html"], input=data, timeout=10)
        # Mirror to X11 as well (see copy_text): a data-control text/html
        # selection never reaches GUI apps otherwise. They derive plain text
        # from text/html when no plain target is offered, so rich paste still
        # lands as at least plain text everywhere.
        self.mirror_to_x11("text/html", data)

    def copy_image(self, data: bytes, mime: str) -> None:
        # An image has to be offered two ways *at the same time*: raw bytes, so
        # chat/editor apps (Slack, WhatsApp, browsers, a VS Code editor pane)
        # accept a direct paste, and a file reference, because file-drop targets
        # — the VS Code Explorer, Nautilus — can do nothing with bytes and would
        # otherwise paste an empty file. Only the persistent owner can offer
        # both at once; wl-copy and xclip serve one type per invocation.
        parts = [(mime, data)]
        # Only when asked: a consumer that understands both flavors may act on
        # both, and chat apps do exactly that (Slack attaches the image *and* an
        # empty file per file flavor). We cannot tell consumers apart from here,
        # so the common case — pasting into chat — wins by default.
        staged = (self._stage_image(data, mime)
                  if settings.get("image_file_flavors") else None)
        if staged:
            parts += self._file_parts(staged)
        if x11clip.publish_parts(parts):
            # Remember what we put there: the next clipboard change is our own
            # publish coming back as a capture, and it must not be mistaken for
            # someone else's copy (which releases the selection).
            import hashlib
            x11clip.note_published(hashlib.sha256(data).hexdigest())
            # The owner holds the X11 selection and Xwayland re-exposes it as the
            # regular Wayland selection, so native-Wayland *and* XWayland apps
            # both see it. Adding a wl-copy here would be worse than useless: it
            # only sets data-control, and loses the selection (and exits) within
            # seconds once the owner's X11 grab bounces back through cosmic-comp.
            return
        # No persistent owner (no X display, or GTK 4 unavailable): fall back to
        # one flavor per channel — bytes only, so file-drop targets stay broken.
        subprocess.run(["wl-copy", "--type", mime], input=data, timeout=15)
        self._x11_mirror(mime, data)

    @staticmethod
    def _stage_image(data: bytes, mime: str) -> Optional[str]:
        """Write the image somewhere it can be referenced as a *file*, under a
        human-friendly name — a file-drop paste is named after the URI, and the
        content-addressed blob is named after its sha256. Rewritten per recover;
        returns None if it can't be staged (callers then offer bytes only).

        Lives in ``FLAVOR_DIR``, apart from the recovered-file staging in
        ``PASTE_DIR``, because ``capture._is_own_staging`` has to ignore the echo
        this produces without also ignoring a genuine file recover."""
        import os
        ext = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/bmp": "bmp",
               "image/tiff": "tiff"}.get(mime.lower(), "png")
        try:
            stage = config.FLAVOR_DIR
            stage.mkdir(parents=True, exist_ok=True)
            dest = stage / f"image.{ext}"
            tmp = stage / f".image.{ext}.tmp"
            # Write-then-rename: a consumer that resolves the URI while we're
            # still writing would otherwise read a truncated file.
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            return str(dest)
        except OSError:
            return None

    @staticmethod
    def _file_parts(path: str) -> List[tuple]:
        """The two flavors a file-drop target understands, for one path."""
        import urllib.request
        uri = "file://" + urllib.request.pathname2url(path)
        return [
            ("text/uri-list", (uri + "\r\n").encode("utf-8")),
            ("x-special/gnome-copied-files", ("copy\n" + uri).encode("utf-8")),
        ]

    def mirror_to_x11(self, mime: Optional[str], data: bytes) -> None:
        """Publish `data` to the X11 clipboard so XWayland apps — and, via the
        Xwayland bridge, native-Wayland GUI apps — can paste it too. cosmic-comp
        mirrors the regular selection *into* data-control but not back out, so
        anything Clippy sets with wl-copy (data-control) is otherwise invisible
        to those apps; this crosses that gap for text, html, images and files
        alike. ``mime=None`` uses xclip's default text targets (UTF8_STRING /
        STRING / TEXT), which Xwayland maps to ``text/plain;charset=utf-8``.

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
    def _x11_has(mime: Optional[str], data: bytes) -> bool:
        """True if the X11 clipboard already serves exactly `data` for `mime`
        (or the default text target when `mime` is None)."""
        cmd = ["xclip", "-selection", "clipboard", "-o"]
        if mime is not None:
            cmd += ["-t", mime]
        try:
            cur = subprocess.run(cmd, capture_output=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return False
        return cur.returncode == 0 and cur.stdout == data

    @staticmethod
    def _x11_mirror(mime: Optional[str], data: bytes) -> None:
        """Also place `data` on the X11 (XWayland) clipboard via xclip, served by
        a detached process so it survives after this call. `mime=None` lets xclip
        use its default text targets. Best-effort: skipped if there's no X display
        or xclip isn't installed (native-Wayland paste still works via wl-copy)."""
        import os
        if not os.environ.get("DISPLAY") or shutil.which("xclip") is None:
            return
        cmd = ["xclip", "-selection", "clipboard"]
        if mime is not None:
            cmd += ["-t", mime]
        try:
            p = subprocess.Popen(
                cmd,
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
        # The persistent owner carries both at once and keeps serving them; the
        # fallback below can only put one on each channel.
        if x11clip.publish_parts(self._file_parts(path)):
            return
        import urllib.request
        uri = urllib.request.pathname2url(path)
        payload = f"copy\nfile://{uri}".encode("utf-8")
        subprocess.run(["wl-copy", "--type", "x-special/gnome-copied-files"],
                       input=payload, timeout=15)
        # Mirror a uri-list to X11 so XWayland apps that accept a pasted file
        # (editors, some chat apps) see it too.
        self._x11_mirror("text/uri-list", f"file://{uri}\r\n".encode("utf-8"))

    def start_watch(self, on_change: Callable[[], None]) -> None:
        # No-op: the daemon spawns `wl-paste --watch ... _store`, which is the
        # capture trigger on Linux.
        return None
