#!/usr/bin/env bash
# Build & install the Clippy Flatpak (user scope) from the Flathub-ready
# manifest. Requires network access to Flathub for the GNOME runtime/SDK on
# first run. The manifest builds from the pinned v0.2.1 git tag.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.davidboulay.Clippy"
MANIFEST="$HERE/$APP_ID.yaml"

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

cat <<EOF

Installed. Start the daemon (and bind a shortcut to the toggle command):

    flatpak run $APP_ID daemon &
    flatpak run $APP_ID settings     # set the shortcut here

Verify while testing (these are the flatpak-specific risk areas):
  * Panel renders as a bottom strip (layer-shell over the Wayland socket).
  * Type-to-search and arrow nav work on open (ON_DEMAND keyboard focus).
  * The IPC socket in \$XDG_RUNTIME_DIR works across 'flatpak run' invocations
    (so 'toggle' reaches the daemon).
  * Tray icon: absent unless the AppIndicator module is enabled in the manifest.
  * Sound: likely silent (no pw-play in the GNOME runtime).
EOF
