# Clippy on macOS — menubar app

Clippy's macOS build is a **menubar app** (no Dock icon, no terminal): it runs
the clipboard-sync engine and the macOS clipboard backend, and does pairing
entirely from the menu. There is no panel/history UI on macOS (that's the Linux
GTK feature) — the Mac is a sync peer for your Linux machines.

## Build it (on a Mac)

```bash
./packaging/macos/build-app.sh          # -> dist/Clippy.app
./packaging/macos/build-app.sh --dmg    # also a .dmg
```

This needs Xcode command-line tools + Python 3. It creates a throwaway venv,
installs the runtime deps (`pynacl`, `zeroconf`, `rumps`, `pyobjc`) **and**
`py2app`, then bundles them into `Clippy.app`. The resulting app is
self-contained — nothing to install afterwards.

Drag `Clippy.app` to `/Applications`. First launch on an unsigned build:
right-click → **Open** (Gatekeeper), then allow clipboard access if prompted.
Start at login via **System Settings → General → Login Items → +**.

## Use it (no CLI)

The menubar icon's menu has:
- **Show pairing code** — shows a 6-digit code; enter it on the other device.
- **Enter code…** — type the code shown on another device to pair.
- **Paired devices** — your peers (● online / ○ offline).

Pair with a Linux machine running Clippy ≥ 1.2.0 (Settings → Sync, or
`clippy pair`). Once paired, copies sync both ways over the LAN, encrypted.

## Notes / limitations

- **Firewall**: if the macOS Application Firewall is on, allow Clippy to accept
  **incoming connections** (System Settings → Network → Firewall → Options →
  Clippy → Allow), or pairing/sync will silently fail. Unsigned apps don't
  always get a clean "allow?" prompt — the app shows a one-time reminder, and
  code-signing makes the prompt appear naturally.
- **"Unidentified developer"** (Gatekeeper + the Login Items label) is unavoidable
  for an *unsigned* build. `build-app.sh` **ad-hoc signs** the app by default (a
  stable local identity so firewall/permission grants persist across rebuilds),
  but Gatekeeper still shows the label. To remove it entirely you need an Apple
  **Developer ID** (paid Apple Developer Program) — then build with:
  ```bash
  export CLIPPY_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
  export CLIPPY_NOTARY_PROFILE="clippy-notary"   # `xcrun notarytool store-credentials`
  ./packaging/macos/build-app.sh
  ```
  That signs (hardened runtime) + notarizes + staples, so it opens with no warning
  and shows your name. For **personal use without a paid account**, just
  right-click → **Open** once (or `xattr -dr com.apple.quarantine Clippy.app`); the
  Login Items label is cosmetic.
- macOS has no background clipboard push, so capture is a ~0.4 s `changeCount`
  poll (Linux capture stays instant). macOS 14+ may show "pasted from" notices.
- iPhone/iPad: use Apple **Universal Clipboard** (same Apple ID, Handoff) with
  this Mac as the bridge — no app needed, nothing for Clippy to do.
