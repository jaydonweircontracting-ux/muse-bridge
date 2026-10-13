"""Simulated window agent for the relay lab.

Implements the production WindowAgent contract against a SimDesktop, but
— critically — delegates completion detection, response extraction,
challenge detection, and submission verification to the REAL production
functions in muse_bridge.uia_agent (CompletionWatcher,
extract_response, detect_challenge, message_in_text,
wait_for_condition) and raises the REAL ChallengeDetected. Faults are
modeled in the desktop (dead HWNDs, reused HWNDs, missing composers,
failed pastes, newline-submits, blocker pages); the agent's job is to
behave like the production UIA agent would against such a desktop.
"""
from __future__ import annotations

from muse_bridge.relay import (ChallengeDetected, WindowAgent,
                             message_in_text)
from muse_bridge.uia_agent import (CompletionWatcher, detect_challenge,
                                   extract_response, wait_for_condition)

from .clock import VirtualClock
from .desktop import SimDesktop, SimWindow


class SimWindowAgent(WindowAgent):
    def __init__(self, desktop: SimDesktop, hwnd: int, role: str,
                 clock: VirtualClock) -> None:
        self.desktop = desktop
        self.hwnd = hwnd
        self.role = role
        self.clock = clock
        self._pid = self._resolve().pid

    def _resolve(self) -> SimWindow:
        w = self.desktop.find_by_hwnd(self.hwnd)
        if w is None or not w.alive:
            raise RuntimeError("window handle is no longer alive")
        return w

    def _window(self) -> SimWindow:
        w = self._resolve()
        if w.pid != self._pid:
            raise RuntimeError(
                f"HWND {self.hwnd} was reused by a different window — "
                f"identity changed, refusing to act")
        return w

    # -- reading ----------------------------------------------------------
    def read_text(self) -> str:
        w = self._window()
        text = w.render_text()
        reason = detect_challenge(text)
        if reason:
            raise ChallengeDetected(f"window shows {reason!r}")
        return text

    def conversation_id(self) -> str:
        w = self._window()
        return w.url if w.url_readable else ""

    # -- acting -----------------------------------------------------------
    def focus(self) -> bool:
        try:
            self._window()
            return True
        except Exception:
            return False

    def type_and_submit(self, text: str) -> bool:
        """Mirror the production UIA agent's structure: act, then verify
        the message is visible; return False unless verified."""
        try:
            w = self._window()
        except Exception:
            return False
        if not w.composer_present or not w.composer_enabled:
            return False
        try:
            self.read_text()  # raises ChallengeDetected on blockers
        except ChallengeDetected:
            raise
        except Exception:
            return False
        if not w.paste_ok:
            return False
        w.composer_text = text
        if not w.submit_ok:
            # ENTER inserted a newline instead of submitting.
            w.composer_text += "\n"
            return False
        w.submit_message(text, self.clock)
        w.composer_text = ""
        try:
            return wait_for_condition(
                lambda: message_in_text(self.read_text(), text),
                timeout_s=5.0, poll_s=0.5,
                sleep=self.clock.sleep, now=self.clock.now)
        except ChallengeDetected:
            raise
        except Exception:
            return False

    def _is_generating(self) -> bool:
        return self._window().generating

    def wait_for_response(self, timeout_s: float, baseline_hash: str,
                          submitted_text: str = "") -> str | None:
        # THE critical path: the real production CompletionWatcher,
        # driven by the virtual clock, against scripted streaming.
        try:
            self._window()
        except Exception:
            return None
        watcher = CompletionWatcher(
            self.read_text, self._is_generating,
            sleep=self.clock.sleep, now=self.clock.now)
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
        try:
            self._window()
        except Exception:
            return None
        watcher = CompletionWatcher(
            self.read_text, self._is_generating,
            sleep=self.clock.sleep, now=self.clock.now)
        try:
            stable_text = watcher.confirm_stable(timeout_s)
        except ChallengeDetected:
            raise
        except Exception:
            return None
        if not stable_text:
            return None
        return extract_response(stable_text, submitted_text) or None
