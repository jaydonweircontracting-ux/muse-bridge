"""Configurable global hotkeys, bot-client style.

Uses the Win32 RegisterHotKey API via ctypes — no new dependencies, and the
hotkeys fire even when the Muse Bridge window is not focused, like the
old-school RuneScape bot clients and screen recorders.

Bindings are NOT hardcoded: defaults are F1/F2/F3 (recorder style), and the
user can reassign any action through the Hotkeys settings screen. Bindings
persist in the app settings and survive restarts.

Deliberate boundary: these hotkeys drive Muse Bridge's own run loop and
window focus only. They never inject keystrokes or clicks into Muse,
ChatGPT, or any other window — handoffs stay explicit clipboard
copy/paste with user confirmation. Run hotkeys (F1/F2/F3) control the run
loop only; they never blast commands at individual workers.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Callable

IS_WINDOWS = os.name == "nt"

# Win32 constants
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
_WM_HOTKEY = 0x0312

# Virtual-key codes for the defaults.
VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_X = 0x58
_VK_DIGITS = {str(i): 0x30 + i for i in range(1, 6)}


def mods(*names: str) -> int:
    m = 0
    for n in names:
        m |= {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "shift": MOD_SHIFT}[n]
    return m


@dataclass
class HotkeyBinding:
    """One configurable action -> key binding."""
    name: str
    modifiers: int
    vk: int
    description: str

    @property
    def label(self) -> str:
        return vk_label(self.modifiers, self.vk)


# Recorder-style defaults. Every one of these is user-reassignable.
DEFAULT_BINDINGS: list[HotkeyBinding] = [
    HotkeyBinding("run_toggle", 0, VK_F1, "Start / Go (run loop)"),
    HotkeyBinding("run_stop", 0, VK_F2, "Stop (run loop, graceful)"),
    HotkeyBinding("run_pause", 0, VK_F3, "Pause / Resume (run loop)"),
    HotkeyBinding("stop_now", mods("ctrl", "shift"), VK_X,
                  "Stop immediately (no grace)"),
    HotkeyBinding("focus_w1", mods("ctrl", "shift"), _VK_DIGITS["1"],
                  "Focus worker 1 window"),
    HotkeyBinding("focus_w2", mods("ctrl", "shift"), _VK_DIGITS["2"],
                  "Focus worker 2 window"),
    HotkeyBinding("focus_w3", mods("ctrl", "shift"), _VK_DIGITS["3"],
                  "Focus worker 3 window"),
    HotkeyBinding("focus_w4", mods("ctrl", "shift"), _VK_DIGITS["4"],
                  "Focus worker 4 window"),
    HotkeyBinding("focus_w5", mods("ctrl", "shift"), _VK_DIGITS["5"],
                  "Focus worker 5 window"),
]

# Backwards compatibility for the earlier hardcoded set.
DEFAULT_HOTKEYS = DEFAULT_BINDINGS


def vk_label(modifiers: int, vk: int) -> str:
    """Human-readable label, e.g. 'F1', 'Ctrl+Shift+X'."""
    parts: list[str] = []
    if modifiers & MOD_CONTROL:
        parts.append("Ctrl")
    if modifiers & MOD_ALT:
        parts.append("Alt")
    if modifiers & MOD_SHIFT:
        parts.append("Shift")
    if VK_F1 <= vk <= 0x87:  # F1..F24
        parts.append(f"F{vk - VK_F1 + 1}")
    elif 0x30 <= vk <= 0x39:
        parts.append(chr(vk))
    elif 0x41 <= vk <= 0x5A:
        parts.append(chr(vk))
    else:
        parts.append(f"VK_{vk:02X}")
    return "+".join(parts)


def find_conflict(bindings: list[HotkeyBinding],
                  name: str, modifiers: int, vk: int) -> HotkeyBinding | None:
    """Return the OTHER binding already using (modifiers, vk), if any."""
    for b in bindings:
        if b.name != name and b.modifiers == modifiers and b.vk == vk:
            return b
    return None


class KeyCapture:
    """State machine for 'press any key to assign' capture.

    Display-free: feed tkinter keysyms via key()/release() and act on the
    returned decision. The GUI layer owns dialogs, message boxes, and
    (re-)registration; this class owns only the key logic, so it is fully
    unit-testable.

    Decisions from key():
      ("modifier",)          — modifier held, keep waiting
      ("cancel",)            — Escape: abort without changing the binding
      ("ignore",)            — key repeat or already finished
      ("rejected", keysym)   — not an assignable key
      ("assign", mods, vk)   — valid key ready to bind
    """

    MOD_KEYSYMS = frozenset({
        "Shift_L", "Shift_R", "Control_L", "Control_R",
        "Alt_L", "Alt_R", "Meta_L", "Meta_R",
    })

    def __init__(self, keysym_to_vk) -> None:
        self._keysym_to_vk = keysym_to_vk
        self.held_mods: set[str] = set()
        self.pressed: set[str] = set()
        self.done = False

    def key(self, keysym: str) -> tuple:
        if self.done:
            return ("ignore",)
        if keysym in self.MOD_KEYSYMS:
            self.held_mods.add(keysym)
            return ("modifier",)
        if keysym == "Escape":
            self.done = True
            return ("cancel",)
        if keysym in self.pressed:
            return ("ignore",)  # key repeat
        self.pressed.add(keysym)
        vk = self._keysym_to_vk(keysym)
        if vk is None:
            self.pressed.clear()
            return ("rejected", keysym)
        self.done = True
        return ("assign", self._mods(), vk)

    def release(self, keysym: str) -> None:
        self.held_mods.discard(keysym)
        self.pressed.discard(keysym)

    def _mods(self) -> int:
        m = 0
        if any("Shift" in k for k in self.held_mods):
            m |= MOD_SHIFT
        if any("Control" in k for k in self.held_mods):
            m |= MOD_CONTROL
        if any("Alt" in k or "Meta" in k for k in self.held_mods):
            m |= MOD_ALT
        return m


class HotkeyManager:
    """Registers Win32 global hotkeys on a background message-loop thread.

    `dispatch` must be thread-safe and run the callback on the GUI thread
    (e.g. ``root.after(0, cb)`` for tkinter). On non-Windows platforms every
    method is a harmless no-op so the app and tests keep working.
    """

    def __init__(self, dispatch: Callable[[Callable[[], None]], None] | None = None):
        self._dispatch = dispatch or (lambda cb: cb())
        self._handlers: dict[str, Callable[[], None]] = {}
        self._ids: dict[str, int] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.active = False

    def register(self, binding: HotkeyBinding,
                 handler: Callable[[], None]) -> bool:
        """Register one hotkey. Returns False if unavailable (non-Windows or
        already taken by another app / the OS)."""
        self._handlers[binding.name] = handler
        if not IS_WINDOWS:
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hid = (hash(binding.name) & 0x3FFF) + 1
            if not user32.RegisterHotKey(None, hid,
                                         binding.modifiers, binding.vk):
                return False
            self._ids[binding.name] = hid
            return True
        except Exception:
            return False

    def unregister(self, name: str) -> None:
        """Remove one hotkey registration (handler is forgotten too)."""
        self._handlers.pop(name, None)
        hid = self._ids.pop(name, None)
        if hid is not None and IS_WINDOWS:
            try:
                import ctypes
                ctypes.windll.user32.UnregisterHotKey(None, hid)
            except Exception:
                pass

    def is_registered(self, name: str) -> bool:
        return name in self._ids

    def start(self) -> bool:
        """Begin listening. Returns False on non-Windows."""
        if not IS_WINDOWS or self.active:
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="muse-bridge-hotkeys")
        self._thread.start()
        self.active = True
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if IS_WINDOWS:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                for hid in self._ids.values():
                    user32.UnregisterHotKey(None, hid)
                # Wake the GetMessage loop so the thread exits.
                user32.PostThreadMessageW(self._thread.ident, _WM_HOTKEY, 0, 0)  # type: ignore[union-attr]
            except Exception:
                pass
        self._ids.clear()
        self.active = False

    # -- internals -----------------------------------------------------------

    def _loop(self) -> None:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        id_to_name = {v: k for k, v in self._ids.items()}
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == _WM_HOTKEY:
                name = id_to_name.get(msg.wParam)
                handler = self._handlers.get(name or "")
                if handler:
                    self._dispatch(handler)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
