#!/usr/bin/env python3
"""Self-test for multi-file copies (1.7.0).

Copying several files used to become several unrelated clips: history showed
them apart, recovering one put back only that file, and sync sent just the
first, so a paste on the other machine dropped one file. Now the files of one
copy share a group:

  1. storage lists a group once (``a.zip + 2 more``), in copy order, and
     delete / pin / touch act on the whole group;
  2. capture of a multi-file clipboard stores one group;
  3. the Wayland backend offers every file in one uri-list;
  4. sync sends every member tagged with the group, and the receiver puts them
     on its clipboard together, once, in order;
  5. an incomplete group still reaches the clipboard after the wait;
  6. a peer's group fields are validated.

Runs against a throwaway XDG data dir, never the real history.

Run:  PYTHONPATH=. python3 scripts/multi_file_groups_test.py
Needs: python3-nacl + python3-zeroconf (sync part)
"""
import os
import pathlib
import sys
import tempfile
import time

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="clippy-groups-test-"))
os.environ["XDG_DATA_HOME"] = str(_TMP / "data")
os.environ["XDG_CONFIG_HOME"] = str(_TMP / "config")
os.environ["XDG_RUNTIME_DIR"] = str(_TMP / "run")
(_TMP / "run").mkdir()

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from clippy import capture, clipboard, config, storage  # noqa: E402

FAILURES = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


src = _TMP / "src"
src.mkdir()
files = []
for i, name in enumerate(["Equalizer.zip", "Old_Radio.xml", "Pip-Boy FX.zip"]):
    p = src / name
    p.write_bytes(f"payload {i} ".encode() * (i + 1) * 100)
    files.append(str(p))

print("1. storage")
gid = "ab" * 16
ids = [storage.add_file_from_path(f, os.path.basename(f), "application/zip",
                                  group_id=gid, group_pos=i)
       for i, f in enumerate(files)]
listed = [e for e in storage.list_entries() if e.kind == "file"]
check("a group is listed once", len(listed), 1)
head = listed[0]
check("by its first file", head.filename, "Equalizer.zip")
check("with its size", head.group_size, 3)
check("is_group", head.is_group, True)
check("footer label", head.file_label, "Equalizer.zip + 2 more")
check("get() fills group_size too", storage.get(ids[1]).group_size, 3)
check("members in copy order",
      [m.filename for m in storage.group_members(head)],
      ["Equalizer.zip", "Old_Radio.xml", "Pip-Boy FX.zip"])
paths = storage.paste_paths(head)
check("paste_paths stages every file under its name",
      [os.path.basename(p) for p in paths],
      ["Equalizer.zip", "Old_Radio.xml", "Pip-Boy FX.zip"])
check("staged copies hold the bytes",
      [pathlib.Path(p).read_bytes() for p in paths],
      [pathlib.Path(f).read_bytes() for f in files])

storage.toggle_pin(head.id)
check("pin applies to every member",
      [storage.get(i).pinned for i in ids], [True, True, True])
storage.toggle_pin(head.id)

with storage._connect() as conn:
    conn.execute("UPDATE entries SET created_at=1 WHERE id IN (?,?,?)", ids)
storage.touch(head.id)
check("touch bumps every member",
      all(storage.get(i).created_at > 1 for i in ids), True)

single = storage.add_file_from_path(files[2], "Pip-Boy FX.zip", "application/zip")
check("copied again on its own, a file leaves its group",
      storage.get(single).group_size, 1)
check("the group shrinks", storage.get(ids[0]).group_size, 2)
check("and both show in history",
      len([e for e in storage.list_entries() if e.kind == "file"]), 2)

with storage._connect() as conn:
    conn.execute("DELETE FROM entries WHERE id=?", (ids[0],))
check("a group whose first file is gone is still listed by the next",
      [e.filename for e in storage.list_entries() if e.kind == "file"
       and e.group_id == gid], ["Old_Radio.xml"])
storage.delete(ids[1])
check("delete removes the whole group",
      [storage.get(i) for i in ids[:2]], [None, None])
storage.delete(single)

print("2. capture")
clipboard.list_types = lambda: ["text/uri-list"]
clipboard.read_file_paths = lambda types: list(files)
from clippy import settings  # noqa: E402
settings.load = lambda: {"sound_on_copy": False}
storage.apply_retention = lambda: 0
new_id = capture.capture_current()
e = storage.get(new_id)
check("capture stores one group", (e.filename, e.group_size, e.group_pos),
      ("Equalizer.zip", 3, 0))
clipboard.read_file_paths = lambda types: [files[0]]
e1 = storage.get(capture.capture_current())
check("a single-file copy is not a group", (e1.group_id, e1.group_size), (None, 1))
storage.delete(e1.id)
clipboard.read_file_paths = lambda types: list(files)
capture.capture_current()

print("3. Wayland backend")
from clippy import x11clip  # noqa: E402
from clippy.backends import wayland  # noqa: E402
calls, mirrors = [], []
x11clip.publish_parts = lambda parts: False
wayland.subprocess.run = lambda cmd, input=None, timeout=None, **kw: calls.append((cmd, input))
wb = wayland.WaylandBackend()
wb._x11_mirror = lambda mime, data: mirrors.append((mime, data))
wb.copy_files(files)
want = "".join(pathlib.Path(f).as_uri() + "\r\n" for f in files).encode()
check("one wl-copy", len(calls), 1)
check("as text/uri-list", calls[0][0], ["wl-copy", "--type", "text/uri-list"])
check("listing every file in order", calls[0][1], want)
check("the xclip mirror carries the same list", mirrors, [("text/uri-list", want)])
parts = dict(wayland.WaylandBackend._file_parts(*files))
check("gnome list holds every file",
      parts["x-special/gnome-copied-files"].decode().splitlines(),
      ["copy"] + [pathlib.Path(f).as_uri() for f in files])

print("4-6. sync")
from clippy import sync  # noqa: E402
if not sync.sync_available():
    print("  skip: pynacl/zeroconf/spake2 not installed")
else:
    sync._SYNC_LOG = _TMP / "sync.log"
    check("group fields accepted", sync._group_fields(
        {"group": "ab" * 16, "group_size": 3, "group_pos": 2}), ("ab" * 16, 3, 2))
    for bad in ({"group": "AB/..", "group_size": 3, "group_pos": 0},
                {"group": "ab" * 16, "group_size": 1, "group_pos": 0},
                {"group": "ab" * 16, "group_size": 3, "group_pos": 3},
                {"group": "ab" * 16, "group_size": 999, "group_pos": 0},
                {"group": "ab" * 16, "group_size": "x", "group_pos": 0}):
        check(f"rejects {bad}", sync._group_fields(bad), None)
    check("no group = single clip", sync._group_fields({"hash": "x"}), None)

    A = sync.SyncEngine(port=48061, state_dir=tempfile.mkdtemp())
    B = sync.SyncEngine(port=48062, state_dir=tempfile.mkdtemp())
    A.start(); B.start()
    B._peers_online.setdefault(A.device_id, ("127.0.0.1", 48061, "A"))
    code = A.enter_pairing()
    assert B.join_pairing(code).get("ok"), "pairing failed"
    A._peers_online[B.device_id] = ("127.0.0.1", 48062, "B")

    injected, singles, notified = [], [], []
    clipboard.copy_files = lambda ps: injected.append(list(ps))
    clipboard.copy_file = lambda p: singles.append(p)
    B._on_received = lambda: notified.append(1)

    head = [x for x in storage.list_entries() if x.is_group][0]
    A._broadcast_entry(head)
    deadline = time.time() + 10
    while not injected and time.time() < deadline:
        time.sleep(0.1)
    check("receiver puts the group on the clipboard once", len(injected), 1)
    check("with every file, in copy order",
          [os.path.basename(p) for p in (injected[0] if injected else [])],
          ["Equalizer.zip", "Old_Radio.xml", "Pip-Boy FX.zip"])
    check("received bytes intact",
          [pathlib.Path(p).read_bytes() for p in (injected[0] if injected else [])],
          [pathlib.Path(f).read_bytes() for f in files])
    check("no file went on alone", singles, [])
    check("one received notification", len(notified), 1)

    injected.clear()
    A._broadcast_entry(head)
    time.sleep(1.0)
    check("a group already sent is not sent again", injected, [])

    print("  incomplete group")
    sync._GROUP_WAIT = 0.5
    late = _TMP / "late.bin"
    late.write_bytes(b"late")
    B._group_arrived("cd" * 16, 3, 1, str(late))
    time.sleep(1.0)
    check("the partial group still reaches the clipboard",
          injected, [[str(late)]])
    late2 = _TMP / "late2.bin"
    late2.write_bytes(b"late2")
    B._group_arrived("cd" * 16, 3, 0, str(late2))
    time.sleep(1.0)
    check("a straggler puts the fuller set on, in order",
          injected[-1], [str(late2), str(late)])
    A.stop(); B.stop()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print("  " + f)
    raise SystemExit(1)
print("ALL OK")
