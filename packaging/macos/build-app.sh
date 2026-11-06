#!/usr/bin/env bash
# Build Clippy.app on macOS — one command, no manual dependency wrangling.
#
#   ./packaging/macos/build-app.sh          # -> dist/Clippy.app
#   ./packaging/macos/build-app.sh --dmg    # also -> dist/Clippy-<ver>.dmg
#   PYTHON=python3.14 ./packaging/macos/build-app.sh   # pick the interpreter
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

# --- interpreter --------------------------------------------------------------
# The app bundles whichever Python builds it. A pre-release Python is refused:
# 1.6.7 was built with a 3.15 alpha, py2app did not know its new stdlib
# extension `_math_integer`, and the app died on every launch with py2app's
# "Launch error" dialog before any Clippy code ran.
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null; then
    echo "Python interpreter not found: $PYTHON" >&2
    exit 1
fi
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d.%d-%s" % (sys.version_info[:3] + (sys.version_info.releaselevel,)))')"
if [[ "$PYVER" != *-final && "${CLIPPY_ALLOW_PRERELEASE_PYTHON:-}" != 1 ]]; then
    echo "Refusing to build with pre-release Python $PYVER ($(command -v "$PYTHON"))." >&2
    echo "Use a stable one, e.g.  PYTHON=python3.14 $0" >&2
    echo "(or set CLIPPY_ALLOW_PRERELEASE_PYTHON=1 to try anyway)" >&2
    exit 1
fi

echo "==> Building Clippy.app $VERSION with Python ${PYVER%-final} ($(command -v "$PYTHON"))"
"$PYTHON" -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip wheel
# spake2 is PINNED to 0.8 on purpose: it must match the Debian python3-spake2
# (0.8) that the Linux build uses, because spake2 0.8 and 0.9 do NOT
# interoperate on the wire (different group parameters) — a Mac on 0.9 and a
# Linux box on 0.8 would silently fail to pair. 0.8 is also pure-Python (pulls
# hkdf, not cryptography), so py2app bundles it cleanly. Do not unpin without
# re-checking cross-platform pairing against the distro version.
pip install --quiet pynacl zeroconf "spake2==0.8" rumps pyobjc-framework-Cocoa pyobjc-framework-Quartz \
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

# --- launch smoke test --------------------------------------------------------
# Start the built app for a few seconds with throwaway data/config dirs (so it
# never touches the real history, settings or peers) and fail the build if it
# dies or shows py2app's "Launch error". Skip with CLIPPY_SKIP_SMOKE=1.
if [[ "${CLIPPY_SKIP_SMOKE:-}" != 1 ]]; then
    echo "==> Smoke test: launching $APP for ${CLIPPY_SMOKE_SECONDS:-8}s…"
    SMOKE="$(mktemp -d)"
    mkdir -p "$SMOKE/data" "$SMOKE/config" "$SMOKE/run"
    XDG_DATA_HOME="$SMOKE/data" XDG_CONFIG_HOME="$SMOKE/config" XDG_RUNTIME_DIR="$SMOKE/run" \
        "$APP/Contents/MacOS/Clippy" >"$SMOKE/out.log" 2>&1 &
    SMOKE_PID=$!
    sleep "${CLIPPY_SMOKE_SECONDS:-8}"
    SMOKE_ALIVE=0
    if kill -0 "$SMOKE_PID" 2>/dev/null; then
        SMOKE_ALIVE=1
        kill "$SMOKE_PID" 2>/dev/null || true
    fi
    wait "$SMOKE_PID" 2>/dev/null || true
    if [[ "$SMOKE_ALIVE" != 1 ]] || grep -qE "Launch error|Traceback \(most recent call last\)" "$SMOKE/out.log"; then
        echo "!! Smoke test FAILED: the built app does not start. Its output:" >&2
        sed 's/^/   | /' "$SMOKE/out.log" >&2
        echo "   Do not install this build. Log kept at $SMOKE/out.log" >&2
        exit 1
    fi
    rm -rf "$SMOKE"
    echo "==> Smoke test passed (app stayed up, no launch error)."
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
