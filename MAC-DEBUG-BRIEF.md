# Clippy — macOS debugging brief (for a Claude session running ON the Mac)

You are picking up a feature that was built on a Linux machine where **macOS code
could not be run or tested**. Your job: get the **macOS menubar app working and
debugged on this Mac**, where you can actually build, run, and observe it. This
brief is self-contained.

## What Clippy is
A clipboard-history + **encrypted LAN clipboard sync** app.
- **Repo:** `github.com/davidboulay/Clippy`. **Work branch: `lan-sync`** (NOT `main`).
- Linux = the full GTK app (panel/tray/settings) + sync. macOS = a **menubar app**
  (no panel/history UI) that runs the same portable sync core + a macOS clipboard
  backend. Mobile is out of scope.
- The Linux side is **done and verified** (text + file/media sync, pairing, etc.).
  A Linux peer named **`david-pop-os`** is paired and on the LAN.

## Get the code (on the Mac)
```bash
git clone -b lan-sync https://github.com/davidboulay/Clippy.git   # or: cd Clippy && git pull
cd Clippy
```

## Build / run / iterate (the loop you'll use constantly)
```bash
rm -rf build dist/Clippy.app
./packaging/macos/build-app.sh        # makes a venv, installs deps + py2app, bundles Clippy.app
open dist/Clippy.app                   # or run unbundled for fast iteration (below)
```
- **Fast iteration without py2app** (recommended while debugging): run the menubar
  app straight from source — no rebuild each change:
  ```bash
  python3 -m venv /tmp/clippy-venv && source /tmp/clippy-venv/bin/activate
  pip install pynacl zeroconf rumps pyobjc-framework-Cocoa
  python3 -c "from clippy.mac_app import run; run()"   # run from the repo root
  ```
  This gives you stdout/stderr in the terminal (crucial — the bundled .app hides it).
- The app is unsigned → Gatekeeper says "unidentified developer". For dev just run
  it; the user has declined paid Apple Developer signing. `build-app.sh` already
  ad-hoc signs and supports `CLIPPY_SIGN_IDENTITY`/`CLIPPY_NOTARY_PROFILE` if ever wanted.

## THE CURRENT BUG (top priority) — ✅ RESOLVED & VERIFIED (macOS 26.5.1, 2026-06-07)
Was: **copying a file in Finder synced the *filename text*, not the file** (and an
image file produced a ~4099 KB "ghost PNG" — macOS's TIFF *preview*).

**Status: fixed at HEAD and verified end-to-end both directions** with `david-pop-os`
(27.8 MB MP4 with spaces in the name, JPG, PNG, GIF all sync as real files).

Non-obvious finding for future debugging: on macOS 26,
`pasteboardItem.stringForType_("public.file-url")` returns an **opaque file-reference
URL** (`file:///.file/id=…`) that fails `os.path.isfile`, so the file-url string branch
yields nothing. The load-bearing branch is **`readObjectsForClasses_([NSURL], None)`**
→ `u.path()`, which resolves the opaque ref to the real path. Keep that branch.

Also fixed here: `MacBackend.list_types()` now reports `text/uri-list` for file copies.
Real Finder copies always include a preview+filename text (so capture proceeded), but a
*bare* `public.file-url` copy made `list_types()` empty and `capture_current()` bailed
at `if not types` before `read_file_paths`. Diagnostic: `scripts/mac_pb_probe.py`
(copy a file, run it, see pasteboard types + what capture stores). There's also a debug
menu item **"Clipboard types (debug)"**.

How to test the backend directly (fast, no app):
```python
from clippy.backends import get_backend
be = get_backend()
print(list(be._pb.types()))                 # what macOS actually offers for a copied file
print(be.read_file_paths([]))               # should be the real path(s)
```
Copy a file in Finder, run that, and see what types appear. Fix `read_file_paths`
to extract the path from whatever type macOS provides on this OS version.

Capture flow (`clippy/capture.py`): checks **files first** (`read_file_paths`) →
`storage.add_file_from_path(real bytes)`; else image *data* (`add_image`); else text.
Receiver injects files via `clipboard.copy_file(path)` (saved into `~/.local/share/clippy/received/`).

## Other macOS items to confirm work (built but never tested on a Mac)
1. **Menubar icon** — uses the SF Symbol `"paperclip"` as a template image
   (`mac_app.py: _fix_retina_icon`, set 1s after launch via a one-shot rumps.Timer).
   Confirm it shows crisp and theme-adaptive.
2. **App icon** — `packaging/macos/clippy.icns` (regenerated at build via `sips`+`iconutil`).
3. **Settings window** (`clippy/mac_settings.py`, PyObjC/AppKit) — version, check-updates,
   auto-update checkbox, pairing (show/enter code + peers), **Start-at-login** (default on,
   installs a LaunchAgent via `mac_app.set_login_item`). Earlier crash fixed: PyObjC turns
   method-name underscores into selector colons — **never use internal underscores in
   NSObject method names** (use camelCase; only a trailing `_` per arg).
4. **Sleep/wake** — `mac_app._install_wake_observer` → `SyncEngine.restart_network()` on
   `NSWorkspaceDidWakeNotification`. Confirm sync resumes after lid close/open.
5. **Firewall** — the app listens on TCP 47823 (to receive); macOS firewall prompts once.
   A one-time hint alert is shown on first run.
6. **Progress** — sends >5 MiB show a menubar `⬆ %` (only when transferring to a peer).

## Architecture / key files
- `clippy/sync.py` — portable engine: X25519 identity, zeroconf mDNS discovery,
  code-authenticated pairing (SAS HMAC), NaCl-Box encrypted TCP. Text = inline JSON
  frame; media = JSON manifest + streamed ~1 MiB encrypted chunks from disk; seen-hash
  LRU for loop prevention. `broadcast_id(id)`, `restart_network()`, `enter_pairing()`,
  `join_pairing(code[, host])`, `status()`.
- `clippy/backends/mac.py` — NSPasteboard backend (read/write/`changeCount` poll watch,
  `read_file_paths`, `copy_file`, `copy_image`).  `clipboard.py` dispatches to it.
- `clippy/capture.py` — `capture_current()` → stores + returns the new id.
- `clippy/mac_app.py` — the rumps menubar app (entry: `packaging/macos/clippy_mac_main.py`).
- `clippy/mac_settings.py` — AppKit Settings window.
- `clippy/{config,settings,storage}.py` — paths, prefs, SQLite. Data persists in
  `~/.local/share/clippy/` (keys/peers/device-id) + `~/.config/clippy/` (settings) —
  independent of the .app, so pairing survives rebuilds.

## Pairing (to test sync against the Linux box)
- Both on the same Wi-Fi. Menubar → **Show pairing code**, then on Linux run
  `clippy pair <code>` (or Linux Settings → Sync → Enter code). Or the reverse:
  Linux `clippy pair` shows a code → Mac menubar **Enter code…**.
- mDNS-free fallback exists: `clippy pair <code> <ip>`.
- The menubar **"Sync status"** line shows "Paired: N (M online)".

## Verified on Linux (don't re-litigate)
Discovery, pairing (+ wrong-code reject), encrypted text + **media/file streaming with
integrity check**, the 512 MiB–2 GiB size cap, and the >5 MiB progress callback all pass
in `scripts/sync_selftest.py` (`PYTHONPATH=. python3 scripts/sync_selftest.py`). The bugs
are macOS-pasteboard-specific, which is why you're on the Mac.

## Test plan on the Mac
1. Fix `read_file_paths` using the debug output; confirm copying an **MP4** and a
   **WEBP/JPG file** in Finder yields the real path.
2. Run the app, pair with `david-pop-os`, and confirm: text both ways; an MP4/file copied
   on the Mac arrives on Linux as the **actual file** (it pastes back as the file); a
   >5 MiB file shows the progress indicator.
3. Confirm icon (menubar + app), Settings window opens, Start-at-login, and sleep/wake.
4. Keep changes on `lan-sync`; commit + push so the Linux side stays in sync. End commit
   messages with the project's Co-Authored-By trailer if used.

## Gotchas
- PyObjC selector naming (underscores → colons) — see Settings note above.
- `rumps` loads icons at 1×; retina needs `initByReferencingFile_`/SF Symbol + explicit size.
- The bundled `.app` swallows stdout — run from source for logs while debugging.
- Don't hold huge files in RAM — sync streams from disk; keep it that way.
