"""Worker slots for Muse Bridge orchestration.

A worker slot is a CONFIGURED POSITION, not a simulated agent. A slot only
becomes READY after a real session is bound AND verified by the user:

  1. The user selects the Muse window for that slot (Detect + Test focus).
  2. The user runs the bridge's probe prompt in that session and pastes the
     reply back. The reply must contain the slot's label.

Only then is the slot counted toward the concurrency limit. The dashboard
shows the verified count (e.g. "2/5 sessions verified") and the scheduler
queues the rest. Nothing here simulates a running worker.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any


class WorkerState(str, Enum):
    OFFLINE = "OFFLINE"            # no session bound
    READY = "READY"                # verified session, idle
    ASSIGNED = "ASSIGNED"          # task assigned, not yet handed off
    WORKING = "WORKING"            # task handed to the session
    WAITING_FOR_RESPONSE = "WAITING_FOR_RESPONSE"  # session thinking (user-confirmed activity)
    WAITING_FOR_USER = "WAITING_FOR_USER"  # blocked on manual transfer/confirm
    REVIEW = "REVIEW"              # report in, awaiting ChatGPT review
    INTEGRATING = "INTEGRATING"    # integration gate running
    PASSED = "PASSED"              # merged, verified
    FAILED = "FAILED"              # failed, needs attention
    BLOCKED = "BLOCKED"            # conflict / dependency / needs user decision


# States in which the slot counts as "doing something" — it must not be
# given a second task.
BUSY_STATES = {
    WorkerState.ASSIGNED, WorkerState.WORKING,
    WorkerState.WAITING_FOR_RESPONSE, WorkerState.WAITING_FOR_USER,
    WorkerState.REVIEW, WorkerState.INTEGRATING,
}

DEFAULT_ROLES = [
    "World & settlements",
    "NPCs & simulation",
    "Gameplay & quests",
    "Visuals & usability",
    "QA & integration",
]

MAX_WORKERS = 5


@dataclass
class WorkerSlot:
    id: str                      # "w1".."w5"
    label: str                   # "Muse 1" (user-editable)
    role: str                    # configurable role
    window_title: str = ""       # bound Muse window/session
    chatgpt_window: str = ""     # mapped ChatGPT conversation window
    account_id: str = ""         # linked account (BUILD 2)
    paused: bool = False         # excluded from scheduling while paused
    mode: str = "supervised"     # "manual" (user drives) or "supervised"
                                 # (run loop may schedule, every send confirmed).
                                 # There is no fully-autonomous mode: the bridge
                                 # never submits into ChatGPT/Muse on its own.
    state: str = WorkerState.OFFLINE.value
    session_verified: bool = False
    verified_at: float = 0.0
    task_id: str = ""            # current task
    workspace: str = ""          # worktree path
    branch: str = ""             # worker branch
    expected_files: list[str] = field(default_factory=list)
    last_activity: float = 0.0   # last USER-VERIFIED activity (transfer/confirm)
    last_test_result: str = ""
    errors: int = 0
    retries: int = 0
    note: str = ""

    def busy(self) -> bool:
        return WorkerState(self.state) in BUSY_STATES

    def available(self) -> bool:
        return (self.session_verified
                and not self.paused
                and self.mode == "supervised"
                and WorkerState(self.state) == WorkerState.READY)


class WorkerManager:
    """Owns the five slots; persists to workers.json."""

    def __init__(self, directory: Path | None = None) -> None:
        from .store import data_dir
        self.dir = directory or data_dir()
        self.path = self.dir / "workers.json"
        self.slots: list[WorkerSlot] = self._load()

    def _load(self) -> list[WorkerSlot]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            # Field-tolerant: old files lack newer fields (chatgpt_window,
            # paused) — load with defaults instead of dropping the slot.
            known = {f.name for f in fields(WorkerSlot)}
            slots = []
            for r in raw:
                try:
                    slots.append(WorkerSlot(
                        **{k: v for k, v in r.items() if k in known}))
                except TypeError:
                    continue
            if slots:
                return self._ensure_five(slots)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return [WorkerSlot(id=f"w{i+1}", label=f"Muse {i+1}",
                           role=DEFAULT_ROLES[i])
                for i in range(MAX_WORKERS)]

    def _ensure_five(self, slots: list[WorkerSlot]) -> list[WorkerSlot]:
        have = {s.id for s in slots}
        for i in range(MAX_WORKERS):
            wid = f"w{i+1}"
            if wid not in have:
                slots.append(WorkerSlot(id=wid, label=f"Muse {i+1}",
                                        role=DEFAULT_ROLES[i]))
        return sorted(slots, key=lambda s: s.id)[:MAX_WORKERS]

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(s) for s in self.slots],
                                 indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    def get(self, worker_id: str) -> WorkerSlot | None:
        for s in self.slots:
            if s.id == worker_id:
                return s
        return None

    def verified_count(self) -> int:
        return sum(1 for s in self.slots
                   if s.session_verified and not s.paused)

    def concurrency_limit(self) -> int:
        """Real verified sessions, capped at 5. This is the honest limit."""
        return min(MAX_WORKERS, self.verified_count())

    def set_state(self, worker_id: str, state: WorkerState,
                  note: str = "") -> bool:
        s = self.get(worker_id)
        if not s:
            return False
        s.state = state.value
        if note:
            s.note = note
        if state not in (WorkerState.OFFLINE, WorkerState.READY):
            pass
        self.save()
        return True

    def touch(self, worker_id: str) -> None:
        """Record user-verified activity. Only call on real user actions."""
        s = self.get(worker_id)
        if s:
            s.last_activity = time.time()
            self.save()

    def bind_session(self, worker_id: str, window_title: str) -> bool:
        s = self.get(worker_id)
        if not s:
            return False
        s.window_title = window_title
        s.session_verified = False
        s.verified_at = 0.0
        if WorkerState(s.state) == WorkerState.OFFLINE:
            s.state = WorkerState.READY.value  # bound but unverified != verified
        self.save()
        return True

    def map_chatgpt(self, worker_id: str, window_title: str) -> bool:
        """Map a worker to its dedicated ChatGPT conversation window."""
        s = self.get(worker_id)
        if not s:
            return False
        s.chatgpt_window = window_title
        self.save()
        return True

    def chatgpt_owner(self, window_title: str,
                      exclude: str = "") -> WorkerSlot | None:
        """One-to-one lock: which worker already owns this ChatGPT window?"""
        if not window_title:
            return None
        for s in self.slots:
            if s.id != exclude and s.chatgpt_window == window_title:
                return s
        return None

    def set_paused(self, worker_id: str, paused: bool) -> bool:
        """Pause/resume a single worker. Paused workers are excluded from
        scheduling but keep their state, branch, and task."""
        s = self.get(worker_id)
        if not s:
            return False
        s.paused = paused
        self.save()
        return True

    def set_mode(self, worker_id: str, mode: str) -> bool:
        """Manual: the run loop never auto-schedules this worker (the user
        drives it from the row buttons). Supervised: the run loop may
        schedule it, but every message exchange is still user-confirmed."""
        if mode not in ("manual", "supervised"):
            return False
        s = self.get(worker_id)
        if not s:
            return False
        s.mode = mode
        self.save()
        return True

    def verify_session(self, worker_id: str, probe_reply: str) -> tuple[bool, str]:
        """Verify a session from the user's pasted probe reply.

        The reply must contain the slot label (e.g. "Muse 2"). Anything
        else is rejected — no fake verifications.
        """
        s = self.get(worker_id)
        if not s:
            return False, "Unknown worker."
        if not s.window_title:
            return False, "No window bound for this worker."
        if s.label.lower() in probe_reply.lower():
            s.session_verified = True
            s.verified_at = time.time()
            s.state = WorkerState.READY.value
            s.last_activity = time.time()
            self.save()
            return True, f"{s.label} verified."
        return False, (f"Reply did not contain the label '{s.label}'. "
                       f"Paste the reply from that Muse session.")

    def release(self, worker_id: str, reason: str = "") -> bool:
        s = self.get(worker_id)
        if not s:
            return False
        s.task_id = ""
        s.expected_files = []
        s.state = (WorkerState.READY.value if s.session_verified
                   else WorkerState.OFFLINE.value)
        if reason:
            s.note = reason
        self.save()
        return True

    def disconnect(self, worker_id: str) -> bool:
        """One account drops; the others are untouched."""
        s = self.get(worker_id)
        if not s:
            return False
        s.session_verified = False
        s.verified_at = 0.0
        # Keep the task assignment so it can be re-queued, but mark blocked.
        if s.busy():
            s.state = WorkerState.BLOCKED.value
            s.note = "Session disconnected; task preserved for reassignment."
        else:
            s.state = WorkerState.OFFLINE.value
        self.save()
        return True

    def status_rows(self) -> list[dict[str, Any]]:
        now = time.time()
        rows = []
        for s in self.slots:
            age = now - s.last_activity if s.last_activity else -1
            rows.append({
                "id": s.id, "label": s.label, "role": s.role,
                "account_id": s.account_id,
                "connection": ("verified" if s.session_verified
                               else ("bound" if s.window_title else "offline")),
                "state": s.state, "task": s.task_id,
                "paused": s.paused, "mode": s.mode,
                "chatgpt_window": s.chatgpt_window,
                "workspace": s.workspace, "branch": s.branch,
                "files": list(s.expected_files),
                "waiting_for_user": s.state == WorkerState.WAITING_FOR_USER.value,
                "last_test": s.last_test_result,
                "errors": s.errors, "retries": s.retries,
                "idle_for_s": age, "note": s.note,
            })
        return rows

    def link_account(self, worker_id: str, account_id: str) -> bool:
        s = self.get(worker_id)
        if not s:
            return False
        s.account_id = account_id
        self.save()
        return True

    def unlink_account(self, worker_id: str) -> bool:
        s = self.get(worker_id)
        if not s:
            return False
        s.account_id = ""
        self.save()
        return True
