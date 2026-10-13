"""Dependency-aware parallel scheduler for Muse Bridge.

Assigns backlog tasks to verified, idle workers:

  1. Only tasks whose dependencies are completed are eligible.
  2. Only verified (session-bound + probe-confirmed) workers are eligible.
  3. A task's expected files must not conflict with in-flight claims.
  4. At most `concurrency_limit()` tasks run at once — the honest count of
     verified sessions, never the number of role definitions.

With fewer sessions than tasks, the rest stay queued. The scheduler never
pretends queued work is running.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ownership import OwnershipRegistry
from .store import Task, TaskQueue
from .workers import WorkerManager, WorkerState


@dataclass
class Assignment:
    task_id: str
    worker_id: str
    blocked_reason: str = ""


class Scheduler:
    def __init__(self, queue: TaskQueue, workers: WorkerManager,
                 ownership: OwnershipRegistry) -> None:
        self.queue = queue
        self.workers = workers
        self.ownership = ownership

    def eligible_tasks(self) -> list[Task]:
        """Backlog tasks with met deps, sorted by priority then age."""
        out = [t for t in self.queue.tasks
               if t.status == "backlog" and t.deps_met(self.queue)]
        out.sort(key=lambda t: (t.priority, t.created_at))
        return out

    def idle_verified_workers(self) -> list:
        return [s for s in self.workers.slots if s.available()]

    def plan(self) -> tuple[list[Assignment], list[dict]]:
        """Compute assignments without applying them (dry run).

        Returns (assignments, blocked) where blocked explains each task
        that could not be scheduled and why. No state is changed.
        """
        assignments: list[Assignment] = []
        blocked: list[dict] = []
        workers = self.idle_verified_workers()
        limit = self.workers.concurrency_limit()
        if limit == 0:
            return [], [{"task": t.id, "reason": "no verified sessions"}
                        for t in self.eligible_tasks()]
        # Track files claimed within this planning pass so two tasks in
        # the same pass cannot take the same files.
        reserved: dict[str, str] = {}
        used = 0
        for task in self.eligible_tasks():
            if used >= limit or used >= len(workers):
                blocked.append({"task": task.id,
                                "reason": "concurrency limit reached; queued"})
                continue
            ok, conflicts = self.ownership.check(task.id, task.expected_files)
            local_conflicts = [f for f in task.expected_files
                               if f in reserved and reserved[f] != task.id]
            if not ok or local_conflicts:
                reasons = list(conflicts) + [
                    f"{f} reserved by {reserved[f]} (this pass)"
                    for f in local_conflicts]
                blocked.append({"task": task.id,
                                "reason": "file conflict: " + "; ".join(reasons)})
                continue
            w = workers[used]
            assignments.append(Assignment(task.id, w.id))
            for f in task.expected_files:
                reserved[f] = task.id
            used += 1
        return assignments, blocked

    def apply(self, assignments: list[Assignment]) -> None:
        for a in assignments:
            task = self.queue.get(a.task_id)
            slot = self.workers.get(a.worker_id)
            if not task or not slot:
                continue
            # Claim files for real at assignment time.
            ok, conflicts = self.ownership.claim(task.id, slot.id,
                                                 task.expected_files)
            if not ok:
                continue  # lost the race; stays queued
            task.owner = slot.id
            task.status = "active"
            self.queue.save()
            slot.task_id = task.id
            slot.expected_files = list(task.expected_files)
            slot.state = WorkerState.ASSIGNED.value
            self.workers.save()

    def run(self) -> dict:
        """Plan + apply. Returns a report of what happened."""
        assignments, blocked = self.plan()
        self.apply(assignments)
        return {
            "assigned": [(a.task_id, a.worker_id) for a in assignments],
            "blocked": blocked,
            "verified_sessions": self.workers.verified_count(),
            "concurrency_limit": self.workers.concurrency_limit(),
        }
