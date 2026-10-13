"""Autonomous development loop for Muse Bridge (headless engine).

The GUI drives this; the logic lives here so it can be unit-tested.

One cycle:
  1. Pick next backlog task (never while another is active).
  2. Ask the planner (ChatGPT API or manual relay) for acceptance criteria.
  3. Hand the task to the Muse link (paste into the Muse window).
  4. Wait for completion (API-confirmed, or uncertain heuristic + user confirm).
  5. Verify against the repo: git diff exists, tests/build pass, diff is clean.
  6. Ask the reviewer to judge the evidence (or record manual review).
  7. Commit + checkpoint on success; mark failed/blocked otherwise.
  8. Update the queue and continue.

A task is NEVER marked complete because a model said so. Evidence wins.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .state import ResponseState, StateTracker
from .store import Checkpoints, EventLog, Settings, Task, TaskQueue, Brief
from . import git_ops


@dataclass
class CycleResult:
    task_id: str
    success: bool
    detail: str
    commit: str = ""


ShouldStop = Callable[[], bool]


class Runner:
    def __init__(self, settings: Settings, log: EventLog,
                 queue: TaskQueue, checkpoints: Checkpoints,
                 brief: Brief) -> None:
        self.settings = settings
        self.log = log
        self.queue = queue
        self.checkpoints = checkpoints
        self.brief = brief
        self.tracker = StateTracker(
            response_timeout_s=float(settings.get("response_timeout_s", 600)),
            max_retries=int(settings.get("max_retries", 3)),
        )
        self.tracker.on_change = lambda tr: log.append(
            "state", f"{tr.from_state} -> {tr.to_state}: {tr.reason}")
        self.cycles_ok = 0
        self.cycles_failed = 0

    # -- hooks the GUI / integrations fill in -----------------------------
    # plan_task(task) -> (acceptance_criteria, planner_notes)
    plan_task: Optional[Callable[[Task], tuple[str, str]]] = None
    # send_to_muse(task_text) -> True if the text reached the Muse window
    send_to_muse: Optional[Callable[[str], bool]] = None
    # wait_muse_done() -> "complete" | "timeout" | "stopped"
    wait_muse_done: Optional[Callable[[], str]] = None
    # review(evidence_text) -> (approved: bool, notes: str)
    review: Optional[Callable[[str], tuple[bool, str]]] = None
    # verify_checks(repo) -> list of argv lists, e.g. [["npm","run","build"]]
    verify_checks: Optional[Callable[[Path], list[list[str]]]] = None

    # -- core ---------------------------------------------------------------

    def repo(self) -> Path | None:
        p = self.settings.get("game_repo_path", "")
        if p and git_ops.is_git_repo(p) and self.settings.get("game_repo_verified"):
            return Path(p)
        return None

    def run_one_cycle(self, should_stop: ShouldStop = lambda: False) -> CycleResult | None:
        if self.tracker.busy():
            self.log.append("runner", "Refusing to start: a task is already in flight.")
            return None
        task = self.queue.active() or self.queue.next_backlog()
        if task is None:
            self.log.append("runner", "Backlog empty; nothing to do.")
            return None
        repo = self.repo()
        if repo is None:
            self.log.append("runner", "No verified game repo configured.", level="error")
            return CycleResult(task.id, False, "No verified game repo.")

        task.status = "active"
        task.attempts += 1
        self.queue.save()
        self.log.append("cycle", f"Starting task {task.id}: {task.title}")
        if not self.tracker.transition(ResponseState.TASK_SENT, task.id):
            return CycleResult(task.id, False, "State machine refused TASK_SENT.")

        try:
            return self._execute(task, repo, should_stop)
        except Exception as e:  # never let one task kill the loop
            self.log.append("error", f"Cycle crashed: {e}")
            self.tracker.transition(ResponseState.ERROR, str(e)[:200])
            self.queue.set_status(task.id, "failed", f"Runner exception: {e}")
            self.cycles_failed += 1
            self.tracker.transition(ResponseState.READY, "recovered")
            return CycleResult(task.id, False, f"Exception: {e}")

    def _execute(self, task: Task, repo: Path, should_stop: ShouldStop) -> CycleResult:
        # 1. Plan / acceptance criteria.
        self.tracker.transition(ResponseState.WAITING_FOR_RESPONSE, "planning")
        acceptance, plan_notes = self._plan(task)
        task.acceptance = acceptance
        self.queue.save()
        self.log.append("plan", f"Acceptance for {task.id}: {acceptance[:200]}")
        self.tracker.transition(ResponseState.RESPONSE_COMPLETE, "plan ready")
        self.tracker.transition(ResponseState.REVIEWING, "plan review")
        # Planning is advisory; the human brief + repo evidence decide.

        # 2. Send to Muse.
        self.tracker.transition(ResponseState.SENDING_TO_MUSE, task.id)
        if not self.send_to_muse:
            raise RuntimeError("Muse link not configured.")
        prompt = self._build_prompt(task)
        if not self.send_to_muse(prompt):
            raise RuntimeError("Failed to deliver prompt to Muse window.")
        self.tracker.transition(ResponseState.MUSE_WORKING, "prompt delivered")

        # 3. Wait for completion. NEVER proceed on a guess.
        outcome = self.wait_muse_done() if self.wait_muse_done else "timeout"
        if outcome == "stopped" or should_stop():
            self.tracker.transition(ResponseState.STOPPED, "user stop")
            self.queue.set_status(task.id, "backlog", "Stopped by user; will retry.")
            self.tracker.transition(ResponseState.READY, "stopped")
            return CycleResult(task.id, False, "Stopped by user.")
        if outcome != "complete":
            return self._fail(task, f"Muse completion not confirmed ({outcome}).")

        # 4. Verify against the repo — evidence, not claims.
        self.tracker.transition(ResponseState.VERIFYING, "checking evidence")
        checks = self.verify_checks(repo) if self.verify_checks else []
        verification = git_ops.verify_task(repo, checks)
        changed = git_ops.diff_names(repo)
        warnings = git_ops.scan_diff(repo)
        evidence = (
            f"Changed files: {changed}\n"
            f"Checks: {verification.notes}\n"
            f"Diff warnings: {warnings or 'none'}\n"
            f"Acceptance: {task.acceptance}\n"
        )
        task.evidence = {"changed": changed, "checks": verification.notes,
                         "warnings": warnings}
        self.queue.save()

        if not changed:
            return self._fail(task, "No repo changes detected; nothing to verify.")

        # 5. Review the evidence.
        approved, review_notes = self._review(evidence)
        self.log.append("review", f"{task.id}: approved={approved} {review_notes[:200]}")
        if not approved:
            if self.settings.get("pause_after_failure", True):
                self.tracker.transition(ResponseState.PAUSED, "review rejected")
            return self._fail(task, f"Review rejected: {review_notes[:200]}")
        if warnings and self.settings.get("require_approval_for_risky", True):
            self.log.append("runner", f"Risky diff warnings; leaving for user approval: {warnings}")
            self.tracker.transition(ResponseState.PAUSED, "risky diff")
            self.queue.set_status(task.id, "blocked", f"Diff warnings need approval: {warnings}")
            self.tracker.transition(ResponseState.READY, "paused for approval")
            return CycleResult(task.id, False, "Paused: diff warnings need approval.")

        # 6. Commit + checkpoint.
        self.tracker.transition(ResponseState.CHECKPOINTING, "committing")
        msg = f"{task.title}\n\nMuse Bridge task {task.id}. Acceptance: {task.acceptance[:150]}"
        cr = git_ops.commit(repo, msg)
        commit = git_ops.head_commit(repo) if cr.ok else ""
        if cr.ok:
            self.checkpoints.record(commit, task.id, task.title)
            self.queue.set_status(task.id, "completed", f"Committed {commit}")
            self.cycles_ok += 1
            self.log.append("cycle", f"Task {task.id} complete at {commit}.")
        else:
            return self._fail(task, f"Commit failed: {cr.stderr[:200]}")
        self.tracker.transition(ResponseState.READY, "cycle complete")
        return CycleResult(task.id, True, "Committed.", commit)

    # -- helpers --------------------------------------------------------------

    def _plan(self, task: Task) -> tuple[str, str]:
        if self.plan_task:
            return self.plan_task(task)
        return (task.acceptance or "Repo tests and build pass; diff is clean.",
                "default planner")

    def _review(self, evidence: str) -> tuple[bool, str]:
        if self.review:
            return self.review(evidence)
        # No reviewer configured: a human must approve in the UI.
        return False, "No reviewer configured; manual approval required."

    def _build_prompt(self, task: Task) -> str:
        brief = self.brief.load()
        return (
            f"Project brief:\n{brief.get('text', '')[:4000]}\n\n"
            f"Task {task.id}: {task.title}\n{task.brief}\n\n"
            f"Acceptance criteria:\n{task.acceptance}\n\n"
            "Rules: preserve existing architecture; verify with tests and a "
            "production build; report the exact files changed and test results. "
            "Do not claim completion without evidence."
        )

    def _fail(self, task: Task, reason: str) -> CycleResult:
        self.log.append("cycle", f"Task {task.id} failed: {reason}")
        self.cycles_failed += 1
        self.tracker.transition(ResponseState.ERROR, reason[:200])
        if self.tracker.can_retry():
            self.tracker.note_retry()
            self.queue.set_status(task.id, "backlog", f"Retry ({reason[:150]})")
        else:
            self.queue.set_status(task.id, "failed", reason[:300])
        self.tracker.transition(ResponseState.READY, "ready after failure")
        return CycleResult(task.id, False, reason)

    def handoff_summary(self) -> str:
        """Compact summary for restarting after a crash or context limit."""
        cp = self.checkpoints.latest()
        counts = self.queue.counts()
        active = self.queue.active()
        return (
            f"State: {self.tracker.state.value}\n"
            f"Tasks: {counts}\n"
            f"Active: {active.id + ' ' + active.title if active else 'none'}\n"
            f"Last checkpoint: {cp['commit'] if cp else 'none'} "
            f"({cp['note'] if cp else ''})\n"
            f"Cycles ok/failed: {self.cycles_ok}/{self.cycles_failed}\n"
        )
