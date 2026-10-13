"""Windows UIA-backed window agents for the relay.

STATUS: PARTIALLY VERIFIED. The decision logic in this module —
challenge detection, response extraction, completion detection,
submission verification — is pure/testable and covered by the
simulation lab (tests/sim_lab/) which drives the REAL CompletionWatcher
against scripted streaming scenarios. The pywinauto element locators
(address bar, composer, stop-generating button) still require
confirmation against the real ChatGPT/Muse DOM on the user's PC via
`python -m muse_bridge.relay_windows_check`.

Requires: pywinauto (already a bridge dependency on Windows).
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable

from .relay import ChallengeDetected, WindowAgent, message_in_text


# ---------------------------------------------------------------------------
# Pure decision functions — no pywinauto, fully unit-tested.
# ---------------------------------------------------------------------------

CHALLENGE_MARKERS = (
    # CAPTCHA / bot checks
    "verify you are human", "captcha", "are you a robot",
    "just a moment", "checking your browser",
    # auth walls
    "log in to continue", "sign in to continue", "session expired",
    # rate limits / errors shown instead of an answer
    "too many requests", "rate limit", "unusual traffic",
    "something went wrong", "failed to get response", "network error",
)


def detect_challenge(text: str) -> str:
    """Return the matched blocker marker, or "" when the page looks clean.

    Errs toward pausing: a false positive only pauses the pair (the user
    resumes); a false negative would automate against a challenge page.
    """
    low = text.lower()
    for marker in CHALLENGE_MARKERS:
        if marker in low:
            return marker
    return ""


def extract_response(full_text: str, submitted_text: str) -> str:
    """Everything after the FIRST occurrence of our submitted message.

    First (not last) occurrence: chronological order means our message
    precedes any quote of it inside the answer, so a first-occurrence
    split errs toward inclusion (complete, maybe noisy) rather than
    truncation (a partial answer delivered as complete — forbidden).
    Falls back to normalized-whitespace matching, then to the full text.
    """
    marker = submitted_text.strip()
    if marker:
        idx = full_text.find(marker)
        if idx != -1:
            return full_text[idx + len(marker):].strip()
        ntext, nmarker = " ".join(full_text.split()), " ".join(marker.split())
        nidx = ntext.find(nmarker)
        if nidx != -1:
            return ntext[nidx + len(nmarker):].strip()
    return full_text.strip()


def wait_for_condition(cond: Callable[[], bool], timeout_s: float,
                       poll_s: float = 0.5, sleep=time.sleep,
                       now=time.time) -> bool:
    """Poll cond() until true or the timeout. Exceptions propagate —
    the caller decides whether they mean pause or failure."""
    deadline = now() + timeout_s
    while now() < deadline:
        if cond():
            return True
        sleep(poll_s)
    return cond()


class CompletionWatcher:
    """Decide when a streamed response is COMPLETE.

    Completion requires ALL of:
      1. text differs from the submit-time baseline,
      2. no "generating" indicator is currently shown,
      3. text unchanged for `stable_s` seconds.

    If the generating indicator was NEVER observed during the wait,
    additionally require `settle_s` of stability (documented best-effort
    policy): a quiet interval alone never establishes completion, because
    a streaming pause looks exactly like silence. If the indicator was
    seen and then disappeared, `stable_s` suffices — the UI told us it
    stopped generating.

    Returns (baseline_text, stable_full_text) or None on timeout.
    Raises ChallengeDetected when the page turns into a blocker mid-wait.
    """

    def __init__(self, read_text: Callable[[], str],
                 is_generating: Callable[[], bool],
                 sleep=time.sleep, now=time.time,
                 poll_s: float = 2.0, stable_s: float = 4.0,
                 settle_s: float = 30.0) -> None:
        self._read_text = read_text
        self._is_generating = is_generating
        self._sleep = sleep
        self._now = now
        self._poll_s = poll_s
        self._stable_s = stable_s
        self._settle_s = settle_s

    def wait(self, timeout_s: float,
             baseline_hash: str) -> tuple[str, str] | None:
        deadline = self._now() + timeout_s
        baseline_text = ""
        last_text = ""
        # None = stability clock not started (as opposed to 0.0, which
        # would fake a huge quiet interval — a real defect this
        # simulation caught: when the generating indicator vanished
        # without a text change, quiet was computed from epoch).
        stable_since: float | None = None
        observed_generating = False
        while self._now() < deadline:
            try:
                generating = self._is_generating()
            except Exception:
                # A broken indicator check must not declare completion by
                # itself — without an observed indicator we fall back to
                # the conservative settle_s path below.
                generating = False
            try:
                text = self._read_text()
            except ChallengeDetected:
                raise
            except Exception:
                return None
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if h == baseline_hash:
                baseline_text = text
                last_text = ""
                stable_since = None
                self._sleep(self._poll_s)
                continue
            if generating:
                observed_generating = True
                last_text = text
                stable_since = None
                self._sleep(self._poll_s)
                continue
            if stable_since is None or text != last_text:
                # (Re)start the stability clock: new text, or the first
                # quiet poll after generation ended.
                last_text = text
                stable_since = self._now()
            quiet = self._now() - stable_since
            if quiet >= self._stable_s and (
                    observed_generating or quiet >= self._settle_s):
                return (baseline_text, text)
            self._sleep(self._poll_s)
        return None

    def confirm_stable(self, timeout_s: float) -> str | None:
        """Return the current text if it stays byte-identical with no
        generating indicator for the whole timeout; else None. Used for
        the adopt path, where the response may already be sitting in the
        conversation from an earlier attempt."""
        deadline = self._now() + timeout_s
        first: str | None = None
        while self._now() < deadline:
            try:
                if self._is_generating():
                    return None
            except Exception:
                pass
            try:
                text = self._read_text()
            except ChallengeDetected:
                raise
            except Exception:
                return None
            if first is None:
                first = text
            elif text != first:
                return None
            self._sleep(self._poll_s)
        return first


# ---------------------------------------------------------------------------
# pywinauto-backed agent (Windows only).
# ---------------------------------------------------------------------------

def _uia():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _hwnd_alive(handle: int) -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindow(handle))
    except Exception:
        return False


def _hwnd_pid(handle: int) -> int | None:
    try:
        import ctypes
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(
            handle, ctypes.byref(pid))
        return pid.value
    except Exception:
        return None


class UiaWindowAgent(WindowAgent):
    """Drives one browser window by HWND via UI Automation."""

    def __init__(self, handle: int, role: str) -> None:
        self.handle = handle
        self.role = role  # "muse" | "chatgpt"
        self._win = None
        self._pid: int | None = None

    def _window(self):
        # Revalidate the handle on EVERY consequential use: never operate
        # on a stale cached element, and detect HWND reuse by another
        # window (same handle, different process).
        if not _hwnd_alive(self.handle):
            self._win = None
            self._pid = None
            raise RuntimeError("window handle is no longer alive")
        pid = _hwnd_pid(self.handle)
        if self._pid is None:
            self._pid = pid
        elif pid != self._pid:
            raise RuntimeError(
                f"HWND {self.handle} was reused by a different window — "
                f"identity changed, refusing to act")
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
        text = "\n".join(parts)
        reason = detect_challenge(text)
        if reason:
            raise ChallengeDetected(f"window shows {reason!r}")
        return text

    def conversation_id(self) -> str:
        # Reads the address bar. Chrome/Edge expose it as an Edit control
        # named "Address and search bar". UNVERIFIED against the live DOM.
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
        # ChatGPT's composer is a contenteditable div; UIA usually exposes
        # it as a Document/Edit named "Message ChatGPT". UNVERIFIED.
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
        # muse.ai composer heuristics. UNVERIFIED.
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
        """Type into the composer and submit. Returns True ONLY if the
        message is verified visible in the conversation afterwards —
        a successful typing call alone proves nothing."""
        if len(text) > 8000:
            # Paste long messages in chunks instead of silently
            # truncating (old defect).
            return self._type_long(text)
        try:
            box = (self._chatgpt_input() if self.role == "chatgpt"
                   else self._muse_input())
            if box is None:
                return False
            try:
                if not box.is_enabled():
                    return False
            except Exception:
                pass
            self.read_text()  # raises ChallengeDetected on blocker pages
            box.click_input()
            time.sleep(0.3)
            import subprocess
            subprocess.run(["powershell", "-command", "Set-Clipboard",
                            "-Value", text],
                           capture_output=True, timeout=15)
            box.type_keys("^v", set_focus=False)
            time.sleep(0.5)
            box.type_keys("{ENTER}", set_focus=False)
            # Verify the message actually landed in the conversation.
            # If the render is just slow, the relay's adopt-check on retry
            # picks it up instead of resubmitting (no duplicates).
            try:
                return wait_for_condition(
                    lambda: message_in_text(self.read_text(), text),
                    timeout_s=5.0, poll_s=0.5)
            except ChallengeDetected:
                raise
            except Exception:
                return False
        except ChallengeDetected:
            raise
        except Exception:
            return False

    def _type_long(self, text: str) -> bool:
        """Chunked paste for messages over 8000 chars."""
        try:
            box = (self._chatgpt_input() if self.role == "chatgpt"
                   else self._muse_input())
            if box is None:
                return False
            self.read_text()
            box.click_input()
            time.sleep(0.3)
            import subprocess
            for i in range(0, len(text), 8000):
                subprocess.run(["powershell", "-command", "Set-Clipboard",
                                "-Value", text[i:i + 8000]],
                               capture_output=True, timeout=15)
                box.type_keys("^v", set_focus=False)
                time.sleep(0.4)
            box.type_keys("{ENTER}", set_focus=False)
            try:
                return wait_for_condition(
                    lambda: message_in_text(self.read_text(),
                                            text[:500]),
                    timeout_s=8.0, poll_s=0.5)
            except ChallengeDetected:
                raise
            except Exception:
                return False
        except ChallengeDetected:
            raise
        except Exception:
            return False

    def _is_generating(self) -> bool:
        # While streaming, ChatGPT shows a "Stop generating" button.
        # UNVERIFIED name — the watcher treats a missing indicator
        # conservatively (settle_s path).
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

    def wait_for_response(self, timeout_s: float, baseline_hash: str,
                          submitted_text: str = "") -> str | None:
        """Return the COMPLETE new response, or None on timeout/ambiguity.
        Never returns a partial streaming response as complete — see
        CompletionWatcher. Raises ChallengeDetected on blocker pages."""
        watcher = CompletionWatcher(self.read_text, self._is_generating)
        try:
            result = watcher.wait(timeout_s, baseline_hash)
        except ChallengeDetected:
            raise
        except Exception:
            return None
        if not result:
            return None
        _, stable_text = result
        if not submitted_text:
            return None
        return extract_response(stable_text, submitted_text) or None

    def stable_response_to(self, submitted_text: str,
                           timeout_s: float = 10.0) -> str | None:
        watcher = CompletionWatcher(self.read_text, self._is_generating)
        try:
            stable_text = watcher.confirm_stable(timeout_s)
        except ChallengeDetected:
            raise
        except Exception:
            return None
        if not stable_text:
            return None
        return extract_response(stable_text, submitted_text) or None


def agent_for(handle: int, role: str) -> WindowAgent:
    """Factory used by the GUI. Raises on non-Windows."""
    import sys
    if sys.platform != "win32":
        raise RuntimeError("UIA agents require Windows")
    return UiaWindowAgent(handle, role)
