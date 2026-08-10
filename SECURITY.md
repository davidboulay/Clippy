# Security

## Reporting a vulnerability

Please report privately through GitHub:
**[Security → Report a vulnerability](https://github.com/davidboulay/Clippy/security/advisories/new)**.

Don't open a public issue for anything exploitable. Private reports go straight to
the maintainer, stay unlisted until there's a fix, and can be turned into a
published advisory afterwards.

Include what you have — a reproducer, the affected version (`clippy status` or
`dpkg -l clippy`), your desktop environment and compositor. Expect a first
response within about a week; this is a single-maintainer project, so please be
patient rather than escalating publicly.

## Supported versions

Only the **latest release** is supported. Fixes ship as a new version rather than
as patches to older ones — see [Releases](https://github.com/davidboulay/Clippy/releases).

## What Clippy actually handles

Worth being explicit, because it shapes what counts as a vulnerability here.

**Your clipboard history is sensitive by nature, and it is not encrypted at
rest.** Anything you copy — passwords out of a password manager, tokens, private
keys — is written to `~/.local/share/clippy/history.db` in plain text, along with
images and copied files under the same directory. This is inherent to what a
clipboard manager is, not a defect, but it means:

- The database is only as protected as your user account and home directory.
- Retention matters: *Settings → History retention* bounds how long a copied
  secret survives, and *Clear history* removes it now. Pinned items are kept
  until unpinned.
- Full-disk encryption is the right mitigation for the at-rest case.

If you'd rather a class of content were never recorded, that's a reasonable
feature request — open an issue.

## Security-relevant surfaces

These are the parts where a report is most likely to be a real vulnerability:

- **LAN clipboard sync** (opt-in, off by default). Pairing uses a SPAKE2
  password-authenticated key exchange keyed by the shown 6-digit code: neither
  side transmits the code or anything derived from it that an eavesdropper could
  offline-crack, and a wrong code cannot complete the handshake. Guessing is
  online-only and capped per pairing window. Paired peers are thereafter
  authenticated by long-term X25519 identity keys and traffic is encrypted with
  NaCl. The private key lives at `~/.local/share/clippy/identity.key` (`0600`)
  and trusted peers in `peers.json` (`0600`). Pairing, key handling, replay, and
  anything that lets an unpaired device read or inject clipboard content are all
  in scope.
- **The IPC control socket** in `$XDG_RUNTIME_DIR` (`0600`). It accepts commands
  that can read and set the clipboard, so anything that widens access to it, or
  gets a command executed that shouldn't be, is in scope.
- **The in-app updater.** *Settings → Check for updates* fetches release
  metadata from the GitHub API over HTTPS and installs the downloaded `.deb` as
  root via `pkexec`. Integrity rests on TLS to GitHub and on PolicyKit prompting
  you; the package is not independently signature-checked by Clippy. Anything
  that could redirect, substitute, or tamper with what gets installed is a
  serious report. On Debian/Ubuntu, installing from the
  [APT repository](README.md#linux-wayland) instead means `apt` verifies the
  repo's GPG signature, which is the stronger path.
- **The X11/XWayland selection owner.** Clippy runs a helper that owns the X11
  `CLIPBOARD` selection and serves clip contents to requesting clients — by
  design, since that is how clips reach XWayland apps. Ways to make it serve
  something it shouldn't, or to a client that shouldn't get it, are in scope.
- **Copied file handling.** Recovering a file stages a copy under
  `~/.local/share/clippy/paste/`; path traversal or symlink issues there are in
  scope.

## Out of scope

- **Clipboard contents being readable by other processes in your session.** On
  both Wayland and X11 the clipboard is session-wide by design; any app you run
  can read it. That's the platform, not Clippy.
- **The plaintext history database**, as described above — unless you've found a
  way for something *outside* your user account to reach it.
- **Compositor and toolkit bugs.** Clipboard behaviour on Wayland depends heavily
  on the compositor, and some misbehaviour originates there — see
  [`docs/cosmic-comp-clipboard-bug.md`](docs/cosmic-comp-clipboard-bug.md) for a
  worked example. Reports are still welcome; they may be redirected upstream.
- Anything requiring an attacker who already has your user account or root.
