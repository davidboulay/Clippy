"""Persistent X11 (XWayland) clipboard owner.

On COSMIC, X11/XWayland apps — e.g. Claude Desktop, which runs
``--ozone-platform=x11`` — read the clipboard only through Xwayland's
Wayland<->X11 bridge, and that bridge is unreliable: Xwayland runs with
``-terminate``, so it exits when the last X11 client disconnects and a fresh
instance may not re-sync the selection. Clippy's older fire-and-forget ``xclip``
mirror doesn't persistently *own* the X11 selection and doesn't survive that
cycle, so a recovered clip could sit unpasteable in X11 apps.

This module runs a small helper process forced onto GDK's **X11 backend**, so it
is itself an XWayland client that

  1. persistently OWNS and serves the ``CLIPBOARD`` selection for every flavor of
     the current clip — text, raw image bytes and file URIs — and
  2. keeps Xwayland alive, so the bridge can't cycle out from under us.

Owning the X11 selection is also how a recovered clip reaches **native-Wayland**
apps: cosmic-comp mirrors the regular ``wl_data_device`` selection into
wlr-data-control but not back out, so a ``wl-copy`` (data-control) clip is
invisible to GUI apps. Xwayland re-exposes whatever owns the X11 selection *as*
the regular selection, which crosses that gap in the one direction that works.

The helper uses **GTK 4**, whose ``Gdk.ContentProvider`` is fully introspectable:
``new_for_bytes`` serves exact bytes for an arbitrary MIME type (no re-encoding,
unlike GTK 3's ``set_image``) and ``new_union`` offers **several MIME types at
once**. That last part matters — a clip usually has to be offered more than one
way simultaneously. An image, for instance, needs raw ``image/png`` bytes for
editors and chat apps *and* a ``text/uri-list`` / ``x-special/gnome-copied-files``
file reference for file-drop targets like the VS Code Explorer or Nautilus.
``wl-copy`` and ``xclip`` can each serve only one type per invocation, so neither
can express that; GTK 3's ``Gtk.Clipboard.set_with_data`` would, but isn't
introspectable in PyGObject.

The daemon feeds it the current clip over a tiny stdin frame protocol. It's
GTK-only — no new dependency — and entirely best-effort: if there's no X display
or the helper can't start, callers fall back to the old ``xclip`` mirror.

Split in two halves:
  * ``run()``  — the helper process (``python3 -m clippy _x11clip``); imports GTK.
  * ``publish*()`` / ``start()`` / ``stop()`` — the daemon-side driver; GTK-free,
    safe to import from the clipboard backend.
"""
from __future__ import annotations

import os
import select
import struct
import subprocess
import sys
import threading
from typing import List, Optional, Sequence, Set, Tuple

from . import debuglog

# Frame protocol (daemon -> helper, over the helper's stdin):
#   1 byte  kind: b'T' legacy text, b'P' multi-part
#   4 bytes big-endian payload length, then <length> payload bytes.
#
#   kind b'T' payload: utf-8 text.
#   kind b'P' payload: 1 byte part count, then per part
#                        2 bytes big-endian MIME length, MIME (utf-8),
#                        4 bytes big-endian data length, data.
#
# A "part" is one MIME flavor of the *same* clip; all parts are offered together
# on one selection, so each consumer picks whichever flavor it understands.
#
# Reply protocol (helper -> daemon, over the helper's stdout), one byte each:
#   b'R'  ready — GTK initialized, an X display is open, stdin is being read
#   b'A'  the last frame was accepted and is now the live selection
#   b'N'  the last frame was refused (set_content failed)
#
# Without these the daemon could only observe that bytes entered a pipe, which
# is not the same as a clip being served: a helper that loses its X display
# exits *after* the write succeeded, so publish() reported success for a clip
# that reached no channel at all — and the caller skipped its fallbacks. The
# ready byte additionally guarantees someone is draining stdin before we push a
# large frame into it, so a doomed helper can't block the caller on a full pipe.
READY, ACK, NACK = b"R", b"A", b"N"
_READY_TIMEOUT = 2.0    # seconds to wait for a fresh helper to come up
_ACK_TIMEOUT = 2.0      # seconds to wait for a frame to be applied

TEXT_MIMES = ("text/plain;charset=utf-8", "text/plain")

Part = Tuple[str, bytes]


def _encode_parts(parts: Sequence[Part]) -> bytes:
    """Serialize parts into a b'P' frame payload (see the protocol above)."""
    out = [struct.pack(">B", len(parts))]
    for mime, data in parts:
        raw = mime.encode("utf-8")
        out.append(struct.pack(">H", len(raw)) + raw)
        out.append(struct.pack(">I", len(data)) + data)
    return b"".join(out)


def _decode_parts(payload: bytes) -> List[Part]:
    """Parse a b'P' frame payload back into parts. Returns [] if malformed."""
    parts: List[Part] = []
    try:
        (count,) = struct.unpack(">B", payload[:1])
        off = 1
        for _ in range(count):
            (mlen,) = struct.unpack(">H", payload[off:off + 2])
            off += 2
            mime = payload[off:off + mlen].decode("utf-8")
            off += mlen
            (dlen,) = struct.unpack(">I", payload[off:off + 4])
            off += 4
            data = payload[off:off + dlen]
            off += dlen
            if len(data) != dlen:
                return []
            parts.append((mime, data))
    except (struct.error, UnicodeDecodeError, IndexError):
        return []
    return parts


# --------------------------------------------------------------------------- #
# Helper process (imports GTK; runs as `python3 -m clippy _x11clip`)
# --------------------------------------------------------------------------- #
def _read_exactly(stream, n: int):
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def run() -> int:
    """Own the X11 CLIPBOARD and keep it in sync with framed messages on stdin.

    Exits when stdin closes (the daemon is gone) or GTK can't reach an X
    display. Returns non-zero only when it never managed to start."""
    os.environ["GDK_BACKEND"] = "x11"   # be an XWayland client, not a Wayland one

    # Claim fd 1 as a private reply channel before GTK can write anything to it,
    # then point the real stdout at /dev/null. A stray library message on stdout
    # would otherwise be read by the daemon as a protocol byte.
    try:
        reply_fd = os.dup(1)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    except OSError:
        return 1

    def reply(byte: bytes) -> None:
        try:
            os.write(reply_fd, byte)
        except OSError:
            pass

    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, GLib, Gtk
    except Exception:
        return 1

    try:
        if not Gtk.init_check():
            return 1   # no Xwayland — daemon falls back to the xclip mirror
    except Exception:
        return 1
    display = Gdk.Display.get_default()
    if display is None:
        return 1

    clipboard = display.get_clipboard()
    loop = GLib.MainLoop()

    def _apply(parts: List[Part]) -> bool:
        # Offer every flavor at once via a union provider, so one selection can
        # satisfy byte-consumers (editors, chat apps) and file-consumers (file
        # managers, the VS Code Explorer) without us guessing which the next
        # paste target will be. new_for_bytes serves the exact bytes — no
        # re-encoding, so a recovered clip stays byte-identical to the original
        # (which also keeps its content hash stable for capture de-dupe).
        ok = False
        try:
            providers = [
                Gdk.ContentProvider.new_for_bytes(mime, GLib.Bytes.new(data))
                for mime, data in parts
            ]
            provider = (providers[0] if len(providers) == 1
                        else Gdk.ContentProvider.new_union(providers))
            # set_content reports failure by returning False rather than
            # raising, so the return value is the only signal there is.
            ok = bool(clipboard.set_content(provider))
        except Exception:
            ok = False
        reply(ACK if ok else NACK)
        return False   # one-shot on the GLib main loop

    def _reader() -> None:
        stream = sys.stdin.buffer
        while True:
            header = _read_exactly(stream, 5)
            if header is None:
                break
            (length,) = struct.unpack(">I", header[1:5])
            payload = _read_exactly(stream, length) if length else b""
            if payload is None:
                break
            if header[0:1] == b"T":
                parts = [(m, payload) for m in TEXT_MIMES]
            else:
                parts = _decode_parts(payload)
            if parts:
                GLib.idle_add(_apply, parts)
            else:
                reply(NACK)     # malformed frame — answer, don't strand the caller
        GLib.idle_add(loop.quit)   # stdin closed -> daemon gone -> exit

    # Announce readiness only once stdin is actually being drained, so the
    # daemon's next write cannot block against a helper that will never read.
    threading.Thread(target=_reader, daemon=True).start()
    reply(READY)
    loop.run()
    return 0


# --------------------------------------------------------------------------- #
# Daemon-side driver (GTK-free; safe to import from the clipboard backend)
# --------------------------------------------------------------------------- #
_proc = None                 # the running helper subprocess
_ready = False               # has *this* helper sent its READY byte?
_last_frame = None           # the last frame we published, replayed after a respawn
_published: Optional[Set[str]] = None   # sha256 hex of the content we're serving
_lock = threading.RLock()


def _spawn():
    """Launch the helper as an XWayland client, or None when X isn't available."""
    global _ready
    _ready = False
    if not os.environ.get("DISPLAY"):
        return None
    env = dict(os.environ)
    env["GDK_BACKEND"] = "x11"
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "clippy", "_x11clip"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, start_new_session=True,
        )
    except OSError:
        return None
    debuglog.log("x11.spawn", pid=proc.pid)
    return proc


def _reap(proc) -> None:
    """Close down a helper we're done with, and actually collect it — a
    terminate() without a wait() leaves a zombie for every recycle."""
    if proc is None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
            proc.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _read_reply(proc, timeout: float) -> Optional[bytes]:
    """One protocol byte from the helper, or None on timeout/EOF/error.

    Reads the raw fd rather than ``proc.stdout.read`` on purpose: a buffered
    reader may pull the *next* reply into its own buffer, after which ``select``
    on the fd reports "nothing to read" for a byte we already have."""
    if proc is None or proc.stdout is None:
        return None
    try:
        fd = proc.stdout.fileno()
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            return None
        return os.read(fd, 1) or None
    except (OSError, ValueError):
        return None


def start() -> bool:
    """Ensure the persistent owner is running (idempotent). Cheap to call on a
    timer so the owner — and thus Xwayland — stays alive. True if running.

    Re-sends the current clip after a respawn: a helper that died took the X11
    selection with it, leaving XWayland advertising a stale offer that reads back
    as zero bytes — an "empty" paste. Replaying the last frame re-establishes a
    live owner for the clip that is still current."""
    global _proc
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return True
        if _proc is not None:
            debuglog.log("x11.helper_died", code=_proc.returncode)
        _proc = _spawn()
        if _proc is None:
            return False
        if _last_frame is not None:
            _send_frame(_last_frame)
        return True


def _send_frame(frame: bytes) -> bool:
    """Write one framed payload to the helper and wait for it to be applied.

    Success means the helper answered ACK — i.e. the bytes are the live X11
    selection — not merely that a write returned. Respawns once when the helper
    turns out to be dead or never became ready."""
    global _proc, _ready
    for _ in (1, 2):   # one respawn-and-retry
        if _proc is None or _proc.poll() is not None:
            _proc = _spawn()
        if _proc is None or _proc.stdin is None:
            return False
        if not _ready:
            if _read_reply(_proc, _READY_TIMEOUT) != READY:
                debuglog.log("x11.not_ready")
                _reap(_proc)
                _proc = None
                continue           # try once with a fresh helper
            _ready = True
        try:
            _proc.stdin.write(frame)
            _proc.stdin.flush()
        except (BrokenPipeError, OSError):
            _reap(_proc)
            _proc = None           # force a respawn on the next attempt
            continue
        reply = _read_reply(_proc, _ACK_TIMEOUT)
        if reply == ACK:
            return True
        debuglog.log("x11.publish_failed", reply=reply)
        # NACK, timeout or EOF: this helper is not serving what we asked for.
        # Recycle it so the next attempt starts clean, and tell the caller so it
        # can fall back to wl-copy/xclip instead of trusting a phantom selection.
        _reap(_proc)
        _proc = None
        if reply == NACK:
            return False           # a live helper refused; retrying won't help
    return False


def _publish_frame(kind: bytes, payload: bytes) -> bool:
    """Hand one frame to the persistent owner and remember it for replay.

    There is deliberately no "same as last time, skip it" short-circuit. A live
    helper process is not proof that the helper still *owns* the selection —
    cosmic-comp's XWM steals it back on the next focus into an X11 window — so
    skipping a repeat publish turned the user's second click on the same tile
    into a silent no-op, exactly when they were clicking again because the first
    paste came up empty. Re-sending is one pipe write, and the resulting echo is
    recognised by content digest in ``release_unless_ours``, so it costs a
    round trip rather than a loop."""
    global _last_frame
    frame = kind + struct.pack(">I", len(payload)) + payload
    if _send_frame(frame):
        _last_frame = frame
        return True
    return False


def publish(data: bytes) -> bool:
    """Hand the current *text* clip (utf-8 bytes) to the persistent X11 owner.
    Returns True if the owner accepted it; False means it isn't available and
    the caller should fall back to the ``xclip`` mirror."""
    with _lock:
        return _publish_frame(b"T", data)


def publish_parts(parts: Sequence[Part]) -> bool:
    """Hand a multi-flavor clip to the persistent X11 owner — every ``(mime,
    bytes)`` part is offered on the same selection. Returns False when the owner
    isn't available, so the caller can fall back to the ``xclip`` mirror (which
    carries only one flavor)."""
    parts = [(m, d) for m, d in parts if m]
    if not parts:
        return False
    with _lock:
        return _publish_frame(b"P", _encode_parts(parts))


def note_published(*digests: Optional[str]) -> None:
    """Record the sha256 hex of the content we just put on the clipboard, so a
    capture of our own clip can be told apart from someone else's copy (see
    ``release_unless_ours``).

    Takes *several* digests because a clip can come back from the round trip
    hashed differently than it went out: a rich recover is published as html
    plus plain text but re-captured (and stored) as the plain flavor alone. Any
    one of them matching means the capture is our own echo."""
    global _published
    values = {d for d in digests if d}
    if not values:
        return
    with _lock:
        _published = values


def published_digest() -> Optional[str]:
    """One sha256 hex of the content we currently have on the clipboard, or
    None. Prefer ``is_published`` — this exists for callers that only want to
    know whether anything is held."""
    with _lock:
        return next(iter(_published)) if _published else None


def is_published(digest: Optional[str]) -> bool:
    """Whether ``digest`` is (one of) the content we are currently serving."""
    with _lock:
        return bool(digest and _published and digest in _published)


def release_unless_ours(digest: Optional[str]) -> bool:
    """Drop the X11 selection unless ``digest`` is the clip we published.

    Called on every clipboard change. Owning the X11 selection is only correct
    while the clip is ours: an X11 owner shadows the selection for XWayland apps,
    so keeping it after another app copies pins those apps to a stale clip (and
    Chromium can hang negotiating against it). Returns True if we released.

    Content-keyed rather than time-keyed because publishing a clip *is* a
    clipboard change: our own recover comes straight back as a capture, and
    releasing then would immediately undo the recover."""
    global _proc, _published, _last_frame
    with _lock:
        if is_published(digest):
            return False            # our own echo — keep serving it
        if _published is None and _last_frame is None:
            return False            # nothing on the clipboard to give up
        _published = None
        # Forget the frame too, whether or not we were tracking its content:
        # start()'s replay-after-respawn must not resurrect a clip that someone
        # else has since replaced.
        _last_frame = None
        debuglog.log("x11.release", digest=(digest or "")[:12])
        # Recycle the helper rather than ask it to clear: GDK has no "disown"
        # call. Gdk.Clipboard.set_content(None) leaves us owning the selection
        # with empty content, which shadows XWayland apps just as badly. An X11
        # selection is released when the owning client disconnects — which is how
        # the old fire-and-forget xclip managed it, by dying. Spawn a fresh helper
        # straight away: it owns nothing until we publish, and its presence keeps
        # Xwayland (which runs with -terminate) from cycling.
        old, _proc = _proc, None
        _reap(old)
        _proc = _spawn()
        return True


def stop() -> None:
    """Terminate the helper (called on daemon shutdown)."""
    global _proc, _last_frame, _published
    with _lock:
        _reap(_proc)
        _proc = None
        _last_frame = None
        _published = None
