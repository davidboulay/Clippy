"""Fire-and-forget desktop notifications.

A shared helper because both the daemon (update progress) and the panel (a
recover that failed) need to tell the user something without owning a dialog.
GTK-free, so the panel can call it from a worker thread and the ``_store`` hook
could too.
"""
from __future__ import annotations

import shutil
import subprocess

APP_NAME = "Clippy"


def send(body: str, title: str = "") -> None:
    """Show a transient notification. Silently does nothing without
    ``notify-send`` — a missing notifier must not turn into an error path."""
    if shutil.which("notify-send") is None:
        return
    cmd = ["notify-send", "--app-name", APP_NAME]
    if title:
        cmd += [title, body]
    else:
        cmd += [body]
    try:
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
