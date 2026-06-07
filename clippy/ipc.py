"""Tiny line-based IPC over a Unix domain socket.

The daemon listens; short-lived CLI invocations (``toggle``, the ``_store``
hook, ``pair``/``peers``) connect and send a single command, optionally with an
argument (``command arg``). Most commands are UI actions dispatched onto the GTK
main thread (fire-and-forget). A few are *queries* (``peers``, ``sync-status``,
``pair``, ``_broadcast``) handled synchronously by a separate callback so the
caller gets data back.
"""
from __future__ import annotations

import os
import socket
import threading
from typing import Callable, Optional

from . import config

# UI commands: acknowledged with "ok", dispatched async to the GTK thread.
VALID_COMMANDS = {
    "toggle", "show", "hide", "refresh", "ping", "quit",
    "open-settings", "reload-settings",
}
# Query commands: handled synchronously; the reply carries data.
QUERY_COMMANDS = {"peers", "sync-status", "pair", "_broadcast"}

_MAX_REPLY = 1 << 16


def _socket_path() -> str:
    return str(config.SOCKET_PATH)


def send(command: str, timeout: float = 5.0) -> Optional[str]:
    """Send a command to a running daemon. Returns the reply, or None if no
    daemon is listening."""
    path = _socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((command.strip() + "\n").encode("utf-8"))
            chunks = []
            while True:
                buf = sock.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
                if sum(len(c) for c in chunks) >= _MAX_REPLY:
                    break
            return b"".join(chunks).decode("utf-8", "replace").strip()
    except (OSError, socket.timeout):
        return None


def daemon_running() -> bool:
    return send("ping") == "pong"


class Server:
    """Accepts connections on a background thread.

    ``handler(command)`` runs UI commands (the daemon marshals to GLib).
    ``query(command, arg) -> str|None`` runs data commands synchronously on the
    server thread (the sync engine is thread-safe)."""

    def __init__(self, handler: Callable[[str], None],
                 query: Optional[Callable[[str, str], Optional[str]]] = None):
        self._handler = handler
        self._query = query
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        config.ensure_dirs()
        path = _socket_path()
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
                    conn.settimeout(30)
                    data = conn.recv(4096).decode("utf-8", "replace").strip()
                except OSError:
                    continue
                cmd, _, arg = data.partition(" ")
                if cmd == "ping":
                    self._reply(conn, "pong")
                elif cmd in QUERY_COMMANDS and self._query is not None:
                    try:
                        reply = self._query(cmd, arg.strip())
                    except Exception as exc:
                        reply = f"err {exc}"
                    self._reply(conn, reply if reply is not None else "ok")
                elif cmd in VALID_COMMANDS:
                    self._reply(conn, "ok")
                    self._handler(cmd)
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
