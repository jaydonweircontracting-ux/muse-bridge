"""Git + verification operations for Muse Bridge.

Safety rules enforced here, not just in the UI:
  - Work only inside the verified repo path.
  - Never force-push, never delete branches.
  - A task is only "done" when the repo, tests, and (where relevant)
    the browser prove it — never because a model said so.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CmdResult:
    ok: bool
    stdout: str
    stderr: str
    code: int


def _run(repo: Path, *args: str, timeout: int = 120) -> CmdResult:
    try:
        p = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True,
            text=True, timeout=timeout,
        )
        return CmdResult(p.returncode == 0, p.stdout.strip(),
                         p.stderr.strip(), p.returncode)
    except FileNotFoundError:
        return CmdResult(False, "", "git executable not found", 127)
    except subprocess.TimeoutExpired:
        return CmdResult(False, "", "git command timed out", 124)


def is_git_repo(path: str | Path) -> bool:
    p = Path(path)
    r = _run(p, "rev-parse", "--is-inside-work-tree")
    return r.ok and r.stdout == "true"


def branch(path: str | Path) -> str:
    r = _run(Path(path), "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout if r.ok else "?"


def status_short(path: str | Path) -> str:
    r = _run(Path(path), "status", "--short")
    return r.stdout if r.ok else r.stderr


def head_commit(path: str | Path) -> str:
    r = _run(Path(path), "rev-parse", "--short", "HEAD")
    return r.stdout if r.ok else "?"


def has_uncommitted(path: str | Path) -> bool:
    return bool(status_short(path))


def diff_names(path: str | Path) -> list[str]:
    # git status --short includes untracked files (??), so new files
    # Muse created are detected too — not just modifications.
    r = _run(Path(path), "status", "--short")
    if not r.ok:
        return []
    names: list[str] = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            # Handle renames: "R  old -> new".
            name = parts[1].split(" -> ")[-1].strip().strip('"')
            if name:
                names.append(name)
    return names


# --- forbidden-content scan ----------------------------------------------

SUSPICIOUS_SUFFIXES = (".pem", ".key", ".pfx", ".p12")
SECRET_HINTS = ("PRIVATE KEY", "BEGIN RSA", "BEGIN OPENSSH")


def scan_diff(path: str | Path) -> list[str]:
    """Return human-readable warnings about the current diff."""
    warnings: list[str] = []
    for name in diff_names(path):
        low = name.lower()
        if low.endswith(SUSPICIOUS_SUFFIXES):
            warnings.append(f"Possible secret file in diff: {name}")
        if "node_modules" in low or low.startswith("dist/"):
            warnings.append(f"Generated/build artifact in diff: {name}")
    r = _run(Path(path), "diff", "HEAD", "--", ".")
    for hint in SECRET_HINTS:
        if hint in r.stdout:
            warnings.append("Diff appears to contain a private key block.")
            break
    return warnings


def commit(path: str | Path, message: str) -> CmdResult:
    return _run(Path(path), "commit", "-m", message)


def push(path: str | Path, remote: str = "origin",
         branch_name: str = "main") -> CmdResult:
    # Deliberately no --force anywhere in this codebase.
    return _run(Path(path), "push", remote, branch_name, timeout=180)


# --- verification --------------------------------------------------------

@dataclass
class Verification:
    tests_ok: bool | None = None
    build_ok: bool | None = None
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


def run_command(repo: Path, cmd: list[str], timeout: int = 600) -> CmdResult:
    try:
        p = subprocess.run(cmd, cwd=str(repo), capture_output=True,
                           text=True, timeout=timeout)
        tail = 4000
        return CmdResult(p.returncode == 0, p.stdout[-tail:], p.stderr[-tail:],
                         p.returncode)
    except FileNotFoundError:
        return CmdResult(False, "", f"not found: {cmd[0]}", 127)
    except subprocess.TimeoutExpired:
        return CmdResult(False, "", "command timed out", 124)


def verify_task(repo: Path, checks: list[list[str]]) -> Verification:
    """Run shell checks (tests, typecheck, build). All must pass.

    `checks` is a list of argv lists, e.g.
      [["npm", "run", "build"], ["npx", "tsc", "--noEmit"]]
    """
    v = Verification()
    for cmd in checks:
        r = run_command(repo, cmd)
        v.notes.append(f"$ {' '.join(cmd)} -> exit {r.code}")
        if not r.ok:
            v.notes.append(r.stderr[-500:] if r.stderr else r.stdout[-500:])
            if "test" in " ".join(cmd).lower() or "tsc" in cmd:
                v.tests_ok = False
            if "build" in " ".join(cmd).lower():
                v.build_ok = False
            return v
    v.tests_ok = True
    v.build_ok = True
    return v
