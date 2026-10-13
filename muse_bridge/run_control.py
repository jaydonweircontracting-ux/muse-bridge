"""Run control for Muse Bridge — the bot-client style START/STOP.

The orchestrator has always been step-driven (schedule -> handoff ->
submitted -> done -> integrate, each confirmed by the user). The run
controller adds a continuous mode on top: while RUNNING, a tick loop keeps
the queue moving — assigning eligible tasks to verified idle workers and
raising each handoff for the user to copy/paste. The user still confirms
every step; the loop just never lets a verified worker sit idle.

STOP is graceful: the current tick finishes, no new assignments start,
and everything is persisted. In-flight handoffs are untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class RunState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"


# Worker states that mean "something is still in flight" — the run loop
# must keep waiting while any worker is in one of these.
_ACTIVE_STATES = frozenset({
    "ASSIGNED", "WORKING", "WAITING_FOR_RESPONSE",
    "WAITING_FOR_USER", "REVIEW", "INTEGRATING",
})


@dataclass
class RunController:
    """Wraps an Orchestrator with start/stop semantics. No threads here —
    the GUI drives tick() on a timer, so all state changes stay on the UI
    thread and tkinter never races."""

    orch: object = None
    state: RunState = RunState.IDLE
    tick_interval_s: float = 5.0
    on_event: Callable[[str], None] | None = None
    _ticks: int = 0
    _last_report: dict = field(default_factory=dict)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> tuple[bool, str]:
        if self.state == RunState.RUNNING:
            return False, "Run already in progress."
        if self.state == RunState.STOPPING:
            return False, "Stop in progress — wait for it to settle."
        verified = self._verified_count()
        if verified == 0:
            return False, (
                "No verified Muse sessions. Bind a window and pass the "
                "probe in Workers first — only verified sessions count."
            )
        self.state = RunState.RUNNING
        self._ticks = 0
        self._emit(f"Run started — {verified} verified worker(s).")
        return True, f"Run started with {verified} verified worker(s)."

    def request_stop(self) -> tuple[bool, str]:
        """Graceful stop: finish the current tick, then stand down."""
        if self.state != RunState.RUNNING:
            return False, "No run in progress."
        self.state = RunState.STOPPING
        self._emit("Stop requested — finishing current tick.")
        return True, "Stopping after the current tick."

    def force_stop(self) -> tuple[bool, str]:
        """Immediate halt. State is persisted; handoffs keep their branches."""
        if self.state == RunState.IDLE:
            return False, "No run in progress."
        self.state = RunState.IDLE
        self._persist()
        self._emit("Run stopped immediately. All state persisted.")
        return True, "Stopped. All state persisted."

    def toggle(self) -> tuple[bool, str]:
        if self.state == RunState.RUNNING:
            return self.request_stop()
        return self.start()

    # -- tick ----------------------------------------------------------------

    def tick(self) -> list[str]:
        """One loop iteration. Returns human-readable events for the GUI."""
        events: list[str] = []
        if self.state == RunState.IDLE:
            return events
        if self.state == RunState.STOPPING:
            self.state = RunState.IDLE
            self._persist()
            msg = "Run stopped gracefully. All state persisted."
            events.append(msg)
            self._emit(msg)
            return events
        # RUNNING
        self._ticks += 1
        try:
            report = self.orch.schedule()  # type: ignore[union-attr]
        except Exception as exc:  # never kill the loop on a scheduler error
            msg = f"Scheduler error (run continues): {exc}"
            events.append(msg)
            self._emit(msg)
            return events
        self._last_report = report or {}
        assigned = (report or {}).get("assigned", [])
        for task_id, worker_id in assigned:
            msg = f"Assigned {task_id} -> worker {worker_id}."
            events.append(msg)
            self._emit(msg)
            # Raise the handoff: copy the prompt, focus the worker's window,
            # move the worker to WORKING. The user still pastes the prompt
            # into the Muse window and confirms each step — the loop just
            # never leaves a newly-assigned worker waiting on a button click.
            try:
                res = self.orch.prepare_worker_handoff(worker_id)  # type: ignore[union-attr]
                hmsg = (f"Handoff ready for worker {worker_id}: {res.detail}"
                        if getattr(res, "ok", False)
                        else f"Handoff FAILED for worker {worker_id}: "
                             f"{getattr(res, 'detail', res)}")
            except Exception as exc:
                hmsg = f"Handoff error for worker {worker_id}: {exc}"
            events.append(hmsg)
            self._emit(hmsg)
        blocked = (report or {}).get("blocked", [])
        for item in blocked:
            msg = f"Blocked: {item}"
            events.append(msg)
            self._emit(msg)
        # Run to completion: nothing assignable and nobody in flight means
        # the queue is drained (or deadlocked on dependencies) — stop.
        drained, dmsg = self._drained()
        if drained:
            self.state = RunState.IDLE
            self._persist()
            events.append(dmsg)
            self._emit(dmsg)
        return events

    def _drained(self) -> tuple[bool, str]:
        """True when the loop has nothing left it can ever do."""
        try:
            slots = self.orch.workers.slots  # type: ignore[union-attr]
        except Exception:
            return False, ""
        if any(getattr(s, "state", "") in _ACTIVE_STATES for s in slots):
            return False, ""
        try:
            eligible = self.orch.scheduler.eligible_tasks()  # type: ignore[union-attr]
        except Exception:
            eligible = []
        if eligible:
            return False, ""
        try:
            remaining = [t for t in self.orch.queue.tasks  # type: ignore[union-attr]
                         if getattr(t, "status", "") in ("backlog", "active")]
        except Exception:
            remaining = []
        if remaining:
            return True, (
                f"Run stopped: {len(remaining)} task(s) remain but none are "
                f"assignable and no workers are active "
                f"(blocked on dependencies or conflicts — needs your call).")
        return True, "Run complete: queue drained, all workers idle."

    # -- helpers ---------------------------------------------------------------

    def _verified_count(self) -> int:
        try:
            workers = self.orch.workers  # type: ignore[union-attr]
            if hasattr(workers, "verified_count"):
                return int(workers.verified_count())
            return sum(1 for s in workers.slots
                       if getattr(s, "session_verified", False))
        except Exception:
            return 0

    def _persist(self) -> None:
        try:
            self.orch.safe_shutdown()  # type: ignore[union-attr]
        except Exception:
            pass

    def _emit(self, msg: str) -> None:
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                pass

    def describe(self) -> str:
        if self.state == RunState.RUNNING:
            return f"RUNNING (tick {self._ticks}, every {self.tick_interval_s:g}s)"
        if self.state == RunState.STOPPING:
            return "STOPPING — finishing current tick"
        return "IDLE"
