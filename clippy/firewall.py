"""Is the local firewall dropping inbound LAN sync?

Sync delivery is sender-driven: a copy on the Mac opens a TCP connection to
this device's sync port. Outbound is what every default firewall policy allows,
so half of sync keeps working perfectly -- this device's copies land on the
peer, the liveness ping goes out and comes back, and `clippy peers` shows the
peer green. Nothing in that picture involves an inbound connection, so a
firewall that drops them looks exactly like a peer that never copies anything.

That cost a real diagnosis: Omarchy enables ufw by default, the Arch install
came up with 47823 closed, and the Mac's every copy was dropped at the door for
a day (110 SYNs in the kernel log) while both machines reported healthy sync.

So we say it out loud, wherever the user is already looking: `clippy status`,
the line after pairing, and the daemon's own sync log at startup.

Read-only and unprivileged. We never touch the firewall -- opening a port is
the user's call, and a package that quietly did it would be a nasty surprise --
so the most this module produces is the exact command to run. Silence is the
default: a check that cannot confirm a problem says nothing rather than nag.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# ufw keeps its rules in world-readable files, so this needs no root. Both
# families matter: an IPv6-only rule would not cover an IPv4 peer.
UFW_RULES = (Path("/etc/ufw/user.rules"), Path("/etc/ufw/user6.rules"))
UFW_DEFAULTS = Path("/etc/default/ufw")


def _run(cmd: List[str], timeout: float = 4.0) -> Optional[str]:
    """stdout of ``cmd``, or None if it isn't there / fails / hangs."""
    if not shutil.which(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def _service_active(unit: str) -> bool:
    out = _run(["systemctl", "is-active", unit])
    return bool(out) and out.strip() == "active"


def _ufw_drops_input() -> bool:
    """Whether ufw's default inbound policy is to drop.

    An `ufw default allow incoming` box needs no per-port rule, and warning it
    about one would be noise.
    """
    try:
        text = UFW_DEFAULTS.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True   # can't read it: assume the usual DROP and check the port
    m = re.search(r'^\s*DEFAULT_INPUT_POLICY\s*=\s*"?(\w+)"?', text, re.MULTILINE)
    return not m or m.group(1).upper() != "ACCEPT"


def _ufw_allows(port: int) -> bool:
    """Whether any ufw rule accepts inbound TCP on ``port``.

    Matched off the generated iptables lines rather than the ### tuple ###
    comments, so a rule added by hand to user.rules counts too. The port may
    appear as a single --dport or inside a multiport list.
    """
    for path in UFW_RULES:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "-j ACCEPT" not in line or "tcp" not in line:
                continue
            ports = re.findall(r"--dports?\s+([\d,:]+)", line)
            for spec in ports:
                for part in spec.split(","):
                    if part == str(port):
                        return True
                    if ":" in part:     # a range, e.g. 47800:47900
                        lo, _, hi = part.partition(":")
                        try:
                            if int(lo) <= port <= int(hi):
                                return True
                        except ValueError:
                            pass
    return False


def _firewalld_allows(port: int) -> Optional[bool]:
    """True/False if firewalld could be asked, None if we couldn't tell."""
    out = _run(["firewall-cmd", "--list-ports"])
    if out is None:
        return None
    return f"{port}/tcp" in out.split()


def blocking(port: int) -> Optional[str]:
    """A note naming the firewall and the command that opens ``port``, or None.

    None means "no problem found", which includes "no firewall we recognise"
    and "we could not read the rules" -- this is a hint, never a gate.
    """
    if sys.platform != "linux":
        return None
    if _service_active("ufw") and _ufw_drops_input() and not _ufw_allows(port):
        return (
            f"ufw is active and nothing allows inbound TCP {port}, so clips "
            f"copied on your other devices are dropped before Clippy sees "
            f"them. Allow it on your LAN:\n"
            f"    sudo ufw allow from {_lan_cidr()} to any port {port} "
            f"proto tcp comment 'Clippy LAN sync'"
        )
    if _service_active("firewalld") and _firewalld_allows(port) is False:
        return (
            f"firewalld is active and TCP {port} is not open, so clips copied "
            f"on your other devices are dropped before Clippy sees them. Allow "
            f"it:\n"
            f"    sudo firewall-cmd --permanent --zone=home --add-port={port}/tcp"
            f" && sudo firewall-cmd --reload"
        )
    return None


def _lan_cidr() -> str:
    """The /24 this machine sits on ("192.168.1.0/24"), for a scoped rule.

    A guess at the useful default, not a claim: sync only ever talks to paired
    peers, and pairing is a SPAKE2 handshake, so the rule decides who may knock
    rather than who gets in. Falls back to the RFC1918 space when the address
    can't be read.
    """
    try:
        from . import sync
        ip = sync._local_ip()
        parts = ip.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    except Exception:
        pass
    return "192.168.0.0/16"


def hint(port: Optional[int] = None) -> Optional[str]:
    """:func:`blocking` for Clippy's own sync port, honouring CLIPPY_NO_FW_HINT."""
    if os.environ.get("CLIPPY_NO_FW_HINT"):
        return None
    if port is None:
        from . import config
        port = config.SYNC_PORT
    try:
        return blocking(port)
    except Exception:
        return None     # a diagnostic must never break the thing it diagnoses
