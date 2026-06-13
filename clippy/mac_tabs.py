"""Mac-only clipboard 'tabs' — named, colored collections of clips.

A small JSON store: custom tab definitions (name + color) and which entry ids
belong to which tabs. macOS-only; the shared core is untouched. Clips kept in a
tab are also `pinned` in the DB (which the pruning protects), so they survive
history rotation. The built-in "Pinned" tab is just the pinned flag — not stored
here. Not imported on Linux.
"""
from __future__ import annotations

import json
from typing import List

from . import config

_PATH = config.DATA_DIR / "mac_tabs.json"

# Fixed swatch palette offered when creating a tab.
PALETTE = ["#E8743B", "#3B82E8", "#2BB673", "#9B59B6",
           "#E84B7C", "#E8B73B", "#16A0A0", "#7F8C8D"]


def _load() -> dict:
    try:
        d = json.loads(_PATH.read_text())
        if isinstance(d, dict):
            d.setdefault("tabs", [])
            d.setdefault("members", {})
            return d
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
