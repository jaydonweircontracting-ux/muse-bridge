"""Simulated desktop for relay testing.

Models the pieces of a Windows desktop the relay depends on: windows
with HWNDs/titles/processes, per-window conversation URLs, message
histories, composer controls, streaming responses, and blocker pages.
Every behavior is scripted and deterministic; time only advances through
the VirtualClock, so the REAL production CompletionWatcher and relay
orchestration can run against it at full speed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .clock import VirtualClock


@dataclass
class SimMessage:
    role: str          # "user" | "assistant"
    text: str
    complete: bool = True


class SimWindow:
    """One simulated top-level window (a browser window in practice)."""

    _next_pid = 5000

    def __init__(self, hwnd: int, title: str, url: str = "",
                 role: str = "chatgpt") -> None:
        self.hwnd = hwnd
        self.title = title
        self.url = url
        self.role = role            # "chatgpt" | "muse"
        self.alive = True
        self.pid = SimWindow._next_pid
        SimWindow._next_pid += 1
        self.messages: list[SimMessage] = []
        # Composer / submission mechanics.
        self.composer_present = True
        self.composer_enabled = True
        self.composer_text = ""
        self.paste_ok = True
        self.submit_ok = True       # False -> ENTER inserts a newline
        self.render_delay_s = 0.0   # user message appears after a delay
        # Blockers.
        self.challenge = ""         # "captcha" | "login" | "ratelimit" | "error"
        self.url_readable = True
        # Streaming.
        self.generating = False
        self.response_mode = "instant"   # set via set_response()
        self.response_text = ""
        self.response_pause_s = 0.0      # mid-stream pause (no indicator)
        # Scheduled events: (at_t, kind, payload).
        self._plan: list[tuple[float, str, object]] = []
        self._pending_render: list[tuple[float, SimMessage]] = []
        # Audit log: every submission with the URL live at that moment.
        self.submissions: list[dict] = []

    # -- configuration --------------------------------------------------
    def set_response(self, text: str, mode: str = "instant",
                     pause_s: float = 0.0) -> None:
        """Script the assistant's reply to the next submitted message.

        modes: instant | slow | pauses (pause_s gap, no indicator) |
               never (streams forever) | stall (partial then stops,
               indicator goes away)
        """
        self.response_text = text
        self.response_mode = mode
        self.response_pause_s = pause_s

    def add_history(self, role: str, text: str) -> None:
        self.messages.append(SimMessage(role, text, True))

    # -- what UIA would see ----------------------------------------------
    def render_text(self) -> str:
        parts: list[str] = []
        if self.challenge == "captcha":
            parts.append("Verify you are human")
        elif self.challenge == "login":
            parts.append("Log in to continue")
        elif self.challenge == "ratelimit":
            parts.append("Too many requests — rate limit reached")
        elif self.challenge == "error":
            parts.append("Something went wrong")
        for m in self.messages:
            parts.append(m.text)
        if self.composer_text:
            parts.append(self.composer_text)
        return "\n".join(parts)

    # -- submission -------------------------------------------------------
    def submit_message(self, text: str, clock: VirtualClock) -> None:
        """The ENTER key was pressed with `text` pasted in the composer."""
        self.submissions.append({"url": self.url, "text": text,
                                 "t": clock.now()})
        now = clock.now()
        appear_at = now + self.render_delay_s
        self._pending_render.append(
            (appear_at, SimMessage("user", text, True)))
        # The assistant can't respond before the message is received.
        self._schedule_response(appear_at)

    def _schedule_response(self, now: float) -> None:
        mode, text = self.response_mode, self.response_text
        if mode == "instant":
            self._plan.append((now, "generating_on", None))
            self._plan.append((now + 1.0, "assistant_append",
                               (text, True)))
            self._plan.append((now + 1.0, "generating_off", None))
        elif mode == "slow":
            chunks = _chunks(text, 5)
            self._plan.append((now, "generating_on", None))
            t = now
            for ch in chunks:
                t += 3.0
                self._plan.append((t, "assistant_append", (ch, False)))
            self._plan.append((t + 0.5, "assistant_complete", None))
            self._plan.append((t + 0.5, "generating_off", None))
        elif mode == "pauses":
            chunks = _chunks(text, 4)
            self._plan.append((now, "generating_on", None))
            t = now
            half = len(chunks) // 2
            for ch in chunks[:half]:
                t += 2.0
                self._plan.append((t, "assistant_append", (ch, False)))
            # The dangerous part: a long pause with NO indicator.
            t += self.response_pause_s
            self._plan.append((t, "generating_off", None))
            for ch in chunks[half:]:
                t += 2.0
                self._plan.append((t, "assistant_append", (ch, False)))
            self._plan.append((t + 0.5, "assistant_complete", None))
        elif mode == "never":
            self._plan.append((now, "generating_on", None))
            # generating never turns off -> watcher must time out
        elif mode == "stall":
            chunks = _chunks(text, 4)
            self._plan.append((now, "generating_on", None))
            t = now
            for ch in chunks[:2]:
                t += 2.0
                self._plan.append((t, "assistant_append", (ch, False)))
            # Stream stalls AND the indicator vanishes (UI thinks done).
            self._plan.append((t + 1.0, "generating_off", None))

    # -- faults -----------------------------------------------------------
    def kill(self) -> None:
        self.alive = False

    def reuse_hwnd(self, new_title: str = "", new_url: str = "") -> None:
        """Simulate the OS handing this HWND to a different window."""
        self.pid = SimWindow._next_pid
        SimWindow._next_pid += 1
        if new_title:
            self.title = new_title
        if new_url:
            self.url = new_url

    def schedule(self, at_t: float, kind: str, payload: object = None) -> None:
        self._plan.append((at_t, kind, payload))

    # -- time -------------------------------------------------------------
    def advance(self, clock: VirtualClock) -> None:
        now = clock.now()
        # Unified timeline: user-message renders and response events
        # apply in timestamp order (a response can never precede the
        # message it answers).
        events: list[tuple[float, int, object]] = []
        for t, kind, payload in self._plan:
            if t <= now:
                events.append((t, 0, (kind, payload)))
        for t, msg in self._pending_render:
            if t <= now:
                events.append((t, 1, msg))
        self._plan = [e for e in self._plan if e[0] > now]
        self._pending_render = [(t, m) for t, m in self._pending_render
                                if t > now]
        for _, _, payload in sorted(events, key=lambda e: (e[0], e[1])):
            if isinstance(payload, tuple):
                self._apply(*payload)
            else:
                self.messages.append(payload)

    def _apply(self, kind: str, payload: object) -> None:
        if kind == "generating_on":
            self.generating = True
        elif kind == "generating_off":
            self.generating = False
        elif kind == "assistant_append":
            text, complete = payload  # type: ignore
            # Continue the in-progress assistant message when streaming.
            if (self.messages and self.messages[-1].role == "assistant"
                    and not self.messages[-1].complete):
                self.messages[-1].text += text
                self.messages[-1].complete = complete
            else:
                self.messages.append(SimMessage("assistant", text, complete))
        elif kind == "assistant_complete":
            for m in reversed(self.messages):
                if m.role == "assistant":
                    m.complete = True
                    break
        elif kind == "set_url":
            self.url = payload  # type: ignore
        elif kind == "kill":
            self.kill()
        elif kind == "challenge":
            self.challenge = payload  # type: ignore


def _chunks(text: str, n: int) -> list[str]:
    # Character slices: concatenating the chunks reproduces the text
    # exactly (token streams carry their own spacing).
    if not text:
        return [text]
    size = max(1, (len(text) + n - 1) // n)
    return [text[i:i + size] for i in range(0, len(text), size)]


class SimDesktop:
    """The simulated desktop: owns windows and pumps their clocks."""

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.windows: list[SimWindow] = []
        self._next_hwnd = 100
        clock.on_advance(self.advance)

    def add_window(self, title: str, url: str = "",
                   role: str = "chatgpt") -> SimWindow:
        w = SimWindow(self._next_hwnd, title, url, role)
        self._next_hwnd += 1
        self.windows.append(w)
        return w

    def find_by_hwnd(self, hwnd: int) -> SimWindow | None:
        for w in self.windows:
            if w.hwnd == hwnd:
                return w
        return None

    def find_by_title(self, title: str) -> list[SimWindow]:
        return [w for w in self.windows if w.title == title and w.alive]

    def advance(self) -> None:
        for w in self.windows:
            w.advance(self.clock)
