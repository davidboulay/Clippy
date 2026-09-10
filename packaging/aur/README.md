# AUR — `aur/clippy`

The AUR is the channel that reaches the most Arch users: most run `yay` or
`paru`, so a published package updates on their normal `-Syu` with nothing to
configure. `packaging/deb` + `apt-repo.yml` already do this for Debian users;
this is the Arch half.

## What lives here

| File | Role |
|---|---|
| `PKGBUILD` | builds a **released tarball**. `packaging/arch/PKGBUILD` builds the working tree for `make arch`; the two share their `package()` body, guarded by `scripts/pkgbuild_parity_test.py`. |
| `publish.sh` | assembles and pushes the AUR checkout. The only supported way to publish. |

`clippy.install` is **not** duplicated here — `publish.sh` copies the canonical
one from `packaging/arch/` so the post-install and post-upgrade messages can
never differ between the two channels.

`pkgver` and `sha256sums` in `PKGBUILD` are a placeholder, not the source of
truth: the tarball does not exist until the tag is pushed, so `publish.sh`
computes the checksum from what GitHub actually serves and rewrites both lines.
Do not hand-edit them to publish.

## One-time setup (manual — needs a human)

The release workflow skips the AUR job with a notice until this is done, so a
release is never blocked on it.

1. **Create an AUR account** at https://aur.archlinux.org/register.
2. **Add an SSH public key** to that account (Account → My Account → SSH Public
   Key). Generate a dedicated one rather than reusing a personal key:
   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/aur_clippy -C "aur@clippy" -N ""
   ```
3. **Create the package** with the first push. The AUR repo does not exist until
   something is pushed to it, and `publish.sh` clones — so the very first
   publish is done by hand:
   ```sh
   git clone ssh://aur@aur.archlinux.org/clippy.git   # empty; this is expected
   packaging/aur/publish.sh <version> --dry-run       # prints where it staged
   # copy PKGBUILD, .SRCINFO and clippy.install into the clone, then:
   git add -A && git commit -m "Initial import" && git push origin master
   ```
   Every release after that is `publish.sh` (or the workflow) on its own.
4. **Add the private key as a repository secret** named `AUR_SSH_PRIVATE_KEY`
   (Settings → Secrets and variables → Actions). The `aur` job in `release.yml`
   picks it up from there and publishes on every `v*` tag.

## Publishing by hand

```sh
packaging/aur/publish.sh 1.6.2 --dry-run   # no credentials needed
packaging/aur/publish.sh 1.6.2             # clones, commits, pushes
```

`publish.sh` refuses to publish a tag whose `clippy/__init__.py` disagrees with
the version being published — a package that lies about its own version makes
the in-app updater offer the same update forever.

## Naming

`clippy` was free on the AUR at the time of writing, but the conventional
companion names are taken by unrelated projects — `clippy-bin` is an LLM runner,
`clippy-git` is unrelated, and `clippy-rs-bin` is a *different clipboard
manager*. Check `aur.archlinux.org` before adding a `-git` or `-bin` variant.

## Other Arch channels

- **`pkgs.omarchy.org`** — Omarchy ships its own binary pacman repo, enabled on
  every Omarchy install. For an app whose Linux reports are mostly
  Hyprland/Omarchy this is the highest-reach channel: binary, no AUR helper, and
  `pacman -Syu` updates it like any core package. Worth asking upstream about.
- **A self-hosted pacman repo** on GitHub Pages, mirroring `apt-repo.yml`, is
  the fallback if that is declined — full control, but users must edit
  `pacman.conf`, which costs most of the reach.
- **Flatpak is not viable.** See `FLATHUB.md`: `wp_security_context_v1` makes
  cosmic-comp withhold layer-shell and data-control from sandboxed clients.
  Hyprland implements that protocol too, so the same failure is expected there —
  untested, but do not assume Flatpak is a way around the AUR.
