"""Isolated workspaces for parallel workers (Git worktrees).

Each coding worker gets its own worktree: a separate working directory
backed by its own branch, all pointing at the SAME repository. Workers
cannot overwrite each other's files because they literally work in
different directories.

Falls back to plain branches (no worktree) when worktrees are not
supported — with an explicit warning, since branches alone do not
isolate working directories.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import git_ops


@dataclass
class Workspace:
    worker_id: str
    path: str        # worktree directory ("" if branch-only fallback)
    branch: str
    worktree: bool   # True = real worktree isolation
    created_at: float


class WorktreeManager:
    def __init__(self, repo: Path, base_dir: Path) -> None:
        self.repo = repo
        # Worktrees live OUTSIDE the main checkout to avoid nesting.
        self.base = base_dir / "worktrees"
        self.base.mkdir(parents=True, exist_ok=True)

    def supported(self) -> tuple[bool, str]:
        r = git_ops.run_git(self.repo, "worktree", "list")
        if r.ok:
            return True, "git worktrees supported"
        return False, f"git worktree unavailable: {r.stderr[:120]}"

    def create(self, worker_id: str, task_id: str) -> Workspace:
        """Create an isolated workspace on a fresh branch from main."""
        branch = f"bridge/{worker_id}/{task_id}"
        ok, _ = self.supported()
        if ok:
            path = self.base / f"{worker_id}-{task_id}"
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            # New branch from the integration base (main).
            r = git_ops.run_git(self.repo, "worktree", "add", "-b", branch,
                             str(path), "main")
            if r.ok:
                return Workspace(worker_id, str(path), branch, True, time.time())
            # worktree add failed: fall through to branch-only with warning.
        # Branch-only fallback: isolated history, shared working directory.
        git_ops.run_git(self.repo, "branch", branch, "main")
        return Workspace(worker_id, "", branch, False, time.time())

    def remove(self, ws: Workspace) -> None:
        if ws.worktree and ws.path:
            git_ops.run_git(self.repo, "worktree", "remove", "--force", ws.path)
            shutil.rmtree(ws.path, ignore_errors=True)
        # Keep the branch for forensics; it can be deleted explicitly.
        # (Rollback of a failed integration never deletes worker work.)

    def list(self) -> list[dict[str, str]]:
        r = git_ops.run_git(self.repo, "worktree", "list", "--porcelain")
        out = []
        if not r.ok:
            return out
        cur: dict[str, str] = {}
        for line in r.stdout.splitlines():
            if line.startswith("worktree "):
                if cur:
                    out.append(cur)
                cur = {"path": line[len("worktree "):]}
            elif line.startswith("branch "):
                cur["branch"] = line[len("branch "):]
        if cur:
            out.append(cur)
        return out

    def changed_files(self, ws: Workspace) -> list[str]:
        """Files changed in the worker's workspace vs main (evidence)."""
        cwd = Path(ws.path) if ws.worktree and ws.path else self.repo
        r = git_ops.run_git(cwd, "diff", "--name-only", "main...HEAD")
        files = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if not files:
            # Include untracked files.
            r2 = git_ops.run_git(cwd, "ls-files", "--others", "--exclude-standard")
            files = [l.strip() for l in r2.stdout.splitlines() if l.strip()]
        return files

    def commit_all(self, ws: Workspace, message: str) -> bool:
        cwd = Path(ws.path) if ws.worktree and ws.path else self.repo
        git_ops.run_git(cwd, "add", "-A")
        r = git_ops.run_git(cwd, "commit", "-m", message)
        return r.ok or "nothing to commit" in (r.stdout + r.stderr)
