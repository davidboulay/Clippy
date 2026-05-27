"""Clippy's panel: a clipboard-tile strip anchored to the bottom of the screen
via wlr-layer-shell, shown as a full-screen overlay so clicking away dismisses
it. Only this module (and tray/settings_window) imports GTK.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, GtkLayerShell, Pango  # noqa: E402

from . import clipboard, config, settings, storage
from .storage import Entry


def _relative_time(ts: float) -> str:
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} d ago"


def _icon_pixbuf(size: int) -> Optional[GdkPixbuf.Pixbuf]:
    for path in (config.ICON_PATH, config.BUNDLED_ICON):
        if path.exists():
            try:
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(path), size, size, True
                )
            except Exception:
                continue
    return None


class Tile(Gtk.EventBox):
    """One clipboard entry rendered as a card."""

    def __init__(self, entry: Entry, panel: "Panel"):
        super().__init__()
        self.entry = entry
        self._panel = panel

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.get_style_context().add_class("tile")
        self.card = card
        self.add(card)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        badge = Gtk.Label(label="IMAGE" if entry.is_image else "TEXT")
        badge.get_style_context().add_class("badge")
        badge.get_style_context().add_class(
            "badge-image" if entry.is_image else "badge-text"
        )
        header.pack_start(badge, False, False, 0)

        if entry.has_formatting:
            rich = Gtk.Label(label="rich")
            rich.get_style_context().add_class("badge")
            rich.get_style_context().add_class("badge-text")
            rich.set_tooltip_text("Has formatting")
            header.pack_start(rich, False, False, 0)

        if entry.pinned:
            pin_marker = Gtk.Label(label="★")
            pin_marker.get_style_context().add_class("pin-marker")
            header.pack_start(pin_marker, False, False, 0)

        del_btn = Gtk.Button(label="×")
        del_btn.get_style_context().add_class("tile-action")
        del_btn.set_tooltip_text("Delete")
        del_btn.connect("clicked", lambda _b: self._panel.delete_entry(self.entry.id))
        header.pack_end(del_btn, False, False, 0)

        pin_btn = Gtk.Button(label="★" if entry.pinned else "☆")
        pin_btn.get_style_context().add_class("tile-action")
        pin_btn.set_tooltip_text("Pin / unpin")
        pin_btn.connect("clicked", lambda _b: self._panel.pin_entry(self.entry.id))
        header.pack_end(pin_btn, False, False, 0)

        card.pack_start(header, False, False, 0)
        card.pack_start(self._build_content(entry), True, True, 0)

        footer = Gtk.Label()
        footer.set_xalign(0.0)
        footer.get_style_context().add_class("meta")
        footer.set_text(self._meta_text(entry))
        footer.set_ellipsize(Pango.EllipsizeMode.END)
        card.pack_start(footer, False, False, 0)

        self.set_size_request(config.TILE_WIDTH, config.TILE_HEIGHT)
        self.connect("button-press-event", self._on_click)

    def _build_content(self, entry: Entry) -> Gtk.Widget:
        if entry.is_image and entry.image_path:
            inner = self._load_image(entry.image_path)
            if inner is None:
                inner = Gtk.Label(label="[image unavailable]")
                inner.get_style_context().add_class("preview-text")
        else:
            inner = Gtk.Label()
            inner.set_xalign(0.0)
            inner.set_yalign(0.0)
            inner.set_line_wrap(True)
            inner.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            inner.set_max_width_chars(30)
            inner.set_lines(10)  # cap to 10 lines, then ellipsize
            inner.set_ellipsize(Pango.EllipsizeMode.END)
            inner.set_text((entry.text or "").strip()[:800])
            inner.get_style_context().add_class("preview-text")

        # EXTERNAL (not NEVER) clips overflow without a scrollbar and does NOT
        # grow to fit the child; with a capped content height every tile stays
        # exactly the same size no matter how long the content is.
        clip = Gtk.ScrolledWindow()
        clip.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        clip.set_propagate_natural_height(False)
        clip.set_propagate_natural_width(False)
        clip.set_min_content_height(config.TILE_CONTENT_HEIGHT)
        clip.set_max_content_height(config.TILE_CONTENT_HEIGHT)
        clip.set_size_request(-1, config.TILE_CONTENT_HEIGHT)
        clip.get_style_context().add_class("tile-content")
        clip.add(inner)
        return clip

    @staticmethod
    def _load_image(path: str) -> Optional[Gtk.Image]:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, config.TILE_WIDTH - 24, config.TILE_CONTENT_HEIGHT, True
            )
        except (GLib.Error, OSError):
            return None
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.set_halign(Gtk.Align.CENTER)
        image.set_valign(Gtk.Align.CENTER)
        image.get_style_context().add_class("preview-image")
        return image

    def _meta_text(self, entry: Entry) -> str:
        when = _relative_time(entry.created_at)
        if entry.is_image:
            return f"{when}  ·  {max(1, entry.size // 1024)} KB"
        text = entry.text or ""
        lines = text.count("\n") + 1
        if lines > 1:
            return f"{when}  ·  {len(text)} chars · {lines} lines"
        return f"{when}  ·  {len(text)} chars"

    def set_selected(self, selected: bool) -> None:
        ctx = self.card.get_style_context()
        (ctx.add_class if selected else ctx.remove_class)("selected")

    def _on_click(self, _widget, event) -> bool:
        self._panel.select_tile(self)
        if event.button == Gdk.BUTTON_PRIMARY:
            self._panel.paste_entry(self.entry)
        elif event.button == Gdk.BUTTON_SECONDARY:
            self._panel.show_context_menu(self.entry, event)
        elif event.button == Gdk.BUTTON_MIDDLE:
            self._panel.delete_entry(self.entry.id)
        return True


class Panel:
    def __init__(self, controller):
        self._controller = controller
        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_app_paintable(True)
        self.window.get_style_context().add_class("clippy-overlay")

        screen = self.window.get_screen()
        visual = screen.get_rgba_visual() if screen is not None else None
        if visual is not None:
            self.window.set_visual(visual)

        self._init_layer_shell()

        self._tiles: List[Tile] = []
        self._selected = -1
        self._visible = False
        self._shown_at = 0.0

        # Non-modal bottom strip: the window *is* the panel (no full-screen
        # backdrop), so the COSMIC panel and other apps stay clickable. Click-
        # away dismissal is handled by hiding on focus-out (see _on_focus_out).
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.get_style_context().add_class("panel-body")
        self.window.add(body)

        body.pack_start(self._build_header(), False, False, 0)

        # Inline action bar (our "context menu"): rendered inside the surface
        # rather than as a popup, which compositors can dismiss on layer-shell
        # surfaces. Hidden until a tile is right-clicked.
        self.action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.action_bar.get_style_context().add_class("action-bar")
        body.pack_start(self.action_bar, False, False, 0)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.scroller.get_style_context().add_class("strip")
        self.scroller.set_min_content_height(config.TILE_HEIGHT + 12)
        self.strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.strip.get_style_context().add_class("strip-inner")
        self.scroller.add(self.strip)
        body.pack_start(self.scroller, True, True, 0)

        self.empty_label = Gtk.Label(
            label="Clipboard history is empty.\nCopy something and it will appear here."
        )
        self.empty_label.get_style_context().add_class("empty")
        self.empty_label.set_justify(Gtk.Justification.CENTER)

        hint = Gtk.Label(
            label="←/→ navigate   ↵ paste   right-click for options   "
                  "☆ pin   Del delete   Esc close"
        )
        hint.get_style_context().add_class("hint")
        body.pack_start(hint, False, False, 0)

        self.window.connect("key-press-event", self._on_key)
        self.window.connect("focus-out-event", self._on_focus_out)
        self.window.connect("delete-event", lambda *_: (self.hide(), True)[1])

    def _build_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.get_style_context().add_class("header")

        pix = _icon_pixbuf(22)
        if pix is not None:
            header.pack_start(Gtk.Image.new_from_pixbuf(pix), False, False, 0)

        title = Gtk.Label(label="Clippy")
        title.get_style_context().add_class("title")
        header.pack_start(title, False, False, 0)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search clipboard history…")
        self.search.get_style_context().add_class("search")
        self.search.connect("search-changed", lambda _e: self.reload())
        header.pack_start(self.search, True, True, 0)

        self.count_label = Gtk.Label(label="")
        self.count_label.get_style_context().add_class("count")
        header.pack_end(self.count_label, False, False, 0)

        gear = Gtk.Button(label="⚙")
        gear.get_style_context().add_class("iconbtn")
        gear.set_tooltip_text("Settings")
        gear.connect("clicked", self._on_settings)
        header.pack_end(gear, False, False, 0)
        return header

    def _init_layer_shell(self) -> None:
        win = self.window
        GtkLayerShell.init_for_window(win)
        GtkLayerShell.set_namespace(win, "clippy")
        # OVERLAY, anchored to the bottom edge + sides: a bottom strip sized to
        # its content that draws *over* the dock. It only covers the bottom
        # region, so the COSMIC top panel and other apps stay fully clickable.
        GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
        for edge in (
            GtkLayerShell.Edge.BOTTOM,
            GtkLayerShell.Edge.LEFT,
            GtkLayerShell.Edge.RIGHT,
        ):
            GtkLayerShell.set_anchor(win, edge, True)
        GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.TOP, False)
        # ON_DEMAND, not EXCLUSIVE: we don't hold a session-wide keyboard grab,
        # so e.g. the COSMIC panel's own right-click menu can still take focus.
        # The panel yields focus when you click away — see _on_focus_out.
        GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.ON_DEMAND)
        # -1: ignore the dock's exclusive zone and anchor to the true screen
        # edge, so the strip overlaps (covers) the dock rather than sitting above it.
        GtkLayerShell.set_exclusive_zone(win, -1)

    def _set_active_monitor(self) -> None:
        """Show the strip on the monitor under the pointer (best effort)."""
        try:
            display = Gdk.Display.get_default()
            monitor = None
            seat = display.get_default_seat() if display else None
            pointer = seat.get_pointer() if seat else None
            if pointer is not None:
                _screen, x, y = pointer.get_position()
                monitor = display.get_monitor_at_point(x, y)
            if monitor is None and display is not None:
                monitor = display.get_primary_monitor() or (
                    display.get_monitor(0) if display.get_n_monitors() else None
                )
            if monitor is not None:
                GtkLayerShell.set_monitor(self.window, monitor)
        except Exception:
            pass

    # -- model / rendering ------------------------------------------------
    def reload(self) -> None:
        self.action_bar.hide()
        query = self.search.get_text().strip()
        entries = storage.list_entries(query=query)

        for child in self.strip.get_children():
            self.strip.remove(child)
        self._tiles = []

        if not entries:
            if self.empty_label.get_parent() is None:
                self.strip.pack_start(self.empty_label, True, True, 0)
        else:
            if self.empty_label.get_parent() is not None:
                self.strip.remove(self.empty_label)
            for entry in entries:
                tile = Tile(entry, self)
                self._tiles.append(tile)
                self.strip.pack_start(tile, False, False, 0)

        total = len(storage.list_entries())
        if query:
            self.count_label.set_text(f"{len(entries)} of {total}")
        else:
            self.count_label.set_text(f"{total} item{'s' if total != 1 else ''}")

        self.strip.show_all()
        self._selected = 0 if self._tiles else -1
        self._refresh_selection()

    def _refresh_selection(self) -> None:
        for i, tile in enumerate(self._tiles):
            tile.set_selected(i == self._selected)
        self._scroll_to_selected()

    def _scroll_to_selected(self) -> None:
        if not (0 <= self._selected < len(self._tiles)):
            return
        tile = self._tiles[self._selected]

        def do_scroll():
            alloc = tile.get_allocation()
            adj = self.scroller.get_hadjustment()
            page = adj.get_page_size()
            target = alloc.x - (page - alloc.width) / 2
            adj.set_value(max(adj.get_lower(), min(target, adj.get_upper() - page)))
            return False

        GLib.idle_add(do_scroll)

    # -- selection / actions ---------------------------------------------
    def select_tile(self, tile: Tile) -> None:
        if tile in self._tiles:
            self._selected = self._tiles.index(tile)
            self._refresh_selection()

    def _move(self, delta: int) -> None:
        if not self._tiles:
            return
        self._selected = max(0, min(self._selected + delta, len(self._tiles) - 1))
        self._refresh_selection()

    def activate_selected(self) -> None:
        if 0 <= self._selected < len(self._tiles):
            self.paste_entry(self._tiles[self._selected].entry)

    def paste_entry(self, entry: Entry, mode: str = "auto") -> None:
        """Load an entry back onto the clipboard, then close.

        mode: 'auto' (respect the always-plain-text setting), 'plain', 'rich'.
        """
        try:
            if entry.is_image and entry.image_path:
                clipboard.copy_image(Path(entry.image_path).read_bytes(),
                                     entry.mime or "image/png")
            else:
                always_plain = bool(settings.get("always_plain_text"))
                use_rich = (
                    entry.html
                    and mode != "plain"
                    and (mode == "rich" or not always_plain)
                )
                if use_rich:
                    clipboard.copy_html(entry.html)
                else:
                    clipboard.copy_text(entry.text or "")
        except OSError:
            pass
        self.hide()

    def show_context_menu(self, entry: Entry, _event=None) -> None:
        """Populate and reveal the inline action bar for an entry."""
        for child in self.action_bar.get_children():
            self.action_bar.remove(child)

        title = Gtk.Label(label="Image" if entry.is_image else "Text")
        title.get_style_context().add_class("action-label")
        self.action_bar.pack_start(title, False, False, 0)

        def add(label, cb, danger=False):
            b = Gtk.Button(label=label)
            ctx = b.get_style_context()
            ctx.add_class("action-btn")
            if danger:
                ctx.add_class("danger")
            b.connect("clicked", lambda _b: cb())
            self.action_bar.pack_start(b, False, False, 0)

        if entry.is_image:
            add("Paste", lambda: self.paste_entry(entry))
        else:
            add("Paste", lambda: self.paste_entry(entry, "auto"))
            add("Copy as plain text", lambda: self.paste_entry(entry, "plain"))
            if entry.has_formatting:
                add("Copy with formatting", lambda: self.paste_entry(entry, "rich"))
        add("Unpin" if entry.pinned else "Pin", lambda: self.pin_entry(entry.id))
        add("Delete", lambda: self.delete_entry(entry.id), danger=True)

        cancel = Gtk.Button(label="✕")
        cancel.get_style_context().add_class("action-btn")
        cancel.set_tooltip_text("Close menu")
        cancel.connect("clicked", lambda _b: self._hide_actions())
        self.action_bar.pack_end(cancel, False, False, 0)

        self.action_bar.show_all()

    def _hide_actions(self) -> None:
        self.action_bar.hide()

    def _on_focus_out(self, _widget, _event) -> bool:
        # Click-away dismissal: when the strip loses keyboard focus (you clicked
        # the COSMIC panel, another window, or the desktop), retract. Ignore the
        # brief focus settle right after showing.
        if self._visible and (time.monotonic() - self._shown_at) > 0.25:
            self.hide()
        return False

    def delete_entry(self, entry_id: int) -> None:
        storage.delete(entry_id)
        prev = self._selected
        self.reload()
        if self._tiles:
            self._selected = min(prev, len(self._tiles) - 1)
            self._refresh_selection()

    def pin_entry(self, entry_id: int) -> None:
        storage.toggle_pin(entry_id)
        self.reload()

    def pin_selected(self) -> None:
        if 0 <= self._selected < len(self._tiles):
            self.pin_entry(self._tiles[self._selected].entry.id)

    def delete_selected(self) -> None:
        if 0 <= self._selected < len(self._tiles):
            self.delete_entry(self._tiles[self._selected].entry.id)

    # -- key handling -----------------------------------------------------
    def _on_key(self, _widget, event) -> bool:
        keyval = event.keyval
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)

        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.activate_selected()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Up):
            self._move(-1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Down):
            self._move(1)
            return True
        if keyval == Gdk.KEY_Home:
            self._selected = 0 if self._tiles else -1
            self._refresh_selection()
            return True
        if keyval == Gdk.KEY_End:
            self._selected = len(self._tiles) - 1
            self._refresh_selection()
            return True
        if keyval == Gdk.KEY_Delete:
            self.delete_selected()
            return True
        if ctrl and keyval in (Gdk.KEY_p, Gdk.KEY_P):
            self.pin_selected()
            return True
        if ctrl and Gdk.KEY_1 <= keyval <= Gdk.KEY_9:
            idx = keyval - Gdk.KEY_1
            if idx < len(self._tiles):
                self._selected = idx
                self.activate_selected()
            return True
        return False  # let typing flow to the search entry

    def _on_settings(self, _btn) -> None:
        self.hide()
        self._controller.open_settings()

    # -- show / hide ------------------------------------------------------
    def show(self) -> None:
        self._controller.refresh_theme()
        self._set_active_monitor()
        self.reload()
        self.window.show_all()
        self.action_bar.hide()  # show_all reveals it; keep hidden until invoked
        self._visible = True
        self._shown_at = time.monotonic()
        self.search.grab_focus()

    def hide(self) -> None:
        self.action_bar.hide()
        self.window.hide()
        self.search.set_text("")
        self._visible = False

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def handle_command(self, command: str) -> bool:
        if command == "toggle":
            self.toggle()
        elif command == "show":
            self.show()
        elif command == "hide":
            self.hide()
        elif command == "refresh":
            if self._visible:
                self.reload()
        return False
