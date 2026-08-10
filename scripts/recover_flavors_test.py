#!/usr/bin/env python3
"""What each recover path actually offers, and what it tells the owner it owns.

Two bugs live here and neither is visible from the outside until a paste fails.
A rich clip published as ``text/html`` alone pastes nothing in apps that ask
only for plain targets — the "it only works if I pick Copy as plain text"
symptom. And a path that publishes without recording the content digest has its
own echo mistaken for someone else's copy, so the selection it just took is
released a moment later.

Stubs the X11 owner instead of taking the real selection, so this is safe to run
on a working desktop."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib                                             # noqa: E402

from clippy import x11clip                                 # noqa: E402
from clippy.backends.wayland import WaylandBackend         # noqa: E402

FAILURES = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


class Recorder:
    """Stands in for the persistent owner and remembers what it was handed."""

    def __init__(self, accept=True):
        self.accept = accept
        self.parts = None
        self.text = None
        self.published = None
        self.wl_copy_calls = []
        self.xclip_calls = []

    # -- x11clip surface
    def publish(self, data):
        self.text = data
        return self.accept

    def publish_parts(self, parts):
        self.parts = list(parts)
        return self.accept

    def note_published(self, *digests):
        self.published = {d for d in digests if d}


def install(monkeypatch_accept=True):
    """Point the backend at a Recorder and neutralise the shell fallbacks."""
    import subprocess

    from clippy.backends import wayland

    rec = Recorder(monkeypatch_accept)
    wayland.x11clip.publish = rec.publish
    wayland.x11clip.publish_parts = rec.publish_parts
    wayland.x11clip.note_published = rec.note_published

    def fake_run(cmd, **kw):
        rec.wl_copy_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    wayland.subprocess.run = fake_run
    WaylandBackend._x11_mirror = staticmethod(
        lambda mime, data: rec.xclip_calls.append((mime, data)))
    return rec


ORIGINALS = (x11clip.publish, x11clip.publish_parts, x11clip.note_published)
HTML = "<meta charset='utf-8'><table><tr><td>a</td><td>b</td></tr></table>"
PLAIN = "a\tb"

print("rich text recover (the 'copy as plain text' bug)")
rec = install()
WaylandBackend().copy_html(HTML, PLAIN)
mimes = [m for m, _ in (rec.parts or [])]
check("html is offered", "text/html" in mimes, True)
check("utf-8 plain text is offered too", "text/plain;charset=utf-8" in mimes, True)
check("bare text/plain is offered too", "text/plain" in mimes, True)
check("html comes first (richest flavor leads)", mimes[0], "text/html")
plain_parts = [d for m, d in rec.parts if m.startswith("text/plain")]
check("the plain flavor is the plain text, not the markup",
      plain_parts[0], PLAIN.encode())
check("no fallback wl-copy when the owner accepted", rec.wl_copy_calls, [])
check("the digest recorded is the plain text's",
      hashlib.sha256(PLAIN.encode()).hexdigest() in (rec.published or set()), True)

print("rich text with no plain flavor stored")
rec = install()
WaylandBackend().copy_html(HTML, None)
plain_parts = [d for m, d in rec.parts if m.startswith("text/plain")]
check("plain text is derived from the markup", plain_parts[0], b"a\tb")

print("rich text recover when the owner is unavailable")
rec = install(monkeypatch_accept=False)
WaylandBackend().copy_html(HTML, PLAIN)
check("falls back to wl-copy", len(rec.wl_copy_calls), 1)
check("and mirrors PLAIN text to X11, which pastes in more apps than html does",
      rec.xclip_calls[0][1], PLAIN.encode())

print("plain text recover")
rec = install()
WaylandBackend().copy_text("hello")
check("published as text", rec.text, b"hello")
check("digest recorded", hashlib.sha256(b"hello").hexdigest() in (rec.published or set()), True)
check("no wl-copy alongside the owner (one authority per selection)",
      rec.wl_copy_calls, [])

print("plain text recover when the owner is unavailable")
rec = install(monkeypatch_accept=False)
WaylandBackend().copy_text("hello")
check("falls back to wl-copy", len(rec.wl_copy_calls), 1)
check("and to the xclip mirror", len(rec.xclip_calls), 1)

print("image recover reaches native-Wayland apps too")
# cosmic-comp's X11->Wayland proxy corrupts large clips (it prepends the
# payload length as a uint32), so an image published only through the X11 owner
# arrives at native-Wayland consumers — Claude Desktop among them — as bytes
# that are not a PNG. The Wayland selection has to be written by us.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048
rec = install()
WaylandBackend().copy_image(PNG, "image/png")
check("published to the X11 owner", [m for m, _ in (rec.parts or [])], ["image/png"])
check("and the Wayland selection is set directly", len(rec.wl_copy_calls), 1)
check("with the right type", "image/png" in rec.wl_copy_calls[0], True)
check("digest recorded before the clipboard changes again",
      hashlib.sha256(PNG).hexdigest() in (rec.published or set()), True)
check("no redundant xclip mirror when the owner accepted", rec.xclip_calls, [])

rec = install(monkeypatch_accept=False)
WaylandBackend().copy_image(PNG, "image/png")
check("owner unavailable: still sets Wayland", len(rec.wl_copy_calls), 1)
check("owner unavailable: falls back to the xclip mirror", len(rec.xclip_calls), 1)

print("file recover")
rec = install()
tmp = Path("/tmp/clippy-recover-flavors-test.txt")
tmp.write_bytes(b"file contents")
WaylandBackend().copy_file(str(tmp))
mimes = [m for m, _ in (rec.parts or [])]
check("uri-list is offered", "text/uri-list" in mimes, True)
check("gnome-copied-files is offered", "x-special/gnome-copied-files" in mimes, True)
check("the file's CONTENT digest is recorded (what capture stores)",
      hashlib.sha256(b"file contents").hexdigest() in (rec.published or set()), True)
tmp.unlink(missing_ok=True)

x11clip.publish, x11clip.publish_parts, x11clip.note_published = ORIGINALS

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
