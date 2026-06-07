"""py2app entry point for Clippy.app (macOS menubar)."""
from clippy.mac_app import run

if __name__ == "__main__":
    raise SystemExit(run())
