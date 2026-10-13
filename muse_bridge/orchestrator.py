"""Multi-worker orchestrator for Muse Bridge (headless engine).

Owns: worker slots, scheduler, workspaces, ownership, integration gate.
The GUI calls this; the logic lives here so it can be unit-tested.

Honesty rules enforced here:
  - A worker is only assigned when its session is verified (never simulated).
  - Concurrency never exceeds the verified-session count.
  - A task only PASSES on repository evidence + green gate.
  - One account disconnecting never touches the other slots.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import git_ops
from .handoff import build_dev_prompt, extract_tagged, new_id
from .integrate import IntegrationGate
from .ownership import OwnershipRegistry
from .profile import ProjectProfile
from .scheduler import Scheduler
from .state import HandoffState  # noqa: F401  (re-export for GUI)
from .store import Brief, Checkpoints, EventLog, Settings, Task, TaskQueue
from .workers import WorkerManager, WorkerState
from .workspaces import Workspace, WorktreeManager


@dataclass
class OpResult:
    ok: bool
    detail: str


class Orchestrator:
    def __init__(self, directory: Path | None = None) -> None:
        from .store import data_dir
        d = directory or data_dir()
        self.settings = Settings(d)
        self.log = EventLog(d)
        self.queue = TaskQueue(d)
        self.checkpoints = Checkpoints(d)
        self.brief = Brief(d)
        self.profile = ProjectProfile(d)
        self.workers = WorkerManager(d)
        self.ownership = OwnershipRegistry(d)
        self.scheduler = Scheduler(self.queue, self.workers, self.ownership)
        self._worktrees: WorktreeManager | None = None

        # GUI-supplied: focus_window(title)->bool, copy_text(str)->None
        self.focus_window: Callable[[str], bool] | None = None
        self.copy_text: Callable[[str], None] | None = None

    # -- repo / worktrees --------------------------------------------------
    def repo(self) -> Path | None:
        p = self.settings.get("game_repo_path", "")
        if p and git_ops.is_git_repo(p) and self.settings.get("game_repo_verified"):
            return Path(p)
        return None

    def worktrees(self) -> WorktreeManager | None:
        repo = self.repo()
        if repo is None:
            return None
        if self._worktrees is None:
            from .store import data_dir
            self._worktrees = WorktreeManager(repo, data_dir())
        return self._worktrees

    # -- scheduling ----------------------------------------------------------
    def schedule(self) -> dict:
        """Assign eligible tasks to verified idle workers + create workspaces."""
        report = self.scheduler.run()
        wt = self.worktrees()
        for task_id, worker_id in report["assigned"]:
            task = self.queue.get(task_id)
            slot = self.workers.get(worker_id)
            if not task or not slot or wt is None:
                continue
            ws = wt.create(worker_id, task_id)
            task.workspace = ws.path
            task.branch = ws.branch
            slot.workspace = ws.path
            slot.branch = ws.branch
            if not ws.worktree:
                slot.note = "Branch-only fallback: no worktree isolation on this machine."
            self.queue.save()
            self.workers.save()
            self.log.append("orchestrator",
                            f"{task_id} -> {slot.label} "
                            f"({'worktree' if ws.worktree else 'branch'} {ws.branch})")
        return report

    # -- worker handoff --------------------------------------------------------
    def worker_prompt(self, worker_id: str) -> str:
        slot = self.workers.get(worker_id)
        task = self.queue.get(slot.task_id) if slot else None
        if not slot or not task:
            return ""
        briefing = self.profile.worker_briefing()
        ws_note = (f"Your isolated workspace: {slot.workspace or '(branch ' + slot.branch + ')'} "
                   f"on branch {slot.branch}. Work ONLY there.")
        files_note = ("Files you may change: " + ", ".join(task.expected_files)
                      if task.expected_files else
                      "No file scope declared — keep the change minimal.")
        prompt = build_dev_prompt(task.title, task.brief, task.acceptance,
                                  self.brief.load().get("text", ""))
        return (f"{briefing}\n\n{ws_note}\n{files_note}\n\n"
                f"---\n{prompt}\n\n"
                f"When finished, commit your work in your workspace with "
                f"message 'bridge/{task.id}' and reply with your completion "
                f"report between <REPORT> and </REPORT> tags.")

    def prepare_worker_handoff(self, worker_id: str) -> OpResult:
        slot = self.workers.get(worker_id)
        if not slot or not slot.task_id:
            return OpResult(False, "Worker has no assigned task.")
        if WorkerState(slot.state) != WorkerState.ASSIGNED:
            return OpResult(False, f"Worker is {slot.state}, not ASSIGNED.")
        prompt = self.worker_prompt(worker_id)
        if self.copy_text:
            self.copy_text(prompt)
        if self.focus_window and slot.window_title:
            self.focus_window(slot.window_title)
        self.workers.set_state(worker_id, WorkerState.WORKING,
                               "task handed to session")
        self.workers.touch(worker_id)
        self.log.append("orchestrator",
                        f"Handoff prepared for {slot.label} ({slot.task_id}).")
        return OpResult(True,
                        f"Task copied for {slot.label}. Paste into its Muse "
                        f"window, submit, and confirm when done.")

    def worker_submitted(self, worker_id: str) -> OpResult:
        slot = self.workers.get(worker_id)
        if not slot or WorkerState(slot.state) != WorkerState.WORKING:
            return OpResult(False, "Worker is not in WORKING state.")
        self.workers.set_state(worker_id, WorkerState.WAITING_FOR_RESPONSE,
                               "session working (user-confirmed submit)")
        self.workers.touch(worker_id)
        return OpResult(True, f"{slot.label}: submitted. Confirm completion only when it is real.")

    def worker_done(self, worker_id: str) -> OpResult:
        """User confirms the session actually finished. Never auto-detected."""
        slot = self.workers.get(worker_id)
        if not slot or WorkerState(slot.state) not in (
                WorkerState.WORKING, WorkerState.WAITING_FOR_RESPONSE):
            return OpResult(False, "Worker is not working on a task.")
        self.workers.set_state(worker_id, WorkerState.WAITING_FOR_USER,
                               "finished (user-confirmed); report not yet transferred")
        self.workers.touch(worker_id)
        return OpResult(True,
                        f"{slot.label}: paste its completion report into the bridge.")

    def submit_worker_report(self, worker_id: str, text: str) -> OpResult:
        slot = self.workers.get(worker_id)
        if not slot or not slot.task_id:
            return OpResult(False, "Worker has no task.")
        if not text.strip():
            return OpResult(False, "Empty report — nothing saved.")
        task = self.queue.get(slot.task_id)
        if task:
            task.evidence["muse_report"] = (extract_tagged(text, "REPORT")
                                            or text.strip())[:4000]
            self.queue.save()
        self.workers.set_state(worker_id, WorkerState.REVIEW,
                               "report transferred; ready for integration gate")
        self.workers.touch(worker_id)
        self.log.append("orchestrator", f"Report saved for {slot.task_id}.")
        return OpResult(True, "Report saved. Run the integration gate when ready.")

    # -- integration -------------------------------------------------------------
    def integrate_worker(self, worker_id: str) -> OpResult:
        slot = self.workers.get(worker_id)
        if not slot or not slot.task_id:
            return OpResult(False, "Worker has no task.")
        task = self.queue.get(slot.task_id)
        wt = self.worktrees()
        if not task or wt is None:
            return OpResult(False, "No repo/worktree manager available.")
        ws = Workspace(worker_id, slot.workspace, slot.branch, bool(slot.workspace),
                       time.time())
        # Commit whatever is in the workspace first (worker's changes).
        wt.commit_all(ws, f"bridge/{task.id}: {task.title}")
        changed = wt.changed_files(ws)
        self.workers.set_state(worker_id, WorkerState.INTEGRATING, "gate running")
        gate = IntegrationGate(wt.repo, self.ownership)
        result = gate.run(task, ws, changed)
        slot.last_test_result = ("PASSED" if result.passed else "FAILED")
        for step in result.steps:
            self.log.append("gate", f"{task.id}: {step}")
        if result.passed:
            self.ownership.release_task(task.id)
            self.queue.set_status(task.id, "completed",
                                  f"Integrated into {gate.target_branch}")
            self.checkpoints.record(git_ops.head_commit(wt.repo), task.id, task.title)
            self.profile.record_outcome(task.id, task.title, True,
                                        f"merged into {gate.target_branch}")
            wt.remove(ws)
            self.workers.release(worker_id, f"Integrated {task.id}.")
            self.workers.set_state(worker_id, WorkerState.PASSED, task.id)
            # PASSED is informational; return slot to READY for next work.
            self.workers.set_state(worker_id, WorkerState.READY, "")
            self.log.append("orchestrator", f"{task.id} PASSED and integrated.")
            return OpResult(True, f"{task.id} integrated into {gate.target_branch}.")
        # Failed: keep worker branch + work for forensics/retry.
        task.status = "backlog"
        task.notes = f"Gate failed: {result.steps[-1][:200] if result.steps else ''}"
        task.owner = ""
        self.queue.save()
        self.ownership.release_task(task.id)
        self.workers.release(worker_id, "Gate failed; task re-queued.")
        self.workers.set_state(worker_id, WorkerState.FAILED, task.id)
        self.profile.record_outcome(task.id, task.title, False, "gate failed")
        return OpResult(False,
                        f"Gate FAILED for {task.id}: "
                        f"{result.steps[-1][:200] if result.steps else 'see log'}. "
                        f"Worker branch {slot.branch} preserved.")

    # -- dashboard ---------------------------------------------------------------
    def dashboard(self) -> dict:
        return {
            "verified_sessions": self.workers.verified_count(),
            "concurrency_limit": self.workers.concurrency_limit(),
            "workers": self.workers.status_rows(),
            "queue": self.queue.counts(),
            "ownership": self.ownership.summary(),
        }

    def safe_shutdown(self) -> None:
        """Persist everything; workers keep their branches/work."""
        self.workers.save()
        self.queue.save()
        self.ownership._save()
        self.log.append("orchestrator", "Safe shutdown: all state persisted.")
