"""Read whatever is on the clipboard right now and persist it.

Shared by the ``_store`` hook (run by ``wl-paste --watch`` on every change)
and by the daemon's one-shot capture at startup. GTK-free on purpose.
"""
from __future__ import annotations

import re
from typing import List, Optional

from . import clipboard, config, debuglog, settings, sound, storage

# When Clippy mirrors an image/file copy to the X11 clipboard, XWayland
# re-publishes it onto the Wayland clipboard, which fires wl-paste --watch a
# second time ~tens of ms later. That re-capture dedups (returns the existing
# id) and would replay the copy sound. Debounce it: skip the sound if we just
# played it for the same entry. _store runs as a fresh subprocess per copy, so
# the last-played id+time is kept in a small state file rather than in memory.
_SOUND_STATE = config.DATA_DIR / ".last_sound"
_SOUND_DEBOUNCE = 1.5  # seconds

# Files stored from a single multi-file copy. A bound, not a preference: a
# "select all" in a big folder would otherwise hash every file inline, inside
# the watcher hook that blocks the next capture.
_MAX_FILES = 25


def _should_play_sound(entry_id: int) -> bool:
    import fcntl
    import os
    import time

    now = time.time()
    # Read, decide and write under one lock. Each _store is its own process and
    # a copy plus its echo arrive tens of milliseconds apart, so without this
    # the two interleave: both read the old state, both decide to play, and the
    # copy sound doubles — the exact bug the debounce was added to fix.
    lock_path = _SOUND_STATE.with_suffix(".lock")
    fd = None
    try:
        # Both the lock and the state file live in DATA_DIR; if it doesn't exist
        # yet, every write here fails and is swallowed, so the debounce silently
        # never engages and the copy sound doubles on every echo.
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        fd = None           # no lock available — fall through unserialized
    try:
        try:
            last_id, last_t = _SOUND_STATE.read_text().split()
            if int(last_id) == entry_id and now - float(last_t) < _SOUND_DEBOUNCE:
                return False  # an echo bounce (or sync round-trip) — stay silent
        except (OSError, ValueError):
            pass
        try:
            # Write-then-rename, so a reader never sees a half-written record.
            tmp = _SOUND_STATE.with_suffix(".tmp")
            tmp.write_text(f"{entry_id} {now}")
            os.replace(tmp, _SOUND_STATE)
        except OSError:
            pass
        return True
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass


# Magic bytes per image type. Reading our own clip back through data-control can
# return the payload with a 4-byte little-endian length prefix glued on the front
# (measured: `d6 a6 07 00` + a 501462-byte PNG, i.e. the length itself). That is
# not a valid image, but it *is* new bytes, so it hashes differently and lands as
# a fresh entry — a duplicate tile that renders as "image unavailable". Left
# unchecked it compounds: each pass prefixes the previous one, which is what drove
# a 213-entry runaway with sizes climbing +4 each round.
# Predicates rather than plain prefixes: the container formats (WebP, AVIF/HEIF)
# identify themselves a few bytes in, not at offset zero.
_IMAGE_SIGS = (
    ("image/png", lambda d: d.startswith(b"\x89PNG\r\n\x1a\n")),
    ("image/jpeg", lambda d: d.startswith(b"\xff\xd8\xff")),
    ("image/gif", lambda d: d.startswith((b"GIF87a", b"GIF89a"))),
    ("image/webp", lambda d: d[:4] == b"RIFF" and d[8:12] == b"WEBP"),
    ("image/bmp", lambda d: d.startswith(b"BM")),
    ("image/tiff", lambda d: d.startswith((b"II*\x00", b"MM\x00*"))),
    ("image/avif", lambda d: d[4:8] == b"ftyp" and d[8:12] in (b"avif", b"avis")),
    ("image/heif", lambda d: d[4:8] == b"ftyp"
        and d[8:12] in (b"heic", b"heix", b"heim", b"heis", b"mif1")),
)


def _canonical_image_mime(mime: str) -> str:
    """Lower-cased type with parameters dropped, and ``image/jpg`` folded into
    the canonical ``image/jpeg`` (apps offer both spellings for one format)."""
    base = (mime or "").split(";")[0].strip().lower()
    return "image/jpeg" if base == "image/jpg" else base


def _looks_like_image(data: bytes, mime: str) -> bool:
    """True unless we can positively tell the bytes are not the image they claim.

    Only rejects when the magic for a *known* type is missing, so an unrecognised
    image type is still stored rather than silently dropped."""
    want = _canonical_image_mime(mime)
    for name, matches in _IMAGE_SIGS:
        if name == want:
            return matches(data)
    return True


def sniff_image_mime(data: bytes) -> Optional[str]:
    """The image type `data` actually *is*, from its magic — or None when it
    matches nothing we know. The inverse of ``_looks_like_image``, which only
    checks bytes against a claim; this derives the claim from the bytes so a
    mislabelled payload can be corrected instead of merely rejected."""
    for name, matches in _IMAGE_SIGS:
        if matches(data):
            return name
    return None


# An html fragment whose whole body is one image — what a browser puts on the
# clipboard for "Copy image". The optional leading comment/meta is the fragment
# header Chromium and Firefox prepend.
_IMG_ONLY_HTML = re.compile(
    r"\A\s*(?:<!--.*?-->\s*|<meta[^>]*>\s*|<html[^>]*>\s*|<body[^>]*>\s*)*"
    r"<img\b[^>]*>"
    r"\s*(?:</body>\s*|</html>\s*)*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _image_is_a_preview(types: List[str]) -> bool:
    """True when an offered image is a *render* of richer content, not the clip.

    Spreadsheet and document apps (OnlyOffice, LibreOffice, Excel) put a bitmap
    picture of the selection on the clipboard next to the real thing — the cell
    text and an html table. Because capture prefers images over text, copying a
    few cells was filed as a picture: no searchable text, and pasting it back
    dropped a screenshot into the target instead of data.

    The inverse case has to keep working, so this stays deliberately narrow. A
    browser's "Copy image" *also* offers html and text alongside the bytes — but
    its html is just the ``<img>`` tag and its text is the image's URL, and there
    the picture really is the content. So: rich content wins only when the html
    is more than an image wrapper and the plain text isn't a bare link."""
    html_mime = clipboard.pick_html_type(types)
    text_mime = clipboard.pick_text_type(types)
    if not html_mime or not text_mime:
        return False           # no rich alternative to prefer — keep the image
    html = clipboard.read_text(html_mime) or ""
    if not html.strip() or _IMG_ONLY_HTML.match(html):
        return False
    text = clipboard.read_text(text_mime if "/" in text_mime else None) or ""
    stripped = text.strip()
    if not stripped:
        return False
    if "\n" not in stripped and stripped.lower().startswith(
            ("http://", "https://", "data:", "file://")):
        return False           # a bare URL beside an image — the image is the clip
    return True


def _is_own_staging(path: str) -> bool:
    """True only for the throwaway image written to back an image's *file* flavor.

    Taking durable ownership of an image can also offer it as a file, so the very
    next ``wl-paste --watch`` tick sees a uri-list pointing into our own staging.
    Without this guard that echo would be filed as a separate *file* entry on
    every image copy.

    Scoped to ``FLAVOR_DIR``, not the whole of ``PASTE_DIR``. Recovering a file
    entry stages a copy under its original name in ``PASTE_DIR`` too, and while
    both lived in one directory this guard dropped those as well: capture
    returned None, so ``_store`` sent no ``_broadcast`` and never reached
    ``sound.play()`` — recovering a file was silent and didn't sync."""
    import os
    try:
        stage = os.path.realpath(str(config.FLAVOR_DIR))
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

    debuglog.log("capture.types", types=",".join(types))
    new_id = None
    # Check for a copied FILE first. macOS (and some Linux apps) also place a
    # rendered preview on the clipboard when you copy an image *file* in the file
    # manager — so checking image data first would grab that fixed-size preview
    # instead of the real file. A real file copy wins: we sync the actual bytes
    # with the original name/extension.
    file_paths = [p for p in clipboard.read_file_paths(types)
                  if not _is_own_staging(p)]
    image_mime = clipboard.pick_image_type(types)
    if file_paths:
        import mimetypes
        import os
        # Every file, not just the first: selecting three files in the file
        # manager and copying used to file one and silently drop the rest. The
        # first is the primary — its id is what gets broadcast and sounded — and
        # the others are stored so they're in history too. Capped because a
        # "select all" in a large folder would otherwise hash the whole thing.
        for src in file_paths[:_MAX_FILES]:
            name = os.path.basename(src) or "file"
            mime = mimetypes.guess_type(src)[0] or "application/octet-stream"
            stored = storage.add_file_from_path(src, name, mime)
            if new_id is None:
                new_id = stored
    elif image_mime and not _image_is_a_preview(types):
        # Image DATA copied from an app (e.g. Copy Image), no file involved.
        data = clipboard.read_bytes(image_mime)
        if data and not _looks_like_image(data, image_mime):
            # The bytes aren't the format they claim. Prefer believing the bytes
            # (a mislabelled but valid image is still a clip); only give up when
            # they aren't a recognisable image at all — see _IMAGE_SIGS.
            actual = sniff_image_mime(data)
            if actual is None:
                debuglog.log("capture.corrupt_image", mime=image_mime, bytes=len(data))
                return None
            debuglog.log("capture.remimed", claimed=image_mime, actual=actual)
            image_mime = actual
        if data:
            new_id = storage.add_image(data, image_mime)
    else:
        text_mime = clipboard.pick_text_type(types)
        html_mime = clipboard.pick_html_type(types)
        text = ""
        if text_mime:
            arg = text_mime if "/" in text_mime else None
            text = clipboard.read_text(arg)
            if not (text and text.strip()) and arg is not None:
                # The advertised type may be one the app can't actually serve
                # (e.g. a case-variant MIME like ';charset=UTF-8'); let wl-paste
                # pick a servable type instead of dropping the copy entirely.
                text = clipboard.read_text(None)
        # Capture the rich version too, so "paste with formatting" works.
        html = clipboard.read_text(html_mime) if html_mime else None
        if not (text and text.strip()) and html and html.strip():
            # An offer with html and no plain flavor at all — some rich editors
            # do this. Deriving the plain text keeps the clip instead of
            # dropping it, and gives the entry both flavors so either recover
            # mode works.
            from . import richtext
            text = richtext.html_to_text(html)
            debuglog.log("capture.html_only", derived=len(text))
        if text and text.strip():
            new_id = storage.add_text(
                text,
                text_mime if (text_mime and "/" in text_mime) else "text/plain",
                html=html or None,
            )

    if new_id is not None:
        prefs = settings.load()
        if prefs.get("sound_on_copy") and _should_play_sound(new_id):
            sound.play()
        storage.apply_retention()
    return new_id
