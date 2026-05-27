"""Follow the COSMIC theme and generate matching GTK CSS.

COSMIC records the active mode in ``…/CosmicTheme.Mode/v1/is_dark`` and the
palette in ``…/CosmicTheme.{Dark,Light}/v1/<key>`` files (RON structs whose
``base: (red, green, blue, alpha)`` floats we parse). We pull the real
background and accent so the panel matches the system, and fall back to a
sensible built-in palette if parsing fails.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

from . import config, settings

_MODE_FILE = config.HOME / ".config/cosmic/com.system76.CosmicTheme.Mode/v1/is_dark"

_BASE_RE = re.compile(
    r"base:\s*\(\s*red:\s*([0-9.]+),\s*green:\s*([0-9.]+),"
    r"\s*blue:\s*([0-9.]+),\s*alpha:\s*([0-9.]+)",
    re.DOTALL,
)

_DARK = {
    "bg": "rgba(28, 29, 34, 0.96)",
    "tile": "rgba(255, 255, 255, 0.05)",
    "tile_hover": "rgba(255, 255, 255, 0.09)",
    "border": "rgba(255, 255, 255, 0.10)",
    "text": "#f2f2f5",
    "dim": "rgba(255, 255, 255, 0.45)",
    "dim2": "rgba(255, 255, 255, 0.33)",
    "accent": "#5b8cff",
    "accent_soft": "rgba(91, 140, 255, 0.16)",
    "field": "rgba(255, 255, 255, 0.07)",
    "field_hover": "rgba(255, 255, 255, 0.10)",
    "backdrop": "rgba(0, 0, 0, 0.35)",
    "badge_text_fg": "#9ec5ff",
    "badge_text_bg": "rgba(91, 140, 255, 0.20)",
    "badge_img_fg": "#ffcf8f",
    "badge_img_bg": "rgba(255, 170, 80, 0.20)",
    "danger": "#ff6b6b",
}

_LIGHT = {
    "bg": "rgba(250, 250, 252, 0.97)",
    "tile": "rgba(0, 0, 0, 0.035)",
    "tile_hover": "rgba(0, 0, 0, 0.07)",
    "border": "rgba(0, 0, 0, 0.12)",
    "text": "#1c1d22",
    "dim": "rgba(0, 0, 0, 0.5)",
    "dim2": "rgba(0, 0, 0, 0.4)",
    "accent": "#3a6ff0",
    "accent_soft": "rgba(58, 111, 240, 0.13)",
    "field": "rgba(0, 0, 0, 0.05)",
    "field_hover": "rgba(0, 0, 0, 0.08)",
    "backdrop": "rgba(0, 0, 0, 0.18)",
    "badge_text_fg": "#1e4fcf",
    "badge_text_bg": "rgba(58, 111, 240, 0.14)",
    "badge_img_fg": "#9a5b00",
    "badge_img_bg": "rgba(255, 170, 80, 0.22)",
    "danger": "#d23b3b",
}


def is_dark() -> bool:
    """The system (COSMIC) dark/light state."""
    try:
        return _MODE_FILE.read_text(encoding="utf-8").strip().lower() != "false"
    except OSError:
        return True


def resolve_dark() -> bool:
    """Effective dark/light, honoring the user's theme_mode preference."""
    mode = settings.get("theme_mode")
    if mode == "dark":
        return True
    if mode == "light":
        return False
    return is_dark()


def _cosmic_dir(dark: bool) -> Path:
    name = "Dark" if dark else "Light"
    return config.HOME / f".config/cosmic/com.system76.CosmicTheme.{name}/v1"


def _first_base(path: Path) -> Optional[Tuple[float, float, float, float]]:
    try:
        m = _BASE_RE.search(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not m:
        return None
    return tuple(float(x) for x in m.groups())  # type: ignore[return-value]


def _rgb(c) -> str:
    return f"rgb({int(c[0] * 255)}, {int(c[1] * 255)}, {int(c[2] * 255)})"


def _rgba(c, a: float) -> str:
    return f"rgba({int(c[0] * 255)}, {int(c[1] * 255)}, {int(c[2] * 255)}, {a})"


def _palette(dark: bool) -> dict:
    c = dict(_DARK if dark else _LIGHT)
    base = _cosmic_dir(dark)
    accent = _first_base(base / "accent")
    bg = _first_base(base / "background")
    primary = _first_base(base / "primary")
    destructive = _first_base(base / "destructive")

    if bg:
        c["bg"] = _rgba(bg, 0.97)
        # Derive text/border/field shades from the real background luminance
        # so contrast matches COSMIC in either mode.
        lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        if lum < 0.5:
            c.update(text="#f3f4f6", dim="rgba(255,255,255,0.55)",
                     dim2="rgba(255,255,255,0.38)", border="rgba(255,255,255,0.12)",
                     field="rgba(255,255,255,0.08)", field_hover="rgba(255,255,255,0.12)",
                     backdrop="rgba(0,0,0,0.45)")
        else:
            c.update(text="#16181d", dim="rgba(0,0,0,0.55)",
                     dim2="rgba(0,0,0,0.40)", border="rgba(0,0,0,0.12)",
                     field="rgba(0,0,0,0.05)", field_hover="rgba(0,0,0,0.09)",
                     backdrop="rgba(0,0,0,0.25)")
    if primary:
        c["tile"] = _rgb(primary)
    if accent:
        c["accent"] = _rgb(accent)
        c["accent_soft"] = _rgba(accent, 0.20)
        c["tile_hover"] = _rgba(accent, 0.10)
        c["badge_text_fg"] = _rgb(accent)
        c["badge_text_bg"] = _rgba(accent, 0.20)
    if destructive:
        c["danger"] = _rgb(destructive)
    return c


def build_css(dark: bool | None = None) -> str:
    if dark is None:
        dark = is_dark()
    c = _palette(dark)
    return f"""
.clippy-overlay {{ background-color: transparent; }}

.backdrop {{ background-color: {c['backdrop']}; }}

.panel-body {{
    background-color: {c['bg']};
    border-top: 1px solid {c['border']};
    border-top-left-radius: 16px;
    border-top-right-radius: 16px;
    padding: 14px 22px 20px 22px;
}}

.header {{ margin-bottom: 8px; }}
.title {{ color: {c['text']}; font-size: 16px; font-weight: bold; }}

.iconbtn {{
    background: transparent; border: none; padding: 2px;
    min-width: 28px; min-height: 28px; border-radius: 8px;
    color: {c['dim']};
}}
.iconbtn:hover {{ background-color: {c['tile_hover']}; color: {c['text']}; }}

.search {{
    background-color: {c['field']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 4px 8px;
    min-height: 28px;
}}
.search:focus {{ border-color: {c['accent']}; background-color: {c['field_hover']}; }}

.count {{ color: {c['dim']}; font-size: 12px; }}

.action-bar {{
    background-color: {c['accent_soft']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 5px 8px;
    margin-bottom: 6px;
}}
.action-label {{ color: {c['dim']}; font-size: 11px; font-weight: bold; margin-right: 4px; }}
.action-btn {{
    background-color: {c['field']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 7px; padding: 2px 10px;
    font-size: 12px;
}}
.action-btn:hover {{ background-color: {c['field_hover']}; }}
.action-btn.danger {{ color: {c['danger']}; }}

.strip {{ background-color: transparent; }}
.strip-inner {{ padding: 4px 8px 10px 8px; }}

.tile {{
    background-color: {c['tile']};
    border: 1px solid {c['border']};
    border-radius: 12px;
    padding: 10px;
}}
.tile:hover {{ background-color: {c['tile_hover']}; }}
.tile.selected {{ border-color: {c['accent']}; background-color: {c['accent_soft']}; }}

.tile-content, .tile-content viewport {{ background-color: transparent; border: none; }}
.preview-text {{ color: {c['text']}; font-size: 12.5px; }}
.preview-image {{ border-radius: 8px; }}

.badge {{ font-size: 9.5px; font-weight: bold; border-radius: 6px; padding: 1px 6px; }}
.badge-text {{ color: {c['badge_text_fg']}; background-color: {c['badge_text_bg']}; }}
.badge-image {{ color: {c['badge_img_fg']}; background-color: {c['badge_img_bg']}; }}
.pin-marker {{ color: #ffd24a; font-size: 11px; margin-left: 2px; }}

.tile-action {{
    color: {c['dim']}; background: transparent; border: none;
    padding: 0 4px; min-height: 18px; min-width: 18px; font-size: 13px;
}}
.tile-action:hover {{ color: {c['text']}; background-color: {c['tile_hover']}; border-radius: 6px; }}

.meta {{ color: {c['dim2']}; font-size: 10.5px; margin-top: 4px; }}
.empty {{ color: {c['dim']}; font-size: 14px; }}
.hint {{ color: {c['dim2']}; font-size: 11px; margin-top: 6px; }}

scrollbar {{ background-color: transparent; }}
scrollbar slider {{ background-color: {c['dim2']}; border-radius: 8px; min-width: 40px; }}

/* ---- settings window ---- */
.settings-body {{ background-color: {c['bg']}; padding: 20px 22px; }}
.settings-title {{ color: {c['text']}; font-size: 18px; font-weight: bold; margin-bottom: 4px; }}
.section-title {{ color: {c['dim']}; font-size: 11px; font-weight: bold; margin-top: 14px; margin-bottom: 4px; }}
.settings-label {{ color: {c['text']}; font-size: 13.5px; }}
.settings-desc {{ color: {c['dim']}; font-size: 11.5px; }}
.shortcut-btn, .normal-btn {{
    background-color: {c['field']}; color: {c['text']};
    border: 1px solid {c['border']}; border-radius: 8px; padding: 4px 12px;
}}
.shortcut-btn:hover, .normal-btn:hover {{ background-color: {c['field_hover']}; }}
.shortcut-btn.capturing {{ border-color: {c['accent']}; color: {c['accent']}; }}
.danger {{ color: {c['danger']}; }}
"""
