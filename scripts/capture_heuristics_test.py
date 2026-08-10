#!/usr/bin/env python3
"""Offline checks for the capture-side classification logic.

Covers the two decisions that were getting clips wrong: telling a bitmap
*render* of a spreadsheet selection apart from a genuinely copied image, and
recognising image formats by their magic rather than by what the offer claims.
Pure functions only — no clipboard, no daemon, safe to run anywhere."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clippy import capture, richtext                       # noqa: E402
from clippy.backends import wayland                        # noqa: E402

FAILURES = []


def check(name, got, want):
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")


# --- image magic -----------------------------------------------------------
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 40
AVIF = b"\x00\x00\x00\x20" + b"ftyp" + b"avif" + b"\x00" * 40
TIFF = b"II*\x00" + b"\x00" * 40

print("image magic sniffing")
check("png", capture.sniff_image_mime(PNG), "image/png")
check("jpeg", capture.sniff_image_mime(JPEG), "image/jpeg")
check("gif", capture.sniff_image_mime(GIF), "image/gif")
check("webp", capture.sniff_image_mime(WEBP), "image/webp")
check("avif", capture.sniff_image_mime(AVIF), "image/avif")
check("tiff", capture.sniff_image_mime(TIFF), "image/tiff")
check("garbage", capture.sniff_image_mime(b"not an image at all"), None)
check("short buffer doesn't crash", capture.sniff_image_mime(b"RI"), None)

print("image claim validation")
check("png claimed png", capture._looks_like_image(PNG, "image/png"), True)
check("jpeg claimed png", capture._looks_like_image(JPEG, "image/png"), False)
check("jpeg claimed image/jpg", capture._looks_like_image(JPEG, "image/jpg"), True)
check("png with charset param", capture._looks_like_image(PNG, "image/png;foo=1"), True)
check("unknown type is trusted", capture._looks_like_image(b"xx", "image/svg+xml"), True)

# --- the "is this image just a render?" heuristic --------------------------
# _image_is_a_preview reads the clipboard through the backend, so stub the
# three reads it makes. This is the decision that files OnlyOffice cells as
# text instead of a screenshot.
class FakeClipboard:
    def __init__(self, html, text):
        self.html, self.text = html, text

    def pick_html_type(self, types):
        return "text/html" if "text/html" in types else None

    def pick_text_type(self, types):
        return "text/plain" if "text/plain" in types else None

    def read_text(self, mime=None):
        return self.html if mime == "text/html" else self.text


def preview_verdict(types, html, text):
    real = capture.clipboard
    capture.clipboard = FakeClipboard(html, text)
    try:
        return capture._image_is_a_preview(types)
    finally:
        capture.clipboard = real


ALL = ["image/png", "text/html", "text/plain"]
SHEET_HTML = ('<meta charset="utf-8"><table><tr><td>Region</td><td>Q1</td></tr>'
              '<tr><td>APAC</td><td>1200</td></tr></table>')
SHEET_TEXT = "Region\tQ1\nAPAC\t1200"
IMG_HTML = '<meta charset="utf-8"><img src="https://example.com/cat.png">'

print("render-vs-image heuristic")
check("spreadsheet cells -> text wins",
      preview_verdict(ALL, SHEET_HTML, SHEET_TEXT), True)
check("browser Copy Image -> image wins",
      preview_verdict(ALL, IMG_HTML, "https://example.com/cat.png"), False)
check("image with a bare URL beside it -> image wins",
      preview_verdict(ALL, "<div>x</div>", "https://example.com/cat.png"), False)
check("screenshot (image only) -> image wins",
      preview_verdict(["image/png"], "", ""), False)
check("image + html but no text -> image wins",
      preview_verdict(["image/png", "text/html"], SHEET_HTML, ""), False)
check("document paragraph + render -> text wins",
      preview_verdict(ALL, "<p>Hello <b>there</b></p>", "Hello there"), True)
check("multi-line text starting with a URL -> text wins",
      preview_verdict(ALL, "<p>see</p>", "https://a.example\nand more"), True)

# --- html -> plain text ----------------------------------------------------
print("html to plain text")
check("table becomes tab/newline separated",
      richtext.html_to_text(SHEET_HTML), "Region\tQ1\nAPAC\t1200")
check("entities are unescaped",
      richtext.html_to_text("<p>a &amp; b</p>"), "a & b")
check("script content is dropped",
      richtext.html_to_text("<p>hi</p><script>var x=1</script>"), "hi")
check("empty html is empty", richtext.html_to_text(""), "")
check("plain string passes through", richtext.html_to_text("just words"), "just words")

# --- the frame protocol round-trips ---------------------------------------
print("x11 frame protocol")
from clippy import x11clip                                  # noqa: E402
parts = [("text/html", b"<b>hi</b>"), ("text/plain", b"hi"), ("image/png", PNG)]
check("parts round-trip",
      x11clip._decode_parts(x11clip._encode_parts(parts)), parts)
check("truncated payload is rejected",
      x11clip._decode_parts(x11clip._encode_parts(parts)[:-5]), [])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
