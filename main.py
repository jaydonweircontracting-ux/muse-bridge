"""Muse Bridge entry point.

Packaging note: this file is the PyInstaller entry script and runs as
``__main__`` with no parent package. It must use ABSOLUTE imports
(``from muse_bridge...``), never relative imports (``from .gui ...``),
or the packaged EXE crashes on startup with::

    ImportError: attempted relative import with no known parent package
"""
import os
import sys
import traceback


def _ensure_repo_on_path() -> None:
    # Make sure the directory containing the ``muse_bridge`` package is
    # importable, whether run as a script (dev) or frozen (PyInstaller).
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def _write_crash_log(exc_text: str) -> str:
    # The --windowed build hides the console, so a crash would otherwise be
    # invisible. Always leave a log file next to the EXE (CWD as fallback).
    try:
        base = os.path.dirname(os.path.abspath(sys.argv[0] or __file__))
    except Exception:
        base = os.getcwd()
    path = os.path.join(base, "MuseBridge-error.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(exc_text + "\n")
        return path
    except Exception:
        return ""


def main() -> None:
    _ensure_repo_on_path()
    # ABSOLUTE import: main.py lives next to the package, not inside it.
    from muse_bridge.gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        text = traceback.format_exc()
        log_path = _write_crash_log(text)
        try:
            # Best-effort visible alert (tkinter itself may be broken).
            import tkinter.messagebox as mb

            mb.showerror(
                "Muse Bridge failed to start",
                "Muse Bridge crashed on startup.\n\n"
                + (f"Details written to:\n{log_path}\n\n" if log_path else "")
                + text[-2000:],
            )
        except Exception:
            pass
        # Also echo to stderr for console / dev runs.
        sys.stderr.write(text)
        sys.exit(1)
