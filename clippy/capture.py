"""Read whatever is on the clipboard right now and persist it.

Shared by the ``_store`` hook (run by ``wl-paste --watch`` on every change)
and by the daemon's one-shot capture at startup. GTK-free on purpose.
"""
from __future__ import annotations

from . import clipboard, settings, sound, storage


def capture_current():
    """Snapshot the current clipboard into history.

    Returns the new entry's id (int) if something was stored, else None — the
    id lets the daemon broadcast exactly this item over sync (not just "the
    newest", which is pinned-first)."""
    types = clipboard.list_types()
    if not types:
        return None

    new_id = None
    # Check for a copied FILE first. macOS (and some Linux apps) also place a
    # rendered preview on the clipboard when you copy an image *file* in the file
    # manager — so checking image data first would grab that fixed-size preview
    # instead of the real file. A real file copy wins: we sync the actual bytes
    # with the original name/extension.
    file_paths = clipboard.read_file_paths(types)
    if file_paths:
        import mimetypes
        import os
        src = file_paths[0]
        name = os.path.basename(src) or "file"
        mime = mimetypes.guess_type(src)[0] or "application/octet-stream"
        new_id = storage.add_file_from_path(src, name, mime)
    elif clipboard.pick_image_type(types):
        # Image DATA copied from an app (e.g. Copy Image), no file involved.
        image_mime = clipboard.pick_image_type(types)
        data = clipboard.read_bytes(image_mime)
        if data:
            new_id = storage.add_image(data, image_mime)
    else:
        text_mime = clipboard.pick_text_type(types)
        if text_mime:
            arg = text_mime if "/" in text_mime else None
            text = clipboard.read_text(arg)
            if text and text.strip():
                # Capture the rich version too, so "paste with formatting" works.
                html = None
                html_mime = clipboard.pick_html_type(types)
                if html_mime:
                    html = clipboard.read_text(html_mime) or None
                new_id = storage.add_text(
                    text,
                    text_mime if "/" in text_mime else "text/plain",
                    html=html,
                )

    if new_id is not None:
        prefs = settings.load()
        if prefs.get("sound_on_copy"):
            sound.play()
        storage.apply_retention()
    return new_id
