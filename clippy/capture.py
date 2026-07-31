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


# Magic bytes per image type. Reading our own clip back through data-control can
# return the payload with a 4-byte little-endian length prefix glued on the front
# (measured: `d6 a6 07 00` + a 501462-byte PNG, i.e. the length itself). That is
# not a valid image, but it *is* new bytes, so it hashes differently and lands as
# a fresh entry — a duplicate tile that renders as "image unavailable". Left
# unchecked it compounds: each pass prefixes the previous one, which is what drove
# a 213-entry runaway with sizes climbing +4 each round.
_IMAGE_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/jpg": (b"\xff\xd8\xff",),
    "image/bmp": (b"BM",),
    "image/tiff": (b"II*\x00", b"MM\x00*"),
}


def _looks_like_image(data: bytes, mime: str) -> bool:
    """True unless we can positively tell the bytes are not the image they claim.

    Only rejects when the magic for a *known* type is missing, so an unrecognised
    image type is still stored rather than silently dropped."""
    magic = _IMAGE_MAGIC.get((mime or "").split(";")[0].strip().lower())
    if not magic:
        return True
    return any(data.startswith(m) for m in magic)


def _is_own_staging(path: str) -> bool:
    """True for a file URI that is Clippy's *own* staged copy (``DATA_DIR/paste``).

    Taking durable ownership of an image also offers it as a file, so the very
    next ``wl-paste --watch`` tick sees a uri-list pointing into our staging dir.
    Without this guard that echo would be filed as a separate *file* entry on
    every image copy."""
    import os
    try:
        stage = os.path.realpath(str(config.DATA_DIR / "paste"))
        return os.path.commonpath([os.path.realpath(path), stage]) == stage
    except (OSError, ValueError):
        return False


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
    file_paths = [p for p in clipboard.read_file_paths(types)
                  if not _is_own_staging(p)]
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
        if data and not _looks_like_image(data, image_mime):
            return None          # corrupt read (see _IMAGE_MAGIC) — don't file it
        if data:
            new_id = storage.add_image(data, image_mime)
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
