# Brief: bring the Linux (GTK) app up to macOS 1.4.0 feature parity

**Audience:** a Claude Code session running on the Linux machine (Pop!_OS 24.04 +
COSMIC), working in the `Clippy` repo.
**Author:** the macOS Claude session that built the 1.4.0 macOS panel.
**Date:** 2026-06-14.

## TL;DR

Between `v1.3.4` and `v1.4.0` the **macOS** app gained a full clipboard-history
panel with several features. Some of that work was in the **shared core** (so
Linux already has it); some was **macOS-only UI** that now needs a GTK
equivalent. This brief is the gap analysis + a port plan. **`__version__` is
already `1.4.0` on `main`** (the macOS app shipped), so the Linux parity work
should land as **`1.4.1`** (or `1.5.0` if you prefer) and be released with a new
tag.

> ⚠️ Read "Repo process" at the bottom first — `main` is **protected** now
> (PRs + a required CI check, no direct pushes). This changed during the macOS
> work.

## Architecture reminder

- **Shared, cross-platform core** (don't fork — improve in place, keep platform
  guards): `storage.py`, `sync.py`, `clipboard.py` + `backends/`, `capture.py`,
  `settings.py`, `config.py`, `sound.py`.
- **Linux GTK UI:** `panel.py` (overlay panel, tiles, tabs), `settings_window.py`,
  `tray.py`, `daemon.py`, `theme.py` (GTK CSS).
- **macOS UI (reference only):** `mac_panel.py`, `mac_settings.py`, `mac_tabs.py`,
  `mac_source.py`, `mac_app.py`.

**Porting principle:** where the macOS feature is pure logic (type classification,
tab membership), **extract it into a shared module** and have *both* the GTK panel
and `mac_panel.py` import it — one source of truth, no divergence. Where it's
inherently UI, build the GTK equivalent.

## What Linux ALREADY has (no work needed)

Confirmed by reading `panel.py` / `settings_window.py` / `storage.py` / `sync.py`:

- Tabs: **History** + **★ Pinned** (toggle buttons in `panel.py` ~line 341).
- Pin / unpin, pin markers, pin buttons.
- Image **and video** thumbnails (ffmpeg) — `panel.py` `_video_thumb`.
- Search, keyboard nav, plain/rich paste, right-click context menu.
- Settings: open-at-login, sound on copy, always-plain-text, **history retention
  + auto-delete + Clear history** (`settings_window.py` docstring/section),
  shortcut picker, full **Sync** section (enable, show/enter code, max size,
  peers label).
- Shared core already merged: `storage.touch()`, `storage.apply_retention()`,
  `sync.SyncEngine.unpair()`, device-id **drift self-heal** in `sync.py`,
  `sound.py` multi-player.

## The GAP — macOS 1.4.0 features to port to Linux

Ordered easiest → hardest. Each item says what macOS did and the GTK plan.

### 1. Recover-to-front (trivial, shared backend already there)
macOS: selecting an old clip calls `storage.touch(id)` so it jumps to position 1.
- **Linux:** in `panel.py` `paste_entry()` (~line 579), after putting the clip on
  the clipboard, call `storage.touch(entry.id)`. `storage.touch` already exists
  (`storage.py:268`). One line. Verify the next panel open shows it first.

### 2. Unpair a device in Settings (backend done, UI missing)
macOS: the paired-devices list shows each device with an **Unpair** button +
confirm dialog, calling `engine.unpair(id)`.
- **Linux:** `settings_window.py` `_refresh_peers()` currently sets a single text
  label ("Paired: ● name"). Replace with a per-device row: name + online dot +
  an **Unpair** `Gtk.Button`. On click, show a `Gtk.MessageDialog` (destructive
  confirm), then `engine.unpair(peer["id"])` and `self._refresh_peers()`.
  `SyncEngine.unpair(peer_id) -> bool` already exists in `sync.py`.

### 3. Granular type categories + colors + icons (extract shared classifier)
macOS: `_category(entry)` and `_entry_type_key(entry)` classify into
Text / Image / Video / Audio / PDF / **Spreadsheet (CSV+Excel, green)** /
Archive / Other, each with a color + icon. Linux only has coarse `IMAGE`/`TEXT`
badges (`panel.py` ~line 66 with CSS classes `badge-image`/`badge-text`).
- **Plan:** create **`clippy/clip_types.py`** with a platform-neutral classifier:
  - `type_key(entry) -> str` (one of: text, image, video, audio, pdf, sheet,
    archive, file) — copy `_entry_type_key` logic verbatim.
  - `category(entry) -> (label, key, icon_name)` where `icon_name` is a
    **symbolic GTK/freedesktop icon** name (NOT an SF Symbol), e.g.
    text→`text-x-generic-symbolic`, image→`image-x-generic-symbolic`,
    video→`video-x-generic-symbolic`, audio→`audio-x-generic-symbolic`,
    pdf→`x-office-document-symbolic`, sheet→`x-office-spreadsheet-symbolic`,
    archive→`package-x-generic-symbolic`, file→`text-x-generic-symbolic`.
  - The ext/mime sets (`_IMAGE_EXTS` etc.) move here too.
  - Refactor `mac_panel.py` to import `type_key`/the ext-sets from this module
    and map keys→(SF symbol, NSColor) locally, so Mac and Linux share the
    classification and only differ in icon/color rendering.
  - **Reference (macOS `_category`/`_entry_type_key`) is in `mac_panel.py`
    lines ~145–210** — copy the branch order exactly (note: CSV-as-text is
    detected by mime and bucketed as `sheet`/green).
- **GTK rendering:** in `panel.py` `Tile`, replace the IMAGE/TEXT badge with the
  category label + a `Gtk.Image.new_from_icon_name(icon, …)`. Add per-type CSS
  classes in `theme.py` (e.g. `.badge-video`, `.badge-pdf`, `.badge-sheet` →
  green, `.badge-archive`, …) following the existing `.badge-image` pattern, and
  pick colors close to the macOS ones (image=teal, video=pink, audio=purple,
  pdf=red, sheet=green, archive=brown, file/other=orange, text=deep blue
  `#1E40AF`).

### 4. Type filter (new GTK control)
macOS: a funnel button beside the search field opens a menu (All + each type with
its icon, single-select); the panel shows only that bucket. Implemented via
`_TYPE_FILTERS`, `_entry_type_key`, and filtering in `_entries_for_tab`.
- **Linux:** add a small `Gtk.MenuButton` (or a `Gtk.ComboBox`) next to the
  search entry in the panel header. Populate from `clip_types` buckets. Keep
  filter state on the panel (e.g. `self._type_filter`), and apply it in the panel
  query/refresh path (wherever the tab's entries are gathered — mirror macOS
  `_entries_for_tab`: over-fetch then filter in memory by `type_key(e)` since the
  SQL has no type column). Empty-state message like "No video clips here."

### 5. Custom tabs (biggest piece — make it shared)
macOS: beyond Recent/Pinned, the user can create **custom colored, named tabs**;
pinning a clip offers a destination tab. Stored in `mac_tabs.py` →
`<DATA_DIR>/mac_tabs.json` with: `tabs/tab_names/add_tab/remove_tab/rename_tab/
set_color/assign/unassign/tabs_for/member_ids/all_member_ids` and an 8-color
`PALETTE`.
- **Decision to make:** the macOS store is named `mac_tabs.json`. For Linux,
  **promote it to a shared `clippy/tabs.py` writing `<DATA_DIR>/tabs.json`** so
  the feature is identical on both (and could even sync later). Migrate
  `mac_panel.py` to use the shared module; keep reading the old `mac_tabs.json`
  once for migration if any Mac user has data (low stakes — it's brand new).
- **Linux UI:** today `panel.py` has two `Gtk.ToggleButton` tabs (History/Pinned,
  ~line 341) and `self._tab` is `"history"|"pinned"`. Generalize to render
  Recent/Pinned + one toggle per custom tab (colored), a **`+`** button to create
  a tab (name + color dialog), and **right-click a custom tab → rename / change
  color / delete** (with confirm). When pinning a clip and ≥1 custom tab exists,
  offer a destination menu (mirror macOS `_show_tab_picker`). Membership comes
  from the shared tabs module. NOTE the existing tab id `"history"` on Linux vs
  `"recent"` on macOS — pick one and align (suggest `"recent"`).

### 6. Source-app icon  ⚠️ Wayland blocker — likely SKIP or X11-only
macOS: at capture time it records the **frontmost app's bundle id**
(`mac_source.py`, called from `mac_app.on_change` via `NSWorkspace
.frontmostApplication()`), and the tile header shows that app's icon.
- **Linux reality:** Wayland deliberately gives apps **no way** to query the
  active/foreground window or its app id (security). There is no COSMIC portal
  for this. So this feature is **not generally implementable on Wayland**.
  Options: (a) **skip on Linux** and document why; (b) best-effort **X11/XWayland
  only** via `_NET_ACTIVE_WINDOW` + `WM_CLASS` (won't work in a pure Wayland
  session, i.e. most of COSMIC). **Recommendation: skip, and note it in the
  README's Limitations.** Do NOT spend long here.

### 7. Visual polish (optional, match where it makes sense)
macOS also got: taller colored header bands, larger text preview font, square
panel corners, an always-visible custom scrollbar, search-left/tabs-centered
layout. These are GTK-CSS/`theme.py` choices — apply tasteful equivalents, but
they're **nice-to-have**, not parity-critical. The type colors (#3) are the part
worth carrying over.

## Suggested PR breakdown (each is its own branch → PR → CI → merge)

1. `feat(linux): recover-to-front + Unpair button` (#1 + #2 — small, high value).
2. `refactor: shared clip_types classifier` (#3 logic) + GTK badges/colors.
3. `feat(linux): type filter` (#4).
4. `feat: shared custom tabs` (#5 — the big one; do the shared `tabs.py`
   refactor first, then GTK UI, then migrate mac_panel).
5. `docs: README Limitations — source-app icon is Wayland-restricted` (#6 note).
6. `release: 1.4.1` — bump `clippy/__init__.py` `__version__`, then push tag
   `v1.4.1`.

## Repo process (IMPORTANT — changed during the macOS work)

- **`main` is protected:** no force-push, no deletion, **PRs required** (0
  approvals, you self-merge), and a **required status check named `test`** must
  pass before merge. You **cannot push to `main` directly** (enforced for admins
  too). Workflow: branch → push → open PR → wait for CI green → `gh pr merge N
  --merge`.
- **CI** (`.github/workflows/ci.yml`, runs on every PR): byte-compiles
  `clippy/` + `scripts/`, then runs `scripts/sync_selftest.py` and
  `scripts/sync_drift_test.py`. To run locally:
  `pip install pynacl zeroconf && PYTHONPATH=. python scripts/sync_selftest.py`
  (and `sync_drift_test.py`). **Keep both green** — don't break the sync core
  while refactoring. If you change clipboard/tab APIs the tests touch, update
  them in the same PR.
- **Release** (`.github/workflows/release.yml`): pushing a `v*` tag builds the
  Linux `.deb` **and** the macOS `.dmg` and publishes the GitHub release. So a
  Linux feature release = bump `__version__`, merge, then `git tag v1.4.1 &&
  git push origin v1.4.1`.
- **Commit trailer:** end commit messages with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Testing the GTK app:** you're on the COSMIC box — run from source with
  `PYTHONPATH=. python -m clippy.cli daemon` (or your installed `clippy daemon`),
  bind the shortcut, and exercise the panel. The macOS app can't be tested from
  Linux; rely on CI + the macOS session for any `mac_*.py` changes you make
  during the shared-module refactors.

## Reference: the macOS classifier to mirror (from `mac_panel.py`)

```python
_IMAGE_EXTS = {"png","jpg","jpeg","gif","webp","bmp","tiff","tif","heic","heif","avif","ico","svg"}
_VIDEO_EXTS = {"mp4","mov","m4v","webm","mkv","avi","wmv","flv","mpg","mpeg"}
_AUDIO_EXTS = {"mp3","m4a","aac","wav","flac","ogg","oga","aiff","aif","opus"}
_ARCHIVE_EXTS = {"zip","tar","gz","tgz","bz2","tbz","7z","rar","xz","zst","dmg"}
_SHEET_EXTS = {"csv","tsv","xls","xlsx","xlsm","xlsb","ods","numbers"}
_SHEET_MIMES = ("text/csv","text/tab-separated-values","application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml",
                "application/vnd.oasis.opendocument.spreadsheet")

def type_key(entry):           # buckets for filter + coloring
    if entry.kind == "text":
        m = (entry.mime or "").lower()
        return "sheet" if ("csv" in m or "tab-separated" in m) else "text"
    if entry.is_image:
        return "image"
    mime = (entry.mime or "").lower()
    ext = _ext(entry.filename or entry.text or "")
    if mime.startswith("image/") or ext in _IMAGE_EXTS:   return "image"
    if mime.startswith("video/") or ext in _VIDEO_EXTS:   return "video"
    if mime.startswith("audio/") or ext in _AUDIO_EXTS:   return "audio"
    if mime == "application/pdf" or ext == "pdf":         return "pdf"
    if ext in _SHEET_EXTS or any(m in mime for m in _SHEET_MIMES): return "sheet"
    if ext in _ARCHIVE_EXTS:                              return "archive"
    return "file"
```

Type → color used on macOS (match in GTK CSS): text `#1E40AF` (deep blue),
image teal, video pink, audio purple, pdf red, sheet **green**, archive brown,
file/other orange.

## Open questions for the user (ask before building #5/#6)

- Custom tabs: shared `tabs.json` (recommended) or keep Linux-separate?
- Source-app icon: confirm **skip on Wayland** (recommended) vs. attempt X11-only.
