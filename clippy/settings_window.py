"""The Clippy settings window (a normal, skip-taskbar GTK window).

Exposes: open-at-login, copy sound, always-paste-as-plain-text, history
retention with auto-delete, a clear-history action, and a shortcut picker that
writes the COSMIC binding directly.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from . import config, settings, setup, storage

_MOD_KEYS = {
    Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Shift_L, Gdk.KEY_Shift_R,
    Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_ISO_Level3_Shift,
    Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
}


def _mods_from_state(state) -> list:
    mods = []
    if state & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.MOD4_MASK):
        mods.append("Super")
    if state & Gdk.ModifierType.CONTROL_MASK:
        mods.append("Ctrl")
    if state & Gdk.ModifierType.MOD1_MASK:
        mods.append("Alt")
    if state & Gdk.ModifierType.SHIFT_MASK:
        mods.append("Shift")
    return mods


def _combo_text(mods: list, key: str) -> str:
    shown_key = key.upper() if len(key) == 1 else key
    return "+".join(list(mods) + [shown_key]) if key else "Not set"


class SettingsWindow:
    def __init__(self, controller):
        self._controller = controller
        self._capturing = False

        self.window = Gtk.Window(title="Clippy Settings")
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_resizable(False)
        self.window.set_default_size(460, -1)
        self.window.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("delete-event", lambda *_: (self.hide(), True)[1])
        self.window.connect("key-press-event", self._on_key)

        self._build()

    # -- UI helpers -------------------------------------------------------
    def _section(self, box, text):
        lbl = Gtk.Label(label=text.upper())
        lbl.set_xalign(0.0)
        lbl.get_style_context().add_class("section-title")
        box.pack_start(lbl, False, False, 0)

    def _switch_row(self, box, label, desc, active, on_toggle):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name = Gtk.Label(label=label)
        name.set_xalign(0.0)
        name.get_style_context().add_class("settings-label")
        text.pack_start(name, False, False, 0)
        if desc:
            d = Gtk.Label(label=desc)
            d.set_xalign(0.0)
            d.set_line_wrap(True)
            d.get_style_context().add_class("settings-desc")
            text.pack_start(d, False, False, 0)
        row.pack_start(text, True, True, 0)

        sw = Gtk.Switch()
        sw.set_active(active)
        sw.set_valign(Gtk.Align.CENTER)
        sw.connect("notify::active", lambda s, _p: on_toggle(s.get_active()))
        row.pack_end(sw, False, False, 0)
        box.pack_start(row, False, False, 0)
        return sw

    def _build(self):
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.get_style_context().add_class("settings-body")
        self.window.add(body)

        title = Gtk.Label(label="Clippy Settings")
        title.set_xalign(0.0)
        title.get_style_context().add_class("settings-title")
        body.pack_start(title, False, False, 0)

        prefs = settings.load()

        self._section(body, "Appearance")
        theme_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        theme_lbl = Gtk.Label(label="Theme")
        theme_lbl.set_xalign(0.0)
        theme_lbl.get_style_context().add_class("settings-label")
        theme_row.pack_start(theme_lbl, True, True, 0)
        self._theme = Gtk.ComboBoxText()
        for tid, tlabel in (("system", "Follow system"), ("dark", "Dark"), ("light", "Light")):
            self._theme.append(tid, tlabel)
        self._theme.set_active_id(prefs["theme_mode"])
        self._theme.connect("changed", self._on_theme_mode)
        theme_row.pack_end(self._theme, False, False, 0)
        body.pack_start(theme_row, False, False, 0)

        self._section(body, "General")
        self._switch_row(
            body, "Open at login", None, bool(prefs["open_at_login"]),
            self._on_open_at_login,
        )
        self._switch_row(
            body, "Sound on copy", "Play a short sound whenever you copy.",
            bool(prefs["sound_on_copy"]), self._on_sound,
        )
        self._switch_row(
            body, "Always paste as plain text",
            "When off, copied formatting is preserved on paste.",
            bool(prefs["always_plain_text"]), self._on_plain,
        )

        self._section(body, "History")
        ret_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ret_lbl = Gtk.Label(label="Keep history for")
        ret_lbl.set_xalign(0.0)
        ret_lbl.get_style_context().add_class("settings-label")
        ret_row.pack_start(ret_lbl, True, True, 0)
        self._retention = Gtk.ComboBoxText()
        for key, label, _secs in config.RETENTION_OPTIONS:
            self._retention.append(key, label)
        self._retention.set_active_id(prefs["retention"])
        self._retention.connect("changed", self._on_retention)
        ret_row.pack_end(self._retention, False, False, 0)
        body.pack_start(ret_row, False, False, 0)

        clear_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        clear_lbl = Gtk.Label(label="Clear all history now")
        clear_lbl.set_xalign(0.0)
        clear_lbl.get_style_context().add_class("settings-label")
        clear_row.pack_start(clear_lbl, True, True, 0)
        clear_btn = Gtk.Button(label="Clear history")
        clear_btn.get_style_context().add_class("normal-btn")
        clear_btn.get_style_context().add_class("danger")
        clear_btn.connect("clicked", self._on_clear)
        clear_row.pack_end(clear_btn, False, False, 0)
        body.pack_start(clear_row, False, False, 0)

        self._section(body, "Shortcut")
        sc_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        sc_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        sc_lbl = Gtk.Label(label="Open Clippy")
        sc_lbl.set_xalign(0.0)
        sc_lbl.get_style_context().add_class("settings-label")
        sc_text.pack_start(sc_lbl, False, False, 0)
        self._sc_desc = Gtk.Label(label="Click, then press your key combo.")
        self._sc_desc.set_xalign(0.0)
        self._sc_desc.set_line_wrap(True)
        self._sc_desc.get_style_context().add_class("settings-desc")
        sc_text.pack_start(self._sc_desc, False, False, 0)
        sc_row.pack_start(sc_text, True, True, 0)

        self._sc_btn = Gtk.Button(label=self._current_combo_text())
        self._sc_btn.get_style_context().add_class("shortcut-btn")
        self._sc_btn.connect("clicked", self._on_capture_start)
        sc_row.pack_end(self._sc_btn, False, False, 0)
        body.pack_start(sc_row, False, False, 0)

        close_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        close_btn = Gtk.Button(label="Close")
        close_btn.get_style_context().add_class("normal-btn")
        close_btn.connect("clicked", lambda _b: self.hide())
        close_row.pack_end(close_btn, False, False, 0)
        body.pack_start(close_row, False, False, 12)

    # -- current shortcut -------------------------------------------------
    def _current_combo(self):
        live = setup.read_cosmic_shortcut()
        if live:
            return live
        sc = settings.get("shortcut") or {}
        return sc.get("modifiers", []), sc.get("key", "")

    def _current_combo_text(self) -> str:
        mods, key = self._current_combo()
        return _combo_text(mods, key)

    # -- callbacks --------------------------------------------------------
    def _on_open_at_login(self, active):
        settings.set_value("open_at_login", active)
        setup.set_open_at_login(active)

    def _on_theme_mode(self, combo):
        tid = combo.get_active_id()
        if tid:
            settings.set_value("theme_mode", tid)
            self._controller.settings_changed()  # re-applies CSS live

    def _on_sound(self, active):
        settings.set_value("sound_on_copy", active)

    def _on_plain(self, active):
        settings.set_value("always_plain_text", active)

    def _on_retention(self, combo):
        key = combo.get_active_id()
        if key:
            settings.set_value("retention", key)
            storage.apply_retention()
            self._controller.settings_changed()

    def _on_clear(self, _btn):
        dlg = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Clear all clipboard history?",
        )
        dlg.format_secondary_text("Pinned items are removed too. This cannot be undone.")
        if dlg.run() == Gtk.ResponseType.OK:
            storage.clear(include_pinned=True)
            self._controller.settings_changed()
        dlg.destroy()

    def _on_capture_start(self, _btn):
        self._capturing = True
        self._sc_btn.set_label("Press keys… (Esc to cancel)")
        self._sc_btn.get_style_context().add_class("capturing")

    def _finish_capture(self, mods, key):
        self._capturing = False
        self._sc_btn.get_style_context().remove_class("capturing")
        if setup.spawn_command() is None:
            self._sc_desc.set_text(
                "Install the launcher first (run scripts/install.sh)."
            )
            self._sc_btn.set_label(self._current_combo_text())
            return
        if setup.set_cosmic_shortcut(mods, key):
            settings.set_value("shortcut", {"modifiers": mods, "key": key})
            self._sc_btn.set_label(_combo_text(mods, key))
            self._sc_desc.set_text("Saved. Try it now.")
        else:
            self._sc_btn.set_label(self._current_combo_text())
            self._sc_desc.set_text("Could not write the COSMIC shortcut.")

    def _on_key(self, _w, event):
        if self._capturing:
            if event.keyval == Gdk.KEY_Escape:
                self._capturing = False
                self._sc_btn.get_style_context().remove_class("capturing")
                self._sc_btn.set_label(self._current_combo_text())
                return True
            if event.keyval in _MOD_KEYS:
                return True  # wait for a non-modifier key
            key = Gdk.keyval_name(Gdk.keyval_to_lower(event.keyval)) or ""
            self._finish_capture(_mods_from_state(event.state), key)
            return True
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    # -- show / hide ------------------------------------------------------
    def show(self):
        self._sc_btn.set_label(self._current_combo_text())
        self.window.show_all()
        self.window.present()

    def hide(self):
        self.window.hide()
