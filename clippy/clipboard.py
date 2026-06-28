"""Clipboard I/O facade.

Historically this wrapped wl-clipboard directly; it now delegates to a
per-platform backend (see ``clippy/backends/``) while keeping the same function
API, so callers (capture, panel, inject, sync) don't care which OS they're on.
Still GTK-free, so the lightweight ``_store`` subprocess can import it.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from .backends import get_backend
from .backends.base import ClipboardError  # noqa: F401 (re-exported)


def require_tools() -> None:
    get_backend().require_tools()


def list_types() -> List[str]:
    return get_backend().list_types()


def pick_image_type(types: List[str]) -> Optional[str]:
    return get_backend().pick_image_type(types)


def pick_text_type(types: List[str]) -> Optional[str]:
    return get_backend().pick_text_type(types)


def pick_html_type(types: List[str]) -> Optional[str]:
    return get_backend().pick_html_type(types)


def read_bytes(mime: str) -> bytes:
    return get_backend().read_bytes(mime)


def read_text(mime: Optional[str] = None) -> str:
    return get_backend().read_text(mime)


def copy_text(text: str) -> None:
    get_backend().copy_text(text)


def copy_html(html: str) -> None:
    get_backend().copy_html(html)


def copy_image(data: bytes, mime: str) -> None:
    get_backend().copy_image(data, mime)


def mirror_to_x11(mime: str, data: bytes) -> None:
    get_backend().mirror_to_x11(mime, data)


def read_file_paths(types: List[str]) -> List[str]:
    return get_backend().read_file_paths(types)


def copy_file(path: str) -> None:
    get_backend().copy_file(path)


def start_watch(on_change: Callable[[], None]) -> None:
    get_backend().start_watch(on_change)
