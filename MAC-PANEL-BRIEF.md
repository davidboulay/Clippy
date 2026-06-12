# Brief: Full Clippy panel/history UI on macOS

**Audience:** a Claude Code session running **on the Mac** (`~/Clippy`), with a fast local
build/run loop. **Author:** the Linux session, which has full codebase context but cannot
build or see macOS UI.

**Goal:** bring the *complete* Clippy experience to macOS — not just the menubar sync peer it
is today, but the **history panel** (tile strip, search, pin/delete, click-to-copy) opened by
a **global hotkey**, matching the Linux app's behavior.

---

## 0. Ground rules

- **You (the Mac session) own all `clippy/mac_*.py` files and the macOS packaging.** The Linux
  session will avoid touching those to prevent git conflicts. If you need to change shared core
  (`storage.py`, `sync.py`, `capture.py`, `clipboard.py`, `config.py`, `settings.py`), keep
  changes additive and platform-guarded so Linux is unaffected.
- **Branch:** work on `main` (the project's convention) or a `mac-panel` branch and PR — your
  call. Commit messages end with the project's `Co-Authored-By` trailer.
- **Build/run loop:** `./packaging/macos/build-app.sh` → `dist/Clippy.app`; or for fast
  iteration run the module directly in a venv with the deps (see §8) — no rebuild needed to test
  Python UI changes.
- This is a **sizable project**. Land it in milestones (§9), verifying each on-screen before moving on.

---

## 1. What already exists and is 100% reusable (do NOT rewrite)

The entire non-UI core is cross-platform and already runs in the macOS headless daemon today.
Bind the panel directly to these — they need no changes:

**`clippy/storage.py`** (SQLite history; `Entry` dataclass with `id, kind, text, html, mime,
image_path, filename, pinned, size, created_at`, and props `is_image/is_file/has_formatting`):
- `list_entries(query="", limit=config.MAX_HISTORY, pinned=None) -> List[Entry]` — newest-first,
  pinned-first; `query` filters text entries by substring. **This is your panel's data source.**
- `get(id)`, `delete(id)`, `toggle_pin(id) -> bool`, `clear(include_pinned=False)`, `count()`.

**`clippy/clipboard.py`** (dispatches to `backends/mac.py` = NSPasteboard):
- `copy_text(str)`, `copy_html(str)`, `copy_image(bytes, mime)`, `copy_file(path)` — use these to
  put a selected tile back on the clipboard (this is the "paste" action; like Linux, **no
  auto-paste** — user hits ⌘V themselves).
- `list_types()`, `read_text()`, `read_bytes(mime)`, `read_file_paths(types)`.
- `start_watch(on_change)` — already polls `changeCount` every 0.4s; capture is already wired in
  `mac_app.py` (`on_change` → `capture_current()` → `engine.broadcast_id`). **The panel only
  READS storage; capture already happens.** Do not add a second watcher.

**`clippy/capture.py`** → `capture_current() -> Optional[int]` (already called on every clipboard
change). **`clippy/sync.py`** → `SyncEngine` with `.broadcast_id(id)`, `.status()`,
`.enter_pairing()`, `.join_pairing(code)`, `.start()/.stop()/.restart_network()`.
**`clippy/settings.py`** → `get(key)`, `set_value(key, val)`, `load()`.
**`clippy/config.py`** → `IMAGE_DIR`, `FILE_DIR`, `RECV_DIR`, `THUMB_DIR`, `MAX_HISTORY`,
`PANEL_HEIGHT=320`, `TILE_WIDTH=230`, `TILE_HEIGHT=250`.

Read `clippy/panel.py` (the GTK panel) as the **behavioral spec** — replicate its UX, not its
GTK code. Key method to mirror: its `_load_to_clipboard(entry, mode)` logic —
`is_file → copy_file(image_path)`, `is_image → copy_image(bytes, mime)`, else text with
plain/rich handling per the `always_plain_text` setting and `entry.html`.

---

## 2. Feature parity checklist (from the Linux app)

- [ ] Global hotkey toggles a panel of recent-clipboard **tiles** (text / image / file).
- [ ] Tiles show: type badge, a `★` for pinned, a `rich` badge if `entry.html`, a preview
      (image thumbnail / file card / text snippet), and a relative-time + meta footer.
- [ ] **Click a tile** (or Enter) → load it onto the clipboard and close. Respect
      `always_plain_text`; offer plain vs rich (right-click menu).
- [ ] **Type to search** (filters via `storage.list_entries(query=…)`).
- [ ] Keyboard nav: ←/→/↑/↓ between tiles, Enter to copy, ⌘1–9 quick-copy, Delete to remove,
      ⌘P pin/unpin, Esc / click-away to close.
- [ ] **Pin** (survives pruning, sorts first) and **Delete** per tile.
- [ ] **Light/dark** follows the system appearance.
- [ ] Panel is an overlay that **doesn't steal app focus** beyond what's needed to type, floats
      above other windows, and **dismisses on click-away / Esc**.

Out of scope (keep it lean, matches Linux): no auto-paste (no Accessibility needed), no tray
*history* — the existing menubar menu stays for sync/pairing/settings.

---

## 3. macOS architecture (PyObjC / AppKit — same Python package, new files)

Stay in Python + PyObjC (the Mac app is already rumps/PyObjC and shares the core). New files:

### `clippy/mac_panel.py` — the panel
- **Window:** `NSPanel` with `NSWindowStyleMaskNonactivatingPanel | NSWindowStyleMaskBorderless`
  (or titled+fullSizeContentView, borderless look). Set:
  - `setLevel_(NSStatusWindowLevel)` (or `NSPopUpMenuWindowLevel`) so it floats over everything.
  - `setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces |
    NSWindowCollectionBehaviorFullScreenAuxiliary)` so it appears on the active Space and over
    fullscreen apps.
  - `setHidesOnDeactivate_(False)`, `setBecomesKeyOnlyIfNeeded_(True)`,
    `setFloatingPanel_(True)`, `setWorksWhenModal_(True)`.
  - `setOpaque_(False)` + a semi-transparent background (a dimmed full-screen overlay like Linux,
    OR a compact bottom strip — see §6 decision).
- **Layout:** a horizontal scroll of tile views. Reuse the Linux geometry as a starting point
  (`TILE_WIDTH=230`, `TILE_HEIGHT=250`, `PANEL_HEIGHT≈320`). An `NSScrollView` with a horizontal
  `NSStackView` of tiles, plus a search `NSTextField` at the top.
- **Positioning:** anchor to the bottom edge of the screen with the active mouse/main screen
  (`NSScreen.mainScreen()` / screen under the cursor). On Linux it's bottom-strip; match that.
- **Tile view (`NSView` subclass or `NSButton`-based card):** badge label, pin/delete buttons,
  preview, footer. Mirror `panel.Tile`. Rounded card via layer `cornerRadius`.
- **Preview/thumbnails:**
  - Images: `NSImage.alloc().initWithContentsOfFile_(entry.image_path)`, drawn aspect-fit into
    the tile (cap to `TILE_CONTENT_HEIGHT`). `image_path` already has a correct extension (≥1.3.2).
  - Files (incl. video): use **QuickLook** `QLThumbnailGenerator` for a real preview, with a
    generic file card fallback (show `entry.filename` + extension). Cache to `config.THUMB_DIR`.
    (Don't port the Linux ffmpeg path — QuickLook is the native, dependency-free choice.)
  - Text: a truncated multi-line label of `entry.text`.
- **Actions** (call shared core directly):
  - select → replicate `panel._load_to_clipboard`: `clipboard.copy_file/copy_image/copy_text`
    (+ `copy_html` for rich), then close.
  - pin → `storage.toggle_pin(id)`, refresh. delete → `storage.delete(id)`, refresh.
  - search → rebuild tiles from `storage.list_entries(query=text)`.
- **Theming:** read `NSApp.effectiveAppearance` (or the panel's), pick light/dark colors; observe
  `NSAppearance` changes. Respect the `theme_mode` setting (`system`/`light`/`dark`) if you want
  parity, else just follow system.
- **Dismiss:** close on `Esc`, on `resignKey`/`resignMain`, and/or a global click monitor outside
  the panel (`NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(NSEventMaskLeftMouseDown…)`).

### Global hotkey
- **Use Carbon `RegisterEventHotKey`** (via `ctypes` against `/System/Library/Frameworks/Carbon.framework`
  or PyObjC). **It does NOT require Accessibility/Input-Monitoring permission** and is the standard
  way to get a system-wide hotkey for a background (LSUIElement) app.
- Avoid `NSEvent.addGlobalMonitorForEventsMatchingMask` for keyDown — that needs Accessibility
  permission and is the wrong tool here.
- Default combo: mirror the Linux default (Super+V → **⌘⇧V** is a reasonable Mac default; confirm
  with the user). Store in the existing `settings["shortcut"]` or a mac-specific key.
- The hotkey handler calls into the panel: toggle show/hide on the main thread.

### Wiring (`clippy/mac_app.py`)
- Add a **"Show Clipboard History"** menu item (and show the hotkey) that toggles the panel.
- Instantiate the panel controller once; register the hotkey at startup (after `app.run()` setup).
- The app stays `LSUIElement` (menubar-only, no Dock). The panel is a floating `NSPanel`, so no
  Dock icon appears.

---

## 4. Permissions & entitlements

- **Global hotkey via RegisterEventHotKey:** no special permission. ✅ (This is why we avoid
  CGEventTap/global NSEvent monitors.)
- **No Accessibility** needed (no synthetic keystrokes / auto-paste).
- **Screen Recording / QuickLook:** thumbnail generation needs no special permission for files the
  user already has.
- Keep the existing **ad-hoc signing** in `build-app.sh` (stable local identity).

---

## 5. Packaging changes (`packaging/macos/`)

- `setup_py2app.py`: ensure new modules are bundled (they're under `clippy/`, already a `packages`
  entry, so they're included automatically). If you add the panel via PyObjC only, **no new pip
  deps** are required (AppKit/Foundation/Quartz/QuickLook are all in pyobjc, already bundled). If
  you pull in `pyobjc-framework-Quartz`/`-QuickLookThumbnailing`, add them to the `pip install`
  line in `build-app.sh` and to `includes` in `setup_py2app.py`.
- `Info.plist`: keep `LSUIElement = True`. No new keys needed for the hotkey approach above.
- Bump `clippy/__init__.py` `__version__` when you ship (Linux is currently **1.3.3**; coordinate
  the next number so tags don't collide — suggest the macOS panel lands as **1.4.0**).

---

## 6. Decisions to confirm with the user before/while building

1. **Panel shape:** full-screen dimmed overlay with a bottom tile-strip (most faithful to Linux),
   **or** a compact floating bar at the bottom/cursor (more "Mac-native, Spotlight-like")? Recommend
   starting with the **bottom strip without full-screen dim** (less intrusive on macOS), then add
   click-away dismissal via a transparent overlay if desired.
2. **Default hotkey:** ⌘⇧V? (⌘V is taken by paste; Super maps to ⌘.)
3. **Theme:** follow system only, or honor the existing `theme_mode` setting?

---

## 7. Gotchas / notes

- **LSUIElement + key window:** a non-activating `NSPanel` can still become key to receive
  typing for the search field — set `canBecomeKeyWindow` to return `True` on an `NSPanel`
  subclass if needed. Test that typing search works without the app stealing focus from the app
  you were in (so ⌘V afterward pastes into the right place).
- **Run on the main thread:** all AppKit UI must be created/updated on the main thread. The
  hotkey callback and timers already run there under rumps; if you spawn work, marshal back via
  `performSelectorOnMainThread` or `rumps.Timer`.
- **rumps run loop:** the app already runs an `NSApplication` loop via `app.run()` in `mac_app.py`.
  Build the panel within that loop; don't start a second `NSApplication`.
- **Capture already works** — the panel must not double-capture. Only read `storage`.
- **Retina:** set tile image reps correctly (NSImage handles @2x); draw with the view's
  `backingScaleFactor` if you go to a custom drawRect.
- **Empty/edge states:** no history, search with no matches, image file missing (`image_path`
  gone) → show a graceful placeholder (Linux shows "[image unavailable]").

---

## 8. Fast iteration without a full rebuild

```zsh
cd ~/Clippy
python3 -m venv /tmp/clippyvenv && source /tmp/clippyvenv/bin/activate
pip install -q pynacl zeroconf rumps pyobjc-framework-Cocoa pyobjc-framework-Quartz
# run the menubar app (with your new panel) straight from source:
PYTHONPATH="$PWD" python3 -m clippy.mac_app
```
Iterate on `clippy/mac_panel.py` and re-run — no py2app needed until you want a shippable `.app`.
Then: `./packaging/macos/build-app.sh --dmg`.

---

## 9. Suggested milestones (verify each on-screen)

1. **Panel shell:** a floating `NSPanel` that the hotkey + menu item show/hide; correct level,
   space behavior, click-away/Esc dismiss. No tiles yet.
2. **Tiles (read-only):** render `storage.list_entries()` as text/image/file cards with badges,
   preview, footer. Scroll horizontally.
3. **Select-to-copy:** click/Enter loads the entry onto the clipboard (port `_load_to_clipboard`)
   and closes. Verify ⌘V pastes it into the previously-focused app.
4. **Search + keyboard nav:** type-to-filter, arrows, ⌘1–9, Delete, ⌘P.
5. **Pin/Delete buttons, rich/plain right-click menu, theming (light/dark).**
6. **QuickLook thumbnails** for files/videos; caching.
7. **Polish:** empty states, retina, animations, positioning on multi-monitor.
8. **Package** `.app` + `.dmg`, bump version, ship.

---

## 10. Definition of done

Pressing the hotkey anywhere on macOS opens a Clippy panel showing your synced + local clipboard
history; you can search, arrow-key/click to a tile, it lands on the clipboard, you ⌘V it into any
app; pin/delete work; light/dark matches the system; the app stays menubar-only with no Dock icon;
and it's packaged as a signed-adhoc `.app`/`.dmg`. Sync, capture, and pairing keep working
unchanged (they already do).
