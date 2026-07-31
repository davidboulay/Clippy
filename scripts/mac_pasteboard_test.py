#!/usr/bin/env python3
"""Self-test: the mac backend never hands back an image under the wrong MIME.

macOS puts ``public.tiff`` on the pasteboard for most copied images — Preview,
Safari's Copy Image, screenshots. ``list_types`` still advertises ``image/png``
for those (one image MIME keeps the portable capture path simple), so
``read_bytes`` owes callers actual PNG bytes. It used to return the TIFF rep
verbatim, and that mislabel escaped the Mac: a 1,590,590-byte uncompressed TIFF
(778x510, magic ``4d 4d 00 2a``) synced to Linux as an ``image/png`` entry,
rendered as a broken tile, and pasted as an empty image in VS Code and Claude
Desktop.

Linux CI can't exercise the real pasteboard — PyObjC isn't installed — so this
drives ``read_bytes`` against a fake pasteboard and asserts the contract that
matters: **whatever comes back for an image/png request is PNG, or nothing.**
Without AppKit the transcode can't run, so the TIFF-only case must come back
empty rather than mislabelled; that is the safe half of the same guarantee, and
it is exactly the branch a Linux run can prove.

Run:  PYTHONPATH=. python3 scripts/mac_pasteboard_test.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from clippy.backends.mac import _PNG, _TIFF, MacBackend  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
TIFF_BE = b"MM\x00*"
PNG_BYTES = PNG_MAGIC + b"pretend pixels"
TIFF_BYTES = TIFF_BE + b"\x00\x18\x37\xb8" + b"pretend pixels"


class FakePasteboard:
    """Only what read_bytes touches: dataForType_ returning bytes or None."""

    def __init__(self, reps):
        self._reps = reps

    def dataForType_(self, uti):
        return self._reps.get(uti)


def _backend(reps):
    # Bypass __init__ — it imports AppKit, which Linux CI doesn't have.
    b = MacBackend.__new__(MacBackend)
    b._pb = FakePasteboard(reps)
    return b


def main():
    failures = []

    def check(label, got, ok):
        if not ok:
            failures.append(f"{label}: got {got[:16]!r} ({len(got)} bytes)")

    # A real PNG rep is passed straight through.
    got = _backend({_PNG: PNG_BYTES}).read_bytes("image/png")
    check("png rep should pass through unchanged", got, got == PNG_BYTES)

    # PNG wins when both reps are present (no needless re-encode).
    got = _backend({_PNG: PNG_BYTES, _TIFF: TIFF_BYTES}).read_bytes("image/png")
    check("png rep should win over tiff", got, got == PNG_BYTES)

    # The regression: TIFF-only must never come back labelled PNG. With AppKit
    # absent the transcode fails, and empty is the required answer.
    got = _backend({_TIFF: TIFF_BYTES}).read_bytes("image/png")
    check("tiff-only must not be returned as png", got,
          got == b"" or got.startswith(PNG_MAGIC))
    check("tiff-only must never return tiff magic", got,
          not got.startswith(TIFF_BE))

    # An explicit tiff request may have the tiff rep — the label is honest there.
    got = _backend({_TIFF: TIFF_BYTES}).read_bytes("image/tiff")
    check("explicit tiff request should get tiff", got, got == TIFF_BYTES)

    # Nothing on the pasteboard is not an error.
    got = _backend({}).read_bytes("image/png")
    check("empty pasteboard should yield b''", got, got == b"")

    # The transcode helper is total: no exception, never echoes its input.
    # Fetched defensively so a missing helper is reported as a plain failure
    # rather than an AttributeError that masks the behavioural checks above.
    tiff_to_png = getattr(MacBackend, "_tiff_to_png", None)
    if tiff_to_png is None:
        failures.append("MacBackend._tiff_to_png is missing")
    else:
        got = tiff_to_png(TIFF_BYTES)
        check("_tiff_to_png must not echo tiff back", got, not got.startswith(TIFF_BE))
        got = tiff_to_png(b"")
        check("_tiff_to_png(b'') must be safe", got, got == b"")

    if failures:
        print("FAIL: mac backend returned bytes that don't match their MIME:")
        for f in failures:
            print("  " + f)
        return 1
    print("OK: read_bytes returns PNG or nothing for an image/png request")
    return 0


if __name__ == "__main__":
    sys.exit(main())
