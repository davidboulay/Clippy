"""Command-line entry point for Clippy.

  daemon            run the background service (watcher + tray + panel + IPC)
  toggle|show|hide  control the panel of a running daemon
  settings          open the settings window
  status            report whether the daemon is up and history size
  clear [--all]     wipe history (``--all`` includes pinned items)
  _store            internal: invoked by ``wl-paste --watch`` on each change
  setup-shortcut    print how to bind a global shortcut
  install-autostart write an XDG autostart entry for the daemon
"""
from __future__ import annotations

import argparse
import sys

_SEND_COMMANDS = {
    "toggle": "toggle",
    "show": "show",
    "hide": "hide",
    "quit": "quit",
    "settings": "open-settings",
}
# Commands that should transparently start the daemon if it isn't running, so a
# fresh install "just works" when launched from the app menu / a shortcut.
_AUTOSTART_COMMANDS = {"toggle", "show", "settings"}


def _ensure_daemon() -> bool:
    """Start the background daemon detached and wait for it to come up."""
    import subprocess
    import time

    from . import ipc

    try:
        subprocess.Popen(
            [sys.executable, "-m", "clippy", "daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # survive this short-lived process
        )
    except OSError:
        return False
    for _ in range(60):  # up to ~6s for the IPC socket to appear
        if ipc.daemon_running():
            return True
        time.sleep(0.1)
    return False


def _cmd_send(command: str) -> int:
    from . import ipc

    if not ipc.daemon_running():
        if command in _AUTOSTART_COMMANDS:
            if not _ensure_daemon():
                print("clippy: could not start the daemon.", file=sys.stderr)
                return 1
        else:
            print("clippy: daemon is not running.", file=sys.stderr)
            return 1
    ipc.send(_SEND_COMMANDS[command])
    return 0


def _cmd_store() -> int:
    """Internal hook for wl-paste --watch."""
    try:
        sys.stdin.buffer.read()  # drain so wl-paste never blocks
    except (OSError, ValueError):
        pass
    from . import ipc
    from .capture import capture_current

    entry_id = capture_current()
    if entry_id:
        ipc.send("refresh")
        ipc.send(f"_broadcast {entry_id}")   # broadcast exactly this item
    return 0


def _cmd_pair(code: str, host: str = "") -> int:
    import json
    from . import ipc
    if not ipc.daemon_running():
        print("clippy: daemon is not running.", file=sys.stderr)
        return 1
    reply = ipc.send(f"pair {code} {host}".strip(), timeout=130)
    if reply is None:
        print("clippy: no response from daemon.", file=sys.stderr)
        return 1
    if reply.startswith("err"):
        print(f"clippy: {reply}", file=sys.stderr)
        return 1
    try:
        data = json.loads(reply)
    except ValueError:
        print(reply)
        return 0
    if "code" in data:
        print(f"Pairing code: {data['code']}")
        print("On the other device run:  clippy pair " + data["code"])
        print("(valid for 2 minutes)")
        return 0
    if data.get("ok"):
        print(f"Paired with {data.get('name', 'device')}.")
        return 0
    print(f"clippy: pairing failed — {data.get('error', 'unknown error')}", file=sys.stderr)
    return 1


def _cmd_peers() -> int:
    import json
    from . import ipc
    reply = ipc.send("peers")
    if reply is None or reply.startswith("err"):
        print(f"clippy: {reply or 'daemon not running'}", file=sys.stderr)
        return 1
    data = json.loads(reply)
    print(f"This device: {data['device']}  ({data['fingerprint']})")
    peers = data.get("peers", [])
    if not peers:
        print("No paired devices. Run 'clippy pair' to add one.")
    for p in peers:
        print(f"  {'●' if p['online'] else '○'} {p['name']}")
    return 0


def _cmd_status() -> int:
    from . import ipc, storage

    running = ipc.daemon_running()
    entries = storage.list_entries()
    pinned = sum(1 for e in entries if e.pinned)
    print(f"daemon:  {'running' if running else 'stopped'}")
    print(f"history: {len(entries)} items ({pinned} pinned)")
    return 0


def _cmd_clear(include_pinned: bool) -> int:
    from . import ipc, storage

    storage.clear(include_pinned=include_pinned)
    if ipc.daemon_running():
        ipc.send("refresh")
    print("clippy: history cleared" + (" (including pinned)" if include_pinned else ""))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="clippy", description="Clipboard history panel for Wayland/COSMIC."
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("daemon", help="run the background service")
    sub.add_parser("toggle", help="show/hide the panel")
    sub.add_parser("show", help="show the panel")
    sub.add_parser("hide", help="hide the panel")
    sub.add_parser("settings", help="open the settings window")
    sub.add_parser("quit", help="stop the running daemon")
    sub.add_parser("status", help="report daemon and history status")
    sub.add_parser("_store")  # internal: wl-paste --watch hook
    sub.add_parser("setup-shortcut", help="how to bind a global shortcut")
    sub.add_parser("install-autostart", help="autostart the daemon on login")
    sub.add_parser("install-icons", help="install the tray/app icon into the theme")
    sub.add_parser("install-desktop", help="add Clippy to the application list")

    pair_p = sub.add_parser("pair", help="pair this device with another for clipboard sync")
    pair_p.add_argument("code", nargs="?", default="",
                        help="the code shown on the other device (omit to show one here)")
    pair_p.add_argument("host", nargs="?", default="",
                        help="other device's IP address (use if mDNS discovery fails)")
    sub.add_parser("peers", help="list paired sync devices")

    clear_p = sub.add_parser("clear", help="wipe clipboard history")
    clear_p.add_argument("--all", action="store_true", help="also remove pinned items")

    args = parser.parse_args(argv)

    if args.command == "daemon":
        from .daemon import run_daemon
        return run_daemon()
    if args.command in _SEND_COMMANDS:
        return _cmd_send(args.command)
    if args.command == "_store":
        return _cmd_store()
    if args.command == "status":
        return _cmd_status()
    if args.command == "pair":
        return _cmd_pair(args.code, args.host)
    if args.command == "peers":
        return _cmd_peers()
    if args.command == "clear":
        return _cmd_clear(args.all)
    if args.command == "setup-shortcut":
        from .setup import print_shortcut_instructions
        return print_shortcut_instructions()
    if args.command == "install-autostart":
        from .setup import install_autostart
        return install_autostart()
    if args.command == "install-icons":
        from .setup import install_icons
        ok = install_icons()
        print("clippy: icons installed" if ok else "clippy: could not install icons")
        return 0 if ok else 1
    if args.command == "install-desktop":
        from .setup import install_desktop_entry
        return install_desktop_entry()

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
