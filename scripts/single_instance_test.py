#!/usr/bin/env python3
"""Daemon single-instance lock: only one daemon may own the runtime.

Runs headless against a throwaway XDG_RUNTIME_DIR — no compositor, no GTK, and
the real daemon is never started.

The bug this pins down: `run_daemon` used to guard with `ipc.daemon_running()`
alone, which asks over the socket, and `Server.start` then unlinks whatever is
there and binds afresh. Two daemons starting together both got no answer, both
bound, and the loser's socket was orphaned — leaving a second daemon with its
own clipboard watcher and its own listener on the sync port, unreachable by
`quit` because that only ever finds the socket's current owner. Autostart
firing while the user presses the shortcut was enough to hit it.

What it checks:

* a second acquirer in a separate process is refused while the first holds on;
* the lock is released when the holder dies *without* unlocking, i.e. the crash
  path leaves nothing stale — the guarantee that makes a lock file safe here;
* re-acquiring in the same process is idempotent, so the daemon never
  double-opens a descriptor it will leak;
* an unusable lock path fails open rather than refusing to start the daemon.

Run:  PYTHONPATH=. python3 scripts/single_instance_test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILED = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        FAILED.append(label)


def fresh_runtime():
    """A temp XDG_RUNTIME_DIR, with clippy.config re-imported to bind to it."""
    tmp = tempfile.mkdtemp(prefix="clippy-lock-test-")
    os.environ["XDG_RUNTIME_DIR"] = tmp
    for mod in [m for m in sys.modules if m.startswith("clippy")]:
        del sys.modules[mod]
    return Path(tmp)


def _child(runtime: Path, body: str) -> subprocess.CompletedProcess:
    """Run `body` in a separate interpreter sharing this runtime dir.

    A second *process* is the only honest way to test flock: within one process
    the kernel treats a re-lock of the same file as the same owner and grants
    it, so a same-process 'contender' would pass against a broken lock too.
    """
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(REPO)!r})
        os.environ["XDG_RUNTIME_DIR"] = {str(runtime)!r}
        from clippy import ipc
        {body}
    """)
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=30)


# --- contention: the second daemon loses ----------------------------------
def test_second_acquirer_refused():
    print("contention")
    runtime = fresh_runtime()
    from clippy import ipc

    check("first acquirer wins", ipc.acquire_single_instance(), True)
    out = _child(runtime, "print(ipc.acquire_single_instance())")
    check("second acquirer refused", out.stdout.strip(), "False")

    # The pid in the file names the winner, so a human can tell who holds it.
    check("lock file records holder", (runtime / "clippy.lock").read_text().strip(),
          str(os.getpid()))

    ipc.release_single_instance()
    out = _child(runtime, "print(ipc.acquire_single_instance())")
    check("acquirable once released", out.stdout.strip(), "True")


# --- the crash path: no stale lock survives the holder --------------------
def test_lock_freed_when_holder_dies():
    print("holder dies")
    runtime = fresh_runtime()
    from clippy import ipc

    # Acquire and exit hard, never unlocking — what a SIGKILL or a segfault
    # leaves behind. If the kernel did not drop it, every later start would be
    # refused and the daemon would be permanently unstartable.
    out = _child(runtime, "ipc.acquire_single_instance()\n        os._exit(0)")
    check("holder exited", out.returncode, 0)
    check("lock free after abnormal exit", ipc.acquire_single_instance(), True)
    ipc.release_single_instance()


# --- idempotence: the daemon must not leak a descriptor -------------------
def test_reacquire_is_idempotent():
    print("idempotence")
    fresh_runtime()
    from clippy import ipc

    check("first acquire", ipc.acquire_single_instance(), True)
    fd = ipc._lock_fd
    check("second acquire", ipc.acquire_single_instance(), True)
    check("same descriptor reused", ipc._lock_fd, fd)
    ipc.release_single_instance()
    check("release clears it", ipc._lock_fd, None)
    check("release is safe twice", ipc.release_single_instance(), None)


# --- an unusable lock path must not block startup -------------------------
def test_unusable_lock_path_fails_open():
    print("unusable path")
    runtime = fresh_runtime()
    from clippy import config, ipc

    # A directory where the lock file should be: os.open fails with EISDIR.
    # Starting unserialized beats not starting at all.
    (runtime / "clippy.lock").mkdir()
    check("starts anyway", ipc.acquire_single_instance(), True)
    check("but holds nothing", ipc._lock_fd, None)
    check("path was the directory", config.LOCK_PATH, runtime / "clippy.lock")


def main() -> int:
    real_runtime = os.environ.get("XDG_RUNTIME_DIR")
    try:
        test_second_acquirer_refused()
        test_lock_freed_when_holder_dies()
        test_reacquire_is_idempotent()
        test_unusable_lock_path_fails_open()
    finally:
        if real_runtime:
            os.environ["XDG_RUNTIME_DIR"] = real_runtime
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("single instance: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
