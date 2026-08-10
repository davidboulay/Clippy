#!/usr/bin/env python3
"""Does a recovered image survive the round trip to each channel, byte-exact?

The failure this guards against is invisible from inside Clippy: the helper
serves the right bytes on X11, but cosmic-comp's X11->Wayland proxy hands large
clips to native-Wayland consumers with the payload length prepended as a
little-endian uint32 — four extra bytes that stop it being a PNG. Apps just
paste an empty image. Claude Desktop runs --ozone-platform=wayland, so that is
the channel that matters for it.

Publishes a synthetic image through the real recover path and reads it back on
both channels, at a size below and above where the corruption appears. Takes the
clipboard while it runs and restores what was there. Needs $DISPLAY and GTK 4.

Run:  PYTHONPATH=. python3 scripts/image_roundtrip_test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clippy import clipboard, x11clip                      # noqa: E402

FAILURES = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def make_png(pixels_wide: int) -> bytes:
    """A real, decodable greyscale PNG of a given width (1 row)."""
    def chunk(tag, payload):
        body = tag + payload
        return (len(payload).to_bytes(4, "big") + body
                + zlib.crc32(body).to_bytes(4, "big"))

    ihdr = (pixels_wide.to_bytes(4, "big") + (1).to_bytes(4, "big")
            + bytes([8, 0, 0, 0, 0]))          # 8-bit greyscale, 1px tall
    raw = b"\x00" + bytes(range(256)) * (pixels_wide // 256 + 1)
    raw = raw[:pixels_wide + 1]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 0))   # level 0: stays big
            + chunk(b"IEND", b""))


def read_wayland(mime="image/png") -> bytes:
    try:
        return subprocess.run(["wl-paste", "-t", mime], capture_output=True,
                              timeout=20).stdout
    except (subprocess.SubprocessError, OSError):
        return b""


def read_x11(mime="image/png") -> bytes:
    try:
        out = subprocess.run(["xclip", "-selection", "clipboard", "-o", "-t", mime],
                             capture_output=True, timeout=20)
        return out.stdout if out.returncode == 0 else b""
    except (subprocess.SubprocessError, OSError):
        return b""


if not os.environ.get("DISPLAY"):
    print("no DISPLAY — skipping (this test needs Xwayland)")
    sys.exit(0)

# Save whatever is on the clipboard so the desktop is left as we found it.
try:
    types = subprocess.run(["wl-paste", "--list-types"], capture_output=True,
                           text=True, timeout=5).stdout.split()
except (subprocess.SubprocessError, OSError):
    types = []
save_mime = next((t for t in types if t.startswith("text/plain")),
                 types[0] if types else None)
saved = read_wayland(save_mime) if save_mime else None
if saved:
    print(f"  (saved current clipboard: {save_mime}, {len(saved)} bytes)")

for label, width in (("small (16 KB)", 16 * 1024), ("large (512 KB)", 512 * 1024)):
    png = make_png(width)
    print(f"\n{label} image — {len(png)} bytes")
    clipboard.copy_image(png, "image/png")
    import time
    time.sleep(1.0)          # let the compositor settle both channels

    wayland = read_wayland()
    x11 = read_x11()
    check("Wayland readers get the exact bytes", len(wayland), len(png))
    check("  and they still start with PNG magic",
          wayland[:8], b"\x89PNG\r\n\x1a\n")
    if wayland and wayland[:8] != b"\x89PNG\r\n\x1a\n":
        print(f"       corrupted head: {wayland[:8].hex(' ')} "
              f"(+{len(wayland) - len(png)} bytes)")
    check("X11 readers get the exact bytes", len(x11), len(png))
    check("  and they still start with PNG magic", x11[:8], b"\x89PNG\r\n\x1a\n")

x11clip.stop()
if saved and save_mime:
    subprocess.run(["wl-copy", "--type", save_mime], input=saved, timeout=10)
    print(f"\nclipboard restored ({save_mime}, {len(saved)} bytes)")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
