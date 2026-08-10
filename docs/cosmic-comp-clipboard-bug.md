# Clipboard corruption: X11 → Wayland selection transfers ≥ 256 KiB gain a 4-byte length prefix

**Filed upstream as
[pop-os/cosmic-comp#2717](https://github.com/pop-os/cosmic-comp/issues/2717).**
This file is the working analysis behind that report; the issue is the canonical
place for any follow-up.

Reproducer: `scripts/cosmic_proxy_corruption_repro.py` in this repo (no Clippy
dependency — a GTK 4 X11 client and `wl-paste` are all it needs).

Clippy works around this by writing the Wayland selection itself in
`backends/wayland.py::copy_image` instead of relying on the compositor to
re-export what the X11 owner is serving. If the upstream fix lands, that
workaround stays harmless but stops being load-bearing.

## Summary

When an X11 client owns the `CLIPBOARD` selection and cosmic-comp re-exports it
to the Wayland selection, payloads of **262144 bytes (256 KiB) or larger** arrive
at Wayland consumers with **four extra bytes prepended**: the payload's own
length as a little-endian `uint32`. Smaller payloads cross intact, and X11
consumers always get the bytes intact.

The 256 KiB boundary is the X11 `INCR` threshold, so this looks like the `INCR`
transfer's initial size property being written into the data stream instead of
being consumed as a header.

## Impact

Any Wayland-native application pasting a large clip that originated on the X11
side receives a corrupt payload. For images this is the whole file: the leading
bytes are no longer a PNG signature, so the paste silently produces nothing.

It also breaks clipboard managers, which commonly serve history entries by
owning the X11 selection — that is how they reach XWayland clients. On
cosmic-comp that strategy silently corrupts every image over 256 KiB for
Wayland-native consumers. This was originally reported to us as "pasting a
screenshot into Claude Desktop gives an empty image"; Claude Desktop runs
`--ozone-platform=wayland`.

## Environment

| | |
|---|---|
| cosmic-comp | `0.1~1785355703~24.04~091583a` |
| Xwayland | `2:24.1.12-1pop2~1782241020~24.04~5ac8336` (runs with `-terminate`) |
| wl-clipboard | 2.2.1 |
| OS | Pop!_OS 24.04 |

## Reproduce

```
python3 scripts/cosmic_proxy_corruption_repro.py
```

It publishes a synthetic payload from a GTK 4 client forced onto `GDK_BACKEND=x11`
(so it is an XWayland client owning `CLIPBOARD`), then reads it back with `xclip`
(X11) and `wl-paste` (Wayland) at a range of sizes.

## Observed

```
   payload      X11 read    Wayland read  verdict
----------------------------------------------------------
     64 KB         65536           65536  ok
    128 KB        131072          131072  ok
    192 KB        196608          196608  ok
    256 KB        262144          262148  CORRUPT (+4 bytes, head 00 00 04 00)
    512 KB        524288          524292  CORRUPT (+4 bytes, head 00 00 08 00)
```

`00 00 04 00` little-endian is 262144; `00 00 08 00` is 524288. In both cases the
prefix equals the payload length exactly.

Real-world instance with a PNG, same clip read on each channel:

| Channel | Bytes | First 4 bytes |
|---|---|---|
| Source file | 281589 | `89 50 4e 47` (PNG) |
| X11 (`xclip -t image/png`) | 281589 | `89 50 4e 47` |
| Wayland (`wl-paste -t image/png`) | **281593** | **`f5 4b 04 00`** (= 281589 LE) |
| Control: same file set with `wl-copy` | 281589 | `89 50 4e 47` |

The control line matters: a payload placed directly on the Wayland selection is
returned intact, so the corruption is specific to the X11 → Wayland proxy path.

## Expected

Wayland consumers receive the payload byte-for-byte, at every size, exactly as
X11 consumers do.

## Where to look

`XwmHandler::send_selection` / the XWM selection transfer in cosmic-comp's
`src/xwayland.rs`, and the `INCR` path of smithay's
`src/xwayland/xwm/mod.rs`. The symptom is consistent with the first `INCR`
property (a `CARDINAL` holding the total transfer size) being forwarded into
the destination pipe rather than being read as the size announcement.

## Related, lower priority

While investigating we also found that a *latched* selection export can steal
the X11 `CLIPBOARD` from a live X11 owner: `SelectionHandler::new_selection`
stores `clipboard_selection_dirty` when no X11 window has focus
(`src/wayland/handlers/selection.rs`), and the next focus-into-X11 transition
replays it (`src/xwayland.rs`) with an unconditional `set_selection_owner`, even
if an X11 client has taken the selection in the meantime. Clearing the latch in
`XwmHandler::new_selection` when an X11 client takes ownership would make that
replay a no-op. This is separate from the corruption above and much less
damaging; happy to file it on its own if preferred.
