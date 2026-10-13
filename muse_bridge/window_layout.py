"""Window tiling for the worker fleet, inspired by OpenMultiBoxing.

What we take from the multiboxing world: track target windows by title and
arrange them in a visible grid so the user can watch all five Muse sessions
at once. What we deliberately leave out: injecting keystrokes or clicks
into background windows (PostMessage/SendMessage). Handoffs stay explicit
clipboard copy/paste with user confirmation.

Windows-only (pywinauto). Every function is a safe no-op elsewhere.
"""
from __future__ import annotations

import os

IS_WINDOWS = os.name == "nt"


def _have_pywinauto() -> bool:
    try:
        import pywinauto  # noqa: F401
        return True
    except ImportError:
        return False


def work_area() -> tuple[int, int, int, int]:
    """Primary monitor work area as (left, top, width, height)."""
    if not IS_WINDOWS:
        return (0, 0, 0, 0)
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        SPI_GETWORKAREA = 0x0030
        rect = RECT()
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                                  ctypes.byref(rect), 0)
        return (rect.left, rect.top,
                rect.right - rect.left, rect.bottom - rect.top)
    except Exception:
        return (0, 0, 0, 0)


def tile_windows(titles: list[str]) -> tuple[int, str]:
    """Arrange the given windows (exact titles) in a grid.

    Returns (placed_count, message). Windows not found are skipped.
    """
    if not IS_WINDOWS:
        return 0, "Window tiling needs Windows."
    if not _have_pywinauto():
        return 0, "pywinauto not installed. pip install pywinauto."
    try:
        from pywinauto import Desktop
        wins = []
        for title in titles:
            found = [w for w in Desktop(backend="uia").windows()
                     if w.window_text() == title]
            if found:
                wins.append(found[0])
        if not wins:
            return 0, "No bound worker windows found."
        left, top, width, height = work_area()
        n = len(wins)
        cols = 3 if n > 4 else 2 if n > 1 else 1
        rows = (n + cols - 1) // cols
        cw, ch = width // cols, height // rows
        for i, w in enumerate(wins):
            r, c = divmod(i, cols)
            try:
                w.move_window(left + c * cw, top + r * ch, cw, ch,
                              repaint=True)
            except Exception:
                continue
        return len(wins), f"Tiled {len(wins)} window(s) in a {cols}x{rows} grid."
    except Exception as exc:
        return 0, f"Tiling failed: {exc}"
