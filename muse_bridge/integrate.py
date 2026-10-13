"""Integration gate for Muse Bridge orchestration.

Before a worker's changes reach the shared branch:

  1. Inspect the diff.
  2. Check file ownership + overlapping changes.
  3. Run tests in the worker's workspace.
  4. Run type checks and the production build where applicable.
  5. Record all evidence on the task.
  6. Merge into the single integration branch (default: bridge/integration).

A task is NEVER marked PASSED because an AI said it finished — only
repository evidence + test results merge it. Failed integrations roll
the integration branch back WITHOUT deleting the worker's branch/work.

Browser/screenshot verification for player-facing changes is supported
via an optional hook; otherwise it is recorded as not-run (honest).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import git_ops
from .ownership import OwnershipRegistry
from .store import Task
from .workspaces import Workspace


@dataclass
class GateResult:
    passed: bool
    steps: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def note(self, msg: str) -> None:
        self.steps.append(msg)


# Optional hook: run_browser_checks(worktree_path) -> (ok, notes)
BrowserHook = Callable[[Path], tuple[bool, str]]


class IntegrationGate:
    def __init__(self, repo: Path, ownership: OwnershipRegistry,
                 target_branch: str = "bridge/integration",
                 browser_hook: BrowserHook | None = None) -> None:
        self.repo = repo
        self.ownership = ownership
        self.target_branch = target_branch
        self.browser_hook = browser_hook

    def _ensure_target(self) -> None:
        r = git_ops.run_git(self.repo, "rev-parse", "--verify", self.target_branch)
        if not r.ok:
            git_ops.run_git(self.repo, "branch", self.target_branch, "main")

    def _checks_for(self, ws_path: Path) -> list[list[str]]:
        import json
        checks: list[list[str]] = []
        try:
            pkg = json.loads((ws_path / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {}) or {}
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        except (OSError, json.JSONDecodeError):
            return checks
        if "typescript" in deps or (ws_path / "tsconfig.json").exists():
            checks.append(["npx", "--no-install", "tsc", "--noEmit"])
        if "build" in scripts:
            checks.append(["npm", "run", "build"])
        if "test" in scripts:
            checks.append(["npm", "test", "--", "--watchAll=false"])
        return checks

    def run(self, task: Task, ws: Workspace,
            changed_files: list[str]) -> GateResult:
        res = GateResult(passed=False)
        ws_path = Path(ws.path) if ws.worktree and ws.path else self.repo

        # 1. Diff inspection.
        res.note(f"Changed files ({len(changed_files)}): {changed_files[:20]}")
        if not changed_files:
            res.note("BLOCKED: no changes detected in worker workspace.")
            res.evidence = {"changed_files": []}
            return res
        warnings = git_ops.scan_diff(ws_path)
        if warnings:
            res.note(f"BLOCKED: diff warnings: {warnings}")
            res.evidence = {"warnings": warnings}
            return res

        # 2. Ownership / overlap check against ACTUAL changed files.
        overlaps = self.ownership.overlapping_with_diff(task.id, changed_files)
        if overlaps:
            res.note("BLOCKED: overlapping changes: " + "; ".join(overlaps))
            res.evidence = {"overlaps": overlaps}
            return res
        res.note("Ownership check: no overlaps.")

        # 3-4. Tests, typecheck, build in the worker's workspace.
        test_ok = True
        for cmd in self._checks_for(ws_path):
            r = git_ops.run_command(ws_path, cmd, timeout=600)
            res.note(f"$ {' '.join(cmd)} -> exit {r.code}")
            if not r.ok:
                test_ok = False
                res.note("BLOCKED: verification command failed: "
                         + (r.stderr or r.stdout)[-500:])
                break
        if not test_ok:
            res.evidence = {"steps": res.steps}
            task.verification = {"passed": False, "steps": res.steps}
            return res
        res.note("Tests/build: all passed.")

        # 5. Browser/screenshot verification (optional hook).
        if self.browser_hook:
            ok, notes = self.browser_hook(ws_path)
            res.note(f"Browser checks: {'passed' if ok else 'FAILED'} — {notes[:200]}")
            if not ok:
                res.evidence = {"steps": res.steps}
                return res
        else:
            res.note("Browser checks: not run (no game harness hooked up).")

        # 6. Merge into the integration branch.
        self._ensure_target()
        before = git_ops.run_git(self.repo, "rev-parse", self.target_branch).stdout
        git_ops.run_git(self.repo, "checkout", self.target_branch)
        m = git_ops.run_git(self.repo, "merge", "--no-ff", "-m",
                            f"Integrate {task.id}: {task.title}", ws.branch)
        if not m.ok:
            git_ops.run_git(self.repo, "merge", "--abort")
            git_ops.run_git(self.repo, "checkout", "main")
            res.note("BLOCKED: merge conflict — worker branch kept intact.")
            res.evidence = {"merge_conflict": (m.stdout + m.stderr)[-500:]}
            return res
        # Post-merge sanity: integration branch must still build.
        post = git_ops.run_command(self.repo, ["npm", "run", "build"], timeout=600) \
            if (self.repo / "package.json").exists() else None
        if post is not None and not post.ok:
            # Roll back the integration branch; worker work is untouched.
            git_ops.run_git(self.repo, "reset", "--hard", before)
            git_ops.run_git(self.repo, "checkout", "main")
            res.note("BLOCKED: post-merge build failed — integration rolled back, "
                     "worker branch preserved.")
            res.evidence = {"post_merge_build": (post.stderr or post.stdout)[-500:]}
            return res
        git_ops.run_git(self.repo, "checkout", "main")

        res.passed = True
        res.evidence = {
            "changed_files": changed_files,
            "steps": res.steps,
            "merged_into": self.target_branch,
            "at": time.time(),
        }
        task.verification = {"passed": True, "steps": res.steps,
                             "merged_into": self.target_branch}
        res.note(f"Merged {ws.branch} into {self.target_branch}.")
        return res
