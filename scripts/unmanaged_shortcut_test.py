#!/usr/bin/env python3
"""A hand-written Clippy binding is seen, but never rewritten.

Runs headless against a throwaway HOME — no compositor, and `_reload` is stubbed
so the developer's live Hyprland is never poked.

The bug this pins down: read_shortcut() reads only Clippy's managed block, so a
binding the user added themselves was invisible. `clippy status` said
"shortcut: not set" next to a binding that plainly worked, setup-shortcut told
them to do a job already done, and the settings picker would then write a second
bind on the same key without noticing the first.

The fix must not overcorrect. Clippy still writes only its own block, so the
guarantee that it never touches a line it did not write has to hold even now
that it can see those lines.

What it checks:

* a hand-written bind in bindings.lua is found, with its combo parsed;
* our own managed block is never reported as unmanaged;
* set_shortcut still leaves the hand-written line byte-for-byte intact, and
  remove_shortcut does too — seeing is not owning;
* bindings that merely contain the word "clippy" are not claimed;
* plain Hyprland looks in hyprland.conf, since clippy.conf is ours entirely.
"""
from __future__ import annotations

import os
import sys
import tempfile
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


def fresh_home():
    tmp = tempfile.mkdtemp(prefix="clippy-unmanaged-test-")
    os.environ["HOME"] = tmp
    for mod in [m for m in sys.modules if m.startswith("clippy")]:
        del sys.modules[mod]
    return Path(tmp)


def hyprland_desktop(hypr_files):
    """A HyprlandDesktop over a fresh HOME seeded with `hypr_files`."""
    home = fresh_home()
    hypr = home / ".config" / "hypr"
    hypr.mkdir(parents=True)
    for name, text in hypr_files.items():
        (hypr / name).write_text(text)
    from clippy.desktops.hyprland import HyprlandDesktop
    d = HyprlandDesktop()
    d._reload = lambda: None
    return d, hypr


# --- Omarchy: a bind the user wrote themselves ----------------------------
HAND_WRITTEN = (
    "-- my own bindings\n"
    'o.bind("SUPER + SHIFT + R", "SSH", "alacritty -e ssh box")\n'
    'o.bind("SUPER + SHIFT + V", "Clipboard history", "/usr/bin/clippy toggle")\n'
)


def test_finds_hand_written():
    print("finds a hand-written bind")
    d, _ = hyprland_desktop({"bindings.lua": HAND_WRITTEN})
    check("dialect is omarchy", d.omarchy, True)
    check("managed block: nothing", d.read_shortcut(), None)
    check("unmanaged: found and parsed",
          d.read_unmanaged_shortcut(), (["Super", "Shift"], "v"))


def test_our_own_block_is_not_unmanaged():
    print("our block is not unmanaged")
    d, _ = hyprland_desktop({"bindings.lua": "-- nothing here yet\n"})
    d.set_shortcut(["Super", "Ctrl"], "v", "/usr/bin/clippy toggle")
    check("managed block found", d.read_shortcut(), (["Super", "Ctrl"], "v"))
    # If our own block leaked through, status would append "(set by hand)" to
    # a binding the settings window is perfectly able to change.
    check("not also reported as unmanaged", d.read_unmanaged_shortcut(), None)


# --- seeing is not owning -------------------------------------------------
def test_hand_written_line_survives_writes():
    print("hand-written line survives")
    d, hypr = hyprland_desktop({"bindings.lua": HAND_WRITTEN})
    d.set_shortcut(["Super", "Alt"], "v", "/usr/bin/clippy toggle")
    after = (hypr / "bindings.lua").read_text()
    check("every original line still there",
          all(line in after for line in HAND_WRITTEN.splitlines() if line.strip()),
          True)
    check("both are now visible, separately",
          (d.read_shortcut(), d.read_unmanaged_shortcut()),
          ((["Super", "Alt"], "v"), (["Super", "Shift"], "v")))

    d.remove_shortcut()
    after = (hypr / "bindings.lua").read_text()
    check("removing ours leaves theirs",
          after.strip(), HAND_WRITTEN.strip())
    check("theirs still found after removal",
          d.read_unmanaged_shortcut(), (["Super", "Shift"], "v"))


# --- do not claim someone else's binding ----------------------------------
def test_does_not_overmatch():
    print("does not overmatch")
    for label, line in [
        ("a checkout path", 'o.bind("SUPER + P", "Proj", "code /home/me/src/clippy")'),
        ("a different program", 'o.bind("SUPER + D", "Dbg", "clippy-debug toggle")'),
        ("the daemon, not the panel", 'o.bind("SUPER + K", nil, "clippy daemon")'),
    ]:
        d, _ = hyprland_desktop({"bindings.lua": line + "\n"})
        check(label, d.read_unmanaged_shortcut(), None)

    # `nil` as the description is Omarchy's own documented form, so a real
    # binding written that way must still be found.
    d, _ = hyprland_desktop(
        {"bindings.lua": 'o.bind("SUPER + SHIFT + V", nil, "clippy toggle")\n'})
    check("nil description still matches",
          d.read_unmanaged_shortcut(), (["Super", "Shift"], "v"))


# --- plain Hyprland reads the user's own file -----------------------------
def test_plain_hyprland_reads_hyprland_conf():
    print("plain hyprland")
    d, _ = hyprland_desktop({
        "hyprland.conf": "bind = SUPER SHIFT, V, exec, /usr/bin/clippy toggle\n"})
    check("dialect is plain", d.omarchy, False)
    check("found in hyprland.conf",
          d.read_unmanaged_shortcut(), (["Super", "Shift"], "v"))


def main() -> int:
    real_home = os.environ.get("HOME")
    try:
        test_finds_hand_written()
        test_our_own_block_is_not_unmanaged()
        test_hand_written_line_survives_writes()
        test_does_not_overmatch()
        test_plain_hyprland_reads_hyprland_conf()
    finally:
        if real_home:
            os.environ["HOME"] = real_home
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("unmanaged shortcut: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
