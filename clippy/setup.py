"""Desktop integration: autostart entry + COSMIC custom-shortcut management.

COSMIC stores custom shortcuts as a RON map at
``~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom``; the action
for a command is ``Spawn("…")``. We edit that file surgically (only our own
``Spawn`` entries) and keep a one-time backup, so existing shortcuts are safe.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from . import config

COSMIC_CUSTOM = (
    config.HOME
    / ".config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom"
)


def _resolve_launcher() -> Optional[str]:
    """Absolute path of the installed `clippy` launcher, if any.

    Covers a system install (/usr/bin/clippy on PATH) and a user install
    (~/.local/bin/clippy)."""
    found = shutil.which("clippy")
    if found:
        return found
    local = config.HOME / ".local" / "bin" / "clippy"
    return str(local) if local.exists() else None

# Matches a whole binding whose action spawns a command containing "clippy".
_CLIPPY_ENTRY = re.compile(
    r'\n?[ \t]*\(\s*modifiers\s*:\s*\[[^\]]*\]\s*,?\s*'
    r'key\s*:\s*"[^"]*"\s*,?\s*\)\s*:\s*'
    r'Spawn\(\s*"[^"]*clippy[^"]*"\s*\)\s*,?',
    re.DOTALL,
)
_CLIPPY_READ = re.compile(
    r'\(\s*modifiers\s*:\s*\[([^\]]*)\]\s*,?\s*'
    r'key\s*:\s*"([^"]*)"\s*,?\s*\)\s*:\s*'
    r'Spawn\(\s*"[^"]*clippy[^"]*"\s*\)',
    re.DOTALL,
)


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
    """Absolute command suitable for COSMIC's Spawn(...): an installed launcher
    on PATH, ~/.local/bin, or a flatpak run invocation."""
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


# ---- COSMIC shortcut ----------------------------------------------------
def read_cosmic_shortcut() -> Optional[Tuple[List[str], str]]:
    try:
        content = COSMIC_CUSTOM.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _CLIPPY_READ.search(content)
    if not m:
        return None
    mods = [tok.strip() for tok in m.group(1).split(",") if tok.strip()]
    return mods, m.group(2)


def set_cosmic_shortcut(modifiers: List[str], key: str) -> bool:
    """Register/replace the Clippy toggle shortcut in COSMIC. Returns success."""
    cmd = spawn_command()
    if cmd is None:
        return False
    COSMIC_CUSTOM.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = COSMIC_CUSTOM.read_text(encoding="utf-8")
    except OSError:
        content = "{\n}\n"

    # One-time backup of the user's original file.
    backup = COSMIC_CUSTOM.with_name("custom.clippy.bak")
    if not backup.exists():
        try:
            backup.write_text(content, encoding="utf-8")
        except OSError:
            pass

    content = _CLIPPY_ENTRY.sub("", content)
    mod_list = ", ".join(modifiers)
    entry = (
        f"\n    (\n        modifiers: [{mod_list}],\n"
        f'        key: "{key}",\n    ): Spawn("{cmd}"),\n'
    )
    brace = content.find("{")
    if brace == -1:
        content = "{" + entry + "}\n"
    else:
        content = content[: brace + 1] + entry + content[brace + 1:]

    try:
        COSMIC_CUSTOM.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def remove_cosmic_shortcut() -> bool:
    try:
        content = COSMIC_CUSTOM.read_text(encoding="utf-8")
    except OSError:
        return False
    new = _CLIPPY_ENTRY.sub("", content)
    try:
        COSMIC_CUSTOM.write_text(new, encoding="utf-8")
        return True
    except OSError:
        return False


# ---- CLI help -----------------------------------------------------------
def print_shortcut_instructions() -> int:
    cmd = spawn_command() or launcher_command("toggle")
    print(
        f"""\
Bind a global shortcut to open Clippy
=====================================

Easiest: open Clippy's Settings (tray icon → Settings, or the ⚙ in the panel)
and use the shortcut picker — it writes the COSMIC binding for you.

Manually, in COSMIC:
  Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts → + Add
    Command:  {cmd}
    Key:      e.g. Super + V

COSMIC stores it in:
    {COSMIC_CUSTOM}
"""
    )
    return 0
