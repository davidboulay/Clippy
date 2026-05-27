"""Thin wrapper over wl-clipboard (wl-paste / wl-copy).

We never depend on GTK here so this module can be imported by the lightweight
``_store`` subprocess that wl-paste spawns on every clipboard change.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import List, Optional

from . import config


class ClipboardError(RuntimeError):
    pass


def require_tools() -> None:
    """Raise if wl-clipboard isn't installed."""
    missing = [t for t in ("wl-paste", "wl-copy") if shutil.which(t) is None]
    if missing:
        raise ClipboardError(
            "Missing required tools: %s. Install with:\n"
            "    sudo apt install wl-clipboard" % ", ".join(missing)
        )


def list_types() -> List[str]:
    """MIME types currently offered by the clipboard (regular selection)."""
    try:
        out = subprocess.run(
            ["wl-paste", "--list-types"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _pick(available: List[str], preferred) -> Optional[str]:
    avail = {t.lower(): t for t in available}
    for want in preferred:
        if want.lower() in avail:
            return avail[want.lower()]
    return None


def pick_image_type(types: List[str]) -> Optional[str]:
    hit = _pick(types, config.IMAGE_TYPES)
    if hit:
        return hit
    for t in types:  # any other image/* the compositor offers
        if t.lower().startswith("image/"):
            return t
    return None


def pick_text_type(types: List[str]) -> Optional[str]:
    hit = _pick(types, config.TEXT_TYPES)
    if hit:
        return hit
    # Fall back to any text/* that isn't html (html handled separately).
    for t in types:
        low = t.lower()
        if low.startswith("text/") and not low.startswith("text/html"):
            return t
    return None


def pick_html_type(types: List[str]) -> Optional[str]:
    return _pick(types, config.HTML_TYPES)


def read_bytes(mime: str) -> bytes:
    try:
        return subprocess.run(
            ["wl-paste", "-t", mime],
            capture_output=True,
            timeout=15,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return b""


def read_text(mime: Optional[str] = None) -> str:
    cmd = ["wl-paste", "--no-newline"]
    if mime:
        cmd += ["-t", mime]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        return ""
    return raw.decode("utf-8", "replace")


def copy_text(text: str) -> None:
    subprocess.run(["wl-copy"], input=text.encode("utf-8"), timeout=10)


def copy_html(html: str) -> None:
    """Place rich text on the clipboard as text/html (formatting follows)."""
    subprocess.run(
        ["wl-copy", "--type", "text/html"],
        input=html.encode("utf-8"),
        timeout=10,
    )


def copy_image(data: bytes, mime: str) -> None:
    subprocess.run(["wl-copy", "--type", mime], input=data, timeout=15)
