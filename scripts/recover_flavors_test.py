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
        self.wl_copy_inputs = []
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


def install(monkeypatch_accept=True, owner_is_enough=True,
            image_file_flavors=False):
    """Point the backend at a Recorder and neutralise the shell fallbacks.

    ``owner_is_enough`` pins the desktop capability rather than letting the host
    decide it: whether publishing to the X11 owner is *also* the Wayland
    selection is true on cosmic-comp and false everywhere else, and the two
    answers give different, both-correct call patterns below.

    ``image_file_flavors`` is pinned for the same reason — it changes which
    single flavor the Wayland selection gets, so reading it off the machine
    running the tests would make them pass or fail on the tester's preferences.
    """
    import subprocess

    from clippy.backends import wayland

    wayland._X11_OWNER_IS_ENOUGH = owner_is_enough
    wayland.settings.get = (
        lambda key: image_file_flavors if key == "image_file_flavors"
        else REAL_SETTINGS_GET(key))
    rec = Recorder(monkeypatch_accept)
    wayland.x11clip.publish = rec.publish
    wayland.x11clip.publish_parts = rec.publish_parts
    wayland.x11clip.note_published = rec.note_published

    def fake_run(cmd, **kw):
        rec.wl_copy_calls.append(cmd)
        rec.wl_copy_inputs.append(kw.get("input"))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    wayland.subprocess.run = fake_run
    WaylandBackend._x11_mirror = staticmethod(
        lambda mime, data: rec.xclip_calls.append((mime, data)))
    return rec


from clippy import settings as _settings                 # noqa: E402

ORIGINALS = (x11clip.publish, x11clip.publish_parts, x11clip.note_published)
REAL_SETTINGS_GET = _settings.get
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
check("cosmic: no fallback wl-copy when the owner accepted", rec.wl_copy_calls, [])
check("the digest recorded is the plain text's",
      hashlib.sha256(PLAIN.encode()).hexdigest() in (rec.published or set()), True)

print("rich text recover off cosmic-comp (the Hyprland regression)")
# Elsewhere the owner reaches XWayland only, so the Wayland selection still has
# to be written or a clicked tile sets nothing at all.
rec = install(owner_is_enough=False)
WaylandBackend().copy_html(HTML, PLAIN)
check("still published to the owner for XWayland", "text/html" in [m for m, _ in rec.parts], True)
check("and wl-copy sets the Wayland selection", len(rec.wl_copy_calls), 1)
check("carrying the plain flavor, which pastes in the most places",
      rec.wl_copy_calls[0], ["wl-copy"])

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
check("cosmic: no wl-copy alongside the owner (one authority per selection)",
      rec.wl_copy_calls, [])

print("plain text recover off cosmic-comp (the Hyprland regression)")
rec = install(owner_is_enough=False)
WaylandBackend().copy_text("hello")
check("published to the owner for XWayland", rec.text, b"hello")
check("and wl-copy sets the Wayland selection", rec.wl_copy_calls, [["wl-copy"]])
check("no redundant xclip mirror when the owner accepted", rec.xclip_calls, [])

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

print("image-file recover also offers the image inline")
# A screenshot copied as a file reference (macOS CleanShot, synced over) must
# still paste as a picture into image targets, not as a file:// link.
rec = install()
png = b"\x89PNG\r\n\x1a\n" + b"screenshot" * 100
imgtmp = Path("/tmp/clippy-recover-flavors-shot.png")
imgtmp.write_bytes(png)
WaylandBackend().copy_file(str(imgtmp))
mimes = [m for m, _ in (rec.parts or [])]
check("image/png is offered alongside the file", "image/png" in mimes, True)
check("the image flavor carries the real bytes",
      dict(rec.parts)["image/png"], png)
check("file flavors are still offered too", "text/uri-list" in mimes, True)
check("cosmic: the owner is the whole offer, no wl-copy", rec.wl_copy_calls, [])

print("image-file recover off cosmic-comp (the Slack paste regression)")
# The owner reaches XWayland only here, and wl-copy carries ONE type, so the
# flavor chosen for the Wayland selection is the whole offer for native-Wayland
# apps. Sending the file reference is what made a Mac-synced CleanShot
# screenshot paste as nothing in Slack (--ozone-platform=wayland: it asks for
# image/png and found only x-special/gnome-copied-files).
rec = install(owner_is_enough=False)
WaylandBackend().copy_file(str(imgtmp))
check("still published to the owner for XWayland",
      "image/png" in [m for m, _ in (rec.parts or [])], True)
check("the Wayland selection is set", len(rec.wl_copy_calls), 1)
check("carrying image/png, which is what chat apps ask for",
      rec.wl_copy_calls[0], ["wl-copy", "--type", "image/png"])
check("with the real bytes, not a file:// URI", rec.wl_copy_inputs[0], png)
check("no redundant xclip mirror when the owner accepted", rec.xclip_calls, [])

print("image-file recover off cosmic-comp with image_file_flavors on")
# The user has asked for file-drop targets to win; the single Wayland flavor
# follows that, and image targets fall back to the owner as before.
rec = install(owner_is_enough=False, image_file_flavors=True)
WaylandBackend().copy_file(str(imgtmp))
check("the file reference takes the Wayland selection",
      rec.wl_copy_calls[0], ["wl-copy", "--type", "x-special/gnome-copied-files"])
check("image/png is still on the owner for XWayland",
      "image/png" in [m for m, _ in (rec.parts or [])], True)

print("image-file recover with no owner at all")
rec = install(monkeypatch_accept=False, owner_is_enough=False)
WaylandBackend().copy_file(str(imgtmp))
check("the xclip mirror carries the image, not the uri-list",
      rec.xclip_calls, [("image/png", png)])

print("non-image file recover off cosmic-comp is unchanged")
# Only an image file has a second flavor worth preferring; everything else
# still goes out as the file reference file managers read.
rec = install(owner_is_enough=False)
WaylandBackend().copy_file(str(tmp))
check("still the gnome-copied-files list",
      rec.wl_copy_calls[0], ["wl-copy", "--type", "x-special/gnome-copied-files"])
check("and its payload is the copy verb plus the URI",
      (rec.wl_copy_inputs[0] or b"").startswith(b"copy\nfile://"), True)

imgtmp.unlink(missing_ok=True)
tmp.unlink(missing_ok=True)

x11clip.publish, x11clip.publish_parts, x11clip.note_published = ORIGINALS
_settings.get = REAL_SETTINGS_GET

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
