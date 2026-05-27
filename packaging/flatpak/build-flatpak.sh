#!/usr/bin/env bash
# Build & install the Clippy Flatpak (user scope). Requires network access to
# Flathub for the GNOME runtime/SDK on first run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$HERE/com.lojel.Clippy.yaml"

flatpak --user remote-add --if-not-exists flathub \
    https://flathub.org/repo/flathub.flatpakrepo

# flatpak-builder may be a system package or the org.flatpak.Builder flatpak.
if command -v flatpak-builder >/dev/null 2>&1; then
    BUILDER="flatpak-builder"
else
    echo "==> flatpak-builder not found; installing org.flatpak.Builder (user)"
    flatpak --user install -y flathub org.flatpak.Builder
    BUILDER="flatpak run org.flatpak.Builder"
fi

$BUILDER --user --force-clean --install --install-deps-from=flathub \
    "$HERE/build-dir" "$MANIFEST"

cat <<'EOF'

Installed. Start the daemon (and bind a shortcut to the toggle command):

    flatpak run com.lojel.Clippy daemon &
    flatpak run com.lojel.Clippy settings     # set the shortcut here

Notes:
  * Tray icon: disabled unless you enable the Ayatana AppIndicator module in
    the manifest (the panel and shortcut work without it).
  * The manifest grants --filesystem=xdg-config/cosmic so Clippy can write the
    COSMIC shortcut; remove it to keep the sandbox tighter and set the key
    manually in COSMIC settings (command: flatpak run com.lojel.Clippy toggle).
EOF
