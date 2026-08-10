"""Opt-in diagnostic log for the clipboard path.

Clipboard bugs on Wayland/X11 are timing- and compositor-dependent: by the time
a user reports "it pasted nothing that once", the state that explains it is
gone. Nothing here runs unless switched on — ``CLIPPY_DEBUG=1`` in the
environment, or the ``debug_log`` setting — and then every capture, publish and
release is appended to ``<DATA_DIR>/debug.log`` with a timestamp and the pid.

Written for cross-process use: ``_store`` is a fresh subprocess per copy and the
daemon is long-lived, so lines are emitted with a single ``O_APPEND`` write
(atomic for the sizes involved) rather than through a shared handle.

GTK-free, import-cheap, and never raises: a broken log must not break a copy.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from . import config

ENV_VAR = "CLIPPY_DEBUG"
LOG_PATH = config.DATA_DIR / "debug.log"
_MAX_BYTES = 2 * 1024 * 1024     # rotate to debug.log.1 past this
_MAX_VALUE = 120                 # truncate long field values

_enabled: Optional[bool] = None


def enabled() -> bool:
    """Whether diagnostics are on. The env var wins over the setting, so a
    one-off ``CLIPPY_DEBUG=1 clippy daemon`` needs no config change."""
    global _enabled
    if _enabled is None:
        raw = os.environ.get(ENV_VAR)
        if raw is not None:
            _enabled = raw.strip().lower() not in ("", "0", "false", "no", "off")
        else:
            try:
                from . import settings
                _enabled = bool(settings.get("debug_log"))
            except Exception:
                _enabled = False
    return _enabled


def refresh() -> None:
    """Forget the cached switch, so a settings change takes effect without a
    restart (the daemon calls this when preferences are saved)."""
    global _enabled
    _enabled = None


def _fmt(value: object) -> str:
    text = str(value)
    if len(text) > _MAX_VALUE:
        text = text[:_MAX_VALUE] + "…"
    # Keep one event per line and keep it greppable.
    return text.replace("\n", "\\n").replace("\r", "")


def _rotate() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > _MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH.with_suffix(".log.1"))
    except OSError:
        pass


def log(event: str, **fields: object) -> None:
    """Append one ``event`` line with arbitrary ``key=value`` context."""
    if not enabled():
        return
    try:
        stamp = time.strftime("%H:%M:%S") + f".{int(time.time() % 1 * 1000):03d}"
        parts = [f"{stamp} pid={os.getpid()} {event}"]
        parts += [f"{k}={_fmt(v)}" for k, v in fields.items()]
        line = (" ".join(parts) + "\n").encode("utf-8", "replace")
        _rotate()
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        pass    # diagnostics must never break the thing they observe
