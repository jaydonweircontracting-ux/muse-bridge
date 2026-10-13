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
from .accounts import AccountManager
from .handoff import build_dev_prompt, extract_tagged, new_id
from .integrate import IntegrationGate
from .ledger import (ACKNOWLEDGED, COPIED, DELIVERED, DETECTED, FAILED,
                     NEEDS_REVIEW, QUEUED, RESPONSE_RECEIVED, SUBMITTED,
                     MessageLedger)
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
        self.queue = TaskQueue(d, max_size=self.settings.get("max_queue_size", 50))
        self.checkpoints = Checkpoints(d)
        self.brief = Brief(d)
        self.profile = ProjectProfile(d)
        self.workers = WorkerManager(d)
        self.ownership = OwnershipRegistry(d)
        self.accounts = AccountManager(d)
        self.ledger = MessageLedger(d)
        self.scheduler = Scheduler(self.queue, self.workers, self.ownership)
        self._worktrees: WorktreeManager | None = None

        # GUI-supplied: focus_window(title)->bool, copy_text(str)->None,
        # window_exists(title)->bool (destination verification before send)
        self.focus_window: Callable[[str], bool] | None = None
        self.copy_text: Callable[[str], None] | None = None
        self.window_exists: Callable[[str], bool] | None = None

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

    def _verify_destination(self, slot, kind: str = "Muse") -> tuple[bool, str]:
        """Pre-send destination verification. Identity is checked by HWND
        handle first (reliable), falling back to exact title re-search.
        A partial title match is ambiguous and does not count. Never send
        when the destination can't be confirmed."""
        is_muse = kind == "Muse"
        title = slot.window_title if is_muse else slot.chatgpt_window
        handle = slot.window_handle if is_muse else slot.chatgpt_handle
        if not title:
            return False, (f"{slot.label} has no {kind} window mapped. "
                           "Map it first — the message was NOT sent.")
        try:
            from . import muse_link
            if handle and muse_link.is_handle_alive(handle):
                return True, ""
        except Exception:
            pass
        check = self.window_exists
        if check is None:
            return True, ""  # headless/test mode: nothing to verify against
        if not check(title):
            return False, (
                f"{slot.label}'s {kind} window '{title[:60]}' was not found. "
                "The message was NOT sent — the window may be closed or "
                "renamed. Re-bind it and try again.")
        # Title re-found but handle died: refresh the handle for next time.
        try:
            from . import muse_link as ml
            new_handle = ml.find_handle(title) or 0
            if is_muse:
                slot.window_handle = new_handle
            else:
                slot.chatgpt_handle = new_handle
            self.workers.save()
        except Exception:
            pass
        return True, ""

    def _latest_handoff(self, worker_id: str):
        msgs = [m for m in self.ledger.by_worker(worker_id)
                if m.kind == "handoff"]
        msgs.sort(key=lambda m: m.created_at, reverse=True)
        return msgs[0] if msgs else None

    def prepare_worker_handoff(self, worker_id: str) -> OpResult:
        slot = self.workers.get(worker_id)
        if not slot or not slot.task_id:
            return OpResult(False, "Worker has no assigned task.")
        if WorkerState(slot.state) != WorkerState.ASSIGNED:
            return OpResult(False, f"Worker is {slot.state}, not ASSIGNED.")
        ok, reason = self._verify_destination(slot, "Muse")
        if not ok:
            return OpResult(False, reason)
        prompt = self.worker_prompt(worker_id)
        max_chars = self.settings.get("max_prompt_chars", 8000)
        if len(prompt) > max_chars:
            return OpResult(
                False,
                f"Handoff prompt is {len(prompt)} chars (cap {max_chars}). "
                "Split the task into smaller pieces — oversized prompts "
                "get truncated or rejected by chat windows.")
        msg = self.ledger.create(worker_id, slot.task_id, "handoff", prompt)
        self.ledger.transition(msg.id, QUEUED, f"assigned to {slot.label}")
        if self.copy_text:
            self.copy_text(prompt)
        if self.focus_window and slot.window_title:
            self.focus_window(slot.window_title)
        self.ledger.transition(msg.id, COPIED,
                              "prompt copied; worker window focused")
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
        if not slot:
            return OpResult(False, "Worker not found.")
        state = WorkerState(slot.state)
        msg = self._latest_handoff(worker_id)
        if (state == WorkerState.WAITING_FOR_RESPONSE and msg is not None
                and msg.state == SUBMITTED):
            # Duplicate confirm (e.g. double-click) — already recorded,
            # harmless no-op rather than an error.
            return OpResult(True,
                            f"{slot.label}: submission already recorded.")
        if state != WorkerState.WORKING:
            return OpResult(False, "Worker is not in WORKING state.")
        if msg is not None:
            self.ledger.transition(msg.id, SUBMITTED, "user confirmed paste")
        self.workers.set_state(worker_id, WorkerState.WAITING_FOR_RESPONSE,
                               "session working (user-confirmed submit)")
        self.workers.touch(worker_id)
        return OpResult(True, f"{slot.label}: submitted. Confirm completion only when it is real.")

    def worker_done(self, worker_id: str) -> OpResult:
        """User confirms the session actually finished. Never auto-detected.
        The ledger message stays in flight until the report is transferred."""
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
        msg = self._latest_handoff(worker_id)
        if msg is not None:
            if msg.state == SUBMITTED:
                self.ledger.transition(msg.id, RESPONSE_RECEIVED,
                                       "report transferred by user")
            if msg.state == RESPONSE_RECEIVED:
                self.ledger.transition(msg.id, DELIVERED,
                                       "report attached to task")
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
            # The handoff round-trip is complete: evidence merged.
            msg = self._latest_handoff(worker_id)
            if msg is not None and msg.state == DELIVERED:
                self.ledger.transition(msg.id, ACKNOWLEDGED,
                                       f"integrated into {gate.target_branch}")
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

    # -- review packets (Phase D) --------------------------------------------------
    def prepare_review_packet(self, worker_id: str) -> tuple[bool, str]:
        """Assemble an evidence review packet for ChatGPT. Copies to clipboard."""
        from .github import GitHubConnection
        from .review_packet import build_review_packet
        slot = self.workers.get(worker_id)
        if not slot or not slot.task_id:
            return False, "Worker has no task."
        task = self.queue.get(slot.task_id)
        repo = self.repo()
        if not task or repo is None:
            return False, "Task or repo unavailable."
        conn = GitHubConnection(repo)
        diff = conn.compare_branch(slot.branch or task.branch or "HEAD")
        changed = diff["files"] or list(task.expected_files)
        packet = build_review_packet(
            task, slot.label, slot.branch or task.branch,
            changed, diff["stat"], diff["patch"],
            task.verification,
            limitations=(task.evidence.get("muse_report", "")[:500]
                         if isinstance(task.evidence, dict) else ""))
        if self.copy_text:
            self.copy_text(packet)
        msg = self.ledger.create(worker_id, task.id, "review_packet", packet)
        self.ledger.transition(msg.id, QUEUED, f"packet for {slot.label}")
        if slot.chatgpt_window:
            ok, reason = self._verify_destination(slot, "ChatGPT")
            if not ok:
                self.ledger.transition(msg.id, NEEDS_REVIEW, reason)
                return False, reason
        self.ledger.transition(msg.id, COPIED,
                               "packet copied; paste into the mapped "
                               "ChatGPT conversation")
        self.log.append("orchestrator",
                        f"Review packet prepared for {task.id} "
                        f"({len(changed)} files).")
        return True, packet

    def attach_chatgpt_review(self, task_id: str, text: str) -> tuple[bool, str]:
        """Attach a transferred ChatGPT review to a task. Advisory only."""
        if not text.strip():
            return False, "Empty review — nothing saved."
        task = self.queue.get(task_id)
        if not task:
            return False, "Unknown task."
        task.evidence["chatgpt_review"] = text[:4000]
        self.queue.save()
        self.log.append("orchestrator",
                        f"ChatGPT review attached to {task_id}.")
        return True, f"Review attached to {task_id} (advisory; tests rule)."

    # -- dashboard ---------------------------------------------------------------
    def dashboard(self) -> dict:
        dash = {
            "verified_sessions": self.workers.verified_count(),
            "concurrency_limit": self.workers.concurrency_limit(),
            "workers": self.workers.status_rows(),
            "queue": self.queue.counts(),
            "ownership": self.ownership.summary(),
            "accounts": self.accounts.summary(),
        }
        # Attach account labels to worker rows for the dashboard.
        for row in dash["workers"]:
            acc = self.accounts.account_for_slot(row["id"])
            row["account_label"] = acc.label if acc else ""
            row["account_status"] = acc.status if acc else ""
        return dash

    def bind_account_to_slot(self, account_id: str,
                             worker_id: str) -> tuple[bool, str]:
        """Link an account to a worker slot (both directions)."""
        ok, msg = self.accounts.bind_worker(account_id, worker_id)
        if not ok:
            return False, msg
        self.workers.link_account(worker_id, account_id)
        # Also carry the window title if the slot already has one bound;
        # otherwise the user binds the window on the Workers tab.
        self.log.append("accounts",
                        f"Account {account_id} linked to slot {worker_id}.")
        return True, msg

    def unbind_account_from_slot(self, account_id: str) -> tuple[bool, str]:
        acc = self.accounts.get(account_id)
        if not acc:
            return False, "Unknown account."
        if acc.worker_slot:
            self.workers.unlink_account(acc.worker_slot)
        self.accounts.unbind_worker(account_id)
        self.log.append("accounts",
                        f"Account {account_id} unlinked from its slot.")
        return True, "Unlinked."

    def safe_shutdown(self) -> None:
        """Persist everything; workers keep their branches/work."""
        self.workers.save()
        self.queue.save()
        self.ownership._save()
        self.log.append("orchestrator", "Safe shutdown: all state persisted.")
