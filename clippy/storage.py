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

# De-duplication is scoped to (kind, hash), not hash alone. The same bytes can
# legitimately be two different clips: copy a picture in a browser and you get an
# image entry, copy the file it was saved to and you get a file entry — identical
# content, but recovering one hands over raw bytes and the other hands over a
# file reference. With a table-wide UNIQUE(hash) the second copy silently
# de-duplicated into the first, so the file copy produced no file entry and
# pasting it dropped image bytes where a file was expected.
#
# The hash column itself stays the plain sha256 of the content: the X11 owner
# recognises its own publish echoing back by comparing that digest against what
# it published (see x11clip.note_published), so namespacing the digest itself
# would silently break echo detection.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,            -- 'text' | 'image' | 'file'
    text        TEXT,                        -- plain text (text entries)
    html        TEXT,                        -- rich text, if the source had it
    mime        TEXT,                        -- source MIME type
    image_path  TEXT,                        -- blob file path (image/file entries)
    filename    TEXT,                        -- original name (file entries)
    hash        TEXT    NOT NULL,            -- sha256 of content
    size        INTEGER NOT NULL DEFAULT 0,  -- bytes
    pinned      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL,
    UNIQUE (kind, hash)
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


class StorageError(OSError):
    """A database failure, raised as an OSError so existing callers catch it.

    ``sqlite3.OperationalError`` is *not* an ``OSError``, so a locked or corrupt
    database sailed straight through every ``except OSError`` guard around a
    storage call and out through whatever was above it — killing a capture, an
    IPC handler, or the retention sweep with a traceback nobody saw. The
    database is a file, callers already treat storage as file I/O, so failures
    are surfaced in the shape they already handle."""


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
    _widen_hash_uniqueness(conn)


def _widen_hash_uniqueness(conn: sqlite3.Connection) -> None:
    """Move a legacy table-wide UNIQUE(hash) to UNIQUE(kind, hash).

    SQLite can't alter a constraint in place, so this is the documented
    rebuild: create the table with the new shape, copy the rows, swap the names.
    Rows can't collide under the wider key — they were unique on `hash` alone,
    which is strictly stronger — so the copy cannot lose an entry. Wrapped in a
    transaction, and skipped entirely once done, so it runs at most once per
    database and leaves history untouched if anything fails."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'"
    ).fetchone()
    if not sql or "UNIQUE (kind, hash)" in (sql["sql"] or ""):
        return                      # already the new shape (or no table yet)
    if "UNIQUE" not in (sql["sql"] or ""):
        return                      # no uniqueness to widen
    try:
        conn.executescript("""
            BEGIN;
            CREATE TABLE entries_migrating (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT    NOT NULL,
                text        TEXT,
                html        TEXT,
                mime        TEXT,
                image_path  TEXT,
                filename    TEXT,
                hash        TEXT    NOT NULL,
                size        INTEGER NOT NULL DEFAULT 0,
                pinned      INTEGER NOT NULL DEFAULT 0,
                created_at  REAL    NOT NULL,
                UNIQUE (kind, hash)
            );
            INSERT INTO entries_migrating
                (id, kind, text, html, mime, image_path, filename,
                 hash, size, pinned, created_at)
                SELECT id, kind, text, html, mime, image_path, filename,
                       hash, size, pinned, created_at FROM entries;
            DROP TABLE entries;
            ALTER TABLE entries_migrating RENAME TO entries;
            CREATE INDEX IF NOT EXISTS idx_entries_order
                ON entries (pinned DESC, created_at DESC);
            COMMIT;
        """)
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        # Keeping the old constraint costs an occasional cross-kind de-dupe;
        # a half-migrated history would cost the history.
        from . import debuglog
        debuglog.log("storage.migrate_failed", step="widen_hash_uniqueness")


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
        # Deliberately NOT an `ON CONFLICT(...)` upsert. That clause has to name
        # a constraint that exists, which welds this query to one schema version:
        # a database migrated to UNIQUE(kind, hash) makes `ON CONFLICT(hash)`
        # raise OperationalError on *every* text capture, so an older build
        # sharing the same history file — an interrupted upgrade, a downgrade, a
        # daemon still running from the previous package — stops recording
        # anything at all. Insert-then-handle-collision needs no constraint name
        # and works against either shape.
        existing = conn.execute(
            "SELECT id, html FROM entries WHERE kind='text' AND hash=?", (digest,)
        ).fetchone()
        if existing:
            # Keep the existing html when the new capture has none. This looks
            # like a bug — re-copying the same sentence from a plain editor
            # leaves the formatting from an earlier rich copy attached, so "Copy
            # with formatting" offers markup that didn't come from this copy.
            # Overwriting is worse: recovering a rich clip as plain text (what
            # the always-plain-text setting does) comes straight back as a
            # capture with no html flavor, and would erase the formatting the
            # entry was kept for. Stale markup is cosmetic; deleting the user's
            # formatting is not.
            conn.execute(
                "UPDATE entries SET created_at=?, html=COALESCE(?, html), mime=?"
                " WHERE id=?",
                (now, html, mime, existing["id"]),
            )
            _prune_count(conn)
            return existing["id"]
        row_id = _insert_or_bump(
            conn,
            """INSERT INTO entries (kind, text, html, mime, hash, size, created_at)
               VALUES ('text', ?, ?, ?, ?, ?, ?)""",
            (text, html, mime, digest, len(text.encode("utf-8")), now),
            "text", digest, now,
        )
        _prune_count(conn)
        return row_id




_IMAGE_EXTS = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
    "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tiff",
    "image/avif": "avif", "image/heif": "heic", "image/heic": "heic",
    "image/svg+xml": "svg", "image/x-icon": "ico",
}


def add_image(data: bytes, mime: str = "image/png") -> Optional[int]:
    """Store an image entry. The bytes are written to a file in IMAGE_DIR."""
    if not data:
        return None
    if len(data) > config.MAX_IMAGE_BYTES:
        from . import debuglog
        debuglog.log("storage.image_too_big", bytes=len(data),
                     cap=config.MAX_IMAGE_BYTES)
        return None
    kind = "image"
    digest = hashlib.sha256(data).hexdigest()
    now = time.time()
    ext = _IMAGE_EXTS.get((mime or "").split(";")[0].strip().lower(), "png")
    path = config.IMAGE_DIR / f"{digest}.{ext}"
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM entries WHERE kind=? AND hash=?", (kind, digest)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE entries SET created_at=? WHERE id=?", (now, existing["id"])
            )
            return existing["id"]
        if not path.exists():
            path.write_bytes(data)
        row_id = _insert_or_bump(
            conn,
            """INSERT INTO entries (kind, mime, image_path, hash, size, created_at)
               VALUES ('image', ?, ?, ?, ?, ?)""",
            (mime, str(path), digest, len(data), now),
            kind, digest, now,
        )
        _prune_count(conn)
        return row_id


def _insert_or_bump(conn: sqlite3.Connection, sql: str, params: tuple,
                    kind: str, digest: str, now: float) -> Optional[int]:
    """Run an INSERT that may collide on the UNIQUE content hash, and return the
    row's id either way.

    The check-then-insert above is not atomic across processes, and two of them
    genuinely race: the daemon snapshots the clipboard at startup while the
    ``wl-paste --watch`` hook it just spawned fires for the same selection. The
    loser used to raise IntegrityError out of capture, so that copy was lost.
    Treat a collision as what it is — the same content, already stored — and
    bump it to the front like any re-copy. Scoped by ``kind``: the same bytes
    are allowed to exist once as an image and once as a file."""
    try:
        cur = conn.execute(sql, params)
        return cur.lastrowid
    except sqlite3.IntegrityError:
        conn.execute("UPDATE entries SET created_at=? WHERE kind=? AND hash=?",
                     (now, kind, digest))
        row = conn.execute("SELECT id FROM entries WHERE kind=? AND hash=?",
                           (kind, digest)).fetchone()
        return row["id"] if row else None


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
    kind = "file"
    digest = hashlib.sha256(data).hexdigest()
    now = time.time()
    path = config.FILE_DIR / (digest + _blob_ext(name, mime))   # content-addressed blob
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM entries WHERE kind=? AND hash=?", (kind, digest)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE entries SET created_at=? WHERE id=?", (now, existing["id"])
            )
            return existing["id"]
        if not path.exists():
            path.write_bytes(data)
        row_id = _insert_or_bump(
            conn,
            """INSERT INTO entries (kind, text, mime, image_path, filename, hash, size, created_at)
               VALUES ('file', ?, ?, ?, ?, ?, ?, ?)""",
            (name, mime, str(path), name, digest, len(data), now),
            kind, digest, now,
        )
        _prune_count(conn)
        return row_id


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
    kind = "file"
    digest = h.hexdigest()
    now = time.time()
    path = config.FILE_DIR / (digest + _blob_ext(name, mime))
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM entries WHERE kind=? AND hash=?", (kind, digest)
        ).fetchone()
        if existing:
            conn.execute("UPDATE entries SET created_at=? WHERE id=?",
                         (now, existing["id"]))
            return existing["id"]
        if not path.exists():
            shutil.copyfile(src, path)
        row_id = _insert_or_bump(
            conn,
            """INSERT INTO entries (kind, text, mime, image_path, filename, hash, size, created_at)
               VALUES ('file', ?, ?, ?, ?, ?, ?, ?)""",
            (name, mime, str(path), name, digest, size, now),
            kind, digest, now,
        )
        _prune_count(conn)
        return row_id


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


def paste_path(entry) -> Optional[str]:
    """A filesystem path with the entry's *original* filename, for putting a
    file back on the clipboard. Blobs are stored content-addressed as
    ``<sha256><ext>``, so pasting the blob directly drops a hash-named file. This
    stages a copy under ``<DATA_DIR>/paste/<original name>`` and returns it, so
    the pasted file carries its real name. Falls back to the blob path. Shared by
    the GTK and macOS panels."""
    import os
    import shutil
    blob = getattr(entry, "image_path", None)
    if not blob:
        return None
    name = os.path.basename(entry.filename or "") or os.path.basename(blob)
    try:
        stage = config.PASTE_DIR
        stage.mkdir(parents=True, exist_ok=True)
        dest = stage / name
        # Re-stage unless an identical copy is already there (size match is a
        # cheap proxy — the blob name is the content hash, so same name+size is
        # the same bytes).
        if not dest.exists() or dest.stat().st_size != (entry.size or 0):
            shutil.copyfile(blob, dest)
        return str(dest)
    except OSError:
        return blob


def touch(entry_id: int) -> None:
    """Bump an entry's created_at to now (move it to the front of history) —
    used when an old clip is recovered from the panel."""
    with _connect() as conn:
        conn.execute("UPDATE entries SET created_at=? WHERE id=?",
                     (time.time(), entry_id))


def latest_created_at() -> float:
    """Newest created_at across all entries. A cheap change-detector for panels:
    unlike (count, newest-id), this changes when an existing entry is bumped to
    the front (touch / re-copy of a file already in history), so the panel
    rebuilds instead of showing a stale order."""
    with _connect() as conn:
        row = conn.execute("SELECT MAX(created_at) AS m FROM entries").fetchone()
        return row["m"] if row and row["m"] is not None else 0.0


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


# --------------------------------------------------------------------------- #
# Surface database failures as OSError (see StorageError).
#
# Applied here in one place rather than as a decorator on each function so the
# list of what's covered is visible and can't drift: every entry point a caller
# outside this module uses goes through the same translation.
# --------------------------------------------------------------------------- #
def _as_os_error(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as exc:
            from . import debuglog
            debuglog.log("storage.db_error", fn=fn.__name__, error=exc)
            raise StorageError(f"{fn.__name__}: {exc}") from exc

    return wrapper


for _name in (
    "add_text", "add_image", "add_file", "add_file_from_path",
    "list_entries", "count", "get", "touch", "latest_created_at",
    "delete", "toggle_pin", "clear", "apply_retention",
):
    globals()[_name] = _as_os_error(globals()[_name])
del _name
