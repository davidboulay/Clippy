#!/usr/bin/env python3
"""Record X11 CLIPBOARD ownership against keyboard focus, to test the latch model.

The model (read from cosmic-comp/smithay sources) says: a Wayland copy is
exported to X11 only while an X11 window holds keyboard focus; otherwise the
compositor *latches* it and fires it at the next focus-into-X11 transition,
stealing the X11 selection from whoever owns it — including Clippy's helper.
If that is right, an image recovered from Clippy's panel dies at the first
paste attempt into an X11 app, and a second recover sticks. That is exactly the
"only works if I re-click the tile" symptom.

Three previous attempts at fixing this failed partly because the instrument was
the problem: a focused GTK probe is itself a latch trigger, so measuring
destroyed what was being measured. This tool never takes focus, never maps a
window, and never sets a selection. It only watches — pairing every ownership
change with what had keyboard focus at that moment, so a steal can be attributed
instead of guessed at.

Usage:
    python3 scripts/latch_experiment.py          # record until Ctrl-C
    python3 scripts/latch_experiment.py --steps  # print the experiment script
"""
from __future__ import annotations

import json
import sys
import time

try:
    from Xlib import X, display
except ImportError:
    print("needs python3-xlib:  sudo apt install python3-xlib")
    sys.exit(1)

STEPS = """\
The experiment — do these in order, slowly, and don't rush between steps.
Leave this recorder running the whole time.

  1. Copy an image in a Wayland app (a COSMIC screenshot, or Copy Image in
     Firefox/Chromium). Do NOT click on any X11 app yet.
       -> expect: 'wayland copy, latch armed' (no X11 owner appears)

  2. Open Clippy (Ctrl+Shift+V) and click that image tile. Still don't touch
     any X11 app.
       -> expect: 'clippy helper took CLIPBOARD'

  3. NOW focus Claude Desktop and press Ctrl+V.
       -> the model predicts: focus fires the latch, the XWM proxy STEALS the
          selection from Clippy, and the paste is empty or hangs.
       -> if instead it pastes fine, the model is wrong and Phase 3 needs a
          rethink before any code is written.

  4. Without copying anything new, open Clippy again and click the SAME tile.
     Then paste into Claude Desktop again.
       -> the model predicts: this one sticks (the latch was consumed in
          step 3), which is exactly the 're-click and it works' symptom.

Then stop the recorder with Ctrl-C and share the summary it prints.
"""

if "--steps" in sys.argv:
    print(STEPS)
    sys.exit(0)

d = display.Display()
root = d.screen().root
CLIPBOARD = d.intern_atom("CLIPBOARD")

if not d.has_extension("XFIXES"):
    print("no XFIXES extension — cannot watch selection ownership")
    sys.exit(1)
d.xfixes_query_version()

# An unmapped 1x1 window, purely to receive events. Never shown, never focused.
win = root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
d.xfixes_select_selection_input(win, CLIPBOARD, 1 | 2 | 4)
d.flush()

KIND = {0: "owner-changed", 1: "owner-window-destroyed", 2: "owner-client-gone"}


def describe(window_id):
    """Name the owner: Clippy's helper, the compositor's X11 proxy, or an app.

    The XWM proxy is the compositor's own stand-in window — unmapped and
    nameless. A real app has a WM_CLASS. Clippy's helper is a GTK client, so it
    carries a WM_CLASS naming the python process."""
    if not window_id:
        return "nobody", None
    try:
        w = d.create_resource_object("window", window_id)
        cls = w.get_wm_class()
        if cls:
            name = "/".join(cls)
            if "clippy" in name.lower() or "python" in name.lower():
                return f"CLIPPY HELPER ({name})", name
            return f"app: {name}", name
        return "compositor XWM proxy (nameless)", None
    except Exception:
        return "unknown", None


def focused_is_x11():
    """Whether an X11 window currently holds the input focus, and its name.

    This is the trigger the model turns on: X11 focus is what makes the
    compositor flush a latched selection onto the X11 side."""
    try:
        f = d.get_input_focus().focus
        if not isinstance(f, int) and f is not None:
            cls = f.get_wm_class()
            if cls:
                return True, "/".join(cls)
            return True, "(nameless X11 window)"
    except Exception:
        pass
    return False, None


events = []
print("recording — leave this running. Ctrl-C to stop and print the summary.")
print("(run with --steps to see what to do)\n")
start = time.time()
last_focus = focused_is_x11()

try:
    while True:
        # Poll focus alongside the event stream: a focus change with no matching
        # X event is exactly the moment the model says a steal should happen.
        cur_focus = focused_is_x11()
        if cur_focus != last_focus:
            stamp = round(time.time() - start, 2)
            where = cur_focus[1] if cur_focus[0] else "a Wayland app"
            print(f"[{stamp:7.2f}s] focus -> {where}")
            events.append({"t": stamp, "type": "focus", "x11": cur_focus[0],
                           "who": where})
            last_focus = cur_focus

        while d.pending_events():
            ev = d.next_event()
            if not hasattr(ev, "selection") or ev.selection != CLIPBOARD:
                continue
            stamp = round(time.time() - start, 2)
            owner = getattr(ev, "owner", None)
            oid = owner.id if owner else 0
            label, _ = describe(oid)
            kind = KIND.get(getattr(ev, "subtype", 0), "?")
            x11_focused, who = focused_is_x11()
            print(f"[{stamp:7.2f}s] CLIPBOARD {kind}: {label}"
                  f"{'   <-- while X11 focused: ' + who if x11_focused else ''}")
            events.append({"t": stamp, "type": "owner", "kind": kind,
                           "owner": hex(oid), "label": label,
                           "x11_focused": x11_focused})
        time.sleep(0.05)
except KeyboardInterrupt:
    pass

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
owners = [e for e in events if e["type"] == "owner"]
steals = [e for e in owners
          if "proxy" in e["label"] and e["x11_focused"]]
clippy_grabs = [e for e in owners if "CLIPPY" in e["label"]]
print(f"ownership changes:            {len(owners)}")
print(f"times Clippy's helper took it: {len(clippy_grabs)}")
print(f"proxy takeovers while an X11 window had focus: {len(steals)}")
if steals and clippy_grabs:
    print("\nVERDICT: consistent with the latch model — the compositor's proxy")
    print("took the selection while focus was entering an X11 window, after")
    print("Clippy had it. Event-driven re-assert (Phase 3) is the right fix.")
elif clippy_grabs and not steals:
    print("\nVERDICT: Clippy held the selection and was NOT robbed on focus.")
    print("The latch model does not explain this run — do not write Phase 3")
    print("code yet; re-run and check whether the paste actually failed.")
else:
    print("\nVERDICT: not enough happened to conclude anything. Re-run and")
    print("follow --steps exactly.")

path = "/tmp/clippy-latch-experiment.json"
try:
    with open(path, "w") as fh:
        json.dump(events, fh, indent=1)
    print(f"\nfull trace: {path}")
except OSError:
    pass
