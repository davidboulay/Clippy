#!/usr/bin/env python3
"""Verify the UNIQUE(hash) -> UNIQUE(kind, hash) rebuild preserves history.

A constraint change in SQLite means rebuilding the table, which is the one
migration that can lose a user's clipboard history if it goes wrong. This runs
it against a throwaway copy of a legacy-shaped database (and, when one exists,
a copy of the real one) and checks row-for-row that nothing changed.

Also checks the behaviour the migration exists for: identical bytes copied as an
image and as a file must produce two entries, not one.

Run:  PYTHONPATH=. python3 scripts/storage_migration_test.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


LEGACY_SCHEMA = """
CREATE TABLE entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    text        TEXT,
    html        TEXT,
    mime        TEXT,
    image_path  TEXT,
    filename    TEXT,
    hash        TEXT    NOT NULL UNIQUE,
    size        INTEGER NOT NULL DEFAULT 0,
    pinned      INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
"""


def rows_of(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = [tuple(r) for r in c.execute(
        "SELECT id, kind, text, html, mime, image_path, filename, hash, size,"
        " pinned, created_at FROM entries ORDER BY id")]
    c.close()
    return out


def schema_of(path):
    c = sqlite3.connect(path)
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='entries'").fetchone()[0]
    c.close()
    return sql


def run_migration(db_path, data_dir):
    """Open the DB through storage (which migrates on connect)."""
    for mod in [m for m in list(sys.modules) if m.startswith("clippy")]:
        del sys.modules[mod]
    os.environ["XDG_DATA_HOME"] = str(data_dir)
    os.environ["XDG_CONFIG_HOME"] = str(data_dir / "config")
    from clippy import storage
    storage.count()          # any call opens a connection -> _migrate runs
    return storage


print("synthetic legacy database")
tmp = Path(tempfile.mkdtemp())
data = tmp / "clippy-data"
(data / "clippy").mkdir(parents=True)
db = data / "clippy" / "history.db"
conn = sqlite3.connect(db)
conn.executescript(LEGACY_SCHEMA)
seed = [
    ("text", "hello", None, "text/plain", None, None, "h" * 64, 5, 0, 1000.0),
    ("image", None, None, "image/png", "/tmp/a.png", None, "i" * 64, 900, 1, 1001.0),
    ("file", "a.pdf", None, "application/pdf", "/tmp/a.pdf", "a.pdf", "f" * 64, 10, 0, 1002.0),
]
conn.executemany(
    "INSERT INTO entries (kind, text, html, mime, image_path, filename, hash,"
    " size, pinned, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", seed)
conn.commit()
conn.close()

before = rows_of(db)
check("legacy schema has table-wide UNIQUE", "hash        TEXT    NOT NULL UNIQUE" in schema_of(db), True)
storage = run_migration(db, data)
after = rows_of(db)
check("schema widened to UNIQUE (kind, hash)", "UNIQUE (kind, hash)" in schema_of(db), True)
check("row count preserved", len(after), len(before))
check("every row preserved byte-for-byte", after, before)
check("pinned flag survived", [r[9] for r in after], [r[9] for r in before])
check("ordering index recreated",
      any("idx_entries_order" in (s or "") for s in
          [r[0] for r in sqlite3.connect(db).execute(
              "SELECT name FROM sqlite_master WHERE type='index'")]), True)

print("migration is idempotent")
sch1 = schema_of(db)
run_migration(db, data)
check("second run is a no-op", schema_of(db), sch1)
check("rows still intact", rows_of(db), before)

print("the behaviour the migration exists for")
same = b"\x89PNG\r\n\x1a\n" + b"payload-bytes" * 64
img_id = storage.add_image(same, "image/png")
blob = tmp / "same.png"
blob.write_bytes(same)
file_id = storage.add_file_from_path(str(blob), "same.png", "image/png")
check("identical bytes as image and as file are two entries",
      img_id is not None and file_id is not None and img_id != file_id, True)
check("re-copying the same image still de-duplicates",
      storage.add_image(same, "image/png"), img_id)
check("re-copying the same file still de-duplicates",
      storage.add_file_from_path(str(blob), "same.png", "image/png"), file_id)

# The real database is the one that matters; migrate a copy of it, never it.
real = Path.home() / ".local/share/clippy/history.db"
if real.exists():
    print(f"copy of the real history ({real})")
    data2 = tmp / "real-copy"
    (data2 / "clippy").mkdir(parents=True)
    shutil.copyfile(real, data2 / "clippy" / "history.db")
    for suffix in ("-wal", "-shm"):
        src = Path(str(real) + suffix)
        if src.exists():
            shutil.copyfile(src, str(data2 / "clippy" / "history.db") + suffix)
    db2 = data2 / "clippy" / "history.db"
    before2 = rows_of(db2)
    run_migration(db2, data2)
    after2 = rows_of(db2)
    check(f"all {len(before2)} real rows preserved", after2, before2)
    check("real schema widened", "UNIQUE (kind, hash)" in schema_of(db2), True)
    check("pinned entries preserved",
          sum(r[9] for r in after2), sum(r[9] for r in before2))
else:
    print("  (no real history database found — skipped)")

print("code works against BOTH schemas (no one-way weld)")
# The migration is forward-only, so at least one moment exists — an interrupted
# upgrade, a downgrade, a daemon still running the previous package — where code
# and schema disagree. A query naming a constraint (ON CONFLICT(hash)) breaks
# every text capture then; these checks pin that it doesn't happen again.
for label, schema in (("legacy UNIQUE(hash)", LEGACY_SCHEMA), ):
    d = tmp / f"compat-{label.split()[0]}"
    (d / "clippy").mkdir(parents=True)
    c = sqlite3.connect(d / "clippy" / "history.db")
    c.executescript(schema)
    c.commit()
    c.close()
    # Reach in and neuter the migration, so we exercise NEW code on an OLD schema.
    for mod in [m for m in list(sys.modules) if m.startswith("clippy")]:
        del sys.modules[mod]
    os.environ["XDG_DATA_HOME"] = str(d)
    os.environ["XDG_CONFIG_HOME"] = str(d / "config")
    from clippy import storage as st
    st._widen_hash_uniqueness = lambda conn: None
    check(f"add_text works on {label}", isinstance(st.add_text("x"), int), True)
    check(f"re-copy de-duplicates on {label}", st.add_text("x"), st.add_text("x"))
    png = b"\x89PNG\r\n\x1a\n" + b"z" * 32
    check(f"add_image works on {label}", isinstance(st.add_image(png), int), True)
    check(f"schema stayed legacy (migration really was skipped)",
          "UNIQUE (kind, hash)" not in schema_of(d / "clippy" / "history.db"), True)

shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
