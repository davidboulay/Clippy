"""Persistent clipboard history backed by SQLite.

Text is stored inline (with an optional rich-text/html copy); images are
written to ``IMAGE_DIR`` and referenced by path. Entries are de-duplicated by
content hash: re-copying something already present bumps it to the top.

GTK-free so the ``_store`` subprocess stays lightweight.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import config, settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,            -- 'text' | 'image' | 'file'
    text        TEXT,                        -- plain text (text entries)
    html        TEXT,                        -- rich text, if the source had it
    mime        TEXT,                        -- source MIME type
    image_path  TEXT,                        -- blob file path (image/file entries)
    filename    TEXT,                        -- original name (file entries)
    hash        TEXT    NOT NULL UNIQUE,     -- sha256 of content
    size        INTEGER NOT NULL DEFAULT 0,  -- bytes
    pinned      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_order
    ON entries (pinned DESC, created_at DESC);
"""


@dataclass
class Entry:
    id: int
    kind: str
    text: Optional[str]
    html: Optional[str]
    mime: Optional[str]
    image_path: Optional[str]
    pinned: bool
    size: int
    created_at: float
    filename: Optional[str] = None
    hash: Optional[str] = None

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def is_file(self) -> bool:
        return self.kind == "file"

    @property
    def has_formatting(self) -> bool:
        return bool(self.html)


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
    if "html" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN html TEXT")
    if "filename" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN filename TEXT")


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        kind=row["kind"],
        text=row["text"],
        html=row["html"] if "html" in row.keys() else None,
        mime=row["mime"],
        image_path=row["image_path"],
        pinned=bool(row["pinned"]),
        size=row["size"],
        created_at=row["created_at"],
        filename=row["filename"] if "filename" in row.keys() else None,
        hash=row["hash"] if "hash" in row.keys() else None,
    )


def add_text(text: str, mime: str = "text/plain", html: Optional[str] = None) -> Optional[int]:
    """Store a text entry (or bump an existing identical one). Returns id."""
    if not text or not text.strip():
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO entries (kind, text, html, mime, hash, size, created_at)
               VALUES ('text', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(hash) DO UPDATE SET
                   created_at = excluded.created_at,
                   html = COALESCE(excluded.html, entries.html)""",
            (text, html, mime, digest, len(text.encode("utf-8")), now),
        )
        row = conn.execute("SELECT id FROM entries WHERE hash=?", (digest,)).fetchone()
        _prune_count(conn)
        return row["id"] if row else None


def add_image(data: bytes, mime: str = "image/png") -> Optional[int]:
    """Store an image entry. The bytes are written to a file in IMAGE_DIR."""
    if not data or len(data) > config.SYNC_MAX_CEILING:
        return None
    digest = hashlib.sha256(data).hexdigest()
    now = time.time()
    ext = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/bmp": "bmp",
           "image/tiff": "tiff"}.get(mime.lower(), "png")
    path = config.IMAGE_DIR / f"{digest}.{ext}"
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM entries WHERE hash=?", (digest,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE entries SET created_at=? WHERE id=?", (now, existing["id"])
            )
            return existing["id"]
        if not path.exists():
            path.write_bytes(data)
        cur = conn.execute(
            """INSERT INTO entries (kind, mime, image_path, hash, size, created_at)
               VALUES ('image', ?, ?, ?, ?, ?)""",
            (mime, str(path), digest, len(data), now),
        )
        _prune_count(conn)
        return cur.lastrowid


def _blob_ext(name: str, mime: str) -> str:
    """Pick a file extension for a content-addressed blob: prefer the original
    name's extension, else derive one from the MIME type. The blob is named
    ``<sha256><ext>`` so that when the file is later put back on the clipboard
    (its path's basename), it carries the right extension and apps recognize the
    type — even when the source had no usable filename (e.g. a copied screenshot)."""
    import mimetypes
    import os
    ext = os.path.splitext(name or "")[1]
    if not ext and mime:
        ext = mimetypes.guess_extension(mime.split(";")[0].strip()) or ""
    return ext


def add_file(data: bytes, name: str, mime: str = "application/octet-stream") -> Optional[int]:
    """Store an arbitrary file entry. Bytes written to FILE_DIR, original name kept."""
    if not data or len(data) > config.SYNC_MAX_CEILING:
        return None
    digest = hashlib.sha256(data).hexdigest()
    now = time.time()
    path = config.FILE_DIR / (digest + _blob_ext(name, mime))   # content-addressed blob
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM entries WHERE hash=?", (digest,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE entries SET created_at=? WHERE id=?", (now, existing["id"])
            )
            return existing["id"]
        if not path.exists():
            path.write_bytes(data)
        cur = conn.execute(
            """INSERT INTO entries (kind, text, mime, image_path, filename, hash, size, created_at)
               VALUES ('file', ?, ?, ?, ?, ?, ?, ?)""",
            (name, mime, str(path), name, digest, len(data), now),
        )
        _prune_count(conn)
        return cur.lastrowid


def add_file_from_path(src: str, name: str,
                       mime: str = "application/octet-stream") -> Optional[int]:
    """Store a copied file by streaming it (no full in-RAM read, so 2 GB works)."""
    import os
    import shutil
    try:
        size = os.path.getsize(src)
    except OSError:
        return None
    if size <= 0 or size > config.SYNC_MAX_CEILING:
        return None
    h = hashlib.sha256()
    with open(src, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    now = time.time()
    path = config.FILE_DIR / (digest + _blob_ext(name, mime))
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM entries WHERE hash=?", (digest,)
        ).fetchone()
        if existing:
            conn.execute("UPDATE entries SET created_at=? WHERE id=?",
                         (now, existing["id"]))
            return existing["id"]
        if not path.exists():
            shutil.copyfile(src, path)
        cur = conn.execute(
            """INSERT INTO entries (kind, text, mime, image_path, filename, hash, size, created_at)
               VALUES ('file', ?, ?, ?, ?, ?, ?, ?)""",
            (name, mime, str(path), name, digest, size, now),
        )
        _prune_count(conn)
        return cur.lastrowid


def list_entries(query: str = "", limit: int = config.MAX_HISTORY,
                 pinned: Optional[bool] = None) -> List[Entry]:
    """Return entries newest-first (pinned first). With a query, only matching
    text entries are returned. ``pinned`` filters by pin state: True for pinned
    only, False for unpinned only, None for both."""
    clauses: List[str] = []
    params: List[object] = []
    if query:
        clauses.append("kind='text' AND text LIKE ? COLLATE NOCASE")
        params.append(f"%{query}%")
    if pinned is not None:
        clauses.append("pinned=?")
        params.append(1 if pinned else 0)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM entries{where}
                ORDER BY pinned DESC, created_at DESC LIMIT ?""",
            params,
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def count(pinned: Optional[bool] = None) -> int:
    """Number of stored entries (cheap; no row materialization). ``pinned``
    filters by pin state: True for pinned only, False for unpinned only."""
    with _connect() as conn:
        if pinned is None:
            return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM entries WHERE pinned=?", (1 if pinned else 0,)
        ).fetchone()[0]


def get(entry_id: int) -> Optional[Entry]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    return _row_to_entry(row) if row else None


def touch(entry_id: int) -> None:
    """Bump an entry's created_at to now (move it to the front of history) —
    used when an old clip is recovered from the panel."""
    with _connect() as conn:
        conn.execute("UPDATE entries SET created_at=? WHERE id=?",
                     (time.time(), entry_id))


def delete(entry_id: int) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT image_path FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    if row and row["image_path"]:
        _maybe_unlink(Path(row["image_path"]))


def toggle_pin(entry_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT pinned FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
        if not row:
            return False
        new = 0 if row["pinned"] else 1
        conn.execute("UPDATE entries SET pinned=? WHERE id=?", (new, entry_id))
        return bool(new)


def clear(include_pinned: bool = False) -> None:
    with _connect() as conn:
        if include_pinned:
            rows = conn.execute("SELECT image_path FROM entries").fetchall()
            conn.execute("DELETE FROM entries")
        else:
            rows = conn.execute(
                "SELECT image_path FROM entries WHERE pinned=0"
            ).fetchall()
            conn.execute("DELETE FROM entries WHERE pinned=0")
    for r in rows:
        if r["image_path"]:
            _maybe_unlink(Path(r["image_path"]))


def apply_retention() -> int:
    """Delete unpinned entries older than the configured retention. Returns
    the number removed."""
    secs = config.retention_seconds(settings.get("retention"))
    if secs is None:  # "forever"
        return 0
    cutoff = time.time() - secs
    with _connect() as conn:
        stale = conn.execute(
            "SELECT id, image_path FROM entries WHERE pinned=0 AND created_at < ?",
            (cutoff,),
        ).fetchall()
        if stale:
            conn.executemany(
                "DELETE FROM entries WHERE id=?", [(r["id"],) for r in stale]
            )
    for r in stale:
        if r["image_path"]:
            _maybe_unlink(Path(r["image_path"]))
    return len(stale)


def _prune_count(conn: sqlite3.Connection) -> None:
    """Drop the oldest unpinned entries beyond the hard MAX_HISTORY cap."""
    stale = conn.execute(
        """SELECT id, image_path FROM entries WHERE pinned=0
           ORDER BY created_at DESC LIMIT -1 OFFSET ?""",
        (config.MAX_HISTORY,),
    ).fetchall()
    if not stale:
        return
    conn.executemany(
        "DELETE FROM entries WHERE id=?", [(r["id"],) for r in stale]
    )
    for r in stale:
        if r["image_path"]:
            _maybe_unlink(Path(r["image_path"]))


def _maybe_unlink(path: Path) -> None:
    """Remove an image file unless another entry still references it."""
    try:
        with _connect() as conn:
            still = conn.execute(
                "SELECT 1 FROM entries WHERE image_path=? LIMIT 1", (str(path),)
            ).fetchone()
        if not still and path.exists():
            path.unlink()
    except OSError:
        pass
