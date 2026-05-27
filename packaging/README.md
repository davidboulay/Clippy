# Packaging Clippy

Clippy is a Python 3 + GTK 3 app that links (via GObject-introspection) to
system libraries: PyGObject, GTK 3, `gtk-layer-shell`, Ayatana AppIndicator,
and the `wl-clipboard` CLI. That shapes how each format works.

| Format | Fit | Build | Notes |
|--------|-----|-------|-------|
| **.deb** | ✅ best on Ubuntu/Pop!_OS/Debian | `make deb` | Built & verified. Deps resolved by apt. |
| **Arch** | ✅ good on Arch/Manjaro | `make arch` | `PKGBUILD` builds from the local tree. |
| **Flatpak** | ⚠️ works, with caveats | `make flatpak` | Bundles layer-shell + wl-clipboard; tray optional; needs broad fs perms to write the COSMIC shortcut. |
| **AppImage** | ⚠️ experimental | `make appimage` | GI/GTK bundling is fragile; prefer .deb/Flatpak. |
| **source** | ✅ any wlroots/COSMIC distro | `./scripts/install.sh` | Installs deps + a `~/.local/bin` launcher. |

All build outputs land in `dist/`.

## .deb  (recommended for your system)

```bash
make deb            # -> dist/clippy_<version>_all.deb
sudo apt install ./dist/clippy_<version>_all.deb
```

- Architecture `all` (pure Python; the GI/GTK deps come from apt).
- Installs the package to `/usr/lib/python3/dist-packages/clippy`, a launcher at
  `/usr/bin/clippy`, the hicolor icons, and `clippy.desktop`.
- `Depends:` python3, python3-gi, gir1.2-gtk-3.0, gir1.2-gtklayershell-0.1,
  libgtk-layer-shell0, gir1.2-ayatanaappindicator3-0.1,
  libayatana-appindicator3-1, wl-clipboard. `Recommends:` pipewire-bin |
  pulseaudio-utils (copy sound).
- No network or sudo needed to *build* it.

After install: launch **Clippy** from the app list, then open Settings to bind a
shortcut. The daemon autostarts after first launch (toggle in Settings).

## Flatpak

```bash
make flatpak        # builds & installs to the user scope
flatpak run com.lojel.Clippy daemon &
flatpak run com.lojel.Clippy settings
```

Requires `flatpak` + Flathub access (the script installs `org.flatpak.Builder`
if `flatpak-builder` is missing, and pulls `org.gnome.Platform//47`).

Caveats specific to a sandboxed clipboard manager:
- **Tray icon** is disabled unless you uncomment the Ayatana AppIndicator module
  in `com.lojel.Clippy.yaml` (panel + shortcut still work without it).
- **COSMIC shortcut / autostart**: the manifest grants
  `--filesystem=xdg-config/cosmic` and `xdg-config/autostart` so Clippy can
  register them; remove those finish-args for a tighter sandbox and set the key
  manually (command: `flatpak run com.lojel.Clippy toggle`).
- **Sound** may be unavailable (no `pw-play`/`paplay` in the GNOME runtime).

## AppImage (experimental)

```bash
make appimage       # -> dist/Clippy-<version>-<arch>.AppImage
```

Run it on a host that already has Clippy's runtime deps installed; the script
bundles Python, the gi bindings, the GTK3/layer-shell/AppIndicator typelibs and
wl-clipboard into an AppDir and runs `linuxdeploy` + the GTK plugin. Bundling
GObject-introspection apps is finicky — if it misbehaves, use the .deb or
Flatpak. Downloads `linuxdeploy`/`appimagetool` from GitHub.

## Arch (PKGBUILD)

```bash
make arch           # or: cd packaging/arch && makepkg -f
sudo pacman -U packaging/arch/clippy-*.pkg.tar.zst
```

Depends on `python-gobject gtk3 gtk-layer-shell libayatana-appindicator
wl-clipboard`.

## Bumping the version

Edit `__version__` in `clippy/__init__.py`; the deb/appimage scripts read it,
and update `pkgver` in `packaging/arch/PKGBUILD` and the `<release>` in
`packaging/flatpak/com.lojel.Clippy.metainfo.xml`.
