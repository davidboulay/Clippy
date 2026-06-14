"""Deprecated alias for :mod:`clippy.tabs`.

Custom tabs were promoted from a macOS-only store to the shared
``clippy/tabs.py`` (writing ``<DATA_DIR>/tabs.json``) so Linux and macOS share
one implementation. This module re-exports the shared API for backward
compatibility; new code should import :mod:`clippy.tabs` directly. The old
``mac_tabs.json`` is migrated automatically on first load by ``tabs._load``.
"""
from __future__ import annotations

from .tabs import (  # noqa: F401
    PALETTE,
    add_tab,
    all_member_ids,
    assign,
    member_ids,
    remove_tab,
    rename_tab,
    set_color,
    tab_names,
    tabs,
    tabs_for,
    unassign,
)
