"""File ownership registry for safe parallel development.

Before a task is assigned, it claims the files it expects to touch.
- If another IN-FLIGHT task owns any of the same files -> conflict:
  the new task waits (it is NOT assigned).
- A small set of HIGH-CONFLICT files (architecture cores) can only be
  owned by one task at a time, ever — even completed tasks hold their
  claim until integration, so two workers never diverge the same core.

Ownership is released when a task is integrated or explicitly released.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


# Files where parallel divergence is never safe. Claimed exclusively.
HIGH_CONFLICT = {
    "src/App.tsx",
    "package.json",
    "vite.config.ts",
    "tsconfig.json",
    "index.html",
}


class OwnershipRegistry:
    def __init__(self, directory: Path | None = None) -> None:
        from .store import data_dir
        self.dir = directory or data_dir()
        self.path = self.dir / "ownership.json"
        self.claims: dict[str, dict] = self._load()  # file -> claim

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.claims, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _norm(files: list[str]) -> set[str]:
        return {f.strip().lstrip("./") for f in files if f.strip()}

    def check(self, task_id: str, files: list[str]) -> tuple[bool, list[str]]:
        """True if the task may claim these files; else (False, blockers)."""
        wanted = self._norm(files)
        blockers: list[str] = []
        for f in sorted(wanted):
            claim = self.claims.get(f)
            if claim and claim["task_id"] != task_id:
                blockers.append(
                    f"{f} owned by {claim['task_id']} ({claim['worker_id']})")
        return (len(blockers) == 0, blockers)

    def claim(self, task_id: str, worker_id: str,
              files: list[str]) -> tuple[bool, list[str]]:
        ok, blockers = self.check(task_id, files)
        if not ok:
            return False, blockers
        for f in self._norm(files):
            self.claims[f] = {
                "task_id": task_id, "worker_id": worker_id,
                "at": time.time(),
                "exclusive": f in HIGH_CONFLICT,
            }
        self._save()
        return True, []

    def release_task(self, task_id: str) -> int:
        """Release all claims held by a task. Returns count released."""
        doomed = [f for f, c in self.claims.items()
                  if c["task_id"] == task_id]
        for f in doomed:
            del self.claims[f]
        if doomed:
            self._save()
        return len(doomed)

    def release_worker(self, worker_id: str) -> int:
        doomed = [f for f, c in self.claims.items()
                  if c["worker_id"] == worker_id]
        for f in doomed:
            del self.claims[f]
        if doomed:
            self._save()
        return len(doomed)

    def overlapping_with_diff(self, task_id: str,
                              actual_files: list[str]) -> list[str]:
        """Files the task actually changed that another task owns."""
        blockers = []
        for f in self._norm(actual_files):
            claim = self.claims.get(f)
            if claim and claim["task_id"] != task_id:
                blockers.append(
                    f"{f} also owned by {claim['task_id']} ({claim['worker_id']})")
        return blockers

    def summary(self) -> dict[str, dict]:
        return dict(self.claims)
