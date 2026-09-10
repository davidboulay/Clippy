#!/usr/bin/env python3
"""The two PKGBUILDs must install the same package.

`packaging/arch/PKGBUILD` builds the working tree (`make arch`);
`packaging/aur/PKGBUILD` builds a released tarball for AUR users. They differ in
exactly one thing — where the source comes from — and share the `package()` body
verbatim.

Nothing enforces that by itself, and the failure is silent and one-sided: a fix
to the local file that misses the AUR one ships a stale package to every AUR
user while `make arch` looks fine on the maintainer's machine, which is the one
place it would never be noticed. So compare the bodies here.

Run:  PYTHONPATH=. python3 scripts/pkgbuild_parity_test.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCAL = ROOT / "packaging/arch/PKGBUILD"
AUR = ROOT / "packaging/aur/PKGBUILD"

failures = []


def check(label, ok, detail=""):
    if not ok:
        failures.append(f"{label}{(' — ' + detail) if detail else ''}")


def package_body(path: pathlib.Path) -> str:
    text = path.read_text()
    if "package() {" not in text:
        return ""
    return text[text.index("package() {"):].strip()


def field(path: pathlib.Path, name: str) -> str:
    for line in path.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


local_body = package_body(LOCAL)
aur_body = package_body(AUR)

check("local PKGBUILD must define package()", bool(local_body))
check("AUR PKGBUILD must define package()", bool(aur_body))

if local_body and aur_body and local_body != aur_body:
    local_lines = local_body.splitlines()
    aur_lines = aur_body.splitlines()
    diff = next(
        (f"line {i + 1}: local {l!r} vs aur {a!r}"
         for i, (l, a) in enumerate(zip(local_lines, aur_lines)) if l != a),
        f"lengths differ: {len(local_lines)} vs {len(aur_lines)}",
    )
    check("package() bodies must be identical", False, diff)
else:
    check("package() bodies must be identical", True)

# The one line that is allowed to differ has to actually differ, or the AUR
# package would try to build from a working tree its users do not have.
check("both must defer the source root into _set_repo",
      "_set_repo()" in LOCAL.read_text() and "_set_repo()" in AUR.read_text(),
      "srcdir is unset when the PKGBUILD is sourced; assigning _repo at top "
      "level makes the AUR build cp from /Clippy-<ver> and fail in fakeroot")
check("local builds the checkout", "startdir" in LOCAL.read_text())
check("AUR builds the release tarball", "$srcdir/Clippy-$pkgver" in AUR.read_text())

# A shared package name and install hook, or an upgrade in one channel would
# not be seen as an upgrade of the other.
check("same pkgname", field(LOCAL, "pkgname") == field(AUR, "pkgname"),
      f"{field(LOCAL, 'pkgname')} vs {field(AUR, 'pkgname')}")
check("same install hook", field(LOCAL, "install") == field(AUR, "install"),
      f"{field(LOCAL, 'install')} vs {field(AUR, 'install')}")
check("same depends", field(LOCAL, "depends") == field(AUR, "depends"))

# The AUR edition needs a checksummed source; publish.sh rewrites both lines,
# so their presence — not their value — is what matters here.
check("AUR declares a source tarball", field(AUR, "source").startswith('("$pkgname'))
check("AUR declares sha256sums", bool(field(AUR, "sha256sums")))

# The local PKGBUILD's version is the release version, so a forgotten bump is
# caught before the tag rather than after.
version = ""
for line in (ROOT / "clippy/__init__.py").read_text().splitlines():
    if line.startswith("__version__"):
        version = line.split('"')[1]
check("local pkgver matches clippy.__version__",
      field(LOCAL, "pkgver") == version,
      f"PKGBUILD {field(LOCAL, 'pkgver')} vs __version__ {version}")

if failures:
    print("FAIL: the Arch and AUR PKGBUILDs have drifted:")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("OK: both PKGBUILDs install the same package")
