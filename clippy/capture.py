"""Read whatever is on the clipboard right now and persist it.

Shared by the ``_store`` hook (run by ``wl-paste --watch`` on every change)
and by the daemon's one-shot capture at startup. GTK-free on purpose.
"""
from __future__ import annotations

from . import clipboard, config, settings, sound, storage

# When Clippy mirrors an image/file copy to the X11 clipboard, XWayland
# re-publishes it onto the Wayland clipboard, which fires wl-paste --watch a
# second time ~tens of ms later. That re-capture dedups (returns the existing
# id) and would replay the copy sound. Debounce it: skip the sound if we just
# played it for the same entry. _store runs as a fresh subprocess per copy, so
# the last-played id+time is kept in a small state file rather than in memory.
_SOUND_STATE = config.DATA_DIR / ".last_sound"
_SOUND_DEBOUNCE = 1.5  # seconds


def _should_play_sound(entry_id: int) -> bool:
    import time
    now = time.time()
    try:
        last_id, last_t = _SOUND_STATE.read_text().split()
        if int(last_id) == entry_id and now - float(last_t) < _SOUND_DEBOUNCE:
            return False  # an echo bounce (or sync round-trip) — stay silent
    except (OSError, ValueError):
        pass
    try:
        _SOUND_STATE.write_text(f"{entry_id} {now}")
    except OSError:
        pass
    return True


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
        # Mirror to X11 so XWayland apps can paste the file too — the compositor
        # bridges text both ways but not file/image selections.
        import urllib.request
        clipboard.mirror_to_x11(
            "text/uri-list",
            ("file://" + urllib.request.pathname2url(src) + "\r\n").encode("utf-8"),
        )
    elif clipboard.pick_image_type(types):
        # Image DATA copied from an app (e.g. Copy Image), no file involved.
        image_mime = clipboard.pick_image_type(types)
        data = clipboard.read_bytes(image_mime)
        if data:
            new_id = storage.add_image(data, image_mime)
            # Push the image to the X11 clipboard too; native-Wayland paste
            # already works via the wl-copy that produced this copy.
            clipboard.mirror_to_x11(image_mime, data)
    else:
        text_mime = clipboard.pick_text_type(types)
        if text_mime:
            arg = text_mime if "/" in text_mime else None
            text = clipboard.read_text(arg)
            if not (text and text.strip()) and arg is not None:
                # The advertised type may be one the app can't actually serve
                # (e.g. a case-variant MIME like ';charset=UTF-8'); let wl-paste
                # pick a servable type instead of dropping the copy entirely.
                text = clipboard.read_text(None)
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
        if prefs.get("sound_on_copy") and _should_play_sound(new_id):
            sound.play()
        storage.apply_retention()
    return new_id
