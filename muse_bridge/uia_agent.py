"""Windows UIA-backed window agents for the relay.

STATUS: UNVERIFIED — written from the UIA patterns these web apps use,
but never executed against real ChatGPT/Muse windows (Linux dev
environment). Every heuristic below must be confirmed on the user's PC
before the relay is trusted. The relay's design assumes these can fail:
any failure pauses the pair with NEEDS_REVIEW instead of guessing.

Requires: pywinauto (already a bridge dependency on Windows).
"""
from __future__ import annotations

import time

from .relay import WindowAgent


def _uia():
    from pywinauto import Desktop
    return Desktop(backend="uia")


class UiaWindowAgent(WindowAgent):
    """Drives one browser window by HWND via UI Automation."""

    def __init__(self, handle: int, role: str) -> None:
        self.handle = handle
        self.role = role  # "muse" | "chatgpt"
        self._win = None

    def _window(self):
        if self._win is None:
            for w in _uia().windows():
                try:
                    if int(w.handle) == self.handle:
                        self._win = w
                        break
                except Exception:
                    continue
        if self._win is None:
            raise RuntimeError("window handle is no longer alive")
        return self._win

    # -- reading ----------------------------------------------------------
    def read_text(self) -> str:
        # UNVERIFIED: exact tree shape depends on the app's current DOM.
        win = self._window()
        parts: list[str] = []
        try:
            for el in win.descendants():
                try:
                    name = el.element_info.name or ""
                    ctype = el.element_info.control_type or ""
                    if ctype in ("Text", "Document", "Edit") and name.strip():
                        parts.append(name.strip())
                except Exception:
                    continue
        except Exception as e:
            raise RuntimeError(f"UIA tree walk failed: {e}")
        # De-duplicate while preserving order (UIA trees repeat text).
        seen: set[str] = set()
        out: list[str] = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return "\n".join(out)

    def conversation_id(self) -> str:
        # UNVERIFIED: reads the address bar. Chrome/Edge expose it as an
        # Edit control named "Address and search bar".
        try:
            win = self._window()
            for el in win.descendants():
                try:
                    if (el.element_info.control_type == "Edit"
                            and "address" in
                            (el.element_info.name or "").lower()):
                        return (el.element_info.value or "").strip()
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    # -- acting -----------------------------------------------------------
    def focus(self) -> bool:
        try:
            self._window().set_focus()
            time.sleep(0.4)
            return True
        except Exception:
            return False

    def _chatgpt_input(self):
        # UNVERIFIED: ChatGPT's composer is a contenteditable div; UIA
        # usually exposes it as a Document/Edit named "Message ChatGPT".
        win = self._window()
        for el in win.descendants():
            try:
                name = (el.element_info.name or "").lower()
                if el.element_info.control_type in ("Document", "Edit") \
                        and "message chatgpt" in name:
                    return el
            except Exception:
                continue
        return None

    def _muse_input(self):
        # UNVERIFIED: muse.ai composer heuristics.
        win = self._window()
        for el in win.descendants():
            try:
                name = (el.element_info.name or "").lower()
                if el.element_info.control_type in ("Document", "Edit") \
                        and ("message" in name or "chat" in name):
                    return el
            except Exception:
                continue
        return None

    def type_and_submit(self, text: str) -> bool:
        try:
            box = (self._chatgpt_input() if self.role == "chatgpt"
                   else self._muse_input())
            if box is None:
                return False
            box.click_input()
            time.sleep(0.3)
            # Paste via clipboard: reliable for long/unicode text.
            import subprocess
            subprocess.run(["powershell", "-command",
                            "Set-Clipboard",
                            "-Value", text[:8000]],
                           capture_output=True, timeout=15)
            box.type_keys("^v", set_focus=False)
            time.sleep(0.5)
            box.type_keys("{ENTER}", set_focus=False)
            return True
        except Exception:
            return False

    def _is_generating(self) -> bool:
        # UNVERIFIED: while streaming, ChatGPT shows a "Stop generating"
        # button. Its absence (with new text present) means complete.
        try:
            win = self._window()
            for el in win.descendants():
                try:
                    name = (el.element_info.name or "").lower()
                    if "stop generating" in name or "stop responding" in name:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def wait_for_response(self, timeout_s: float,
                          baseline_hash: str) -> str | None:
        import hashlib
        deadline = time.time() + timeout_s
        last_text = ""
        stable_since = 0.0
        while time.time() < deadline:
            if self._is_generating():
                stable_since = 0.0
                time.sleep(2.0)
                continue
            try:
                text = self.read_text()
            except Exception:
                return None
            h = hashlib.sha256(text.encode()).hexdigest()
            if h == baseline_hash:
                time.sleep(2.0)
                continue
            # Require the text to be stable across two polls with no
            # generating indicator before calling it complete — a mere
            # pause in streaming must not count.
            if text == last_text and stable_since:
                if time.time() - stable_since >= 4.0:
                    return self._extract_new(text, baseline_hash)
            elif text != last_text:
                last_text = text
                stable_since = time.time()
            time.sleep(2.0)
        return None

    def _extract_new(self, text: str, baseline_hash: str) -> str:
        # Best effort: return the tail that wasn't in the baseline view.
        # The controller treats this as the response; delivery is still
        # verified by the user-visible ledger, not by this heuristic.
        return text[-6000:]


def agent_for(handle: int, role: str) -> WindowAgent:
    """Factory used by the GUI. Raises on non-Windows."""
    import sys
    if sys.platform != "win32":
        raise RuntimeError("UIA agents require Windows")
    return UiaWindowAgent(handle, role)
