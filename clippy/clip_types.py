"""Platform-neutral clipboard-entry type classification.

One source of truth for *what kind* of thing a history entry is, shared by the
GTK panel (`panel.py`) and the macOS panel (`mac_panel.py`). This module knows
nothing about rendering: it returns a coarse bucket key, a short human label,
and a freedesktop **symbolic icon name**. Each platform maps the key to its own
colour and (on macOS) SF Symbol locally, so the classification never diverges.

Buckets (the `type_key` values): text, image, video, audio, pdf, sheet,
archive, file.
"""
from __future__ import annotations

import os

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif",
              "heic", "heif", "avif", "ico", "svg"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "mkv", "avi", "wmv", "flv", "mpg", "mpeg"}
AUDIO_EXTS = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "aiff", "aif", "opus"}
ARCHIVE_EXTS = {"zip", "tar", "gz", "tgz", "bz2", "tbz", "7z", "rar", "xz", "zst", "dmg"}
SHEET_EXTS = {"csv", "tsv", "xls", "xlsx", "xlsm", "xlsb", "ods", "numbers"}
SHEET_MIMES = ("text/csv", "text/tab-separated-values",
               "application/vnd.ms-excel",
               "application/vnd.openxmlformats-officedocument.spreadsheetml",
               "application/vnd.oasis.opendocument.spreadsheet")

# Coarse bucket key -> freedesktop symbolic icon name (used by GTK directly;
# macOS maps the same keys to SF Symbols locally).
ICON_NAMES = {
    "text": "text-x-generic-symbolic",
    "image": "image-x-generic-symbolic",
    "video": "video-x-generic-symbolic",
    "audio": "audio-x-generic-symbolic",
    "pdf": "x-office-document-symbolic",
    "sheet": "x-office-spreadsheet-symbolic",
    "archive": "package-x-generic-symbolic",
    "file": "text-x-generic-symbolic",
}

# Type filter menu order: (key, menu label, freedesktop symbolic icon name).
TYPE_FILTERS = [
    ("text", "Text", ICON_NAMES["text"]),
    ("image", "Image", ICON_NAMES["image"]),
    ("video", "Video", ICON_NAMES["video"]),
    ("audio", "Audio", ICON_NAMES["audio"]),
    ("pdf", "PDF", ICON_NAMES["pdf"]),
    ("sheet", "Spreadsheet", ICON_NAMES["sheet"]),
    ("archive", "Archive", ICON_NAMES["archive"]),
    ("file", "Other files", ICON_NAMES["file"]),
]


def ext(name: str) -> str:
    """Lowercase file extension (no dot) of a name/path, or ''."""
    return os.path.splitext(name or "")[1].lstrip(".").lower()


def category(entry):
    """Classify a history entry into ``(label, key, icon_name)``.

    ``label`` is a short uppercase human tag (granular for files, e.g. EXCEL,
    ZIP, the bare extension); ``key`` is the coarse bucket; ``icon_name`` is a
    freedesktop symbolic icon name. Branch order matches the original macOS
    ``_category`` exactly — CSV/TSV text is bucketed as ``sheet``.
    """
    if entry.kind == "text":
        m = (entry.mime or "").lower()
        if "csv" in m or "tab-separated" in m:
            return "CSV", "sheet", ICON_NAMES["sheet"]
        return "TEXT", "text", ICON_NAMES["text"]
    if entry.is_image:                       # image DATA (Copy Image)
        return "IMAGE", "image", ICON_NAMES["image"]
    mime = (entry.mime or "").lower()
    e = ext(entry.filename or entry.text or "")
    if mime.startswith("image/") or e in IMAGE_EXTS:
        return "IMAGE", "image", ICON_NAMES["image"]
    if mime.startswith("video/") or e in VIDEO_EXTS:
        return "VIDEO", "video", ICON_NAMES["video"]
    if mime.startswith("audio/") or e in AUDIO_EXTS:
        return "AUDIO", "audio", ICON_NAMES["audio"]
    if mime == "application/pdf" or e == "pdf":
        return "PDF", "pdf", ICON_NAMES["pdf"]
    if e in SHEET_EXTS or any(s in mime for s in SHEET_MIMES):
        label = "EXCEL" if e.startswith("xls") else (e.upper()[:6] or "SHEET")
        return label, "sheet", ICON_NAMES["sheet"]
    if e in ARCHIVE_EXTS:
        return "ZIP", "archive", ICON_NAMES["archive"]
    if e:
        return e.upper()[:6], "file", ICON_NAMES["file"]
    return "FILE", "file", ICON_NAMES["file"]


def type_key(entry) -> str:
    """Coarse bucket key for an entry — derived from :func:`category` so the
    two can never disagree."""
    return category(entry)[1]
