"""Keep the clipboard when the app that owned it goes away.

On Wayland (and X11) the app you copied from *is* the clipboard: it serves the
bytes on every paste. Close that window or quit that app and the clipboard is
empty, even though Clippy has the clip safely in history. Desktops paper over
this with a clipboard keeper of their own — Klipper on Plasma, Mutter's built-in
one on GNOME — but wlroots compositors (Hyprland, Sway), COSMIC, and a Plasma
session that runs Clippy *instead of* Klipper have nothing, so a paste after
closing the source quietly produces nothing at all.

``wl-paste --watch`` already tells us the moment that happens: it runs the
``_store`` hook with ``CLIPBOARD_STATE=nil``. The hook forwards that to the
daemon, and this module puts the clip that was just lost back on the clipboard
through the same path a panel recover uses.

What it deliberately does *not* put back:

  * anything after ``CLIPBOARD_STATE=sensitive`` — a password manager marked the
    clip (``x-kde-passwordManagerHint``; KeePassXC and Bitwarden both do), and
    resurrecting a password its owner withdrew is the opposite of what the user
    wants. This is the guard that protects passwords, because of the next point;
  * anything after ``CLIPBOARD_STATE=clear`` — someone asked for an empty
    clipboard. Only some compositors report it: measured on KWin (Plasma 6.8),
    ``wl-copy --clear`` arrives as ``nil``, indistinguishable from the owner
    closing, so there an explicit clear of an ordinary clip is undone. That
    matches Klipper's own "prevent empty clipboard" and ``wl-clip-persist``;
  * a clip Clippy could not capture: if the last change didn't produce an entry,
    the clip that vanished isn't one we have, and the previous entry would be a
    clip the user had already replaced;
  * a clip deleted from history in the meantime.

An empty clipboard is not proof the owner is gone. Chromium and every Electron
app (Slack, VS Code, Discord, Chrome itself) drop their old selection and offer
the new one a few milliseconds later whenever you copy again while they still
own the clipboard: measured on KWin 2026-11-02, Slack sends ``nil`` then
``data`` 3 ms apart on every repeat copy. Restoring on that ``nil`` landed our
old clip on top of the new one, and the copy the user just made was gone before
anything (Clippy included) could read it. So the keeper waits ``_GRACE``
seconds and only restores if nothing happened meanwhile *and* the clipboard is
still empty.

Restores are rate-capped, so a compositor or app that keeps emptying the
clipboard costs a few republishes rather than a loop. GTK-free: the daemon runs
it on the IPC thread, and ``put_back`` is shared with the panel.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List, Optional

from . import clipboard, debuglog, settings, storage

_lock = threading.Lock()
_last_id: Optional[int] = None     # entry currently on the clipboard, if known
_restore_times: List[float] = []

_RESTORE_MAX = 3        # restores allowed per window before we stand down
_RESTORE_WINDOW = 60    # seconds
_GRACE = 0.3            # seconds an empty clipboard must stay empty (Slack: 3 ms)
_generation = 0         # bumped by every clipboard event the daemon hears of
_sleep = time.sleep     # swapped out by the tests


def _bump() -> int:
    global _generation
    with _lock:
        _generation += 1
        return _generation


def _still_empty() -> bool:
    """Whether the clipboard offers nothing at all right now."""
    try:
        return not clipboard.list_types()
    except Exception:   # a backend error is not evidence of an empty clipboard
        return False


def put_back(entry, mode: str = "auto") -> bool:
    """Put ``entry`` back on the clipboard. False if it couldn't be done.

    Each step is guarded separately: reading a blob that has gone missing is
    a different failure from the clipboard write, and only the former means
    the history row is now a dead reference."""
    try:
        if entry.is_file and entry.image_path:
            # Stage a copy under the original filename so the pasted file
            # isn't named after its content hash (the blob's name).
            path = storage.paste_path(entry) or entry.image_path
            if not Path(path).exists():
                debuglog.log("recover.blob_missing", id=entry.id, path=path)
                return False
            clipboard.copy_file(path)
        elif entry.is_image and entry.image_path:
            try:
                data = Path(entry.image_path).read_bytes()
            except OSError:
                debuglog.log("recover.blob_missing", id=entry.id,
                             path=entry.image_path)
                return False
            clipboard.copy_image(data, entry.mime or "image/png")
        else:
            always_plain = bool(settings.get("always_plain_text"))
            use_rich = (
                entry.html
                and mode != "plain"
                and (mode == "rich" or not always_plain)
            )
            if use_rich:
                clipboard.copy_html(entry.html, entry.text)
            else:
                clipboard.copy_text(entry.text or "")
    except OSError as exc:
        debuglog.log("recover.failed", id=entry.id, error=exc)
        return False
    debuglog.log("recover.ok", id=entry.id, kind=entry.kind, mode=mode)
    return True


def note_current(entry_id) -> None:
    """The clipboard now holds this history entry."""
    global _last_id
    try:
        eid = int(entry_id)
    except (TypeError, ValueError):
        return
    _bump()
    with _lock:
        _last_id = eid


def forget(reason: str = "") -> None:
    """The clipboard holds something we must not (or cannot) put back."""
    global _last_id
    _bump()
    with _lock:
        _last_id = None
    debuglog.log("keeper.forget", reason=reason)


def _allowed(now: float) -> bool:
    recent = [t for t in _restore_times if now - t < _RESTORE_WINDOW]
    _restore_times[:] = recent
    if len(recent) >= _RESTORE_MAX:
        return False
    _restore_times.append(now)
    return True


def restore(entry) -> bool:
    """Put ``entry`` back without it sounding like a fresh copy."""
    from .capture import mute_echo
    mute_echo(entry.id)         # the republish echoes back through _store
    ok = put_back(entry)
    if ok:
        note_current(entry.id)
    return ok


def on_lost(state: str) -> bool:
    """React to the clipboard going empty. True if a clip was put back.

    ``state`` is wl-paste's CLIPBOARD_STATE: only ``nil`` (the owner went away)
    is restored; ``clear`` is an explicit request for an empty clipboard."""
    if state != "nil":
        forget(state or "empty")
        return False
    if not settings.get("keep_clipboard"):
        return False
    # Give a copy in progress time to land: an app replacing its own selection
    # empties the clipboard for a few ms first, and restoring then destroys the
    # copy the user just made.
    gen = _bump()
    _sleep(_GRACE)
    with _lock:
        superseded = _generation != gen
    if superseded:
        debuglog.log("keeper.skip", reason="superseded")
        return False
    if not _still_empty():
        debuglog.log("keeper.skip", reason="refilled")
        return False
    with _lock:
        eid = _last_id
        if eid is None:
            debuglog.log("keeper.skip", reason="nothing to keep")
            return False
        if not _allowed(time.time()):
            debuglog.log("keeper.skip", reason="rate cap", id=eid)
            return False
    try:
        entry = storage.get(eid)
    except OSError:
        entry = None
    if entry is None:
        forget("deleted from history")
        return False
    ok = restore(entry)
    debuglog.log("keeper.restore", id=eid, ok=ok)
    return ok


def _reset_for_tests() -> None:
    global _last_id, _generation
    with _lock:
        _last_id = None
        _generation = 0
        _restore_times.clear()
