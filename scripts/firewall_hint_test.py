#!/usr/bin/env python3
"""The firewall hint: does it speak up exactly when inbound sync is dropped?

Runs headless against synthetic ufw rule files — no firewall is read, changed
or even required. What it pins down:

* an active ufw with no rule for the port warns, and names the port;
* a rule that allows the port — plain, multiport, or a range — silences it;
* an IPv6-only rule still counts (the peer may be on either family);
* `ufw default allow incoming` needs no rule, so nothing is said;
* an inactive ufw, and a machine with no firewall, say nothing;
* the check never raises and never opens anything: it returns text or None.

The bug this exists to prevent recurring: Omarchy enables ufw by default, the
Arch install came up with 47823 closed, and every clip copied on the paired Mac
was dropped at the door for a day — while `clippy peers` showed the Mac green
and this device's own copies kept landing on it.
"""
from __future__ import annotations

import sys
from pathlib import Path

FAILED = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        FAILED.append(label)


def with_rules(fw, tmp: Path, v4: str = "", v6: str = "", default="DROP",
               active=("ufw",)):
    """Point the module at rule files we wrote, and fake the service state."""
    r4, r6, dflt = tmp / "user.rules", tmp / "user6.rules", tmp / "default-ufw"
    r4.write_text(v4)
    r6.write_text(v6)
    dflt.write_text(f'DEFAULT_INPUT_POLICY="{default}"\n')
    fw.UFW_RULES = (r4, r6)
    fw.UFW_DEFAULTS = dflt
    fw._service_active = lambda unit: unit in active
    fw._run = lambda *a, **k: None      # no firewall-cmd, no systemctl, no shelling out


ALLOW = "-A ufw-user-input -p tcp --dport 47823 -j ACCEPT\n"
ALLOW_LAN = ("-A ufw-user-input -p tcp --dport 47823 -s 192.168.1.0/24 "
             "-j ACCEPT\n")
ALLOW_MULTI = "-A ufw-user-input -p tcp -m multiport --dports 22,47823,80 -j ACCEPT\n"
ALLOW_RANGE = "-A ufw-user-input -p tcp --dports 47800:47900 -j ACCEPT\n"
OTHER_PORT = "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n"
DENY_SAME = "-A ufw-user-input -p tcp --dport 47823 -j DROP\n"
UDP_ONLY = "-A ufw-user-input -p udp --dport 47823 -j ACCEPT\n"


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import tempfile
    from clippy import firewall as fw

    tmp = Path(tempfile.mkdtemp(prefix="clippy-fw-"))
    fw._lan_cidr = lambda: "192.168.1.0/24"

    print("closed port")
    with_rules(fw, tmp, v4=OTHER_PORT)
    note = fw.blocking(47823)
    check("warns", bool(note), True)
    check("names the port", "47823" in (note or ""), True)
    check("gives the command", "ufw allow" in (note or ""), True)
    check("scopes it to the LAN", "192.168.1.0/24" in (note or ""), True)

    print("port allowed")
    for label, v4, v6 in (
        ("plain rule", ALLOW, ""),
        ("LAN-scoped rule", ALLOW_LAN, ""),
        ("multiport rule", ALLOW_MULTI, ""),
        ("port range", ALLOW_RANGE, ""),
        ("IPv6-only rule", OTHER_PORT, ALLOW),
    ):
        with_rules(fw, tmp, v4=v4, v6=v6)
        check(label + " silences it", fw.blocking(47823), None)

    print("rules that do not count")
    with_rules(fw, tmp, v4=DENY_SAME)
    check("a DROP on the port is not an allow", bool(fw.blocking(47823)), True)
    with_rules(fw, tmp, v4=UDP_ONLY)
    check("udp does not open tcp", bool(fw.blocking(47823)), True)

    print("nothing to warn about")
    with_rules(fw, tmp, v4=OTHER_PORT, default="ACCEPT")
    check("default allow incoming", fw.blocking(47823), None)
    with_rules(fw, tmp, v4=OTHER_PORT, active=())
    check("ufw inactive", fw.blocking(47823), None)
    with_rules(fw, tmp, v4="", active=())
    check("no firewall at all", fw.blocking(47823), None)

    print("it stays a hint")
    with_rules(fw, tmp, v4=OTHER_PORT)
    fw.UFW_RULES = (Path("/nonexistent/user.rules"),)
    check("unreadable rules still answer", isinstance(fw.blocking(47823), str), True)
    def boom(*a, **k):
        raise RuntimeError("firewall check exploded")
    fw.blocking = boom
    check("hint() swallows a broken check", fw.hint(47823), None)

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("firewall hint: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
