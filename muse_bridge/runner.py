"""Guided handoff coordinator for Muse Bridge (free edition, headless).

The GUI drives this; the logic lives here so it can be unit-tested.

The coordinator NEVER talks to ChatGPT or Muse directly. It:
  - prepares the exact prompt for the current stage,
  - tracks the explicit handoff state,
  - runs local verification (tests/build) when asked,
  - records user-transferred responses,
  - decides pass/fail from the transferred review,
  - persists everything after every step so a restart recovers.

A response only exists when the user transfers it. No guessing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import git_ops
from .handoff import (Handoff, build_dev_prompt, build_planning_prompt,
                      build_review_prompt, extract_tagged,
                      looks_like_approval, new_id, parse_task_lines)
from .profile import ProjectProfile
from .state import HandoffState, StateTracker
from .store import Brief, Checkpoints, EventLog, Settings, Task, TaskQueue


@dataclass
class StepResult:
    ok: bool
    detail: str
    handoff: Handoff | None = None


class Coordinator:
    def __init__(self, settings: Settings, log: EventLog,
                 queue: TaskQueue, checkpoints: Checkpoints,
                 brief: Brief, profile: ProjectProfile) -> None:
        self.settings = settings
        self.log = log
        self.queue = queue
        self.checkpoints = checkpoints
        self.brief = brief
        self.profile = profile
        self.tracker = StateTracker(
            max_retries=int(settings.get("max_retries", 3)))
        self.tracker.on_change = lambda tr: log.append(
            "state", f"{tr.from_state} -> {tr.to_state}: {tr.reason}")
        self.current: Handoff | None = None
        self.last_test_output: str = ""
        self.last_diff_summary: str = ""
        # Restore persisted tracker state (crash/restart recovery).
        try:
            saved = settings.get("_tracker", None)
            if isinstance(saved, dict):
                self.tracker = StateTracker.load(saved)
        except Exception:
            pass
        self.tracker.on_change = self._on_change

    def _on_change(self, tr) -> None:
        self.log.append("state",
                        f"{tr.from_state} -> {tr.to_state}: {tr.reason}")
        self._persist()

    # -- persistence -------------------------------------------------------
    def _persist(self) -> None:
        try:
            self.settings.set("_tracker", self.tracker.save())
        except Exception:
            pass

    def describe_status(self) -> str:
        """Plain-language 'what was happening' for restart recovery."""
        s = self.tracker.state
        cur = f" (handoff: {self.current.title})" if self.current else ""
        active = self.queue.active()
        nxt = self.queue.next_backlog()
        return (
            f"State: {s.value}{cur}\n"
            f"Active task: {active.title if active else 'none'}\n"
            f"Next queued: {nxt.title if nxt else 'none'}\n"
            f"Queue: {self.queue.counts()}"
        )

    # -- window targeting (delegated to GUI-supplied callables) ------------
    # focus_chatgpt() / focus_muse() -> bool ; copy_text(str) -> None
    focus_chatgpt: Callable[[], bool] | None = None
    focus_muse: Callable[[], bool] | None = None
    copy_text: Callable[[str], None] | None = None

    def _copy(self, text: str) -> None:
        if self.copy_text:
            self.copy_text(text)

    # -- planning stage ----------------------------------------------------
    def prepare_planning(self) -> StepResult:
        if self.tracker.busy():
            return StepResult(False, "A handoff is already in flight.")
        prof = self.profile.load()
        prompt = build_planning_prompt(
            self.brief.load().get("text", ""),
            prof.get("project_summary", ""),
            self.profile.repo_facts_text())
        h = Handoff(new_id(), "plan", "", "Planning: next task",
                    prompt, "chatgpt",
                    "Copy this prompt, paste it into ChatGPT, submit, then "
                    "paste ChatGPT's completed response back into the bridge.")
        self.current = h
        self.tracker.transition(HandoffState.PREPARED, "planning prompt ready")
        self._copy(prompt)
        self.tracker.transition(HandoffState.WAITING_FOR_USER, "awaiting plan")
        self.log.append("handoff", f"Planning handoff {h.id} prepared.")
        return StepResult(True, "Planning prompt copied. Paste into ChatGPT.", h)

    def submit_planning_response(self, text: str) -> StepResult:
        if not self.current or self.current.stage != "plan":
            return StepResult(False, "No planning handoff awaiting a response.")
        if not text.strip():
            return StepResult(False, "Empty response — nothing saved.")
        tasks = parse_task_lines(text)
        summary = extract_tagged(text, "STATE_SUMMARY")
        if summary:
            self.profile.update_summary(summary)
        added = 0
        for title, brief in tasks:
            self.queue.add(Task(id=new_id(), title=title, brief=brief))
            added += 1
        self.log.append("handoff",
                        f"Plan received: {added} task(s) queued.")
        self.current.response = text
        self.current.done = True
        self.current = None
        self.tracker.transition(HandoffState.READY, f"plan saved ({added} tasks)")
        return StepResult(True, f"Plan saved: {added} task(s) queued.")

    # -- development stage ---------------------------------------------------
    def prepare_dev(self, task: Task | None = None) -> StepResult:
        if self.tracker.busy():
            return StepResult(False, "A handoff is already in flight.")
        task = task or self.queue.active() or self.queue.next_backlog()
        if task is None:
            return StepResult(False, "Queue is empty — run planning first.")
        task.status = "active"
        task.attempts += 1
        self.queue.save()
        prompt = build_dev_prompt(task.title, task.brief, task.acceptance,
                                  self.brief.load().get("text", ""))
        h = Handoff(new_id(), "dev", task.id, f"Develop: {task.title}",
                    prompt, "muse",
                    "Copy this task, paste it into Muse, submit, and let Muse "
                    "finish. Only confirm completion when Muse is actually done.")
        self.current = h
        self.tracker.transition(HandoffState.PREPARED, f"dev prompt for {task.id}")
        self._copy(prompt)
        self.tracker.transition(HandoffState.WAITING_FOR_USER, "user handing to Muse")
        self.log.append("handoff", f"Dev handoff {h.id} prepared for {task.id}.")
        return StepResult(True, "Task copied. Paste into Muse.", h)

    def mark_muse_submitted(self) -> StepResult:
        if not self.tracker.transition(HandoffState.MUSE_RUNNING, "task with Muse"):
            return StepResult(False, "Cannot mark running from here.")
        return StepResult(True, "Muse is working. Confirm completion only when it is actually done.")

    def mark_muse_done(self) -> StepResult:
        """The USER confirms Muse finished — never auto-detected."""
        if not self.tracker.transition(HandoffState.AWAITING_REPORT,
                                        "user confirmed Muse finished"):
            return StepResult(False, "Cannot confirm completion from here.")
        return StepResult(True,
                          "Paste Muse's completion report into the bridge.")

    def submit_muse_report(self, text: str) -> StepResult:
        if not self.current or self.current.stage != "dev":
            return StepResult(False, "No dev handoff awaiting a report.")
        if not text.strip():
            return StepResult(False, "Empty report — nothing saved.")
        report = extract_tagged(text, "REPORT") or text.strip()
        task = self.queue.get(self.current.task_id)
        if task:
            task.evidence = {"muse_report": report[:4000]}
            self.queue.save()
        self.current.response = text
        self.current.done = True
        self.log.append("handoff", f"Muse report saved for {self.current.task_id}.")
        return StepResult(True, "Report saved. Running local verification…")

    # -- local verification (bridge runs tests/build; never edits game) -----
    def verification_checks(self, repo: Path) -> tuple[list[list[str]], list[str]]:
        """Derive checks from the repo's actual package.json.

        Only tools that are actually available are used — never pretend.
        Returns (checks, skipped_reasons).
        """
        import json
        checks: list[list[str]] = []
        skipped: list[str] = []
        npm = git_ops.run_command(repo, ["npm", "--version"], timeout=30)
        if not npm.ok:
            return [], ["npm not available on this machine"]
        scripts: dict = {}
        try:
            pkg = json.loads((repo / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {}) or {}
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        except (OSError, json.JSONDecodeError):
            deps = {}
        if "build" in scripts:
            checks.append(["npm", "run", "build"])
        else:
            skipped.append("no 'build' script in package.json")
        if "test" in scripts:
            checks.append(["npm", "test", "--", "--watchAll=false"])
        else:
            skipped.append("no 'test' script in package.json")
        if "typescript" in deps or (repo / "tsconfig.json").exists():
            checks.append(["npx", "--no-install", "tsc", "--noEmit"])
        else:
            skipped.append("typescript not a dependency — tsc skipped")
        return checks, skipped

    def run_verification(self) -> StepResult:
        repo = self._repo()
        if repo is None:
            return StepResult(False, "No verified game repo configured.")
        checks, skipped = self.verification_checks(repo)
        out_lines = [f"skipped: {s}" for s in skipped]
        ok = True
        for cmd in checks:
            r = git_ops.run_command(repo, cmd, timeout=600)
            out_lines.append(f"$ {' '.join(cmd)} -> exit {r.code}")
            if not r.ok:
                ok = False
                out_lines.append((r.stderr or r.stdout)[-800:])
                break
        self.last_test_output = "\n".join(out_lines)
        changed = git_ops.diff_names(repo)
        warnings = git_ops.scan_diff(repo)
        self.last_diff_summary = (
            f"Changed files ({len(changed)}): {changed[:30]}\n"
            f"Diff warnings: {warnings or 'none'}\n"
            f"HEAD: {git_ops.head_commit(repo)} "
            f"({git_ops.branch(repo)})")
        self.log.append("verify",
                        f"verification {'PASSED' if ok else 'FAILED'}; "
                        f"{len(changed)} files changed.")
        return StepResult(ok, self.last_test_output[:500])

    def _repo(self) -> Path | None:
        p = self.settings.get("game_repo_path", "")
        if p and git_ops.is_git_repo(p) and self.settings.get("game_repo_verified"):
            return Path(p)
        return None

    # -- review stage --------------------------------------------------------
    def prepare_review(self) -> StepResult:
        if not self.current or self.current.stage != "dev":
            return StepResult(False, "No dev handoff ready for review.")
        task = self.queue.get(self.current.task_id)
        if task is None:
            return StepResult(False, "Task vanished from the queue.")
        prompt = build_review_prompt(
            task.title, task.acceptance,
            (task.evidence or {}).get("muse_report", ""),
            self.last_test_output, self.last_diff_summary)
        h = Handoff(new_id(), "review", task.id,
                    f"Review: {task.title}", prompt, "chatgpt",
                    "Copy this review prompt, paste it into ChatGPT, submit, "
                    "then paste the completed review back into the bridge.")
        self.current = h
        if not self.tracker.transition(HandoffState.REVIEW_PENDING,
                                       "review prepared; awaiting ChatGPT review"):
            return StepResult(False, "Cannot prepare review from here.")
        self._copy(prompt)
        self.log.append("handoff", f"Review handoff {h.id} prepared.")
        return StepResult(True, "Review prompt copied. Paste into ChatGPT.", h)

    def submit_review(self, text: str) -> StepResult:
        if not self.current or self.current.stage != "review":
            return StepResult(False, "No review awaiting transfer.")
        if not text.strip():
            return StepResult(False, "Empty review — nothing saved.")
        task = self.queue.get(self.current.task_id)
        summary = extract_tagged(text, "STATE_SUMMARY")
        if summary:
            nxt = extract_tagged(text, "NEXT")
            self.profile.update_summary(summary, nxt)
        approved = looks_like_approval(text)
        self.current.response = text
        self.current.done = True
        task_id, title = self.current.task_id, self.current.title
        self.current = None
        if approved and task and "FAILED" not in self.last_test_output:
            cp = {"commit": git_ops.head_commit(self._repo()) if self._repo() else "",
                  "at": time.time(), "note": title}
            self.checkpoints.record(cp["commit"], task_id, title)
            if task:
                self.queue.set_status(task_id, "completed", "Review APPROVED")
            self.profile.record_outcome(task_id, title, True)
            self.tracker.transition(HandoffState.TASK_PASSED, "review approved")
            self.tracker.transition(HandoffState.READY, "ready for next")
            return StepResult(True, "APPROVED — checkpoint saved. Ready for next task.")
        # Rejected (or tests failed): correction task, bounded retries.
        if task:
            self.queue.set_status(task_id, "backlog",
                                  f"Review feedback: {text[:200]}")
        if self.tracker.can_retry():
            self.tracker.note_retry()
            self.tracker.transition(HandoffState.TASK_FAILED, "review rejected")
            self.tracker.transition(
                HandoffState.READY,
                f"correction queued (retry {self.tracker.retries_used}/"
                f"{self.tracker.max_retries}) — press NEXT HANDOFF")
            return StepResult(False,
                              "REJECTED — task returned to queue with feedback. "
                              f"Press NEXT HANDOFF for the correction "
                              f"(retry {self.tracker.retries_used}/"
                              f"{self.tracker.max_retries}).")
        if task:
            self.queue.set_status(task_id, "failed", "Review rejected; retries exhausted")
            self.profile.record_outcome(task_id, title, False)
        self.tracker.transition(HandoffState.TASK_FAILED, "rejected, no retries left")
        self.tracker.transition(HandoffState.BLOCKED, "needs user decision")
        return StepResult(False, "REJECTED — retries exhausted. Marked BLOCKED for your decision.")

    # -- controls --------------------------------------------------------------
    def pause(self) -> bool:
        return self.tracker.transition(HandoffState.PAUSED, "user pressed PAUSE")

    def resume(self) -> bool:
        return self.tracker.transition(HandoffState.READY, "user pressed RESUME")

    def stop(self) -> bool:
        self.current = None
        ok = self.tracker.transition(HandoffState.STOPPED, "user pressed STOP")
        self._persist()
        return ok

    def next_handoff(self) -> StepResult:
        """Prepare whatever the next step is, based on explicit state."""
        s = self.tracker.state
        if s in (HandoffState.READY, HandoffState.TASK_PASSED,
                 HandoffState.TASK_FAILED):
            if s != HandoffState.READY:
                self.tracker.transition(HandoffState.READY, "next")
            if self.queue.active() or self.queue.next_backlog():
                return self.prepare_dev()
            return self.prepare_planning()
        return StepResult(False, f"Finish the current step first (state={s.value}).")
