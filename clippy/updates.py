"""Check GitHub Releases for a newer Clippy version.

GTK-free: uses only the stdlib (urllib) so it can run on a background thread or
be imported anywhere. Network failures are reported, never raised.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

from . import APP_NAME, __version__, config

REPO = "davidboulay/clippy"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"

_NOTIFY_ACTIONS = None   # cache: does the local notify-send support -A/--action?


@dataclass
class UpdateResult:
    latest: Optional[str]          # e.g. "0.2.4" (no leading 'v'); None on error
    url: str                       # release page to open
    update_available: bool
    error: Optional[str] = None    # human-readable reason the check failed
    deb_url: Optional[str] = None  # direct .deb download, if the release has one
    dmg_url: Optional[str] = None  # direct .dmg download (macOS), if present


def _parse(version: str) -> Tuple[int, ...]:
    """Lenient numeric version tuple: 'v0.2.10-rc' -> (0, 2, 10)."""
    out = []
    for part in version.strip().lstrip("vV").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def current_version() -> str:
    return __version__


def check(timeout: float = 8.0) -> UpdateResult:
    """Query GitHub for the latest release and compare to the running version."""
    req = urllib.request.Request(
        LATEST_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Clippy/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        return UpdateResult(None, RELEASES_PAGE, False, error=f"GitHub returned {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return UpdateResult(None, RELEASES_PAGE, False, error="No network connection")
    except (ValueError, json.JSONDecodeError):
        return UpdateResult(None, RELEASES_PAGE, False, error="Unexpected response")

    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return UpdateResult(None, RELEASES_PAGE, False, error="No releases found")
    latest = tag.lstrip("vV")
    url = data.get("html_url") or RELEASES_PAGE
    available = _parse(latest) > _parse(__version__)
    assets = data.get("assets") or []
    deb_url = next(
        (a.get("browser_download_url") for a in assets
         if str(a.get("name", "")).endswith(".deb")),
        None,
    )
    dmg_url = next(
        (a.get("browser_download_url") for a in assets
         if str(a.get("name", "")).endswith(".dmg")),
        None,
    )
    return UpdateResult(latest, url, available, deb_url=deb_url, dmg_url=dmg_url)


def auto_check(min_interval: float = 24 * 3600) -> Optional[UpdateResult]:
    """Gated check for the background auto-updater, shared by Linux and macOS.

    Returns the UpdateResult when a check actually ran (so the caller can look
    at ``update_available``), or None when auto-checking is disabled, not yet
    due, or the network call failed. The timestamp is recorded only on a
    successful check, so a transient failure retries on the next tick instead
    of burning the whole interval. Safe to call on a background thread.
    """
    import time as _time

    from . import settings
    if not settings.get("auto_check_updates"):
        return None
    last = settings.get("last_update_check") or 0
    if _time.time() - last < min_interval:
        return None
    result = check()
    if result.error:
        return None
    settings.set_value("last_update_check", _time.time())
    return result


def download_deb(deb_url: str, timeout: float = 180.0) -> str:
    """Download a release .deb to a temp file and return its path."""
    return _download(deb_url, ".deb", timeout)


def download_dmg(dmg_url: str, timeout: float = 300.0) -> str:
    """Download a release .dmg (macOS) to a temp file and return its path."""
    return _download(dmg_url, ".dmg", timeout)


def _download(url: str, suffix: str, timeout: float) -> str:
    fd, path = tempfile.mkstemp(prefix="clippy-update-", suffix=suffix)
    os.close(fd)
    req = urllib.request.Request(url, headers={"User-Agent": f"Clippy/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(path, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    return path


def install_deb(path: str, timeout: float = 300.0) -> Tuple[bool, str]:
    """Install a .deb as root via PolicyKit (pkexec shows a password dialog).

    Returns (success, message). Never raises.
    """
    if shutil.which("pkexec") is None:
        return False, "pkexec (PolicyKit) is not available"
    if not os.path.isfile(path):
        return False, "downloaded file is missing"
    try:
        proc = subprocess.run(
            ["pkexec", "apt-get", "install", "-y", "--allow-downgrades", path],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "installed"
    if proc.returncode in (126, 127):
        return False, "Authentication cancelled"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, (detail[-1] if detail else f"apt-get exited {proc.returncode}")


def _notify_has_actions() -> bool:
    """Whether the local notify-send supports action buttons (-A/--action).
    Cached — the answer doesn't change while the daemon runs."""
    global _NOTIFY_ACTIONS
    if _NOTIFY_ACTIONS is None:
        _NOTIFY_ACTIONS = False
        try:
            help_txt = subprocess.run(
                ["notify-send", "--help"], capture_output=True, text=True, timeout=5,
            ).stdout or ""
            _NOTIFY_ACTIONS = "--action" in help_txt
        except (OSError, subprocess.SubprocessError):
            pass
    return _NOTIFY_ACTIONS


def _open_url(url: str) -> None:
    if shutil.which("xdg-open") is None:
        return
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def _notify_icon() -> str:
    return str(config.ICON_PATH if config.ICON_PATH.exists() else "clippy")


def notify(result, on_update=None) -> None:
    """Best-effort desktop notification that an update is available.

    When notify-send supports action buttons, show 'Update now' (only if the
    release ships a .deb and ``on_update`` is given) and 'Release notes':
    clicking Update now calls ``on_update(result)`` on the calling thread;
    Release notes opens the release page. Otherwise fall back to a plain,
    informational notification pointing at Settings. Runs quietly if
    notify-send is absent. ``result`` is an UpdateResult.
    """
    if shutil.which("notify-send") is None:
        return
    latest, url = result.latest, result.url
    title = f"{APP_NAME} {latest} is available"
    body = f"You have {__version__}."
    can_update = bool(getattr(result, "deb_url", None)) and on_update is not None

    if _notify_has_actions():
        cmd = ["notify-send", "--app-name", APP_NAME, "--icon", _notify_icon()]
        if can_update:
            cmd += ["--action", "update=Update now"]
        cmd += ["--action", "open=Release notes", title, body]
        try:
            # -A implies --wait: notify-send blocks until the user clicks an
            # action or the notification is dismissed, then prints its name.
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            return
        key = (proc.stdout or "").strip()
        if key == "update" and can_update:
            try:
                on_update(result)
            except Exception:
                pass
        elif key == "open":
            _open_url(url)
        return

    # No action support: a plain, informational notification.
    try:
        subprocess.Popen(
            ["notify-send", "--app-name", APP_NAME, "--icon", _notify_icon(), title,
             f"{body} Open Settings → check for updates, or visit {url}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
