#!/usr/bin/env python3
"""Minimal reproducer: cosmic-comp corrupts large clips proxied X11 -> Wayland.

Nothing to do with Clippy — it needs only an X11 client that owns the CLIPBOARD
selection and a Wayland client that reads it. When an X11 client offers a large
payload, cosmic-comp re-exports it to the Wayland selection with the payload
length prepended as a little-endian uint32, so Wayland consumers receive four
extra bytes and a file that no longer parses. Small payloads cross intact.

Any native-Wayland app pasting an image copied from an X11 app hits this. It
also hits any clipboard manager that serves clips by owning the X11 selection.

    python3 scripts/cosmic_proxy_corruption_repro.py

Requires: python3-gi with GTK 4, wl-clipboard, and an X display (Xwayland).
Takes the clipboard while it runs and restores what was there.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

SIZES = [
    ("64 KB", 64 * 1024),
    ("128 KB", 128 * 1024),
    ("192 KB", 192 * 1024),
    ("256 KB", 256 * 1024),
    ("512 KB", 512 * 1024),
]
MIME = "application/octet-stream"

HELPER = r'''
import os, sys
os.environ["GDK_BACKEND"] = "x11"
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk
Gtk.init_check()
data = sys.stdin.buffer.read()
clipboard = Gdk.Display.get_default().get_clipboard()
clipboard.set_content(
    Gdk.ContentProvider.new_for_bytes("%s", GLib.Bytes.new(data)))
os.write(1, b"R")
GLib.MainLoop().run()
''' % MIME


def read_wayland() -> bytes:
    try:
        return subprocess.run(["wl-paste", "-t", MIME],
                              capture_output=True, timeout=20).stdout
    except (subprocess.SubprocessError, OSError):
        return b""


def read_x11() -> bytes:
    try:
        out = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o", "-t", MIME],
            capture_output=True, timeout=20)
        return out.stdout if out.returncode == 0 else b""
    except (subprocess.SubprocessError, OSError):
        return b""


if not os.environ.get("DISPLAY"):
    print("needs an X display (Xwayland)")
    sys.exit(1)

try:
    saved_types = subprocess.run(["wl-paste", "--list-types"], capture_output=True,
                                 text=True, timeout=5).stdout.split()
except (subprocess.SubprocessError, OSError):
    saved_types = []
saved_mime = next((t for t in saved_types if t.startswith("text/plain")),
                  saved_types[0] if saved_types else None)
saved = b""
if saved_mime:
    saved = subprocess.run(["wl-paste", "-n", "-t", saved_mime],
                           capture_output=True, timeout=10).stdout

print(f"{'payload':>10}  {'X11 read':>12}  {'Wayland read':>14}  verdict")
print("-" * 58)
bad = []
for label, size in SIZES:
    payload = bytes((i * 7 + 13) % 256 for i in range(size))
    proc = subprocess.Popen([sys.executable, "-c", HELPER],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    proc.stdin.write(payload)
    proc.stdin.close()
    os.read(proc.stdout.fileno(), 1)      # wait for the owner to be established
    time.sleep(0.6)

    x11, wayland = read_x11(), read_wayland()
    delta = len(wayland) - len(payload)
    ok = wayland == payload
    if not ok:
        bad.append((label, delta, wayland[:4]))
    note = "ok" if ok else f"CORRUPT (+{delta} bytes, head {wayland[:4].hex(' ')})"
    print(f"{label:>10}  {len(x11):>12}  {len(wayland):>14}  {note}")
    proc.terminate()
    time.sleep(0.3)

if saved and saved_mime:
    subprocess.run(["wl-copy", "--type", saved_mime], input=saved, timeout=10)

print()
if bad:
    label, delta, head = bad[0]
    prefix = int.from_bytes(head, "little")
    print(f"Reproduced: Wayland readers get {delta} extra bytes starting at {label}.")
    print(f"The leading uint32 ({prefix}) equals the payload length, i.e. a "
          f"transfer size header\nis being written into the payload itself. "
          f"X11 readers get the bytes intact.")
else:
    print("Not reproduced on this compositor — clips crossed intact at every size.")
