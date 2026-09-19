"""Tiny line-based IPC over a Unix domain socket.

The daemon listens; short-lived CLI invocations (``toggle``, the ``_store``
hook, ``pair``/``peers``) connect and send a single command, optionally with an
argument (``command arg``). Most commands are UI actions dispatched onto the GTK
main thread (fire-and-forget). A few are *queries* (``peers``, ``sync-status``,
``pair``, ``_broadcast``) handled synchronously by a separate callback so the
caller gets data back.
"""
from __future__ import annotations

import fcntl
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
QUERY_COMMANDS = {"peers", "sync-status", "pair", "_broadcast", "_current"}

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


# Held open for the life of the daemon process; the kernel drops the flock when
# the process dies, so an abnormal exit leaves nothing stale behind.
_lock_fd: Optional[int] = None


def acquire_single_instance() -> bool:
    """Take the daemon's exclusive lock. True if we now own it.

    ``daemon_running`` cannot serialize two daemons starting at once: it asks
    over the socket, and ``Server.start`` then unlinks whatever is there and
    binds afresh. Both racers get no answer, both bind, and the loser's socket
    is orphaned -- leaving a second daemon with its own clipboard watcher and
    its own listener on the sync port, which ``quit`` can never reach because
    that only ever finds the socket's current owner. Autostart firing while the
    user presses the shortcut is enough to hit it.

    The lock is taken before the ping and before the socket, so a loser exits
    having stolen nothing. If the lock file itself is unusable we fall through
    and start anyway: refusing to run at all would be a worse failure than the
    narrow race it guards.
    """
    global _lock_fd
    if _lock_fd is not None:
        return True
    try:
        fd = os.open(str(config.LOCK_PATH), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _lock_fd = fd
    # Record the holder; purely so `fuser`/a human can identify the winner.
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return True


def release_single_instance() -> None:
    """Drop the lock. The kernel does this on exit; this is for in-process
    callers (tests, and the upgrade path that backs out after the ping)."""
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None


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
        # Create the socket already 0600 rather than binding at the process
        # umask and widening the window with a follow-up chmod. Normally the
        # socket lives in XDG_RUNTIME_DIR (0700, owner-only), but it can fall
        # back to a shared /tmp; there, the bind-then-chmod gap let another
        # local user connect to a briefly world-accessible control socket. The
        # umask closes that gap at creation; the chmod stays as a backstop for
        # platforms that ignore umask on AF_UNIX binds.
        old_umask = os.umask(0o177)
        try:
            self._sock.bind(path)
        finally:
            os.umask(old_umask)
        os.chmod(path, 0o600)
        self._sock.listen(8)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        """Accept loop. Each connection is handled on its own short-lived thread.

        Serving them one at a time meant a slow command blocked every other one,
        and one command is deliberately slow: ``pair`` waits on a human at the
        other device for up to two minutes. During that wait, every copy's
        ``_current`` (which keeps the X11 selection in step with the clipboard)
        and every ``ping`` timed out, so pairing quietly broke capture. The
        handlers are already safe to run concurrently — UI work is marshalled
        through ``GLib.idle_add`` and both the X11 owner and the sync engine are
        lock-guarded."""
        assert self._sock is not None
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(30)
                data = conn.recv(4096).decode("utf-8", "replace").strip()
            except OSError:
                return
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
