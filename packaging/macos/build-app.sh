#!/usr/bin/env bash
# Build Clippy.app on macOS — one command, no manual dependency wrangling.
#
#   ./packaging/macos/build-app.sh          # -> dist/Clippy.app
#   ./packaging/macos/build-app.sh --dmg    # also -> dist/Clippy-<ver>.dmg
#
# Run this ON A MAC (it needs Apple's toolchain). It creates a throwaway venv,
# installs the runtime deps + py2app, and bundles everything into the .app, so
# the resulting app has no external Python/dependency requirements.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
    echo "This builds the macOS app and must run on macOS (uname=$(uname))." >&2
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
VERSION="$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' clippy/__init__.py)"
VENV="$(mktemp -d)/venv"

echo "==> Building Clippy.app $VERSION"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip wheel
pip install --quiet pynacl zeroconf rumps pyobjc-framework-Cocoa py2app

rm -rf build dist/Clippy.app
python packaging/macos/setup_py2app.py py2app

echo "==> Built dist/Clippy.app"
echo "    Drag it to /Applications. First launch: right-click → Open"
echo "    (unsigned build), then allow it under System Settings → Privacy."
echo "    To start at login: System Settings → General → Login Items → +."

if [[ "${1:-}" == "--dmg" ]]; then
    DMG="dist/Clippy-${VERSION}.dmg"
    rm -f "$DMG"
    hdiutil create -volname "Clippy" -srcfolder dist/Clippy.app -ov -format UDZO "$DMG" >/dev/null
    echo "==> Built $DMG"
fi

deactivate
echo
echo "NOTE: unsigned apps are blocked by Gatekeeper for other users. For"
echo "      distribution, code-sign + notarize with an Apple Developer ID."
