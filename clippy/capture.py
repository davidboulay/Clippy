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
    if image_mime:
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
