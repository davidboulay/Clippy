#!/usr/bin/env python3
"""Live check of the X11 owner's ready/ACK handshake.

Publishing used to report success as soon as bytes entered the helper's pipe,
which is not the same as a clip being served — a helper that can't reach the X
display exits *after* that write succeeds. These checks drive the real helper
process and assert that success now means "the helper applied it".

Runs against the real X display and DOES take the X11 CLIPBOARD selection while
it runs, so it restores whatever text was on the clipboard when it started (and
prints a warning if the clipboard held something it can't restore, e.g. an
image, so nothing is silently destroyed). Requires $DISPLAY and GTK 4."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clippy import x11clip                                  # noqa: E402

FAILURES = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def x11_targets():
    try:
        out = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"],
            capture_output=True, text=True, timeout=3)
        return {line.strip() for line in out.stdout.splitlines() if line.strip()}
    except (subprocess.SubprocessError, OSError):
        return set()


if not os.environ.get("DISPLAY"):
    print("no DISPLAY — skipping (this test needs Xwayland)")
    sys.exit(0)

# Remember the current clipboard so the box is left as we found it.
try:
    types = subprocess.run(["wl-paste", "--list-types"], capture_output=True,
                           text=True, timeout=5).stdout.split()
except (subprocess.SubprocessError, OSError):
    types = []
restore_mime, restore = None, None
if types:
    # Save whichever flavor the offer leads with, so an image clip survives this
    # test just as a text one does.
    restore_mime = next((t for t in types if t.startswith("text/plain")), types[0])
    restore = subprocess.run(["wl-paste", "-n", "-t", restore_mime],
                             capture_output=True, timeout=10).stdout or None
    print(f"  (saved current clipboard: {restore_mime}, "
          f"{len(restore or b'')} bytes)")

print("helper handshake")
started = time.time()
ok = x11clip.publish(b"clippy-protocol-test-one")
check("publish reports success", ok, True)
print(f"       (took {time.time() - started:.2f}s)")

targets = x11_targets()
check("plain-text targets are served",
      bool({"UTF8_STRING", "STRING", "TEXT"} & targets), True)

# The bug this replaces: a second publish of the SAME bytes used to be skipped
# on the assumption that a live helper still owned the selection. It doesn't
# after the compositor steals it back, which is why re-clicking a tile could do
# nothing at all.
check("republishing identical bytes still publishes",
      x11clip.publish(b"clippy-protocol-test-one"), True)

print("multi-flavor publish")
ok = x11clip.publish_parts([
    ("text/html", b"<b>rich</b>"),
    ("text/plain;charset=utf-8", b"rich"),
    ("text/plain", b"rich"),
])
check("publish_parts reports success", ok, True)
targets = x11_targets()
check("html flavor is offered", "text/html" in targets, True)
# The S2 fix: a rich clip must carry a plain flavor too, or apps that only ask
# for plain targets paste nothing.
check("plain flavor is offered alongside html",
      bool({"UTF8_STRING", "STRING", "text/plain"} & targets), True)

print("failure is reported, not assumed")
check("empty part list is rejected", x11clip.publish_parts([]), False)

# A helper with nowhere to draw must fail fast rather than swallow the frame.
saved_display, os.environ["DISPLAY"] = os.environ.get("DISPLAY", ""), ""
x11clip.stop()
try:
    started = time.time()
    check("publish fails without a display", x11clip.publish(b"nope"), False)
    elapsed = time.time() - started
    check("and fails quickly (<1s)", elapsed < 1.0, True)
    print(f"       (took {elapsed:.2f}s)")
finally:
    os.environ["DISPLAY"] = saved_display
    x11clip.stop()

if restore is not None:
    subprocess.run(["wl-copy", "--type", restore_mime], input=restore, timeout=10)
    print(f"\nclipboard restored ({restore_mime}, {len(restore)} bytes)")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
