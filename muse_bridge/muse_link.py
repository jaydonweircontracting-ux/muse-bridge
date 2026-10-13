"""Muse window link for Muse Bridge.

HONEST CAPABILITY STATEMENT (shown in the UI, not hidden):
  - On Windows, this module can find a Muse window by title, bring it to
    the foreground, and paste text into its chat input using supported
    UI-automation (pywinauto/pyautogui). That part is automatable.
  - Detecting that Muse has FINISHED a task has NO reliable signal.
    There is no official completion API. The fallback watches for
    "no new text for N seconds" after activity was seen, which can be
    wrong (slow responses look finished; streaming pauses look finished).
    The UI therefore labels Muse completion detection UNCERTAIN and
    requires the user to confirm completion for risky operations.

If pywinauto is not installed, every function reports itself
unavailable instead of pretending to work.
"""

from __future__ import annotations

import platform
import time
from dataclasses import dataclass


IS_WINDOWS = platform.system() == "Windows"


@dataclass
class WindowInfo:
    title: str
    handle: int = 0


@dataclass
class CapabilityReport:
    windows_listable: bool
    can_focus: bool
    can_paste: bool
    completion_detection: str  # "api" | "uncertain" | "unavailable"
    detail: str


def capabilities() -> CapabilityReport:
    if not IS_WINDOWS:
        return CapabilityReport(
            windows_listable=False, can_focus=False, can_paste=False,
            completion_detection="unavailable",
            detail="Not running on Windows; window automation unavailable here.",
        )
    try:
        import pywinauto  # noqa: F401
        have_pw = True
    except ImportError:
        have_pw = False
    try:
        import pyautogui  # noqa: F401
        have_pg = True
    except ImportError:
        have_pg = False
    if have_pw and have_pg:
        return CapabilityReport(
            windows_listable=True, can_focus=True, can_paste=True,
            completion_detection="uncertain",
            detail=("Window control available via pywinauto/pyautogui. "
                    "Completion detection remains UNCERTAIN: no official "
                    "Muse completion signal exists; the bot uses a "
                    "no-new-text timeout heuristic and asks you to confirm "
                    "before risky steps."),
        )
    missing = [n for n, h in (("pywinauto", have_pw), ("pyautogui", have_pg)) if not h]
    return CapabilityReport(
        windows_listable=False, can_focus=False, can_paste=False,
        completion_detection="unavailable",
        detail=f"Missing on Windows: {', '.join(missing)}. pip install them.",
    )


def list_windows(substring: str = "") -> list[WindowInfo]:
    """List visible top-level windows, optionally filtered by title."""
    if not IS_WINDOWS:
        return []
    try:
        from pywinauto import Desktop
    except ImportError:
        return []
    out: list[WindowInfo] = []
    for w in Desktop(backend="uia").windows():
        try:
            title = w.window_text()
        except Exception:
            continue
        if title and (not substring or substring.lower() in title.lower()):
            try:
                out.append(WindowInfo(title=title, handle=int(w.handle)))
            except Exception:
                out.append(WindowInfo(title=title))
    return out


def focus_window(title: str) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        from pywinauto import Desktop
        wins = [w for w in Desktop(backend="uia").windows()
                if w.window_text() == title]
        if not wins:
            return False
        wins[0].set_focus()
        return True
    except Exception:
        return False


def paste_text(text: str) -> bool:
    """Paste text via clipboard + Ctrl+V. Caller must focus the input first."""
    if not IS_WINDOWS:
        return False
    try:
        import pyautogui
        import subprocess
        # Put text on the clipboard without extra dependencies.
        p = subprocess.run(["clip"], input=text.encode("utf-16-le"),
                           capture_output=True, timeout=10)
        if p.returncode != 0:
            return False
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "v")
        return True
    except Exception:
        return False


class CompletionWatch:
    """UNCERTAIN completion heuristic for the Muse window.

    Watches a user-designated screen region for text change; declares
    "possibly complete" after `quiet_s` seconds with no change AND at
    least one change was seen (so an already-idle window is not
    mistaken for a finished task). The UI must present this as
    "possibly complete — confirm?" rather than certain.
    """

    def __init__(self, quiet_s: float = 20.0, poll_s: float = 2.0) -> None:
        self.quiet_s = quiet_s
        self.poll_s = poll_s

    def wait(self, region: tuple[int, int, int, int] | None = None,
             timeout_s: float = 600.0,
             should_stop: "callable | None" = None) -> str:
        """Returns 'possibly_complete' | 'timeout' | 'stopped'."""
        if not IS_WINDOWS:
            return "timeout"
        try:
            import pyautogui
        except ImportError:
            return "timeout"
        start = time.time()
        last_sig = None
        last_change = start
        saw_activity = False
        while time.time() - start < timeout_s:
            if should_stop and should_stop():
                return "stopped"
            try:
                shot = pyautogui.screenshot(region=region)
                sig = shot.tobytes().__hash__()
            except Exception:
                time.sleep(self.poll_s)
                continue
            if last_sig is not None and sig != last_sig:
                saw_activity = True
                last_change = time.time()
            last_sig = sig
            if saw_activity and time.time() - last_change >= self.quiet_s:
                return "possibly_complete"
            time.sleep(self.poll_s)
        return "timeout"
