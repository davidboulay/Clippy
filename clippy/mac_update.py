"""macOS end-to-end self-update.

The download itself is `updates.download_dmg`. macOS can't overwrite a running
`.app` in place, so this mounts the downloaded `.dmg`, stages the new
`Clippy.app`, then hands off to a small detached helper that waits for this
process to quit, swaps the bundle (escalating to an admin prompt only if the
install location isn't writable), strips the download's quarantine, and
relaunches the new version. The caller quits the app right after `install()`
returns so the helper can proceed.

macOS-only; nothing here is imported on Linux.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Optional, Tuple


def bundle_path() -> Optional[str]:
    """Path of the running .app bundle, or None when run from source (dev)."""
    try:
        from Foundation import NSBundle
        p = str(NSBundle.mainBundle().bundlePath())
        return p if p.endswith(".app") and os.path.isdir(p) else None
    except Exception:
        return None


def _mount_and_stage(dmg_path: str) -> str:
    """Mount the .dmg, copy the .app out to a staging dir, detach. Returns the
    staged app path (a private copy that survives unmount)."""
    mount = tempfile.mkdtemp(prefix="clippy-mnt-")
    subprocess.run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly",
                    "-mountpoint", mount, dmg_path],
                   check=True, capture_output=True)
    try:
        app = next((os.path.join(mount, n) for n in os.listdir(mount)
                    if n.endswith(".app")), None)
        if not app:
            raise RuntimeError("no .app found inside the .dmg")
        stage = tempfile.mkdtemp(prefix="clippy-stage-")
        dest = os.path.join(stage, os.path.basename(app))
        subprocess.run(["/usr/bin/ditto", app, dest], check=True, capture_output=True)
        return dest
    finally:
        subprocess.run(["/usr/bin/hdiutil", "detach", mount, "-force"],
                       capture_output=True)


# Waits for the old app (pid $1) to quit, replaces the bundle ($3) with the
# staged copy ($2), clears quarantine, relaunches. Tries unprivileged first and
# only prompts for admin if the destination isn't writable.
_HELPER = r'''#!/bin/bash
pid="$1"; src="$2"; dest="$3"
case "$dest" in *.app) ;; *) exit 1 ;; esac        # guard: never rm a non-.app
for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.3; done
if ! ( /bin/rm -rf "$dest" && /usr/bin/ditto "$src" "$dest" ) 2>/dev/null; then
    /usr/bin/osascript -e "do shell script \"/bin/rm -rf '$dest' && /usr/bin/ditto '$src' '$dest'\" with administrator privileges" || exit 1
fi
/usr/bin/xattr -dr com.apple.quarantine "$dest" 2>/dev/null || true
/bin/rm -rf "$(dirname "$src")" 2>/dev/null || true
/usr/bin/open "$dest"
'''


def install(dmg_path: str) -> Tuple[bool, str]:
    """Stage the new app and spawn the swap-and-relaunch helper. On success the
    caller MUST quit the app shortly after (the helper is waiting on our PID).
    Returns (ok, message); ok=False means fall back to opening the .dmg."""
    dest = bundle_path()
    if not dest:
        return False, "not running as an installed .app"
    try:
        staged = _mount_and_stage(dmg_path)
    except Exception as exc:
        return False, f"could not stage update: {exc}"
    fd, script = tempfile.mkstemp(prefix="clippy-update-", suffix=".sh")
    try:
        os.write(fd, _HELPER.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(script, 0o755)
    try:
        subprocess.Popen(["/bin/bash", script, str(os.getpid()), staged, dest],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return False, f"could not launch updater: {exc}"
    return True, "updating"
