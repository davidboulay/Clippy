"""Shared clipboard 'tabs' — named, colored collections of clips.

A small JSON store (``<DATA_DIR>/tabs.json``): custom tab definitions
(name + color) and which entry ids belong to which tabs. Shared by the GTK
panel (``panel.py``) and the macOS panel (``mac_panel.py``) so the feature is
identical on both platforms. Clips kept in a tab are also ``pinned`` in the DB
(which retention/pruning protects), so they survive history rotation. The
built-in "Pinned" tab is just the pinned flag — not stored here.

Migration: the macOS app used to write ``mac_tabs.json``; on first load, if
``tabs.json`` doesn't exist yet but ``mac_tabs.json`` does, its contents are
copied over once.
"""
from __future__ import annotations

import json
from typing import List

from . import config

_PATH = config.DATA_DIR / "tabs.json"
_LEGACY_PATH = config.DATA_DIR / "mac_tabs.json"

# Fixed swatch palette offered when creating a tab.
PALETTE = ["#E8743B", "#3B82E8", "#2BB673", "#9B59B6",
           "#E84B7C", "#E8B73B", "#16A0A0", "#7F8C8D"]


def _coerce(d) -> dict:
    if isinstance(d, dict):
        d.setdefault("tabs", [])
        d.setdefault("members", {})
        return d
    return {"tabs": [], "members": {}}


def _load() -> dict:
    try:
        return _coerce(json.loads(_PATH.read_text()))
    except (OSError, ValueError):
        pass
    # One-time migration from the old macOS-only store.
    try:
        if _LEGACY_PATH.exists():
            migrated = _coerce(json.loads(_LEGACY_PATH.read_text()))
            _save(migrated)
            return migrated
    except (OSError, ValueError):
        pass
    return {"tabs": [], "members": {}}


def _save(d: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(d, indent=2))
    except OSError:
        pass


def tabs() -> List[dict]:
    """Ordered list of custom tabs: [{'name': str, 'color': '#rrggbb'}, ...]."""
    return _load().get("tabs", [])


def tab_names() -> List[str]:
    return [t.get("name", "") for t in tabs()]


def add_tab(name: str, color: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    d = _load()
    if any(t.get("name") == name for t in d["tabs"]):
        return False
    d["tabs"].append({"name": name, "color": color})
    _save(d)
    return True


def rename_tab(old: str, new: str) -> bool:
    new = (new or "").strip()
    if not new:
        return False
    d = _load()
    if any(t.get("name") == new for t in d["tabs"]):
        return False
    for t in d["tabs"]:
        if t.get("name") == old:
            t["name"] = new
    for k in d["members"]:
        d["members"][k] = [new if x == old else x for x in d["members"][k]]
    _save(d)
    return True


def set_color(name: str, color: str) -> None:
    d = _load()
    for t in d["tabs"]:
        if t.get("name") == name:
            t["color"] = color
    _save(d)


def remove_tab(name: str) -> None:
    d = _load()
    d["tabs"] = [t for t in d["tabs"] if t.get("name") != name]
    for eid in list(d["members"]):
        d["members"][eid] = [t for t in d["members"][eid] if t != name]
        if not d["members"][eid]:
            del d["members"][eid]
    _save(d)


def assign(entry_id: int, name: str) -> None:
    d = _load()
    k = str(entry_id)
    cur = d["members"].get(k, [])
    if name not in cur:
        cur.append(name)
    d["members"][k] = cur
    _save(d)


def unassign(entry_id: int, name: str) -> None:
    d = _load()
    k = str(entry_id)
    cur = [t for t in d["members"].get(k, []) if t != name]
    if cur:
        d["members"][k] = cur
    else:
        d["members"].pop(k, None)
    _save(d)


def tabs_for(entry_id: int) -> List[str]:
    return list(_load().get("members", {}).get(str(entry_id), []))


def member_ids(name: str) -> List[int]:
    d = _load()
    return [int(k) for k, v in d["members"].items() if name in v]


def all_member_ids() -> set:
    return {int(k) for k in _load().get("members", {})}
