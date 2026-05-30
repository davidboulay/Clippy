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


@dataclass
class UpdateResult:
    latest: Optional[str]          # e.g. "0.2.4" (no leading 'v'); None on error
    url: str                       # release page to open
    update_available: bool
    error: Optional[str] = None    # human-readable reason the check failed
    deb_url: Optional[str] = None  # direct .deb download, if the release has one


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
    deb_url = next(
        (a.get("browser_download_url") for a in (data.get("assets") or [])
         if str(a.get("name", "")).endswith(".deb")),
        None,
    )
    return UpdateResult(latest, url, available, deb_url=deb_url)


def download_deb(deb_url: str, timeout: float = 180.0) -> str:
    """Download a release .deb to a temp file and return its path."""
    fd, path = tempfile.mkstemp(prefix="clippy-update-", suffix=".deb")
    os.close(fd)
    req = urllib.request.Request(deb_url, headers={"User-Agent": f"Clippy/{__version__}"})
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


def notify(latest: str, url: str) -> None:
    """Best-effort desktop notification that an update is available."""
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.Popen(
            [
                "notify-send",
                "--app-name", APP_NAME,
                "--icon", str(config.ICON_PATH if config.ICON_PATH.exists() else "clippy"),
                f"{APP_NAME} {latest} is available",
                f"You have {__version__}. Open Settings → check for updates, "
                f"or visit {url}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass
