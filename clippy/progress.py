"""Tiny GTK transfer-progress window for the sending device (Linux).

Shown only for large media sends to a paired peer (the sync engine decides the
threshold). Minimizable — the transfer keeps running in the background. One
window per in-flight item, keyed by name.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class ProgressManager:
    """Owns the per-transfer windows. Call update() on the GTK main thread."""

    def __init__(self):
        self._items = {}   # name -> (window, bar)

    def update(self, name: str, sent: int, total: int, done: bool) -> None:
        if done:
            item = self._items.pop(name, None)
            if item is not None:
                GLib.timeout_add(700, lambda w=item[0]: (w.destroy(), False)[1])
            return
        item = self._items.get(name)
        if item is None:
            item = self._build(name)
            self._items[name] = item
        win, bar = item
        frac = (sent / total) if total else 0.0
        bar.set_fraction(min(1.0, frac))
        bar.set_text(f"{int(frac * 100)}%  ·  {_human(sent)} / {_human(total)}")

    def _build(self, name):
        win = Gtk.Window(title="Clippy — sending")
        win.set_default_size(380, -1)
        win.set_resizable(False)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(False)   # keep it in the taskbar when minimized
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14); box.set_margin_bottom(14)
        box.set_margin_start(16); box.set_margin_end(16)
        lbl = Gtk.Label(label=f"Sending “{name}” to your devices…")
        lbl.set_xalign(0.0)
        lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        box.pack_start(lbl, False, False, 0)
        bar = Gtk.ProgressBar()
        bar.set_show_text(True)
        box.pack_start(bar, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        mini = Gtk.Button(label="Minimize")
        mini.connect("clicked", lambda _b: win.iconify())
        row.pack_end(mini, False, False, 0)
        box.pack_start(row, False, False, 0)
        win.add(box)
        win.show_all()
        return win, bar
