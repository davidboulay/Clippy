"""Command-line entry point for Clippy.

  daemon            run the background service (watcher + tray + panel + IPC)
  toggle|show|hide  control the panel of a running daemon
  settings          open the settings window
  status            report whether the daemon is up and history size
  types             show what the clipboard is offering right now (diagnostic)
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
        # Every clipboard change has to reach the daemon so it can keep the X11
        # selection in step: mirror an image there (Xwayland never exports image
        # selections, so XWayland apps otherwise can't paste it) and hand the
        # selection back for anything else.
        #
        # _current is the one that matters for correctness — a dropped one
        # leaves the X11 owner serving a clip the user has already replaced —
        # and the daemon's IPC server is single-threaded, so a long-running
        # query (pairing waits up to two minutes) can make the first attempt
        # time out. Retry it once; the others are cosmetic and can be missed.
        from . import debuglog
        if not _send_current(entry_id):
            debuglog.log("store.current_undelivered", id=entry_id)
        ipc.send("refresh")
        ipc.send(f"_broadcast {entry_id}")   # broadcast exactly this item
    return 0


def _send_current(entry_id: int) -> bool:
    import time
    from . import ipc
    for attempt in (1, 2):
        reply = ipc.send(f"_current {entry_id}")
        if reply is not None and not reply.startswith("err"):
            return True
        if attempt == 1:
            time.sleep(0.5)
    return False


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


def _cmd_types() -> int:
    """Dump what the clipboard is offering right now, on both channels.

    The question behind most clipboard bugs is "what did that app actually put
    there?", and answering it otherwise means a debugger or a guess. Read-only:
    it never sets or clears a selection."""
    import os
    import subprocess

    from . import capture, clipboard

    types = clipboard.list_types()
    if not types:
        print("Wayland selection: (empty)")
    else:
        print("Wayland selection offers:")
        for t in types:
            print(f"  {t}")
        print()
        img = clipboard.pick_image_type(types)
        txt = clipboard.pick_text_type(types)
        html = clipboard.pick_html_type(types)
        files = clipboard.read_file_paths(types)
        print(f"picked image: {img or '-'}")
        print(f"picked text:  {txt or '-'}")
        print(f"picked html:  {html or '-'}")
        print(f"file paths:   {', '.join(files) if files else '-'}")
        if img:
            preview = clipboard.read_bytes(img)
            sniffed = capture.sniff_image_mime(preview) if preview else None
            print(f"image bytes:  {len(preview)} (looks like {sniffed or 'unknown'})")
            if not files:
                verdict = ("text (a render of richer content)"
                           if capture._image_is_a_preview(types) else "image")
                print(f"would capture as: {verdict}")
        elif txt:
            body = clipboard.read_text(txt if "/" in txt else None)
            print(f"text ({len(body)} chars): {body[:200]!r}")

    # The X11 side is what XWayland apps (Electron, Claude Desktop) actually
    # read, and it can be dead while the Wayland side looks perfectly healthy.
    print()
    if not os.environ.get("DISPLAY"):
        print("X11: no DISPLAY")
        return 0
    owner = _x11_owner()
    print(f"X11 CLIPBOARD owner: {owner or 'unknown'}")
    try:
        out = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"],
            capture_output=True, text=True, timeout=3,
        )
        targets = out.stdout.strip()
        print("X11 selection targets:" if targets else "X11 selection: (none served)")
        for line in targets.splitlines():
            print(f"  {line.strip()}")
    except (subprocess.SubprocessError, OSError):
        print("X11: xclip unavailable")
    # Read this section carefully, because it lies by omission. cosmic-comp
    # exports a Wayland selection to X11 only while an X11 window holds
    # keyboard focus, and gates reads of a proxied selection the same way — so
    # an owner of 0x0 and "none served" is the *normal* state whenever the
    # focused app is a Wayland one, which includes the terminal this just ran
    # in. It means "not exported yet", not "broken". The way to see the real
    # X11 state is to read it from a focused X11 window.
    print("  (note: X11 export happens on focus into an X11 window — an empty\n"
          "   result here is expected while a Wayland app has focus)")
    return 0


def _x11_owner():
    """The X11 CLIPBOARD owner window id, via libX11 — ``xdotool`` can't query
    selection owners and ``xlsclients`` only sees clients that own a window."""
    import ctypes
    import ctypes.util
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("X11"))
        lib.XOpenDisplay.restype = ctypes.c_void_p
        display = lib.XOpenDisplay(None)
        if not display:
            return None
        atom = lib.XInternAtom(ctypes.c_void_p(display), b"CLIPBOARD", 0)
        return hex(lib.XGetSelectionOwner(ctypes.c_void_p(display), atom))
    except Exception:
        return None


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
    sub.add_parser("types", help="show what the clipboard is offering right now")
    sub.add_parser("_store")  # internal: wl-paste --watch hook
    sub.add_parser("_x11clip")  # internal: persistent X11 (XWayland) clipboard owner
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
    if args.command == "_x11clip":
        from .x11clip import run
        return run()
    if args.command == "status":
        return _cmd_status()
    if args.command == "types":
        return _cmd_types()
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
