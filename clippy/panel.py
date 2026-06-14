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
gi.require_version("Gdk", "3.0")  # else an unversioned Gdk import can grab GTK4
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, GtkLayerShell, Pango  # noqa: E402

from . import clipboard, config, settings, storage
from .storage import Entry

# Opening cost scales with the number of tiles built (each image tile decodes a
# thumbnail). Only ~8 tiles fit on screen, so we build the first screenful
# before mapping the window and stream the rest in idle chunks afterwards.
_FIRST_BATCH = 12   # tiles built synchronously, before the window appears
_STREAM_CHUNK = 12  # tiles appended per idle tick after the window is up


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
        badge = Gtk.Label(label="IMAGE" if entry.is_image
                          else ("FILE" if entry.is_file else "TEXT"))
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

    _IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif",
                   "heic", "heif", "avif", "ico", "svg"}
    _VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "mkv", "avi", "wmv", "flv", "mpg", "mpeg"}

    def _build_content(self, entry: Entry) -> Gtk.Widget:
        inner = self._render_preview(entry)
        if inner is None and entry.is_image:
            inner = Gtk.Label(label="[image unavailable]")
            inner.get_style_context().add_class("preview-text")
        if inner is None:
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

    @staticmethod
    def _ext(name: str) -> str:
        import os
        return os.path.splitext(name or "")[1].lstrip(".").lower()

    def _render_preview(self, entry: Entry) -> Optional[Gtk.Widget]:
        """A thumbnail for image/video entries, or a file card for other files."""
        import os
        path = entry.image_path
        if not path:
            return None
        mime = (entry.mime or "").lower()
        name = entry.filename or entry.text or ""
        ext = self._ext(name)
        # Images (incl. image files synced from a peer) — GdkPixbuf sniffs by
        # content, so the digest-named blob renders fine.
        if entry.is_image or mime.startswith("image/") or ext in self._IMAGE_EXTS:
            img = self._load_image(path)
            if img is not None:
                return img
        # Videos — grab a frame with ffmpeg (cached).
        if mime.startswith("video/") or ext in self._VIDEO_EXTS:
            thumb = self._video_thumb(path, entry.hash or os.path.basename(path))
            if thumb:
                img = self._load_image(thumb)
                if img is not None:
                    return img
            return self._file_card(name, "VIDEO")
        # Any other file: a clean type card instead of the raw filename text.
        if entry.is_file:
            return self._file_card(name, ext.upper() or "FILE")
        return None

    @staticmethod
    def _video_thumb(path: str, key: str) -> Optional[str]:
        """Extract+cache a single video frame as a thumbnail. ffmpeg, best-effort."""
        import os
        import shutil
        import subprocess
        if not shutil.which("ffmpeg"):
            return None
        out = config.THUMB_DIR / f"{os.path.basename(key)}.jpg"
        if out.exists():
            return str(out)
        try:
            config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "1", "-i", path, "-frames:v", "1",
                 "-vf", "scale='min(206,iw)':-2", "-q:v", "5", str(out)],
                capture_output=True, timeout=8,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return str(out) if out.exists() else None

    def _file_card(self, name: str, label: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        kind = Gtk.Label(label=(label or "FILE")[:6])
        kind.get_style_context().add_class("badge")
        kind.get_style_context().add_class("badge-text")
        box.pack_start(kind, False, False, 0)
        fn = Gtk.Label(label=name or "file")
        fn.set_max_width_chars(24)
        fn.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        fn.set_justify(Gtk.Justification.CENTER)
        fn.get_style_context().add_class("preview-text")
        box.pack_start(fn, False, False, 0)
        return box

    def _meta_text(self, entry: Entry) -> str:
        when = _relative_time(entry.created_at)
        if entry.is_image or entry.is_file:
            sz = entry.size or 0
            human = (f"{sz / 1024 / 1024:.1f} MB" if sz >= 1024 * 1024
                     else f"{max(1, sz // 1024)} KB")
            return f"{when}  ·  {human}"
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
        self._tab = "history"  # "history" (unpinned) | "pinned"
        self._switching_tab = False
        # Per-tab render cache: switching tabs reuses already-built tiles
        # instead of reconstructing widgets (and re-decoding images) every
        # time. Invalidated whenever the underlying data changes.
        self._tile_cache: dict = {}
        # Bumped on every reload so an in-flight streamed build can detect it
        # has been superseded and stop.
        self._build_seq = 0

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

        self.tab_history = Gtk.ToggleButton(label="History")
        self.tab_history.get_style_context().add_class("tab")
        self.tab_history.set_active(True)
        self.tab_history.connect("toggled", self._on_tab, "history")
        header.pack_start(self.tab_history, False, False, 0)

        self.tab_pinned = Gtk.ToggleButton(label="★ Pinned")
        self.tab_pinned.get_style_context().add_class("tab")
        self.tab_pinned.connect("toggled", self._on_tab, "pinned")
        header.pack_start(self.tab_pinned, False, False, 0)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search clipboard history…")
        self.search.get_style_context().add_class("search")
        self.search.connect("search-changed", self._on_search)
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
        pinned = self._tab == "pinned"
        self._build_seq += 1  # supersede any in-flight streamed build

        # Fast path: a fully-built tab is cached — re-pack its tiles instantly
        # (no widget construction, no image decoding). Switching tabs is a
        # frequent action, so this keeps it snappy. Bypassed during search; the
        # cache is dropped whenever the data changes (pin/delete/new copy/open).
        cached = self._tile_cache.get(self._tab) if not query else None
        if cached is not None:
            self._mount(cached["tiles"], cached["total"],
                        cached["pinned_total"], pinned, query)
            return

        entries = storage.list_entries(
            query=query, limit=config.DISPLAY_LIMIT, pinned=pinned
        )
        total = storage.count(pinned=pinned)
        pinned_total = total if pinned else storage.count(pinned=True)

        self._reset_strip()
        self._update_header(len(entries), total, pinned_total, query)
        if not entries:
            self._mount_empty(pinned)
            return

        if self.empty_label.get_parent() is not None:
            self.strip.remove(self.empty_label)

        # Build only the first screenful now; stream the rest in idle chunks so
        # the window maps immediately instead of after decoding every tile.
        first, rest = entries[:_FIRST_BATCH], entries[_FIRST_BATCH:]
        self._tiles = [self._make_tile(e) for e in first]
        self.strip.show_all()
        self._selected = 0
        self._refresh_selection()

        if rest:
            GLib.idle_add(self._stream_tiles, rest, self._build_seq,
                          total, pinned_total, query)
        else:
            self._cache_current(query, total, pinned_total)

    def _stream_tiles(self, entries, seq, total, pinned_total, query) -> bool:
        if seq != self._build_seq:
            return False  # a newer reload superseded this build
        for entry in entries[:_STREAM_CHUNK]:
            self._tiles.append(self._make_tile(entry))
        self.strip.show_all()
        rest = entries[_STREAM_CHUNK:]
        if rest:
            GLib.idle_add(self._stream_tiles, rest, seq,
                          total, pinned_total, query)
        else:
            self._cache_current(query, total, pinned_total)
        return False

    def _mount(self, tiles, total, pinned_total, pinned, query) -> None:
        """Re-pack an already-built (cached) tile list, instantly."""
        self._reset_strip()
        self._update_header(len(tiles), total, pinned_total, query)
        if not tiles:
            self._mount_empty(pinned)
            return
        if self.empty_label.get_parent() is not None:
            self.strip.remove(self.empty_label)
        self._tiles = tiles
        for tile in tiles:
            self.strip.pack_start(tile, False, False, 0)
        self.strip.show_all()
        self._selected = 0
        self._refresh_selection()

    def _mount_empty(self, pinned: bool) -> None:
        self.empty_label.set_text(
            "No pinned items yet.\n"
            "Pin a clip (☆) to keep it here, safe from history cleanup."
            if pinned else
            "Clipboard history is empty.\n"
            "Copy something and it will appear here."
        )
        if self.empty_label.get_parent() is None:
            self.strip.pack_start(self.empty_label, True, True, 0)
        self.strip.show_all()
        self._selected = -1
        self._refresh_selection()

    def _make_tile(self, entry: Entry) -> "Tile":
        tile = Tile(entry, self)
        self.strip.pack_start(tile, False, False, 0)
        return tile

    def _reset_strip(self) -> None:
        # Detach whatever is shown. Cached tiles keep their Python references
        # (in _tile_cache), so removing them here does not destroy them — they
        # can be re-packed instantly on the next switch.
        for child in self.strip.get_children():
            self.strip.remove(child)
        self._tiles = []

    def _update_header(self, shown: int, total: int, pinned_total: int,
                       query: str) -> None:
        if query:
            self.count_label.set_text(f"{shown} of {total}")
        elif shown < total:
            self.count_label.set_text(f"showing {shown} of {total}")
        else:
            self.count_label.set_text(f"{total} item{'s' if total != 1 else ''}")
        self._set_tab_label(
            self.tab_pinned,
            f"★ Pinned ({pinned_total})" if pinned_total else "★ Pinned",
        )

    def _cache_current(self, query: str, total: int, pinned_total: int) -> None:
        if not query:
            self._tile_cache[self._tab] = {
                "tiles": list(self._tiles),
                "total": total,
                "pinned_total": pinned_total,
            }

    def _invalidate_cache(self) -> None:
        """Drop the per-tab render cache so the next reload rebuilds tiles."""
        self._tile_cache.clear()

    def _set_tab_label(self, btn: Gtk.ToggleButton, label: str) -> None:
        if btn.get_label() != label:
            btn.set_label(label)

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
            if entry.is_file and entry.image_path:
                clipboard.copy_file(entry.image_path)   # put the real file back
            elif entry.is_image and entry.image_path:
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
        # Recover-to-front: a recovered clip jumps back to position 1 so it's
        # where you'd expect it next time the panel opens.
        try:
            storage.touch(entry.id)
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
        self._invalidate_cache()
        prev = self._selected
        self.reload()
        if self._tiles:
            self._selected = min(prev, len(self._tiles) - 1)
            self._refresh_selection()

    def pin_entry(self, entry_id: int) -> None:
        storage.toggle_pin(entry_id)
        # A pin moves the entry between tabs, so both views are now stale.
        self._invalidate_cache()
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

    def _reset_to_history(self) -> None:
        self._switching_tab = True
        try:
            self._tab = "history"
            self.tab_history.set_active(True)
            self.tab_pinned.set_active(False)
        finally:
            self._switching_tab = False

    def _on_tab(self, _btn, tab: str) -> None:
        # Radio behavior across the two toggle buttons. set_active()/set_text()
        # re-enter their handlers, so guard against the recursive churn — and
        # keep the search-clear inside the guard so it can't fire a second,
        # redundant reload.
        if self._switching_tab:
            return
        self._switching_tab = True
        try:
            self._tab = tab
            self.tab_history.set_active(tab == "history")
            self.tab_pinned.set_active(tab == "pinned")
            self.search.set_text("")
        finally:
            self._switching_tab = False
        self.reload()

    def _on_search(self, _entry) -> None:
        if not self._switching_tab:
            self.reload()

    def _on_settings(self, _btn) -> None:
        self.hide()
        self._controller.open_settings()

    # -- show / hide ------------------------------------------------------
    def show(self) -> None:
        self._controller.refresh_theme()
        self._set_active_monitor()
        self._reset_to_history()
        # Rebuild from scratch on open: new clips may have been copied (and
        # retention may have pruned old ones) since the panel was last shown.
        self._invalidate_cache()
        self.reload()
        # Force the compositor to route the keyboard to our layer surface no
        # matter how we were opened. ON_DEMAND alone works for the global
        # shortcut but NOT for the tray menu — COSMIC won't hand focus to a
        # layer surface mapped from a menu, leaving the panel stuck (Escape,
        # click-away and search all need focus). EXCLUSIVE makes the grab
        # unconditional; we relax to ON_DEMAND a moment later so a real click
        # elsewhere can still move focus away and dismiss us (_on_focus_out).
        GtkLayerShell.set_keyboard_mode(
            self.window, GtkLayerShell.KeyboardMode.EXCLUSIVE
        )
        self.window.show_all()
        self.action_bar.hide()  # show_all reveals it; keep hidden until invoked
        self._visible = True
        self._shown_at = time.monotonic()
        self.search.grab_focus()
        GLib.timeout_add(180, self._relax_keyboard)

    def _relax_keyboard(self) -> bool:
        # Drop back to ON_DEMAND so click-away dismissal works again. This fires
        # inside the _on_focus_out settle window (0.25s), so if the mode change
        # momentarily blips focus it's ignored rather than self-dismissing.
        if self._visible:
            GtkLayerShell.set_keyboard_mode(
                self.window, GtkLayerShell.KeyboardMode.ON_DEMAND
            )
        return False

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
                self._invalidate_cache()
                self.reload()
        return False
