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
pip install --quiet pynacl zeroconf rumps pyobjc-framework-Cocoa pyobjc-framework-Quartz \
            pyobjc-framework-QuickLookThumbnailing py2app

# Regenerate a crisp multi-size app icon from the 512px source (Apple tools).
ICNS="packaging/macos/clippy.icns"
SRC="clippy/icons/clippy.png"
if command -v iconutil >/dev/null && command -v sips >/dev/null && [[ -f "$SRC" ]]; then
    ISET="$(mktemp -d)/clippy.iconset"; mkdir -p "$ISET"
    for s in 16 32 128 256 512; do
        sips -z "$s" "$s" "$SRC" --out "$ISET/icon_${s}x${s}.png" >/dev/null
        d=$((s * 2)); sips -z "$d" "$d" "$SRC" --out "$ISET/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$ISET" -o "$ICNS" && echo "==> App icon: regenerated $ICNS"
fi

rm -rf build dist/Clippy.app
python packaging/macos/setup_py2app.py py2app
APP="dist/Clippy.app"

# --- code signing -----------------------------------------------------------
# Default: ad-hoc sign ("-") — gives the app a STABLE local identity (so the
# firewall/permission grants persist across rebuilds), but Gatekeeper still
# shows "unidentified developer".
# To remove that label, sign with an Apple Developer ID and notarize:
#   export CLIPPY_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   export CLIPPY_NOTARY_PROFILE="clippy-notary"   # a stored notarytool profile
IDENTITY="${CLIPPY_SIGN_IDENTITY:--}"
if [[ "$IDENTITY" == "-" ]]; then
    echo "==> Ad-hoc signing (no Developer ID set)…"
    codesign --force --deep --sign - "$APP"
else
    echo "==> Signing with: $IDENTITY"
    codesign --force --deep --options runtime --timestamp \
             --sign "$IDENTITY" "$APP"
    if [[ -n "${CLIPPY_NOTARY_PROFILE:-}" ]]; then
        echo "==> Notarizing (this uploads to Apple and waits)…"
        ZIP="dist/Clippy-notarize.zip"
        ditto -c -k --keepParent "$APP" "$ZIP"
        xcrun notarytool submit "$ZIP" --keychain-profile "$CLIPPY_NOTARY_PROFILE" --wait
        xcrun stapler staple "$APP"
        rm -f "$ZIP"
        echo "==> Notarized + stapled."
    fi
fi

echo "==> Built $APP"
if [[ "$IDENTITY" == "-" ]]; then
    echo "    Unsigned/ad-hoc: first launch right-click → Open (one time),"
    echo "    or: xattr -dr com.apple.quarantine '$APP'  (if it was downloaded)."
fi
echo "    Drag it to /Applications. Start at login is in Clippy → Settings."

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
