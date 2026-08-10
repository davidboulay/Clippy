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

from .. import config, debuglog, richtext, settings, x11clip
from .base import ClipboardError


def _note_published(*payloads: bytes) -> None:
    """Tell the X11 owner which content it is now serving, by sha256 hex.

    Every recover path has to do this, not just images. The digest is how
    ``x11clip.release_unless_ours`` recognises our own publish coming back
    through the compositor as a capture; a path that skips it either has its
    fresh clip released by its own echo, or — when nothing is tracked at all —
    keeps serving a clip long after another app has replaced it."""
    import hashlib
    x11clip.note_published(*(hashlib.sha256(p).hexdigest() for p in payloads if p))


# A recover runs on the GTK main thread, so every blocking call here is UI
# freeze the user feels. The normal path is a couple of hundred milliseconds
# (an acknowledged publish plus a wl-copy that forks straight away); a write
# still going after this long is not going to succeed, and failing fast leaves
# the panel responsive instead of hung.
_WRITE_TIMEOUT = 5


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

    # A clipboard read is served by the *source* app, so a hung or dying source
    # hangs us. That matters more than it looks: wl-paste --watch runs its hooks
    # strictly one after another, so seconds spent here are seconds during which
    # later copies aren't captured at all. Local transfers finish in
    # milliseconds; anything near this bound is a source that is never going to
    # answer, and giving up early costs a clip rather than a run of them.
    _READ_TIMEOUT = 6

    def read_bytes(self, mime: str) -> bytes:
        try:
            return subprocess.run(
                ["wl-paste", "-t", mime], capture_output=True,
                timeout=self._READ_TIMEOUT,
            ).stdout
        except subprocess.TimeoutExpired:
            debuglog.log("read.timeout", mime=mime)
            return b""
        except (subprocess.SubprocessError, OSError):
            return b""

    def read_text(self, mime: Optional[str] = None) -> str:
        cmd = ["wl-paste", "--no-newline"]
        if mime:
            cmd += ["-t", mime]
        try:
            raw = subprocess.run(cmd, capture_output=True,
                                 timeout=self._READ_TIMEOUT).stdout
        except subprocess.TimeoutExpired:
            debuglog.log("read.timeout", mime=mime or "<default>")
            return ""
        except (subprocess.SubprocessError, OSError):
            return ""
        return raw.decode("utf-8", "replace")

    def copy_text(self, text: str) -> None:
        # wl-copy only sets the wlr-data-control selection. cosmic-comp bridges
        # the *regular* wl_data_device selection into data-control (so wl-paste
        # sees app copies) but NOT reliably the other way — a data-control
        # selection is invisible to GUI apps that read the regular selection
        # (Chromium/Brave, GTK, every XWayland app), so a recovered clip would
        # sit unpasteable there. Hand it to the persistent X11 owner, which
        # serves XWayland apps directly (and, via Xwayland re-exposing the X11
        # selection as the regular one, native-Wayland apps too) and keeps
        # Xwayland alive; fall back to wl-copy plus a one-shot xclip mirror only
        # when that owner isn't available.
        data = text.encode("utf-8")
        if x11clip.publish(data):
            _note_published(data)
            return
        subprocess.run(["wl-copy"], input=data, timeout=_WRITE_TIMEOUT)
        self._x11_mirror(None, data)

    def copy_html(self, html: str, text: Optional[str] = None) -> None:
        # A rich clip has to be offered *both* ways at once. Serving text/html
        # alone is what made ordinary copied text unpasteable in some apps until
        # the user picked "Copy as plain text": a consumer that asks only for
        # plain targets (UTF8_STRING/STRING/text/plain) finds nothing to
        # convert, and gets nothing. wl-copy and xclip each carry a single type
        # per invocation and so cannot express this; the persistent owner's
        # union provider can, and GDK's X11 backend expands
        # text/plain;charset=utf-8 into the classic targets for us.
        data = html.encode("utf-8")
        plain = (text if text is not None else richtext.html_to_text(html))
        plain_data = (plain or "").encode("utf-8")
        parts: List[tuple] = [("text/html", data)]
        if plain_data:
            parts += [(m, plain_data) for m in x11clip.TEXT_MIMES]
        if x11clip.publish_parts(parts):
            # Track the plain flavor: a rich clip is re-captured (and stored) as
            # its plain text, so that — not the html — is the digest the echo
            # arrives with. Passing both keeps us honest if that ever changes.
            _note_published(plain_data, data)
            return
        # No persistent owner: fall back to one flavor per channel. Plain text
        # reaches far more apps than html does, so when only one can be served,
        # serve the one that pastes.
        subprocess.run(["wl-copy", "--type", "text/html"], input=data,
                       timeout=_WRITE_TIMEOUT)
        self._x11_mirror(None, plain_data or data)

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
        published = x11clip.publish_parts(parts)
        if published:
            # Remember what we put there before touching the clipboard again:
            # the next change is our own publish coming back as a capture, and
            # it must not be mistaken for someone else's copy (which releases
            # the selection).
            _note_published(data)
        # Set the Wayland selection ourselves as well, and set it LAST.
        #
        # This used to be skipped, on the reasoning that the X11 owner is
        # re-exposed as the regular Wayland selection anyway. It is — but not
        # intact. Measured on cosmic-comp: an image published through the X11
        # owner reads back on X11 byte-exact, while the *same* clip read from
        # the Wayland side comes back four bytes longer, with its own length
        # glued on the front as a little-endian uint32 (a 281589-byte PNG
        # arrives as 281593 bytes starting f5 4b 04 00). That is a chunked
        # transfer's size header leaking into the payload, and it only shows up
        # on large clips — a 17 KB image crossed the same bridge unharmed.
        #
        # Those mangled bytes are not a PNG, so every native-Wayland consumer
        # pastes nothing. That now includes the app this mattered for:
        # Claude Desktop runs --ozone-platform=wayland. Writing the selection
        # ourselves means Wayland readers get our bytes from us instead of the
        # compositor's re-encoding of them, and going last means our source
        # replaces the proxy rather than the other way round.
        try:
            subprocess.run(["wl-copy", "--type", mime], input=data,
                           timeout=_WRITE_TIMEOUT)
        except (subprocess.SubprocessError, OSError) as exc:
            debuglog.log("copy_image.wl_copy_failed", error=exc)
        if not published:
            # No persistent owner (no X display, or GTK 4 unavailable): the
            # one-shot mirror is all XWayland apps will get, and it carries a
            # single flavor, so file-drop targets stay broken.
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
            # Short on purpose: this runs on the GTK main thread during a
            # recover, and against a selection whose owner has gone unresponsive
            # it is the whole freeze. It is only an optimisation — a wrong
            # answer costs one redundant mirror, a slow answer costs the UI.
            cur = subprocess.run(cmd, capture_output=True, timeout=2)
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
        parts = self._file_parts(path)
        # If the file IS an image, also offer its raw bytes. A screenshot tool
        # (macOS CleanShot, some Linux ones) copies a file *reference* to the
        # image, not image data, so it arrives here as a file — and pasting a
        # file:// URI into a chat/editor (Claude Desktop, Slack) drops a link,
        # not a picture. Offering image/png alongside the file lets image
        # targets take the picture while file managers still take the file.
        img = self._image_bytes_for(path)
        if img is not None:
            parts = [img] + parts
        if x11clip.publish_parts(parts):
            # Track the file's *content* hash: that is what capture stores for a
            # file clip (storage.add_file_from_path), so it's the digest our own
            # echo arrives with. Without it the echo looked like someone else's
            # copy and released the selection we had just taken.
            try:
                import hashlib
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                x11clip.note_published(h.hexdigest())
            except OSError:
                pass
            return
        import urllib.request
        uri = urllib.request.pathname2url(path)
        payload = f"copy\nfile://{uri}".encode("utf-8")
        subprocess.run(["wl-copy", "--type", "x-special/gnome-copied-files"],
                       input=payload, timeout=_WRITE_TIMEOUT)
        # Mirror a uri-list to X11 so XWayland apps that accept a pasted file
        # (editors, some chat apps) see it too.
        self._x11_mirror("text/uri-list", f"file://{uri}\r\n".encode("utf-8"))

    @staticmethod
    def _image_bytes_for(path: str):
        """(mime, bytes) if ``path`` is an image we can offer inline, else None.

        Uses the file's magic, not its extension, so a mislabelled name can't
        make us claim a type the bytes aren't. Capped so a huge file isn't read
        into memory for the inline flavor (the file reference still carries it)."""
        import os
        from ..capture import sniff_image_mime
        try:
            if os.path.getsize(path) > config.MAX_IMAGE_BYTES:
                return None
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        mime = sniff_image_mime(data)
        return (mime, data) if mime else None

    def start_watch(self, on_change: Callable[[], None]) -> None:
        # No-op: the daemon spawns `wl-paste --watch ... _store`, which is the
        # capture trigger on Linux.
        return None
