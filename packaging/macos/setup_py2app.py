"""py2app build config for Clippy.app (macOS menubar clipboard sync).

Run via packaging/macos/build-app.sh (which sets up a venv with the deps).
Produces dist/Clippy.app — a LSUIElement (menubar-only, no Dock icon) app that
bundles Python + pynacl + zeroconf + rumps, so the user installs nothing.
"""
import os
import sys

from setuptools import setup

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
from clippy import __version__  # noqa: E402

APP = [os.path.join(os.path.dirname(__file__), "clippy_mac_main.py")]

OPTIONS = {
    "argv_emulation": False,
    # Recursively bundle these packages (incl. their compiled .so files):
    #  - nacl + cffi: PyNaCl reaches libsodium through cffi/_cffi_backend
    #  - zeroconf + ifaddr (+ async_timeout): mDNS discovery
    #  - spake2: the SPAKE2 pairing handshake. MUST be named, or sync_available()
    #    is False on the built app and the Sync UI shows "No module named
    #    'spake2'" — py2app only bundles what it can statically trace, and the
    #    sync.py import is guarded (try/except), so py2app treats it as optional
    #    and drops it unless named here. spake2 imports hkdf, which is a single
    #    .py module (not a package), so it belongs in `includes` below, not here.
    "packages": ["clippy", "nacl", "cffi", "zeroconf", "ifaddr", "rumps", "Quartz",
                 "QuickLookThumbnailing", "spake2"],
    "includes": ["_cffi_backend", "async_timeout", "AppKit", "Foundation", "objc",
                 "hkdf"],
    # Copy the menubar template icons into Contents/Resources (packages alone
    # doesn't reliably bundle package data files).
    "resources": [
        os.path.join(REPO, "clippy", "icons", "clippy-menubar.png"),
        os.path.join(REPO, "clippy", "icons", "clippy-menubar@2x.png"),
    ],
    "plist": {
        "CFBundleName": "Clippy",
        "CFBundleDisplayName": "Clippy",
        "CFBundleIdentifier": "io.github.davidboulay.Clippy",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,   # menubar-only; no Dock icon
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "MIT",
    },
}
# An .icns at packaging/macos/clippy.icns is used if present.
_icns = os.path.join(os.path.dirname(__file__), "clippy.icns")
if os.path.exists(_icns):
    OPTIONS["iconfile"] = _icns

setup(
    name="Clippy",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
