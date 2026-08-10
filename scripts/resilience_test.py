#!/usr/bin/env python3
"""Failure-path checks for storage errors, IPC concurrency and the sound debounce.

Each of these is a bug that only shows up when something else has already gone
wrong, which is exactly when nobody is watching: a database error that escaped
every caller's guard, an IPC server that stopped answering while pairing waited
on a human, and a copy-sound debounce that two processes could race through.

Uses a throwaway XDG root, so it never touches the real history or socket.

Run:  PYTHONPATH=. python3 scripts/resilience_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="clippy-resilience-")
os.environ["XDG_DATA_HOME"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP + "/config"
os.environ["XDG_RUNTIME_DIR"] = _TMP

from clippy import capture, ipc, storage                    # noqa: E402

FAILURES = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


print("copy-sound debounce")
check("first capture of an entry plays", capture._should_play_sound(1), True)
check("its echo moments later stays silent", capture._should_play_sound(1), False)
check("a different entry plays", capture._should_play_sound(2), True)

# Two _store processes race on every copy (the copy and its echo). Serialised
# correctly, exactly one of a burst for the same entry may play.
results = []
lock = threading.Lock()


def racer():
    r = capture._should_play_sound(99)
    with lock:
        results.append(r)


threads = [threading.Thread(target=racer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("a burst for one entry plays at most once", sum(results) <= 1, True)

print("database failures reach callers as OSError")
storage.add_text("seed entry")
check("storage works normally first", storage.count() >= 1, True)
check("StorageError is an OSError", issubclass(storage.StorageError, OSError), True)

# Injected rather than provoked by corrupting the database file. Overwriting a
# SQLite header looks like the more honest test, but it is not a stable fixture:
# whether — and at which statement — SQLite rejects the file depends on its
# version, on WAL checkpoint timing, and on what survived the overwrite. Two CI
# runs of identical code disagreed about it, and each disagreed with this
# machine. A flaky assertion is worse than none, because it teaches you to
# ignore the signal.
#
# What the code under test actually does is translate sqlite3.Error into an
# OSError subclass at the storage boundary, so that is what gets asserted, at
# every entry point a caller wraps in `except OSError`.
import sqlite3 as _sqlite3                                  # noqa: E402


def _raising_connect(*_a, **_kw):
    raise _sqlite3.OperationalError("database is locked")


_real_connect = storage._connect
storage._connect = _raising_connect
try:
    guarded = []
    for name, call in (
        ("get", lambda: storage.get(1)),
        ("list_entries", lambda: storage.list_entries()),
        ("count", lambda: storage.count()),
        ("touch", lambda: storage.touch(1)),
        ("add_text", lambda: storage.add_text("x")),
        ("apply_retention", lambda: storage.apply_retention()),
    ):
        try:
            call()
            guarded.append(f"{name}:no-raise")
        except OSError:
            guarded.append(f"{name}:ok")       # the guard callers write catches it
        except Exception as exc:               # noqa: BLE001
            guarded.append(f"{name}:{type(exc).__name__}")
    check("every entry point a caller guards raises OSError on a locked db",
          [g for g in guarded if not g.endswith(":ok")], [])
finally:
    storage._connect = _real_connect

print("IPC stays responsive while a slow command runs")
started = threading.Event()


def query(cmd, arg):
    if cmd == "pair":
        started.set()
        time.sleep(3)          # stands in for pairing waiting on a human
        return "paired"
    return "ok"


server = ipc.Server(handler=lambda c: None, query=query)
server.start()
threading.Thread(target=lambda: ipc.send("pair CODE", timeout=10),
                 daemon=True).start()
check("slow command started", started.wait(5), True)
t0 = time.time()
reply = ipc.send("ping", timeout=5)
elapsed = time.time() - t0
check("ping is answered during it", reply, "pong")
check(f"without waiting for it ({elapsed:.2f}s)", elapsed < 1.0, True)
server.stop()

import shutil                                               # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
