"""Desktop integration: autostart entry, icons, app entry, and the global
shortcut.

Only the *paths* are handled here. Where a shortcut is actually registered
differs per desktop and lives behind ``clippy/desktops/`` — this module resolves
the launcher command and hands it over.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import config
from .desktops import get_desktop


def _resolve_launcher() -> Optional[str]:
    """Absolute path of the installed `clippy` launcher, if any.

    Covers a system install (/usr/bin/clippy on PATH) and a user install
    (~/.local/bin/clippy)."""
    found = shutil.which("clippy")
    if found:
        return found
    local = config.HOME / ".local" / "bin" / "clippy"
    return str(local) if local.exists() else None

def launcher_command(action: str) -> str:
    """The host command a desktop entry / shortcut should run."""
    flatpak_id = os.environ.get("FLATPAK_ID")
    if flatpak_id:  # the host must invoke us via flatpak, not a sandbox path
        return f"flatpak run {flatpak_id} {action}"
    exe = _resolve_launcher()
    if exe:
        return f"{exe} {action}"
    root = str(config.PROJECT_ROOT)
    return f'sh -c "PYTHONPATH={root} {sys.executable} -m clippy {action}"'


def spawn_command() -> Optional[str]:
    """Absolute command a desktop shortcut can spawn: an installed launcher on
    PATH, in ~/.local/bin, or a flatpak run invocation. None when Clippy is
    running from a source tree with no launcher installed — there is no stable
    command to bind in that case."""
    flatpak_id = os.environ.get("FLATPAK_ID")
    if flatpak_id:
        return f"flatpak run {flatpak_id} toggle"
    exe = _resolve_launcher()
    return f"{exe} toggle" if exe else None


# ---- autostart ----------------------------------------------------------
def _autostart_path() -> Path:
    return config.HOME / ".config" / "autostart" / "clippy.desktop"


def autostart_installed() -> bool:
    return _autostart_path().exists()


def install_autostart() -> int:
    target = _autostart_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Clippy\n"
        "Comment=Clipboard history panel\n"
        f"Exec={launcher_command('daemon')}\n"
        "Icon=clippy\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    print(f"clippy: autostart entry written to {target}")
    return 0


def remove_autostart() -> int:
    p = _autostart_path()
    if p.exists():
        p.unlink()
        print(f"clippy: autostart entry removed ({p})")
    return 0


def set_open_at_login(enabled: bool) -> None:
    install_autostart() if enabled else remove_autostart()


# ---- icons + app-list entry --------------------------------------------
_ICON_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)


def install_icons() -> bool:
    """Install the paperclip into the hicolor icon theme so the tray host and
    desktop entry can resolve it by name ('clippy'). Returns success."""
    src = config.BUNDLED_ICON if config.BUNDLED_ICON.exists() else config.ICON_PATH
    if not src.exists():
        return False
    config.ensure_dirs()
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return False

    # Keep a private copy too (used for the in-panel header image).
    try:
        if not config.ICON_PATH.exists():
            import shutil
            shutil.copyfile(src, config.ICON_PATH)
    except OSError:
        pass

    hicolor = config.HOME / ".local/share/icons/hicolor"
    ok = False
    for size in _ICON_SIZES:
        out_dir = hicolor / f"{size}x{size}" / "apps"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                str(src), size, size, True
            )
            pb.savev(str(out_dir / "clippy.png"), "png", [], [])
            ok = True
        except Exception:
            continue
    return ok


def install_desktop_entry() -> int:
    """Add Clippy to the application list (not pinned to the dock)."""
    apps = config.HOME / ".local/share/applications"
    apps.mkdir(parents=True, exist_ok=True)
    target = apps / "clippy.desktop"
    target.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Clippy\n"
        "GenericName=Clipboard Manager\n"
        "Comment=Show your clipboard history\n"
        f"Exec={launcher_command('toggle')}\n"
        "Icon=clippy\n"
        "Terminal=false\n"
        "Categories=Utility;GTK;\n"
        "Keywords=clipboard;history;paste;copy;\n"
        "StartupNotify=false\n"
        "Actions=Settings;\n\n"
        "[Desktop Action Settings]\n"
        "Name=Settings\n"
        f"Exec={launcher_command('settings')}\n"
    )
    print(f"clippy: application entry written to {target}")
    return 0


# ---- global shortcut ----------------------------------------------------
# Delegated to the active desktop; see clippy/desktops/.
def shortcut_backend():
    """The desktop that owns shortcut registration (for labels and probing)."""
    return get_desktop()


def can_bind_shortcut() -> bool:
    """Whether we can write the binding ourselves, or only explain it."""
    return bool(get_desktop().can_bind()) and spawn_command() is not None


def read_shortcut() -> Optional[Tuple[List[str], str]]:
    """The binding currently registered for Clippy, or None."""
    try:
        return get_desktop().read_shortcut()
    except Exception:
        return None


def set_shortcut(modifiers: List[str], key: str) -> bool:
    """Register/replace the Clippy toggle shortcut. Returns success."""
    cmd = spawn_command()
    if cmd is None:
        return False
    try:
        return bool(get_desktop().set_shortcut(modifiers, key, cmd))
    except Exception:
        return False


def remove_shortcut() -> bool:
    try:
        return bool(get_desktop().remove_shortcut())
    except Exception:
        return False


# ---- CLI help -----------------------------------------------------------
def print_shortcut_instructions() -> int:
    desktop = get_desktop()
    cmd = spawn_command() or launcher_command("toggle")
    print("Bind a global shortcut to open Clippy")
    print("=====================================")
    print(f"Detected desktop: {desktop.label}\n")
    print(desktop.shortcut_help(cmd))
    return 0
