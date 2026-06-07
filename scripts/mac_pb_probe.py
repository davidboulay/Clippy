"""Probe what macOS actually puts on the pasteboard for a copied file.

Usage (from repo root, in the venv):
    1. In Finder, select a file and press Cmd+C.
    2. python3 scripts/mac_pb_probe.py

Prints the raw pasteboard types, per-item public.file-url, NSURL objects,
the backend's read_file_paths() result, list_types(), and what a real
capture_current() would store. This is the ground truth for the file-sync bug.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AppKit import NSPasteboard, NSURL  # noqa: E402

pb = NSPasteboard.generalPasteboard()

print("changeCount:", pb.changeCount())
print("\n=== pb.types() ===")
for t in (pb.types() or []):
    print("  ", t)

print("\n=== per pasteboardItem ===")
for i, it in enumerate(pb.pasteboardItems() or []):
    print(f"  item[{i}] types:", list(it.types() or []))
    print(f"  item[{i}] public.file-url:", it.stringForType_("public.file-url"))

print("\n=== readObjectsForClasses_([NSURL]) ===")
for u in (pb.readObjectsForClasses_options_([NSURL], None) or []):
    try:
        print("   isFileURL=%s path=%s" % (u.isFileURL(), u.path()))
    except Exception as exc:
        print("   <err>", exc)

print("\n=== backend.read_file_paths([]) ===")
from clippy.backends import get_backend  # noqa: E402

be = get_backend()
print("  ", be.read_file_paths([]))

print("\n=== backend.list_types() ===")
print("  ", be.list_types())

print("\n=== what capture_current() would store ===")
from clippy import capture, storage  # noqa: E402

new_id = capture.capture_current()
print("  new_id:", new_id)
if new_id is not None:
    e = storage.get(new_id)
    if e is not None:
        print("  kind=%s filename=%s mime=%s size=%s text=%r"
              % (e.kind, e.filename, e.mime,
                 e.size, (e.text or "")[:80]))
