"""Verify restoring a rich-text entry offers BOTH flavors on the pasteboard.

Regression test for: history items with HTML paste fine in TextEdit but do
nothing in VS Code / Slack. Electron apps read public.utf8-plain-text; a
restore that writes only public.html gives them nothing to paste.

Usage (from repo root, on a Mac):
    python3 scripts/mac_rich_copy_test.py

NOTE: overwrites the current clipboard contents.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AppKit import NSPasteboard  # noqa: E402

from clippy.backends import get_backend  # noqa: E402

HTML = '<code style="color: red">hello</code> world'
TEXT = "hello world"

be = get_backend()
be.copy_html(HTML, TEXT)

pb = NSPasteboard.generalPasteboard()
types = list(pb.types() or [])
print("pasteboard types:", types)

failures = []
if "public.html" not in types:
    failures.append("public.html missing")
elif pb.stringForType_("public.html") != HTML:
    failures.append("public.html content mismatch: %r"
                    % pb.stringForType_("public.html"))
if "public.utf8-plain-text" not in types:
    failures.append("public.utf8-plain-text missing (Electron paste breaks)")
elif pb.stringForType_("public.utf8-plain-text") != TEXT:
    failures.append("public.utf8-plain-text content mismatch: %r"
                    % pb.stringForType_("public.utf8-plain-text"))

# Omitting text must keep the old html-only behavior (no bogus empty flavor).
be.copy_html(HTML)
types_no_text = list(pb.types() or [])
if "public.utf8-plain-text" in types_no_text:
    failures.append("plain text flavor written even though no text was given")

if failures:
    for f in failures:
        print("FAIL:", f)
    sys.exit(1)
print("PASS: rich restore offers both public.html and public.utf8-plain-text")
