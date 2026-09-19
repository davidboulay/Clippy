#!/usr/bin/env python3
"""The sync-unavailable hint names what is really missing, in local spelling.

Runs headless — no GTK, no network, nothing installed or removed. The import
flags are forced, which is the only way to see the hint a user without the
dependencies would get on a machine that has them.

The bug this pins down: the hint was the fixed string "Install python3-nacl and
python3-zeroconf to enable sync." It was wrong twice. It named Debian packages
on every distro, and it named two modules when sync_available() requires three
— so a user who installed exactly what it asked got the same unchanged message
with no way to discover spake2 was also wanted. scripts/install.sh had the same
omission on its apt and dnf branches.

What it checks:

* the hint names precisely the modules that failed, not a fixed list;
* each package manager gets its own spelling, and the command to run;
* spake2 is never dropped, on any route — the regression that made the hint a
  dead end;
* an unknown package manager still says something useful;
* a Flatpak blames the build rather than asking the user to install anything;
* install.sh installs all three modules on every branch it handles.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FAILED = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        FAILED.append(label)


def check_in(label, needle, haystack):
    if needle in haystack:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         {needle!r} not in {haystack!r}")
        FAILED.append(label)


def hint_with(sync, *, missing, manager, flatpak=False):
    """The hint a machine missing `missing` under `manager` would show."""
    sync._HAVE_NACL = "nacl" not in missing
    sync._HAVE_ZC = "zeroconf" not in missing
    sync._HAVE_SPAKE2 = "spake2" not in missing
    sync._package_manager = lambda: manager
    if flatpak:
        os.environ["FLATPAK_ID"] = "io.github.davidboulay.Clippy"
    else:
        os.environ.pop("FLATPAK_ID", None)
    return sync.missing_packages_hint()


# --- the hint follows the real imports ------------------------------------
def test_names_what_is_missing():
    print("names what is missing")
    from clippy import sync

    check("nothing missing, nothing said",
          hint_with(sync, missing=[], manager="pacman"), "")
    check("one module",
          hint_with(sync, missing=["spake2"], manager="pacman"),
          "To enable sync:  sudo pacman -S python-spake2")
    check("all three, arch",
          hint_with(sync, missing=["nacl", "zeroconf", "spake2"], manager="pacman"),
          "To enable sync:  sudo pacman -S python-pynacl python-zeroconf python-spake2")
    check("missing list is import names",
          sorted(sync.missing_modules()), ["nacl", "spake2", "zeroconf"])


# --- each manager spells them its own way ---------------------------------
def test_per_manager_spelling():
    print("per-manager spelling")
    from clippy import sync

    every = ["nacl", "zeroconf", "spake2"]
    check("apt", hint_with(sync, missing=every, manager="apt"),
          "To enable sync:  sudo apt install python3-nacl python3-zeroconf python3-spake2")
    check("dnf", hint_with(sync, missing=every, manager="dnf"),
          "To enable sync:  sudo dnf install python3-pynacl python3-zeroconf python3-spake2")
    # nacl is the one that differs between apt and dnf; a single shared table
    # would have quietly told Fedora users to install a package that is not there.
    check_in("dnf uses python3-pynacl, not python3-nacl", "python3-pynacl",
             hint_with(sync, missing=["nacl"], manager="dnf"))


# --- spake2 is never dropped ----------------------------------------------
def test_spake2_never_dropped():
    print("spake2 parity")
    from clippy import sync

    # The original bug: sync_available() wants three, the hint offered two.
    for mgr in ("pacman", "apt", "dnf"):
        check_in(f"{mgr} hint includes spake2", "spake2",
                 hint_with(sync, missing=["nacl", "zeroconf", "spake2"], manager=mgr))

    # install.sh is the source route, and it must offer the same packages the
    # hint tells people to install -- drift there is how the apt and dnf
    # branches came to omit spake2 while the hint table knew about it. Derived
    # from _SYNC_PACKAGES so adding a dependency cannot be half-done.
    install_sh = (REPO / "scripts" / "install.sh").read_text()
    for mgr, packages in sync._SYNC_PACKAGES.items():
        for module, package in packages.items():
            check_in(f"install.sh offers {package} ({mgr}/{module})",
                     package, install_sh)


# --- routes we cannot name packages for -----------------------------------
def test_unknown_and_flatpak():
    print("unknown manager and flatpak")
    from clippy import sync

    check("no manager: still names the modules",
          hint_with(sync, missing=["nacl", "spake2"], manager=None),
          "Install nacl, spake2 to enable sync.")
    check("flatpak blames the build, not the user",
          hint_with(sync, missing=["spake2"], manager="pacman", flatpak=True),
          "Sync is unavailable: this Flatpak build is missing spake2.")


def main() -> int:
    from clippy import sync
    saved = (sync._HAVE_NACL, sync._HAVE_ZC, sync._HAVE_SPAKE2,
             sync._package_manager, os.environ.get("FLATPAK_ID"))
    try:
        test_names_what_is_missing()
        test_per_manager_spelling()
        test_spake2_never_dropped()
        test_unknown_and_flatpak()
    finally:
        (sync._HAVE_NACL, sync._HAVE_ZC, sync._HAVE_SPAKE2,
         sync._package_manager, flatpak) = saved
        if flatpak is None:
            os.environ.pop("FLATPAK_ID", None)
        else:
            os.environ["FLATPAK_ID"] = flatpak
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("sync hint: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
