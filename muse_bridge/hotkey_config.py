"""Persistence for user-configured hotkey bindings.

Bindings live in the app settings under the ``hotkeys`` key, as overrides on
top of the defaults — only customized actions are stored, so new default
actions appear automatically and "Reset to Defaults" just clears the key.
"""
from __future__ import annotations

from typing import Any

from .hotkeys import DEFAULT_BINDINGS, HotkeyBinding

SETTINGS_KEY = "hotkeys"


def load_bindings(settings: Any) -> list[HotkeyBinding]:
    """Defaults overlaid with the user's saved customizations."""
    try:
        saved = settings.get(SETTINGS_KEY, {}) or {}
    except Exception:
        saved = {}
    out: list[HotkeyBinding] = []
    for default in DEFAULT_BINDINGS:
        custom = saved.get(default.name) if isinstance(saved, dict) else None
        if (isinstance(custom, dict)
                and isinstance(custom.get("mods"), int)
                and isinstance(custom.get("vk"), int)):
            out.append(HotkeyBinding(default.name, custom["mods"],
                                     custom["vk"], default.description))
        else:
            out.append(HotkeyBinding(default.name, default.modifiers,
                                     default.vk, default.description))
    return out


def binding_for(settings: Any, name: str) -> HotkeyBinding:
    for b in load_bindings(settings):
        if b.name == name:
            return b
    # Unknown name: fall back to the default of the same name, else blank.
    for d in DEFAULT_BINDINGS:
        if d.name == name:
            return HotkeyBinding(d.name, d.modifiers, d.vk, d.description)
    return HotkeyBinding(name, 0, 0, name)


def save_binding(settings: Any, name: str, modifiers: int, vk: int) -> None:
    try:
        saved = settings.get(SETTINGS_KEY, {}) or {}
    except Exception:
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    saved[name] = {"mods": modifiers, "vk": vk}
    settings.set(SETTINGS_KEY, saved)


def reset_bindings(settings: Any) -> None:
    settings.set(SETTINGS_KEY, {})
