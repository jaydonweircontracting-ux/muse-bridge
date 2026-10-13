"""Muse integration for Muse Bridge.

TWO integration levels, probed at runtime — best available wins:

Level 1 — Muse Code CLI (`muse` command).
  Meta's terminal coding agent (macOS/Linux/Windows, Meta Model API
  account). The bot runs it non-interactively in the repo directory;
  the PROCESS EXIT CODE is the completion signal (reliable) and stdout
  is the structured report. This is the preferred path whenever the
  binary is present. Flag auto-discovery: the adapter reads
  `muse --help` for a non-interactive/print/exec flag instead of
  hardcoding one it has not verified.

Level 2 — Muse app window automation (fallback).
  Find window by title, focus, paste via pywinauto/pyautogui.
  HONEST LIMITATION: detecting "task finished" has NO reliable signal.
  The fallback watches for no-new-text quiet periods, which can be
  wrong. The UI labels this UNCERTAIN and requires user confirmation
  before risky operations.

If neither is available, the module says so instead of pretending.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field


IS_WINDOWS = platform.system() == "Windows"


@dataclass
class WindowInfo:
    title: str
    handle: int = 0


@dataclass
class CapabilityReport:
    cli_available: bool
    cli_detail: str
    windows_listable: bool
    can_focus: bool
    can_paste: bool
    completion_detection: str  # "cli-exit-code" | "uncertain" | "unavailable"
    detail: str


# ---------------------------------------------------------------- Muse Code CLI

@dataclass
class CliProbe:
    found: bool
    path: str = ""
    version: str = ""
    non_interactive_flag: str = ""  # auto-discovered from --help, else ""
    detail: str = ""


@dataclass
class CliResult:
    ok: bool                    # True iff process exited 0
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _run_capture(argv: list[str], timeout: int = 30,
                 cwd: str | None = None) -> tuple[int, str, str, bool]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr, False
    except FileNotFoundError:
        return 127, "", "binary not found", False
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return 124, out, err, True


class MuseCodeCli:
    """Adapter for the `muse` terminal coding agent.

    Completion detection: RELIABLE — the process exiting (code 0) means
    the agent finished. Non-zero / timeout means it did not.
    """

    # Flag names to look for in `muse --help`, in preference order.
    _FLAG_HINTS = ("--print", "--non-interactive", "--exec", "--batch",
                   "-p", "--yes")

    def __init__(self, invoke_template: str = "") -> None:
        # e.g. 'muse --print "{prompt}"' — {prompt} and {cwd} substituted.
        self.invoke_template = invoke_template

    def probe(self) -> CliProbe:
        path = shutil.which("muse")
        if not path:
            return CliProbe(found=False,
                            detail="`muse` not on PATH. Install from "
                                   "https://dev.meta.ai (Muse Code).")
        code, out, err, _ = _run_capture([path, "--version"], timeout=20)
        version = (out or err).strip().splitlines()[0][:80] if (out or err) else ""
        # Auto-discover a non-interactive flag from --help.
        flag = ""
        _, help_out, help_err, _ = _run_capture([path, "--help"], timeout=20)
        help_text = (help_out or "") + "\n" + (help_err or "")
        for hint in self._FLAG_HINTS:
            if re.search(rf"(^|\s){re.escape(hint)}(\s|,|\[|=|$)", help_text, re.M):
                flag = hint
                break
        detail = f"Found at {path} ({version or 'version unknown'})."
        detail += (f" Non-interactive flag: {flag}." if flag
                   else " No non-interactive flag found in --help; set one manually.")
        return CliProbe(found=True, path=path, version=version,
                        non_interactive_flag=flag, detail=detail)

    def run_task(self, prompt: str, cwd: str,
                 timeout_s: int = 3600) -> CliResult:
        """Run one task non-interactively. Blocks until exit/timeout."""
        probe = self.probe()
        if not probe.found:
            return CliResult(False, 127, "", "muse CLI not found")
        if self.invoke_template:
            cmd = self.invoke_template.replace("{prompt}", prompt)
            cmd = cmd.replace("{cwd}", cwd)
            argv = self._split(cmd)
        elif probe.non_interactive_flag:
            argv = [probe.path, probe.non_interactive_flag, prompt]
        else:
            return CliResult(False, 2, "",
                             "No non-interactive flag known; configure "
                             "muse_cli_template in settings.")
        # Write prompt to a temp file to avoid shell-quoting issues when
        # the template uses {prompt_file}.
        code, out, err, timed_out = _run_capture(
            argv, timeout=timeout_s, cwd=cwd)
        # Keep output bounded for logs.
        return CliResult(ok=(code == 0 and not timed_out), exit_code=code,
                         stdout=out[-8000:], stderr=err[-2000:],
                         timed_out=timed_out)

    @staticmethod
    def _split(cmd: str) -> list[str]:
        import shlex
        return shlex.split(cmd, posix=(platform.system() != "Windows"))


# ---------------------------------------------------------------- window automation (fallback)

def capabilities() -> CapabilityReport:
    cli = MuseCodeCli().probe()
    if not IS_WINDOWS:
        # CLI can still work on macOS/Linux; window automation cannot.
        det = ("Window automation unavailable off Windows. "
               + cli.detail)
        return CapabilityReport(
            cli_available=cli.found, cli_detail=cli.detail,
            windows_listable=False, can_focus=False, can_paste=False,
            completion_detection=("cli-exit-code" if cli.found else "unavailable"),
            detail=det)
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
    if cli.found:
        completion = "cli-exit-code"
        detail = "Muse Code CLI available (reliable completion). " + cli.detail
    elif have_pw and have_pg:
        completion = "uncertain"
        detail = ("Window control available via pywinauto/pyautogui, but "
                  "completion detection is UNCERTAIN: no official Muse "
                  "completion signal exists; the bot uses a no-new-text "
                  "timeout heuristic and asks you to confirm before risky steps.")
    else:
        completion = "unavailable"
        missing = [n for n, h in (("pywinauto", have_pw), ("pyautogui", have_pg)) if not h]
        detail = f"No Muse integration: CLI missing and window deps missing ({', '.join(missing)})."
    return CapabilityReport(
        cli_available=cli.found, cli_detail=cli.detail,
        windows_listable=have_pw and have_pg,
        can_focus=have_pw and have_pg, can_paste=have_pw and have_pg,
        completion_detection=completion, detail=detail)


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
