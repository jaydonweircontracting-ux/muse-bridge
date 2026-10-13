"""Window coordination for Muse Bridge (free edition).

What this module does (and only this):
  - list visible windows so the user can SELECT their ChatGPT and Muse windows,
  - bring a selected window to the foreground,
  - report honestly what is and is not automated.

What it deliberately does NOT do:
  - submit messages on its own (the user pastes + submits, or confirms),
  - detect "task finished" by guessing (the user confirms completion),
  - scrape page content, use hidden APIs, or inject into the browser.

Every automated action here is a legitimate, supported OS-level operation
(window focus). Clipboard copy is done by the GUI toolkit.
"""

from __future__ import annotations

import platform
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
    detail: str


def _have_pywinauto() -> bool:
    try:
        import pywinauto  # noqa: F401
        return True
    except ImportError:
        return False


def capabilities() -> CapabilityReport:
    if not IS_WINDOWS:
        return CapabilityReport(
            windows_listable=False,
            can_focus=False,
            detail="Not running on Windows; window coordination needs Windows.",
        )
    if _have_pywinauto():
        return CapabilityReport(
            windows_listable=True,
            can_focus=True,
            detail=("Window listing + focus available via pywinauto. "
                    "Message submission and completion detection are "
                    "explicit user steps — never automated guesses."),
        )
    return CapabilityReport(
        windows_listable=False,
        can_focus=False,
        detail="pywinauto not installed. pip install pywinauto.",
    )


def list_windows(substring: str = "") -> list[WindowInfo]:
    """List visible top-level windows, optionally filtered by title."""
    if not IS_WINDOWS or not _have_pywinauto():
        return []
    from pywinauto import Desktop
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
    """Bring the selected window to the foreground. Returns success."""
    if not IS_WINDOWS or not _have_pywinauto():
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
