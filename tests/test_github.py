"""Tests for GitHub integration (read-only) and review packets.

Covers: remote URL parsing, repo identity, commit history, file reads,
branch compare, gh auth status reporting (no token handling), review
packet contents, ChatGPT review attachment.

Run: python -m unittest discover -s tests
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muse_bridge import github as G
from muse_bridge.orchestrator import Orchestrator
from muse_bridge.review_packet import build_review_packet
from muse_bridge.store import Task


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def fixture_repo() -> Path:
    d = tmpdir()
    for a in (["init", "-q"], ["config", "user.email", "t@t"],
              ["config", "user.name", "t"], ["checkout", "-q", "-b", "main"]):
        subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)
    (d / "a.txt").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=d, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin",
                    "https://github.com/someone/adventure-game-demo-v2-.git"],
                   cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-qb", "bridge/w1/t1"], cwd=d,
                   check=True, capture_output=True)
    (d / "b.txt").write_text("worker change\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "worker work"], cwd=d, check=True,
                   capture_output=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=d, check=True,
                   capture_output=True)
    return d


class TestGitHubConnection(unittest.TestCase):
    def test_parse_remote_url(self) -> None:
        self.assertEqual(
            G.parse_remote_url("https://github.com/a/b.git"), ("a", "b"))
        self.assertEqual(
            G.parse_remote_url("git@github.com:a/b.git"), ("a", "b"))
        self.assertEqual(G.parse_remote_url("not a url"), ("", ""))

    def test_identity(self) -> None:
        conn = G.GitHubConnection(fixture_repo())
        ident = conn.identity()
        self.assertEqual(ident["owner"], "someone")
        self.assertEqual(ident["repo"], "adventure-game-demo-v2-")
        self.assertEqual(ident["branch"], "main")
        self.assertTrue(ident["head"])

    def test_latest_commit_and_history(self) -> None:
        conn = G.GitHubConnection(fixture_repo())
        latest = conn.latest_commit()
        self.assertEqual(latest["subject"], "first")
        hist = conn.commit_history(5)
        self.assertEqual(len(hist), 1)

    def test_read_file(self) -> None:
        conn = G.GitHubConnection(fixture_repo())
        self.assertEqual(conn.read_file("a.txt").strip(), "hello")
        self.assertIsNone(conn.read_file("nope.txt"))
        self.assertEqual(conn.read_file("a.txt", ref="WORKTREE").strip(),
                         "hello")

    def test_compare_branch(self) -> None:
        conn = G.GitHubConnection(fixture_repo())
        diff = conn.compare_branch("bridge/w1/t1", "main")
        self.assertIn("b.txt", diff["files"])
        self.assertIn("b.txt", diff["stat"])

    def test_repo_context_block(self) -> None:
        conn = G.GitHubConnection(fixture_repo())
        ctx = G.repo_context_block(conn, ["bridge/w1/t1"])
        self.assertIn("someone/adventure-game-demo-v2-", ctx)
        self.assertIn("bridge/w1/t1", ctx)

    def test_gh_auth_status_no_token_handling(self) -> None:
        # Must never raise; reports honestly when gh is missing/logged out.
        with mock.patch("muse_bridge.github.gh_path", return_value=None):
            st = G.gh_auth_status()
            self.assertFalse(st.connected)
            self.assertIn("not installed", st.detail)
        with mock.patch("muse_bridge.github.gh_path", return_value="/usr/bin/gh"), \
             mock.patch("muse_bridge.github._sh", return_value=(1, "", "not logged in")):
            st = G.gh_auth_status()
            self.assertFalse(st.connected)
            self.assertIn("gh auth login", st.detail)


class TestReviewPacket(unittest.TestCase):
    def test_packet_contents(self) -> None:
        task = Task(id="t1", title="Torch", brief="add light",
                    acceptance="lights up")
        packet = build_review_packet(
            task, "Muse 1", "bridge/w1/t1", ["src/torch.ts"],
            " 1 file changed", "+light", {"passed": True, "steps": ["npm run build -> exit 0"]})
        for needle in ("t1", "Muse 1", "bridge/w1/t1", "src/torch.ts",
                       "npm run build", "MERGE", "APPROVE or REJECT"):
            self.assertIn(needle, packet)

    def test_packet_recommends_no_merge_on_failure(self) -> None:
        task = Task(id="t1", title="Torch")
        packet = build_review_packet(task, "Muse 1", "b", ["f"],
                                     "", "", {"passed": False})
        self.assertIn("DO NOT MERGE", packet)

    def test_orchestrator_prepares_packet(self) -> None:
        d = tmpdir()
        repo = fixture_repo()
        o = Orchestrator(d)
        o.settings.set("game_repo_path", str(repo))
        o.settings.set("game_repo_verified", True)
        o.copy_text = lambda t: None
        # Fake a worker with a branch that has real changes.
        o.workers.get("w1").task_id = "t1"
        o.workers.get("w1").branch = "bridge/w1/t1"
        o.workers.get("w1").label = "Muse 1"
        o.queue.add(Task(id="t1", title="Torch", branch="bridge/w1/t1",
                         verification={"passed": True, "steps": ["build ok"]}))
        ok, packet = o.prepare_review_packet("w1")
        self.assertTrue(ok, packet)
        self.assertIn("b.txt", packet)

    def test_attach_chatgpt_review(self) -> None:
        d = tmpdir()
        o = Orchestrator(d)
        o.queue.add(Task(id="t9", title="X", status="completed"))
        ok, msg = o.attach_chatgpt_review("t9", "APPROVE — solid work.")
        self.assertTrue(ok, msg)
        t = o.queue.get("t9")
        self.assertEqual(t.evidence["chatgpt_review"], "APPROVE — solid work.")
        ok, _ = o.attach_chatgpt_review("t9", "   ")
        self.assertFalse(ok)
        ok, _ = o.attach_chatgpt_review("nope", "APPROVE")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
