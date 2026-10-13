"""Global hotkeys, bot-client style.

Uses the Win32 RegisterHotKey API via ctypes — no new dependencies, and the
hotkeys fire even when the Muse Bridge window is not focused, like the
old-school RuneScape bot clients.

Deliberate boundary: these hotkeys drive Muse Bridge's own run loop and
window focus only. They never inject keystrokes or clicks into Muse,
ChatGPT, or any other window — handoffs stay explicit clipboard
copy/paste with user confirmation.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable

IS_WINDOWS = os.name == "nt"

# Win32 constants
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_WM_HOTKEY = 0x0312

_VK = {
    "s": 0x53,
    "x": 0x58,
    "1": 0x31,
    "2": 0x32,
    "3": 0x33,
    "4": 0x34,
    "5": 0x35,
}


def _mods(*names: str) -> int:
    m = 0
    for n in names:
        m |= {"alt": _MOD_ALT, "ctrl": _MOD_CONTROL, "shift": _MOD_SHIFT}[n]
    return m


@dataclass
class HotkeySpec:
    name: str
    modifiers: int
    vk: int
    description: str


# The default bot-client bindings.
DEFAULT_HOTKEYS: list[HotkeySpec] = [
    HotkeySpec("toggle_run", _mods("ctrl", "shift"), _VK["s"],
               "Start / Stop the run"),
    HotkeySpec("stop_now", _mods("ctrl", "shift"), _VK["x"],
               "Stop immediately"),
    HotkeySpec("focus_w1", _mods("ctrl", "shift"), _VK["1"], "Focus worker 1"),
    HotkeySpec("focus_w2", _mods("ctrl", "shift"), _VK["2"], "Focus worker 2"),
    HotkeySpec("focus_w3", _mods("ctrl", "shift"), _VK["3"], "Focus worker 3"),
    HotkeySpec("focus_w4", _mods("ctrl", "shift"), _VK["4"], "Focus worker 4"),
    HotkeySpec("focus_w5", _mods("ctrl", "shift"), _VK["5"], "Focus worker 5"),
]


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

    def register(self, spec: HotkeySpec, handler: Callable[[], None]) -> bool:
        """Register one hotkey. Returns False if unavailable (non-Windows or
        already taken by another app)."""
        self._handlers[spec.name] = handler
        if not IS_WINDOWS:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hid = (hash(spec.name) & 0x3FFF) + 1
            if not user32.RegisterHotKey(None, hid, spec.modifiers, spec.vk):
                return False
            self._ids[spec.name] = hid
            return True
        except Exception:
            return False

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
