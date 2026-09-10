#!/usr/bin/env bash
# Publish a released version to the AUR (aur/clippy).
#
# Usage:  packaging/aur/publish.sh 1.6.2 [--dry-run]
#
# Assembles the AUR checkout from three places so nothing is maintained twice:
#   packaging/aur/PKGBUILD        this repo's template (pkgver/sha256 rewritten)
#   packaging/arch/clippy.install the canonical install hook, copied verbatim
#   .SRCINFO                      generated, never hand-written
#
# The sha256 is computed from the tarball GitHub actually serves, not guessed:
# the AUR is the one channel where a wrong checksum is a hard failure for every
# user at once, and the tarball does not exist until the tag is pushed.
#
# --dry-run does everything except clone and push, and leaves the assembled
# package in a temp dir for inspection. It needs no AUR credentials, which is
# what makes it usable from CI as a check and from a laptop before release.
set -euo pipefail

VERSION="${1:-}"
DRY_RUN=""
[ "${2:-}" = "--dry-run" ] && DRY_RUN=1
if [ -z "$VERSION" ]; then
    echo "usage: $0 <version> [--dry-run]" >&2
    exit 2
fi
VERSION="${VERSION#v}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AUR_PKG="${AUR_PKG:-clippy}"
TARBALL_URL="https://github.com/davidboulay/Clippy/archive/refs/tags/v${VERSION}.tar.gz"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "==> Fetching $TARBALL_URL"
if ! curl -fsSL "$TARBALL_URL" -o "$work/src.tar.gz"; then
    echo "FAILED: no release tarball for v${VERSION}." >&2
    echo "Push the tag and let the release publish before running this." >&2
    exit 1
fi
SHA256="$(sha256sum "$work/src.tar.gz" | cut -d' ' -f1)"
echo "    sha256 $SHA256"

# Prove the tag really contains the version it claims, before it reaches users:
# a PKGBUILD whose pkgver disagrees with __version__ installs a package that
# lies about itself to the in-app updater, which then offers the same update
# forever.
tar -xzf "$work/src.tar.gz" -C "$work"
IN_TARBALL="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$work/Clippy-${VERSION}/clippy/__init__.py")"
if [ "$IN_TARBALL" != "$VERSION" ]; then
    echo "FAILED: tag v${VERSION} contains __version__ = ${IN_TARBALL}" >&2
    exit 1
fi
echo "    __version__ agrees: $IN_TARBALL"

stage="$work/aur"
mkdir -p "$stage"
sed -e "s/^pkgver=.*/pkgver=${VERSION}/" \
    -e "s/^sha256sums=.*/sha256sums=('${SHA256}')/" \
    "$REPO_ROOT/packaging/aur/PKGBUILD" > "$stage/PKGBUILD"
cp "$REPO_ROOT/packaging/arch/clippy.install" "$stage/clippy.install"

echo "==> Generating .SRCINFO"
# makepkg refuses to run as root, which is exactly what a container CI job is.
if [ "$(id -u)" -eq 0 ]; then
    build_user=aurbuild
    id -u "$build_user" >/dev/null 2>&1 || useradd -m "$build_user"
    chown -R "$build_user" "$stage"
    su "$build_user" -c "cd '$stage' && makepkg --printsrcinfo" > "$stage/.SRCINFO"
else
    (cd "$stage" && makepkg --printsrcinfo) > "$stage/.SRCINFO"
fi
grep -q "pkgver = ${VERSION}" "$stage/.SRCINFO" || {
    echo "FAILED: .SRCINFO does not carry pkgver ${VERSION}" >&2; exit 1; }

if [ -n "$DRY_RUN" ]; then
    keep="$(mktemp -d)"
    cp "$stage/PKGBUILD" "$stage/.SRCINFO" "$stage/clippy.install" "$keep/"
    echo "==> Dry run: nothing pushed. Assembled package in $keep"
    exit 0
fi

echo "==> Publishing to ssh://aur@aur.archlinux.org/${AUR_PKG}.git"
# Honour a caller-supplied command (CI points it at a deploy key); only
# supply a default so an interactive run still works with the agent.
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o StrictHostKeyChecking=accept-new}"
git clone "ssh://aur@aur.archlinux.org/${AUR_PKG}.git" "$work/clone"
cp "$stage/PKGBUILD" "$stage/.SRCINFO" "$stage/clippy.install" "$work/clone/"
cd "$work/clone"
if git diff --quiet; then
    echo "==> Already at ${VERSION}; nothing to push."
    exit 0
fi
git add PKGBUILD .SRCINFO clippy.install
git -c user.name="${GIT_AUTHOR_NAME:-David Boulay}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-89959743+davidboulay@users.noreply.github.com}" \
    commit -m "Update to ${VERSION}"
git push origin master
echo "==> Published ${AUR_PKG} ${VERSION} to the AUR."
