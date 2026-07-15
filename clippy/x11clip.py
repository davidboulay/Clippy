"""Persistent X11 (XWayland) clipboard owner.

On COSMIC, X11/XWayland apps — e.g. Claude Desktop, which runs
``--ozone-platform=x11`` — read the clipboard only through Xwayland's
Wayland<->X11 bridge, and that bridge is unreliable: Xwayland runs with
``-terminate``, so it exits when the last X11 client disconnects and a fresh
instance may not re-sync the selection. Clippy's older fire-and-forget ``xclip``
mirror doesn't persistently *own* the X11 selection and doesn't survive that
cycle, so a recovered clip could sit unpasteable in X11 apps.

This module runs a small helper process forced onto GTK's **X11 backend**, so it
is itself an XWayland client that

  1. persistently OWNS and serves the ``CLIPBOARD`` selection for **text** (GTK's
     ``set_text`` provides the UTF8_STRING/STRING/TEXT/text-plain targets and,
     unlike ``set_image``, never re-encodes), and
  2. keeps Xwayland alive, so the bridge can't cycle out from under us.

Text is the common recover and the case that must be exact; images and files
stay on the backend's ``xclip`` mirror (raw bytes, no re-encoding), which this
owner's mere presence makes reliable by keeping Xwayland from cycling. (Serving
raw image/uri bytes from GTK would need ``Gtk.Clipboard.set_with_data``, which
isn't introspectable in PyGObject.)

The daemon feeds it the current clip over a tiny stdin frame protocol. It's
GTK-only — no new dependency — and entirely best-effort: if there's no X display
or the helper can't start, callers fall back to the old ``xclip`` mirror.

Split in two halves:
  * ``run()``  — the helper process (``python3 -m clippy _x11clip``); imports GTK.
  * ``publish()`` / ``start()`` / ``stop()`` — the daemon-side driver; GTK-free,
    safe to import from the clipboard backend.
"""
from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
import threading

# Frame protocol (daemon -> helper, over the helper's stdin):
#   1 byte kind ('T' text — the only kind today), 4 bytes big-endian payload
#   length, then <length> utf-8 payload bytes.


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
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import Gdk, GdkPixbuf, GLib, Gtk
    except Exception:
        return 1

    if Gdk.Display.get_default() is None:
        return 1   # no Xwayland — daemon falls back to the xclip mirror

    clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    def _apply(_kind: bytes, data: bytes) -> bool:
        # Text only: GTK's set_text serves the exact bytes (with all the usual
        # UTF8_STRING/STRING/TEXT/text-plain targets) and, unlike set_image,
        # doesn't re-encode. Serving raw image/uri bytes would need
        # Gtk.Clipboard.set_with_data, which isn't introspectable in PyGObject —
        # so images and files stay on the backend's xclip mirror, which the mere
        # presence of this persistent owner already makes reliable (it keeps
        # Xwayland from terminating and cycling).
        try:
            clip.set_text(data.decode("utf-8", "replace"), -1)
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
            data = _read_exactly(stream, length) if length else b""
            if data is None:
                break
            GLib.idle_add(_apply, header[0:1], data)
        GLib.idle_add(Gtk.main_quit)   # stdin closed -> daemon gone -> exit

    threading.Thread(target=_reader, daemon=True).start()
    Gtk.main()
    return 0


# --------------------------------------------------------------------------- #
# Daemon-side driver (GTK-free; safe to import from the clipboard backend)
# --------------------------------------------------------------------------- #
_proc = None                 # the running helper subprocess
_last = None                 # sha256 of the last text payload we published
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
    timer so the owner — and thus Xwayland — stays alive. True if running."""
    global _proc
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return True
        _proc = _spawn()
        return _proc is not None


def _write(kind_byte: bytes, data: bytes) -> bool:
    """Frame + send one payload to the helper; respawn once on a broken pipe."""
    global _proc
    frame = kind_byte + struct.pack(">I", len(data)) + data
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


def publish(data: bytes) -> bool:
    """Hand the current *text* clip (utf-8 bytes) to the persistent X11 owner.
    Returns True if the owner accepted it; False means it isn't available and
    the caller should fall back to the ``xclip`` mirror.

    De-dupes on content: re-publishing the same payload is skipped, which breaks
    the Wayland<->X11 echo the owner would otherwise cause (its X11 grab bounces
    back through the compositor into ``wl-paste --watch`` and re-enters capture)."""
    global _last
    with _lock:
        digest = hashlib.sha256(data).digest()
        if digest == _last and _proc is not None and _proc.poll() is None:
            return True   # already the live selection — nothing to do
        if _write(b"T", data):
            _last = digest
            return True
        return False


def stop() -> None:
    """Terminate the helper (called on daemon shutdown)."""
    global _proc, _last
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
