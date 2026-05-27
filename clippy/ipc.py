"""Tiny line-based IPC over a Unix domain socket.

The daemon listens; short-lived CLI invocations (``toggle``, and the
``_store`` hook) connect and send a single command. This is how a COSMIC
custom keyboard shortcut talks to the already-running panel without needing
any global-hotkey grab (which Wayland forbids for ordinary apps).
"""
from __future__ import annotations

import os
import socket
import threading
from typing import Callable, Optional

from . import config

VALID_COMMANDS = {
    "toggle", "show", "hide", "refresh", "ping", "quit",
    "open-settings", "reload-settings",
}


def _socket_path() -> str:
    return str(config.SOCKET_PATH)


def send(command: str, timeout: float = 1.0) -> Optional[str]:
    """Send a command to a running daemon. Returns the reply, or None if no
    daemon is listening."""
    path = _socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((command.strip() + "\n").encode("utf-8"))
            return sock.recv(64).decode("utf-8", "replace").strip()
    except (OSError, socket.timeout):
        return None


def daemon_running() -> bool:
    return send("ping") == "pong"


class Server:
    """Accepts connections on a background thread and dispatches commands via
    a callback. The callback is invoked off the GTK main thread, so it should
    marshal UI work with GLib.idle_add."""

    def __init__(self, handler: Callable[[str], None]):
        self._handler = handler
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        config.ensure_dirs()
        path = _socket_path()
        # Clear a stale socket left by an unclean shutdown.
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        os.chmod(path, 0o600)
        self._sock.listen(8)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            with conn:
                try:
                    data = conn.recv(64).decode("utf-8", "replace").strip()
                except OSError:
                    continue
                if data == "ping":
                    self._reply(conn, "pong")
                    continue
                if data in VALID_COMMANDS:
                    self._reply(conn, "ok")
                    self._handler(data)
                else:
                    self._reply(conn, "err")

    @staticmethod
    def _reply(conn: socket.socket, msg: str) -> None:
        try:
            conn.sendall((msg + "\n").encode("utf-8"))
        except OSError:
            pass

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        path = _socket_path()
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
