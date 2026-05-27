#!/usr/bin/env bash
# Stops Clippy and removes the launcher / autostart / icon.
# History under ~/.local/share/clippy is kept unless --purge is given.
set -euo pipefail

BIN="$HOME/.local/bin/clippy"

echo "==> Stopping daemon"
"$BIN" quit 2>/dev/null || true
pkill -f "wl-paste --watch .* -m clippy _store" 2>/dev/null || true

echo "==> Removing launcher, autostart, icon"
rm -f "$BIN"
rm -f "$HOME/.config/autostart/clippy.desktop"
rm -f "${XDG_RUNTIME_DIR:-/tmp}/clippy.sock" 2>/dev/null || true

echo "==> NOTE: a COSMIC shortcut you set is left in place."
echo "    Original shortcuts backup (if any): "
echo "    ~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom.clippy.bak"

if [ "${1:-}" = "--purge" ]; then
    echo "==> Purging history and settings"
    rm -rf "$HOME/.local/share/clippy" "$HOME/.config/clippy"
fi

echo "Done. (System packages were left installed.)"
