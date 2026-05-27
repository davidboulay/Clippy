# Flatpak / Flathub — and why it does NOT work on COSMIC

> **Verdict: Clippy cannot be shipped as a Flatpak for COSMIC users.**
> Distribute the native **`.deb`** (and AUR) instead. The COSMIC Store, which
> lists Flathub apps, is therefore not a viable channel for this app.

## Why (confirmed empirically)

Clippy's two core mechanisms use **privileged Wayland protocols**:
- the panel uses **`zwlr_layer_shell_v1`** (layer-shell),
- clipboard history uses **`wlr/ext-data-control`** (via `wl-paste --watch`).

`cosmic-comp` (smithay) implements **`wp_security_context_v1`**, and Flatpak
(1.15+) attaches a security context to every sandboxed app. Smithay then
**withholds privileged protocols from security-context clients**. Result, when
run as a Flatpak on COSMIC:

```
your Wayland compositor does not support the Layer Shell protocol → Falling back to XDG shell
Watch mode requires a compositor that supports the wlroots data-control protocol
```

The native package (no security context) sees both protocols and works; the
sandboxed Flatpak does not. **No Flatpak permission can re-enable them** — that
is the explicit purpose of the security context. This affects clipboard
managers and layer-shell bars generally, which is why working examples are
essentially absent from Flathub on Wayland.

## So how do COSMIC users get Clippy?

- **`.deb`** from GitHub Releases (built by CI): `sudo apt install ./clippy_*.deb`
- **AUR** (`packaging/arch/PKGBUILD`) for Arch-based distros
- A personal apt repo if you want `apt`-style updates

## The manifest (kept for non-security-context compositors)

`packaging/flatpak/io.github.davidboulay.Clippy.yaml` builds correctly (it
bundles `gtk-layer-shell` + `wl-clipboard`). On compositors that do **not**
gate privileged protocols behind a security context (some sway/labwc setups),
the Flatpak may work. To actually submit to Flathub you would still need to:
pin the app source to a release tag+commit (currently `branch: main`), bump the
runtime to a supported GNOME version (47 is EOL), and pass `flatpak-builder-lint`.
But for COSMIC — the target audience — it will not function, so we are not
pursuing it.
