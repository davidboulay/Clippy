"""Platform clipboard backend interface.

A backend is the *only* OS-specific surface for clipboard I/O. The portable
core (capture, storage, sync) talks to a backend through this protocol.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ClipboardBackend(Protocol):
    def require_tools(self) -> None: ...
    def list_types(self) -> List[str]: ...
    def pick_image_type(self, types: List[str]) -> Optional[str]: ...
    def pick_text_type(self, types: List[str]) -> Optional[str]: ...
    def pick_html_type(self, types: List[str]) -> Optional[str]: ...
    def read_text(self, mime: Optional[str] = None) -> str: ...
    def read_bytes(self, mime: str) -> bytes: ...
    def copy_text(self, text: str) -> None: ...
    def copy_html(self, html: str) -> None: ...
    def copy_image(self, data: bytes, mime: str) -> None: ...

    def mirror_to_x11(self, mime: str, data: bytes) -> None:
        """Best-effort: also publish freshly-*captured* bytes to the X11
        (XWayland) clipboard, for kinds the compositor doesn't bridge itself
        (image/file selections). No-op where it doesn't apply (macOS, no display)."""
        ...

    def read_file_paths(self, types: List[str]) -> List[str]:
        """Local file paths the clipboard currently offers (a file copy), or []."""
        ...

    def copy_file(self, path: str) -> None:
        """Put a file reference on the clipboard (so apps paste the file)."""
        ...

    def start_watch(self, on_change: Callable[[], None]) -> None:
        """Begin watching for clipboard changes, calling ``on_change`` on each.

        Linux returns immediately — the daemon drives capture via the external
        ``wl-paste --watch`` subprocess. macOS starts a ``changeCount`` poll
        thread that calls ``on_change`` in-process."""
        ...


class ClipboardError(RuntimeError):
    pass
