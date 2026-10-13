"""GitHub integration for Muse Bridge (read-only first, write optional).

Two backends, both official and supported:

  1. Local git — always available once the repo folder is selected. Reads
     files, commits, diffs, and branches with no credentials at all.
  2. GitHub CLI (`gh`) — the official GitHub tool. `gh auth login` performs
     the browser OAuth flow and `gh` stores its own token; the bridge NEVER
     sees, stores, or handles the token. Used for auth status, repository
     visibility, and remote operations.

What the bridge does NOT do:
  - scrape ChatGPT or reuse its connector (separate capabilities),
  - extract browser cookies or reuse ChatGPT login credentials,
  - store passwords or tokens in plaintext (there is no token path at all).

Default mode is read-only. Write operations (push, PRs) go through local
git or `gh` only when the user explicitly runs them, and the integration
gate never pushes untested work to main.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConnectionStatus:
    mode: str                      # "gh" | "local" | "none"
    connected: bool
    detail: str
    user: str = ""
    scopes: list[str] = field(default_factory=list)


def _sh(args: list[str], cwd: Path | None = None,
        timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", "not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def gh_path() -> str | None:
    return shutil.which("gh")


def gh_auth_status() -> ConnectionStatus:
    """Ask `gh` whether the user is logged in. Token stays inside gh."""
    exe = gh_path()
    if not exe:
        return ConnectionStatus("none", False,
                                "GitHub CLI (gh) not installed. "
                                "Install from https://cli.github.com/ for "
                                "authenticated GitHub access.")
    code, out, err = _sh([exe, "auth", "status"])
    if code != 0:
        return ConnectionStatus(
            "gh", False,
            "gh is installed but not logged in. Run `gh auth login` once, "
            "then Refresh. The bridge never handles your token.")
    # Parse the logged-in user from gh's output.
    user = ""
    m = re.search(r"Logged in to [\w.]+ account (\S+)", out)
    if m:
        user = m.group(1)
    return ConnectionStatus("gh", True,
                            f"Authenticated via gh as {user or 'unknown'}.",
                            user=user)


def parse_remote_url(url: str) -> tuple[str, str]:
    """(owner, repo) from a git remote URL, or ('','') if unparseable."""
    url = url.strip()
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return "", ""


class GitHubConnection:
    """Read-only GitHub/repository access for the bridge."""

    def __init__(self, repo_path: str | Path) -> None:
        self.repo = Path(repo_path)

    # -- identity ----------------------------------------------------------
    def identity(self) -> dict:
        code, url, _ = _sh(["git", "config", "--get", "remote.origin.url"],
                           self.repo)
        owner, name = parse_remote_url(url) if code == 0 else ("", "")
        code, branch, _ = _sh(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                              self.repo)
        code2, head, _ = _sh(["git", "rev-parse", "--short", "HEAD"], self.repo)
        return {
            "owner": owner, "repo": name,
            "remote_url": url if code == 0 else "",
            "branch": branch if code == 0 else "",
            "head": head if code2 == 0 else "",
        }

    def latest_commit(self) -> dict:
        code, out, _ = _sh(
            ["git", "log", "-1", "--format=%H|%h|%an|%ad|%s",
             "--date=short"], self.repo)
        if code != 0 or not out:
            return {}
        h, sh, author, date, subject = out.split("|", 4)
        return {"hash": h, "short": sh, "author": author,
                "date": date, "subject": subject}

    def commit_history(self, n: int = 20) -> list[dict]:
        code, out, _ = _sh(
            ["git", "log", f"-{n}", "--format=%h|%ad|%an|%s",
             "--date=short"], self.repo)
        if code != 0:
            return []
        rows = []
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                rows.append({"short": parts[0], "date": parts[1],
                             "author": parts[2], "subject": parts[3]})
        return rows

    def branches(self) -> dict:
        _, local, _ = _sh(["git", "branch", "--format=%(refname:short)"],
                          self.repo)
        _, remote, _ = _sh(
            ["git", "branch", "-r", "--format=%(refname:short)"], self.repo)
        return {
            "local": [b for b in local.splitlines() if b],
            "remote": [b for b in remote.splitlines() if b],
        }

    # -- reads ---------------------------------------------------------------
    def read_file(self, relpath: str, ref: str = "HEAD",
                  max_chars: int = 20000) -> str | None:
        """Read a file from a ref (default HEAD) or working tree."""
        if ref == "WORKTREE":
            p = self.repo / relpath
            try:
                return p.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except OSError:
                return None
        code, out, _ = _sh(["git", "show", f"{ref}:{relpath}"], self.repo)
        return out[:max_chars] if code == 0 else None

    def diff(self, ref_a: str, ref_b: str = "HEAD",
             max_chars: int = 30000) -> dict:
        """Diff between two refs. Truncated; never dumps whole files blindly."""
        return self._diff_args([ref_a, ref_b], max_chars)

    def diff_range(self, rev_range: str, max_chars: int = 30000) -> dict:
        """Diff for a single revision range like 'main...branch'."""
        return self._diff_args([rev_range], max_chars)

    def _diff_args(self, revs: list[str], max_chars: int) -> dict:
        code, stat, _ = _sh(["git", "diff", "--stat", *revs], self.repo)
        code2, names, _ = _sh(["git", "diff", "--name-only", *revs], self.repo)
        code3, patch, _ = _sh(["git", "diff", *revs], self.repo)
        return {
            "stat": stat if code == 0 else "",
            "files": [f for f in names.splitlines() if f] if code2 == 0 else [],
            "patch": patch[:max_chars] if code3 == 0 else "",
            "truncated": len(patch) > max_chars if code3 == 0 else False,
        }

    def compare_branch(self, branch: str, base: str = "main") -> dict:
        """Compare a worker branch against the base (default main)."""
        return self.diff_range(f"{base}...{branch}")

    def status(self) -> str:
        _, out, _ = _sh(["git", "status", "--short"], self.repo)
        return out

    # -- gh-backed remote info ---------------------------------------------------
    def remote_info(self) -> dict:
        """Repository visibility etc. via gh (only when authenticated)."""
        exe = gh_path()
        ident = self.identity()
        if not exe or not ident["owner"]:
            return {}
        code, out, _ = _sh(
            [exe, "repo", "view", f"{ident['owner']}/{ident['repo']}",
             "--json", "nameWithOwner,visibility,defaultBranchRef,pushedAt"],
            self.repo)
        if code != 0:
            return {"error": out[:200]}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {}


def repo_context_block(conn: "GitHubConnection",
                       worker_branches: list[str] | None = None) -> str:
    """Concise live context for ChatGPT prompts. Real data only."""
    ident = conn.identity()
    latest = conn.latest_commit()
    lines = [
        f"Repository: {ident['owner']}/{ident['repo'] or '(unknown)'}",
        f"Branch: {ident['branch']}   HEAD: {ident['head']}",
    ]
    if latest:
        lines.append(
            f"Latest commit: {latest['short']} ({latest['date']}) "
            f"{latest['subject'][:100]}")
    hist = conn.commit_history(5)
    if hist:
        lines.append("Recent commits:")
        lines += [f"  - {c['short']} {c['subject'][:80]}" for c in hist]
    if worker_branches:
        lines.append("Active worker branches: " + ", ".join(worker_branches))
    status = conn.status()
    if status:
        lines.append(f"Uncommitted changes:\n{status[:500]}")
    return "\n".join(lines)
