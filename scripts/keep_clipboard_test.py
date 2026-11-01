#!/usr/bin/env python3
"""The clipboard keeper: when it puts a clip back, and when it must not.

Closing the app you copied from empties a Wayland clipboard; wl-paste --watch
runs the _store hook with CLIPBOARD_STATE=nil and the daemon puts the last clip
back (keeper.py). The dangerous half is restoring the wrong thing — a password a
manager withdrew, a clip the user explicitly cleared, or an older entry standing
in for a clip we never captured — so most cases here are refusals.

Stubs storage, settings, the clipboard writes and IPC: safe to run on a working
desktop, needs no GTK and no compositor."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clippy import capture, cli, clipboard, keeper, settings, storage  # noqa: E402

FAILURES = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def entry(eid, text="hello", html=None):
    return SimpleNamespace(id=eid, kind="text", text=text, html=html,
                           mime="text/plain", image_path=None,
                           is_file=False, is_image=False)


class World:
    """History, settings and the clipboard, as far as the keeper can see."""

    def __init__(self):
        self.history = {}
        self.prefs = {"keep_clipboard": True, "always_plain_text": False}
        self.copied = []
        self.muted = []
        storage.get = lambda eid: self.history.get(eid)
        settings.get = lambda key: self.prefs.get(key)
        clipboard.copy_text = lambda text: self.copied.append(("text", text))
        clipboard.copy_html = lambda html, text=None: self.copied.append(("html", html))
        capture.mute_echo = lambda eid: self.muted.append(eid)
        keeper._reset_for_tests()


def test_keeper():
    print("keeper")
    w = World()
    w.history[7] = entry(7, "copied text")

    check("nothing known: no restore", keeper.on_lost("nil"), False)

    keeper.note_current(7)
    check("owner closed: restores", keeper.on_lost("nil"), True)
    check("  the clip that was lost", w.copied, [("text", "copied text")])
    check("  echo muted", w.muted, [7])

    w.copied.clear()
    keeper.note_current(7)
    check("explicit clear: no restore", keeper.on_lost("clear"), False)
    check("  and forgotten", keeper.on_lost("nil"), False)
    check("  nothing written", w.copied, [])

    keeper.note_current(7)
    keeper.forget("sensitive")
    check("sensitive clip: never restored", keeper.on_lost("nil"), False)

    keeper.note_current(99)                       # not in history any more
    check("deleted from history: no restore", keeper.on_lost("nil"), False)
    w.history[99] = entry(99)
    check("  and forgotten, not retried", keeper.on_lost("nil"), False)

    w.prefs["keep_clipboard"] = False
    keeper.note_current(7)
    check("setting off: no restore", keeper.on_lost("nil"), False)
    w.prefs["keep_clipboard"] = True

    keeper._reset_for_tests()
    keeper.note_current(7)
    results = [keeper.on_lost("nil") for _ in range(keeper._RESTORE_MAX + 2)]
    check("rate cap", results.count(True), keeper._RESTORE_MAX)

    keeper._reset_for_tests()
    w.history[8] = entry(8, "plain", html="<b>plain</b>")
    w.copied.clear()
    keeper.note_current(8)
    keeper.on_lost("nil")
    check("rich clip comes back rich", w.copied, [("html", "<b>plain</b>")])

    keeper.note_current("not-a-number")           # ignored, keeps 8
    check("garbage id ignored", keeper._last_id, 8)


def run_store(state, captured):
    """Run the _store hook as wl-paste would, recording the IPC it sends."""
    sent = []
    import clippy.ipc as ipc
    ipc.send = lambda cmd, timeout=5.0: (sent.append(cmd), "ok")[1]
    capture.capture_current = lambda: captured
    if state is None:
        os.environ.pop("CLIPBOARD_STATE", None)
    else:
        os.environ["CLIPBOARD_STATE"] = state
    stdin = sys.stdin
    sys.stdin = SimpleNamespace(buffer=SimpleNamespace(read=lambda: b""))
    try:
        cli._cmd_store()
    finally:
        sys.stdin = stdin
    return sent


def test_hook():
    print("_store hook")
    check("nil -> _lost only", run_store("nil", 5), ["_lost nil"])
    check("clear -> _lost only", run_store("clear", 5), ["_lost clear"])
    check("data -> current, refresh, broadcast", run_store("data", 5),
          ["_current 5", "refresh", "_broadcast 5"])
    check("old wl-paste (unset) behaves as data", run_store(None, 5),
          ["_current 5", "refresh", "_broadcast 5"])
    check("uncaptured -> forget", run_store("data", None), ["_forget uncaptured"])
    sent = run_store("sensitive", 6)
    check("sensitive: forget after current",
          sent.index("_forget sensitive") > sent.index("_current 6"), True)


def main():
    test_keeper()
    test_hook()
    if FAILURES:
        print("\n".join(["", "FAILED:"] + FAILURES))
        return 1
    print("\nall keeper checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
