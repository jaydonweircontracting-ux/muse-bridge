"""Explicit handoff states for Muse Bridge (free existing-window edition).

The bot NEVER guesses: every state transition is explicit, logged, and
persisted. A response is only "received" when the user transfers it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class HandoffState(str, Enum):
    READY = "READY"                    # idle, nothing in flight
    PREPARED = "PREPARED"              # prompt prepared & copied, not yet handed off
    WAITING_FOR_USER = "WAITING_FOR_USER"  # user pasting into ChatGPT/Muse
    MUSE_RUNNING = "MUSE_RUNNING"      # task with Muse, awaiting completion
    AWAITING_REPORT = "AWAITING_REPORT"    # Muse done (user-confirmed), report not yet transferred
    REVIEW_PENDING = "REVIEW_PENDING"  # review prompt prepared, awaiting ChatGPT review
    TASK_PASSED = "TASK_PASSED"        # review approved, checkpoint saved
    TASK_FAILED = "TASK_FAILED"        # review rejected / error
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"


# Legal transitions. Anything else is rejected and logged.
LEGAL: dict[HandoffState, set[HandoffState]] = {
    HandoffState.READY: {HandoffState.PREPARED, HandoffState.PAUSED,
                         HandoffState.STOPPED, HandoffState.BLOCKED},
    HandoffState.PREPARED: {HandoffState.WAITING_FOR_USER,
                            HandoffState.READY,   # reprepare / cancel
                            HandoffState.PAUSED, HandoffState.STOPPED},
    HandoffState.WAITING_FOR_USER: {HandoffState.MUSE_RUNNING,
                                    HandoffState.REVIEW_PENDING,
                                    HandoffState.READY, HandoffState.PAUSED,
                                    HandoffState.STOPPED, HandoffState.BLOCKED},
    HandoffState.MUSE_RUNNING: {HandoffState.AWAITING_REPORT,
                                HandoffState.PAUSED, HandoffState.STOPPED,
                                HandoffState.BLOCKED},
    HandoffState.AWAITING_REPORT: {HandoffState.REVIEW_PENDING,
                                   HandoffState.PAUSED, HandoffState.STOPPED},
    HandoffState.REVIEW_PENDING: {HandoffState.TASK_PASSED,
                                  HandoffState.TASK_FAILED,
                                  HandoffState.PREPARED,  # correction task
                                  HandoffState.PAUSED, HandoffState.STOPPED,
                                  HandoffState.BLOCKED},
    HandoffState.TASK_PASSED: {HandoffState.PREPARED, HandoffState.READY,
                               HandoffState.PAUSED, HandoffState.STOPPED},
    HandoffState.TASK_FAILED: {HandoffState.PREPARED, HandoffState.READY,
                               HandoffState.PAUSED, HandoffState.STOPPED,
                               HandoffState.BLOCKED},
    HandoffState.PAUSED: {HandoffState.READY, HandoffState.STOPPED},
    HandoffState.BLOCKED: {HandoffState.READY, HandoffState.STOPPED},
    HandoffState.STOPPED: {HandoffState.READY},
}

# States where a handoff is "in flight": starting another is forbidden.
IN_FLIGHT = {
    HandoffState.PREPARED, HandoffState.WAITING_FOR_USER,
    HandoffState.MUSE_RUNNING, HandoffState.AWAITING_REPORT,
    HandoffState.REVIEW_PENDING,
}


@dataclass
class Transition:
    at: float
    from_state: str
    to_state: str
    reason: str = ""


@dataclass
class StateTracker:
    state: HandoffState = HandoffState.READY
    history: list[Transition] = field(default_factory=list)
    max_retries: int = 3
    retries_used: int = 0
    on_change: Callable[[Transition], None] | None = None

    def can(self, to: HandoffState) -> bool:
        return to in LEGAL.get(self.state, set())

    def transition(self, to: HandoffState, reason: str = "") -> bool:
        if not self.can(to):
            tr = Transition(time.time(), self.state.value, to.value,
                            f"REJECTED illegal: {reason}")
            self.history.append(tr)
            if self.on_change:
                self.on_change(tr)
            return False
        tr = Transition(time.time(), self.state.value, to.value, reason)
        self.history.append(tr)
        self.state = to
        if to in (HandoffState.TASK_PASSED, HandoffState.TASK_FAILED,
                  HandoffState.READY):
            self.retries_used = 0
        if self.on_change:
            self.on_change(tr)
        return True

    def busy(self) -> bool:
        """True while a handoff is in flight — never start a second one."""
        return self.state in IN_FLIGHT

    def can_retry(self) -> bool:
        return self.retries_used < self.max_retries

    def note_retry(self) -> None:
        self.retries_used += 1

    def save(self) -> dict:
        return {
            "state": self.state.value,
            "retries_used": self.retries_used,
            "history": [vars(t) for t in self.history[-50:]],
        }

    @classmethod
    def load(cls, data: dict) -> "StateTracker":
        t = cls()
        try:
            t.state = HandoffState(data.get("state", "READY"))
        except ValueError:
            t.state = HandoffState.READY
        t.retries_used = int(data.get("retries_used", 0))
        for h in data.get("history", []) or []:
            try:
                t.history.append(Transition(**h))
            except TypeError:
                continue
        return t
