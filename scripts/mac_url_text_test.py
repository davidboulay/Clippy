#!/usr/bin/env python3
"""Self-test: a link copied on an iPhone is captured as text on the Mac.

Universal Clipboard delivers a link copied on iOS as ``public.url`` only — no
``public.utf8-plain-text``. Every app pastes it, but ``list_types`` used to
report nothing for it, so ``capture_current`` bailed at ``if not types`` and
the link never reached history (seen with a GitHub issue link, 2026-11-12).

A web URL with no text rep must now surface as ``text/plain`` and read back as
the URL. A ``file://`` URL must not: file copies have their own path, and a
text entry for them would duplicate the file.

Run:  PYTHONPATH=. python3 scripts/mac_url_text_test.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from clippy.backends.mac import _FILE_URL, _TEXT, MacBackend  # noqa: E402

_URL = "public.url"
LINK = "https://github.com/davidboulay/CosmicShot/issues/7"


class FakePasteboard:
    def __init__(self, reps):
        self._reps = reps

    def types(self):
        return list(self._reps)

    def stringForType_(self, uti):
        return self._reps.get(uti)


def _backend(reps):
    # Bypass __init__ — it imports AppKit, which Linux CI doesn't have.
    b = MacBackend.__new__(MacBackend)
    b._pb = FakePasteboard(reps)
    return b


def main():
    failures = []

    def check(label, ok):
        if not ok:
            failures.append(label)

    # The iPhone case: URL only.
    b = _backend({_URL: LINK})
    types = b.list_types()
    check(f"url-only should list text/plain, got {types}", "text/plain" in types)
    check(f"url-only should read back the link, got {b.read_text()!r}",
          b.read_text() == LINK)
    check(f"url-only should read back the link for text/plain, got "
          f"{b.read_text('text/plain')!r}", b.read_text("text/plain") == LINK)

    # Real text wins over the URL rep (Safari's Copy Link puts both).
    b = _backend({_URL: LINK, _TEXT: "label text"})
    check("text rep should win over url", b.read_text() == "label text")

    # A file URL is a file copy, never a text entry.
    file_url = "file:///Users/me/a.txt"
    b = _backend({_URL: file_url, _FILE_URL: file_url})
    types = b.list_types()
    check(f"file url must not be listed as text, got {types}", "text/plain" not in types)
    check("file url must not read back as text", b.read_text() == "")

    # Empty pasteboard stays empty.
    check("empty pasteboard lists nothing", _backend({}).list_types() == [])

    if failures:
        print("FAIL: URL-only clipboard handling:")
        for f in failures:
            print("  " + f)
        return 1
    print("OK: a URL-only clipboard is captured as text; file URLs are not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
