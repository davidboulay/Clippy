#!/usr/bin/env python3
"""Self-test: capture-time X11 takeover stays opt-in and behind its rate cap.

Taking the X11 selection after every image copy is how an XWayland app (Claude
Desktop runs --ozone-platform=x11) could paste an image copied on Wayland — it
gets nothing today, because Xwayland exports text selections to X11 but never
image ones. It is also the change that was shipped in 1.4.21 and reverted in
1.4.22 for leaving *both* channels dead ~35s after a copy.

So the switch matters more than most: on by accident, an image copy can take the
clipboard down system-wide. This asserts it defaults off, that the off path
touches nothing, that the on path publishes, and that our own echo is still
recognised before any of it runs.

Run:  PYTHONPATH=. python3 scripts/x11_takeover_gate_test.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from clippy import clipboard, daemon, settings, storage, x11clip  # noqa: E402

failures = []


def check(label, ok, detail=""):
    if not ok:
        failures.append(f"{label}{(' — ' + detail) if detail else ''}")


class Entry:
    is_image = True
    is_file = False
    mime = "image/png"
    text = None
    hash = "d" * 64

    def __init__(self, path):
        self.image_path = str(path)


def run_capture(*, takeover, published=None):
    """Call _current_clipboard with everything external stubbed; report calls."""
    calls = {"copy_image": [], "released": []}
    blob = pathlib.Path(tempfile.mkdtemp()) / "img.png"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n" + b"z" * 16)

    real = (storage.get, clipboard.copy_image, x11clip.published_digest,
            x11clip.release_unless_ours, settings.get)
    try:
        storage.get = lambda _id: Entry(blob)
        clipboard.copy_image = lambda data, mime: calls["copy_image"].append(mime)
        x11clip.published_digest = lambda: published
        x11clip.release_unless_ours = lambda d: calls["released"].append(d)
        _real_get = real[4]
        settings.get = (lambda k: takeover if k == "x11_image_takeover"
                        else _real_get(k))
        daemon._MIRROR_TIMES[:] = []          # fresh rate-cap window
        daemon._current_clipboard("1")
    finally:
        (storage.get, clipboard.copy_image, x11clip.published_digest,
         x11clip.release_unless_ours, settings.get) = real
    return calls


def main():
    # 1. Ships off. Anything else is a system-wide clipboard risk.
    check("x11_image_takeover must default to False",
          settings.DEFAULTS.get("x11_image_takeover") is False,
          repr(settings.DEFAULTS.get("x11_image_takeover")))

    # 2. Off: never publish; hand the selection back instead.
    calls = run_capture(takeover=False)
    check("off must not publish", not calls["copy_image"], str(calls["copy_image"]))
    check("off must still release the selection", calls["released"] == ["d" * 64])

    # 3. On: publish the image, and do NOT release afterwards (releasing would
    #    undo the takeover we just performed).
    calls = run_capture(takeover=True)
    check("on must publish the image", calls["copy_image"] == ["image/png"],
          str(calls["copy_image"]))
    check("on must not release after publishing", not calls["released"],
          str(calls["released"]))

    # 4. Our own publish echoing back must short-circuit before either path —
    #    otherwise every publish re-triggers a publish.
    calls = run_capture(takeover=True, published="d" * 64)
    check("our own echo must not republish", not calls["copy_image"])
    check("our own echo must not release", not calls["released"])

    # 5. The rate cap is the backstop if a clip ever fails to round-trip
    #    byte-for-byte: bounded republishes, not a runaway.
    daemon._MIRROR_TIMES[:] = []
    allowed = sum(1 for _ in range(daemon._MIRROR_MAX + 5) if daemon._mirror_allowed())
    check("rate cap must bound publishes", allowed == daemon._MIRROR_MAX,
          f"allowed {allowed}, cap {daemon._MIRROR_MAX}")

    if failures:
        print("FAIL: capture-time X11 takeover gate is wrong:")
        for f in failures:
            print("  " + f)
        return 1
    print("OK: takeover is opt-in, echo-safe, and rate-capped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
