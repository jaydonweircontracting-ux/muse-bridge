"""Response-state tracker for Muse Bridge.

The core safety requirement: the bot must NEVER assume a response is
complete merely because the screen stopped changing, must never send a
second prompt while the previous task is running, and must never treat
an incomplete response as final.

States mirror the project spec. Every transition is logged with a
timestamp. Timeouts and retries are bounded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class ResponseState(str, Enum):
    READY = "READY"
    TASK_SENT = "TASK_SENT"
    WAITING_FOR_RESPONSE = "WAITING_FOR_RESPONSE"
    RESPONSE_IN_PROGRESS = "RESPONSE_IN_PROGRESS"
    RESPONSE_COMPLETE = "RESPONSE_COMPLETE"
    REVIEWING = "REVIEWING"
    SENDING_TO_MUSE = "SENDING_TO_MUSE"
    MUSE_WORKING = "MUSE_WORKING"
    VERIFYING = "VERIFYING"
    CHECKPOINTING = "CHECKPOINTING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


# Legal transitions. Anything not listed here is rejected and logged.
_ALLOWED: dict[ResponseState, set[ResponseState]] = {
    ResponseState.READY: {
        ResponseState.TASK_SENT, ResponseState.PAUSED,
        ResponseState.STOPPED, ResponseState.BLOCKED,
    },
    ResponseState.TASK_SENT: {
        ResponseState.WAITING_FOR_RESPONSE, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.WAITING_FOR_RESPONSE: {
        ResponseState.RESPONSE_IN_PROGRESS, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.RESPONSE_IN_PROGRESS: {
        ResponseState.RESPONSE_COMPLETE, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.RESPONSE_COMPLETE: {
        ResponseState.REVIEWING, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.REVIEWING: {
        ResponseState.SENDING_TO_MUSE, ResponseState.READY,
        ResponseState.BLOCKED, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.SENDING_TO_MUSE: {
        ResponseState.MUSE_WORKING, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.MUSE_WORKING: {
        ResponseState.VERIFYING, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.VERIFYING: {
        ResponseState.CHECKPOINTING, ResponseState.READY,
        ResponseState.BLOCKED, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.CHECKPOINTING: {
        ResponseState.READY, ResponseState.ERROR,
        ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.PAUSED: {
        ResponseState.READY, ResponseState.STOPPED,
    },
    ResponseState.BLOCKED: {
        ResponseState.READY, ResponseState.STOPPED,
    },
    ResponseState.ERROR: {
        ResponseState.READY, ResponseState.PAUSED, ResponseState.STOPPED,
    },
    ResponseState.STOPPED: {
        ResponseState.READY,
    },
}


@dataclass
class Transition:
    from_state: str
    to_state: str
    at: float
    reason: str = ""


@dataclass
class StateTracker:
    """Tracks one task's lifecycle through the response states."""

    state: ResponseState = ResponseState.READY
    state_entered_at: float = field(default_factory=time.time)
    history: list[Transition] = field(default_factory=list)
    # Configurable safety bounds
    response_timeout_s: float = 600.0
    max_retries: int = 3
    retries_used: int = 0
    on_change: Optional[Callable[[Transition], None]] = None

    def transition(self, new_state: ResponseState, reason: str = "") -> bool:
        """Attempt a transition. Returns False (and logs) if illegal."""
        allowed = _ALLOWED.get(self.state, set())
        now = time.time()
        if new_state not in allowed:
            tr = Transition(self.state.value, new_state.value, now,
                            f"REJECTED illegal transition: {reason}")
            self.history.append(tr)
            if self.on_change:
                self.on_change(tr)
            return False
        tr = Transition(self.state.value, new_state.value, now, reason)
        self.history.append(tr)
        self.state = new_state
        self.state_entered_at = now
        if new_state == ResponseState.TASK_SENT:
            self.retries_used = 0
        if self.on_change:
            self.on_change(tr)
        return True

    def time_in_state(self) -> float:
        return time.time() - self.state_entered_at

    def is_timed_out(self) -> bool:
        return self.state in (
            ResponseState.WAITING_FOR_RESPONSE,
            ResponseState.RESPONSE_IN_PROGRESS,
            ResponseState.MUSE_WORKING,
        ) and self.time_in_state() > self.response_timeout_s

    def can_retry(self) -> bool:
        return self.retries_used < self.max_retries

    def note_retry(self) -> None:
        self.retries_used += 1

    def busy(self) -> bool:
        """True while a task is in flight — new tasks must NOT start."""
        return self.state not in (
            ResponseState.READY, ResponseState.PAUSED,
            ResponseState.BLOCKED, ResponseState.ERROR,
            ResponseState.STOPPED,
        )
