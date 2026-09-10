#!/usr/bin/env python3
"""Self-test: capture ignores the image *file-flavor* echo, not file recovers.

Two different things get staged on the way back onto the clipboard:

  PASTE_DIR/<original name>       storage.paste_path() — a recovered file entry,
                                  copied out so the paste carries its real name
                                  instead of its sha256. A genuine user clip.
  FLAVOR_DIR/image.<ext>          _stage_image() — a throwaway backing file for
                                  an image that is also offered as a file. Its
                                  uri-list bounces straight back through
                                  wl-paste --watch and must not be filed.

While both lived in `DATA_DIR/paste`, `_is_own_staging` matched the whole
directory and dropped *both*. `capture_current()` then returned None, so
`_cmd_store` sent no `_broadcast` and never reached `sound.play()` — recovering
a file was silent and didn't sync, while the entry's created_at still moved
(storage.touch runs in the panel), so it looked like it had worked.

Run:  PYTHONPATH=. python3 scripts/capture_staging_test.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from clippy import capture, config, storage  # noqa: E402

failures = []


def check(label, ok, detail=""):
    if not ok:
        failures.append(f"{label}{(' — ' + detail) if detail else ''}")


def main():
    root = pathlib.Path(tempfile.mkdtemp())
    config.PASTE_DIR = root / "paste"
    config.FLAVOR_DIR = config.PASTE_DIR / ".image-flavor"
    config.FLAVOR_DIR.mkdir(parents=True, exist_ok=True)

    # The echo: still ignored, which is what the guard exists for.
    flavor = config.FLAVOR_DIR / "image.png"
    flavor.write_bytes(b"\x89PNG\r\n\x1a\n")
    check("image file-flavor must be ignored", capture._is_own_staging(str(flavor)))

    # The regression: a recovered file staged under its real name must NOT be.
    recovered = config.PASTE_DIR / "CleanShot 2026-07-31 at 22.08.51@2x.png"
    recovered.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    check("recovered file must be captured, not ignored",
          not capture._is_own_staging(str(recovered)),
          "this is the bug: no copy sound and no sync broadcast on a file recover")

    # A file the user copied from anywhere else is obviously not ours.
    outside = root / "elsewhere.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    check("unrelated path must not be ignored",
          not capture._is_own_staging(str(outside)))

    # Nested deeper inside the flavor dir still counts as ours.
    nested = config.FLAVOR_DIR / "sub"
    nested.mkdir(exist_ok=True)
    deep = nested / "image.png"
    deep.write_bytes(b"\x89PNG\r\n\x1a\n")
    check("anything under the flavor dir is ours", capture._is_own_staging(str(deep)))

    # A path that doesn't exist must not raise.
    try:
        capture._is_own_staging(str(config.PASTE_DIR / "nope.png"))
    except Exception as exc:
        check("missing path must not raise", False, repr(exc))

    # End to end across the two modules: whatever paste_path() hands the panel
    # has to survive capture's filter, or the recover goes silent again.
    blob = root / "deadbeef.png"
    blob.write_bytes(b"\x89PNG\r\n\x1a\n" + b"y" * 64)

    class Entry:
        image_path = str(blob)
        filename = "Report Q3.pdf"
        size = blob.stat().st_size

    staged = storage.paste_path(Entry())
    check("paste_path must stage somewhere", staged is not None)
    if staged:
        check("paste_path output must not be treated as own staging",
              not capture._is_own_staging(staged), staged)
        check("paste_path must keep the original filename",
              pathlib.Path(staged).name == "Report Q3.pdf", staged)

    # The two staging areas must stay distinct, or the guard collapses again.
    check("flavor dir must not equal paste dir",
          config.FLAVOR_DIR.resolve() != config.PASTE_DIR.resolve())

    # The *other* echo of a file recover, which no staging path can catch.
    #
    # Off cosmic-comp the Wayland selection holds one flavor, so an image file
    # is put back as raw bytes alone (backends/wayland.copy_file) — there is no
    # uri-list left for _is_own_staging to inspect. Filing those bytes would add
    # an 'image' row byte-identical to the 'file' row they came from, since
    # UNIQUE(kind, hash) makes the two different rows, and _cmd_store would
    # broadcast the duplicate straight back to the peer that sent the file.
    config.DATA_DIR = root
    config.DB_PATH = root / "history.db"
    config.IMAGE_DIR = root / "images"
    config.FILE_DIR = root / "files"
    config.IMAGE_DIR.mkdir(exist_ok=True)
    config.FILE_DIR.mkdir(exist_ok=True)

    shot = root / "CleanShot 2026-09-10 at 10.37.42@2x.png"
    png = b"\x89PNG\r\n\x1a\n" + b"screenshot" * 512
    shot.write_bytes(png)
    file_id = storage.add_file_from_path(str(shot), shot.name, "image/png")
    check("the synced screenshot lands as a file entry", file_id is not None)

    class FakeClipboard:
        """The selection as it looks right after copy_file put the bytes back."""
        @staticmethod
        def list_types():
            return ["image/png"]

        @staticmethod
        def read_file_paths(types):
            return []

        @staticmethod
        def pick_image_type(types):
            return "image/png"

        @staticmethod
        def pick_html_type(types):
            return None

        @staticmethod
        def pick_text_type(types):
            return None

        @staticmethod
        def read_bytes(mime):
            return png

    real_clipboard = capture.clipboard
    capture.clipboard = FakeClipboard
    try:
        echoed = capture.capture_current()
    finally:
        capture.clipboard = real_clipboard

    check("the bytes echo must not be filed as a new clip",
          echoed is None,
          "a second, identical tile appears on every image-file recover "
          "and gets broadcast back over sync")
    check("and history must still hold exactly the one entry",
          storage.count() == 1, f"count={storage.count()}")
    check("the file entry is the one that was found",
          storage.find_by_hash(
              __import__("hashlib").sha256(png).hexdigest(), "file") == file_id)

    if failures:
        print("FAIL: capture staging guard is wrong:")
        for f in failures:
            print("  " + f)
        return 1
    print("OK: flavor echo ignored, file recovers captured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
