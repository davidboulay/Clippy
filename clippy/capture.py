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
    image_mime = clipboard.pick_image_type(types)
    file_paths = [] if image_mime else clipboard.read_file_paths(types)
    if image_mime:
        data = clipboard.read_bytes(image_mime)
        if data:
            new_id = storage.add_image(data, image_mime)
    elif file_paths:
        # A copied file (image, video, PDF, …): store the bytes, not the path.
        import mimetypes
        import os
        src = file_paths[0]
        name = os.path.basename(src) or "file"
        mime = mimetypes.guess_type(src)[0] or "application/octet-stream"
        try:
            fsize = os.path.getsize(src)
        except OSError:
            fsize = 0
        # A reasonably-sized image file: store as an image so it pastes as an
        # image on the other device; everything else (video, PDF, big images)
        # syncs as a file (streamed from disk).
        if mime.startswith("image/") and 0 < fsize <= 64 * 1024 * 1024:
            data = open(src, "rb").read()
            new_id = storage.add_image(data, mime) if data else None
        else:
            new_id = storage.add_file_from_path(src, name, mime)
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
