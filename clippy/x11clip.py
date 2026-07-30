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

import hashlib
import os
import struct
import subprocess
import sys
import threading
from typing import List, Sequence, Tuple

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
        try:
            providers = [
                Gdk.ContentProvider.new_for_bytes(mime, GLib.Bytes.new(data))
                for mime, data in parts
            ]
            provider = (providers[0] if len(providers) == 1
                        else Gdk.ContentProvider.new_union(providers))
            clipboard.set_content(provider)
        except Exception:
            pass
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
        GLib.idle_add(loop.quit)   # stdin closed -> daemon gone -> exit

    threading.Thread(target=_reader, daemon=True).start()
    loop.run()
    return 0


# --------------------------------------------------------------------------- #
# Daemon-side driver (GTK-free; safe to import from the clipboard backend)
# --------------------------------------------------------------------------- #
_proc = None                 # the running helper subprocess
_last = None                 # sha256 of the last frame we published
_last_frame = None           # that frame, replayed after a respawn
_lock = threading.Lock()


def _spawn():
    """Launch the helper as an XWayland client, or None when X isn't available."""
    if not os.environ.get("DISPLAY"):
        return None
    env = dict(os.environ)
    env["GDK_BACKEND"] = "x11"
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "clippy", "_x11clip"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, start_new_session=True,
        )
    except OSError:
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
        _proc = _spawn()
        if _proc is None:
            return False
        if _last_frame is not None:
            _send_frame(_last_frame)
        return True


def _send_frame(frame: bytes) -> bool:
    """Write one framed payload to the helper; respawn once on a broken pipe."""
    global _proc
    for _ in (1, 2):   # one respawn-and-retry on a broken pipe
        if _proc is None or _proc.poll() is not None:
            _proc = _spawn()
        if _proc is None or _proc.stdin is None:
            return False
        try:
            _proc.stdin.write(frame)
            _proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            _proc = None   # force a respawn on the next attempt
    return False


def _publish_frame(kind: bytes, payload: bytes) -> bool:
    """De-dupe on content, then hand the frame to the persistent owner.

    De-duping breaks the Wayland<->X11 echo the owner would otherwise cause (its
    X11 grab bounces back through the compositor into ``wl-paste --watch`` and
    re-enters capture)."""
    global _last, _last_frame
    frame = kind + struct.pack(">I", len(payload)) + payload
    digest = hashlib.sha256(frame).digest()
    if digest == _last and _proc is not None and _proc.poll() is None:
        return True   # already the live selection — nothing to do
    if _send_frame(frame):
        _last, _last_frame = digest, frame
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


def stop() -> None:
    """Terminate the helper (called on daemon shutdown)."""
    global _proc, _last, _last_frame
    with _lock:
        if _proc is not None:
            try:
                if _proc.stdin is not None:
                    _proc.stdin.close()
                _proc.terminate()
            except OSError:
                pass
        _proc = None
        _last = None
        _last_frame = None
