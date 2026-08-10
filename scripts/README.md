# Scripts

Install helpers and the test suite. **Run everything with `PYTHONPATH=.` from the
repo root** — without it, `import clippy` resolves to the installed package in
`/usr/lib/python3/dist-packages` and you will be testing the last release
instead of your working tree. That failure is silent when the installed copy
happens to pass.

```sh
PYTHONPATH=. python3 scripts/<name>.py
```

## Runs in CI (headless, no clipboard touched)

These monkeypatch the clipboard backend or use a throwaway `XDG_DATA_HOME`, so
they are safe anywhere and run on every PR.

| Script | What it pins down |
|---|---|
| `capture_heuristics_test.py` | image magic sniffing, telling a spreadsheet's bitmap render apart from a real image copy, html → text |
| `recover_flavors_test.py` | which MIME flavors each recover path offers, and the ownership digests it records |
| `capture_staging_test.py` | our own staged-file echo is ignored without swallowing genuine file recovers |
| `storage_migration_test.py` | the `UNIQUE(kind, hash)` rebuild preserves every row; new code still works against the *old* schema |
| `resilience_test.py` | corrupt-database errors reach callers as `OSError`, IPC stays responsive during a slow command, sound-debounce races |
| `x11_takeover_gate_test.py` | capture-time X11 takeover stays opt-in, echo-safe and rate-capped |
| `sync_selftest.py`, `sync_delivery_test.py`, `sync_drift_test.py`, `sync_readvertise_test.py` | LAN sync: crypto, delivery hardening, device-id drift, mDNS refresh |
| `mac_selector_test.py`, `mac_pasteboard_test.py` | PyObjC selector prototypes and macOS pasteboard MIME honesty (no Mac required) |

## Needs a real display — **not** in CI

These drive actual Xwayland clients and take the clipboard while they run. Each
one saves the current clipboard and restores it afterwards, but don't run them in
the middle of something you care about. They need `$DISPLAY` and GTK 4, and they
exit cleanly (skipping) when there's no X display.

| Script | What it measures |
|---|---|
| `x11clip_protocol_test.py` | the helper's ready/ACK handshake against real Xwayland — that a successful publish means the clip is genuinely being served, and that failure is reported fast rather than swallowed |
| `image_roundtrip_test.py` | a recovered image comes back byte-exact on **both** the X11 and Wayland channels, below and above the 256 KiB threshold where the compositor corrupts proxied clips |
| `cosmic_proxy_corruption_repro.py` | a standalone reproducer for that compositor bug, with no Clippy involvement — attach its output to an upstream report (see `docs/cosmic-comp-clipboard-bug.md`) |
| `latch_experiment.py` | passively records X11 CLIPBOARD ownership changes against keyboard focus. Run `--steps` for the procedure. Purely an observer: it never takes focus, maps a window, or sets a selection — which matters, because a *focused* probe is itself what triggers the compositor behaviour under investigation, and that ruined three earlier attempts at diagnosing it |

## Install helpers

`install.sh` and `uninstall.sh` (`--purge` also removes history and settings).
