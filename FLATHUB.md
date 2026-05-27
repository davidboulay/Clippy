# Publishing Clippy to Flathub (and thus the COSMIC Store)

The **COSMIC Store** has no separate submission process — it's a storefront over
**Flathub** (plus your configured apt repos). So getting Clippy into the COSMIC
Store = getting it onto Flathub. Once it's on Flathub, it appears in the Store
automatically.

App ID: **`io.github.davidboulay.Clippy`** (derived from the GitHub repo, so it's
verifiable on Flathub via your GitHub login — no domain ownership needed).

Manifest: [`packaging/flatpak/io.github.davidboulay.Clippy.yaml`](packaging/flatpak/io.github.davidboulay.Clippy.yaml)
— builds from the pinned `v0.2.1` tag and bundles `gtk-layer-shell` +
`wl-clipboard` (not in the GNOME runtime).

## 1. Build & test locally first (the real prerequisite)

```bash
make flatpak
flatpak run io.github.davidboulay.Clippy daemon &
flatpak run io.github.davidboulay.Clippy settings
```

Verify (flatpak-specific risk areas):
- Panel renders as a bottom strip; type-to-search + arrow nav work on open.
- The IPC socket in `$XDG_RUNTIME_DIR` works across `flatpak run` calls, so
  `toggle` reaches the daemon.
- Setting the shortcut writes `~/.config/cosmic/...` (needs the
  `--filesystem=xdg-config/cosmic` permission in the manifest).
- Tray icon is **absent** unless you enable the AppIndicator module (panel +
  shortcut + settings still work without it).
- Sound is likely silent (no `pw-play` in the runtime).

## 2. Lint & validate (Flathub gates on these)

```bash
# AppStream metainfo
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    appstream packaging/flatpak/io.github.davidboulay.Clippy.metainfo.xml

# Manifest
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    manifest packaging/flatpak/io.github.davidboulay.Clippy.yaml

# Built result (after `make flatpak` produced build-dir)
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    builddir packaging/flatpak/build-dir
```

Fix anything they report before submitting.

## 3. Submit to Flathub

1. Sign in to https://flathub.org with your **GitHub** account.
2. Fork https://github.com/flathub/flathub.
3. Create a branch (any name, e.g. `io.github.davidboulay.Clippy`).
4. Add **only** the manifest at the repo root, named exactly
   `io.github.davidboulay.Clippy.yaml`. (The `.desktop`, metainfo, launcher and
   icon are pulled from Clippy's own git tag, so they don't need to be copied
   into the Flathub repo.)
5. Open a Pull Request against `master`. The build bot will test it; a reviewer
   will review. On merge, a dedicated `flathub/io.github.davidboulay.Clippy`
   repo is created and you become its maintainer.

## Likely review discussion points

- **`--filesystem=xdg-config/cosmic`** (so Clippy can write the COSMIC
  shortcut) is broad; reviewers may ask to drop it. If removed, set the key
  manually in COSMIC settings (command: `flatpak run
  io.github.davidboulay.Clippy toggle`). Consider gating the in-app shortcut
  picker on this permission so it doesn't silently no-op when sandboxed.
- **Autostart**: writing `~/.config/autostart` directly is allowed by the
  current manifest; the "portal-correct" alternative is the Background portal.
- **Tray**: optional; enable the Ayatana module if you want it bundled.

## Updating later

Bump `__version__` in `clippy/__init__.py`, tag a release (`vX.Y.Z`), then update
the manifest's `clippy` module `tag:`/`commit:` and the metainfo `<release>`,
and open a PR to the `flathub/io.github.davidboulay.Clippy` repo.
