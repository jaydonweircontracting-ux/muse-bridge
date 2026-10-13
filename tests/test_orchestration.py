"""Tests for the multi-worker orchestration engine.

Covers: session verification honesty, concurrency limits, dependency
scheduling, file-conflict blocking, worktree isolation, integration
gate (pass / conflict / test-failure), disconnect isolation, recovery.

Run: python -m unittest discover -s tests
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from muse_bridge.integrate import IntegrationGate
from muse_bridge.orchestrator import Orchestrator
from muse_bridge.ownership import OwnershipRegistry
from muse_bridge.store import Task
from muse_bridge.workers import WorkerManager, WorkerState
from muse_bridge.workspaces import WorktreeManager, Workspace


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True)


def fixture_repo(pass_tests: bool = True) -> Path:
    d = tmpdir()
    git(["init", "-q"], d)
    git(["config", "user.email", "t@t"], d)
    git(["config", "user.name", "t"], d)
    git(["checkout", "-q", "-b", "main"], d)
    test_js = ('console.log("ok");\n' if pass_tests
               else 'console.error("boom"); process.exit(1);\n')
    (d / "package.json").write_text(
        '{"name": "game", "scripts": {"build": "node build.js", '
        '"test": "node test.js"}}')
    (d / "build.js").write_text('console.log("build ok");\n')
    (d / "test.js").write_text(test_js)
    src = d / "src"
    src.mkdir()
    (src / "world.ts").write_text("// world\n")
    (src / "npc.ts").write_text("// npc\n")
    git(["add", "-A"], d)
    git(["commit", "-qm", "init"], d)
    return d


def make_orch(d: Path, repo: Path) -> Orchestrator:
    o = Orchestrator(d)
    o.settings.set("game_repo_path", str(repo))
    o.settings.set("game_repo_verified", True)
    copied: list[str] = []
    o.copy_text = copied.append
    o.focus_window = lambda t: True
    return o


def verify(o: Orchestrator, wid: str) -> None:
    o.workers.bind_session(wid, f"{wid}-window")
    ok, _ = o.workers.verify_session(wid, f"{o.workers.get(wid).label} is live")
    assert ok


class TestWorkerVerification(unittest.TestCase):
    def test_probe_must_contain_label(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        wm.bind_session("w1", "win")
        ok, _ = wm.verify_session("w1", "hello there")
        self.assertFalse(ok)
        self.assertFalse(wm.get("w1").session_verified)
        ok, _ = wm.verify_session("w1", "Muse 1 is live")
        self.assertTrue(ok)
        self.assertTrue(wm.get("w1").session_verified)

    def test_unverified_worker_not_available(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        wm.bind_session("w1", "win")  # bound but not verified
        self.assertFalse(wm.get("w1").available())
        self.assertEqual(wm.concurrency_limit(), 0)

    def test_concurrency_limit_is_verified_count(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        for wid in ("w1", "w2"):
            wm.bind_session(wid, "win")
            wm.verify_session(wid, f"Muse {wid[1]} is live")
        self.assertEqual(wm.verified_count(), 2)
        self.assertEqual(wm.concurrency_limit(), 2)

    def test_paused_worker_not_available(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        wm.bind_session("w1", "win")
        wm.verify_session("w1", "Muse 1 is live")
        self.assertTrue(wm.get("w1").available())
        self.assertTrue(wm.set_paused("w1", True))
        self.assertFalse(wm.get("w1").available())
        self.assertEqual(wm.verified_count(), 0)
        self.assertTrue(wm.set_paused("w1", False))
        self.assertTrue(wm.get("w1").available())

    def test_old_workers_json_loads_with_new_fields(self) -> None:
        import json
        d = tmpdir()
        # Simulate a workers.json written before chatgpt_window/paused existed.
        old = [{"id": "w1", "label": "Muse 1", "role": "world",
                "window_title": "win", "account_id": "",
                "state": "READY", "session_verified": True,
                "verified_at": 0.0, "task_id": "", "workspace": "",
                "branch": "", "expected_files": [], "last_activity": 0.0,
                "last_test_result": "", "errors": 0, "retries": 0, "note": ""}]
        (d / "workers.json").write_text(json.dumps(old), encoding="utf-8")
        wm = WorkerManager(d)
        s = wm.get("w1")
        assert s is not None
        self.assertEqual(s.window_title, "win")
        self.assertEqual(s.chatgpt_window, "")
        self.assertFalse(s.paused)

    def test_map_chatgpt_persists(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        self.assertTrue(wm.map_chatgpt("w1", "ChatGPT - convo 3"))
        self.assertEqual(wm.get("w1").chatgpt_window, "ChatGPT - convo 3")
        wm2 = WorkerManager(d)
        self.assertEqual(wm2.get("w1").chatgpt_window, "ChatGPT - convo 3")
        self.assertTrue(wm2.map_chatgpt("w1", ""))
        self.assertEqual(wm2.get("w1").chatgpt_window, "")


class TestScheduler(unittest.TestCase):
    def test_assigns_up_to_verified_limit(self) -> None:
        d = tmpdir()
        o = make_orch(d, fixture_repo())
        verify(o, "w1")
        verify(o, "w2")
        for i in range(4):
            o.queue.add(Task(id=f"t{i}", title=f"task {i}", priority=5,
                             expected_files=[f"src/f{i}.ts"]))
        report = o.schedule()
        self.assertEqual(len(report["assigned"]), 2)
        self.assertEqual(len(report["blocked"]), 2)
        self.assertIn("concurrency limit", report["blocked"][0]["reason"])

    def test_paused_workers_are_skipped(self) -> None:
        d = tmpdir()
        o = make_orch(d, fixture_repo())
        verify(o, "w1")
        verify(o, "w2")
        o.workers.set_paused("w1", True)
        o.queue.add(Task(id="t0", title="task 0", priority=5,
                         expected_files=["src/f0.ts"]))
        report = o.schedule()
        self.assertEqual(len(report["assigned"]), 1)
        self.assertEqual(report["assigned"][0][1], "w2")

    def test_dependencies_gate_scheduling(self) -> None:
        d = tmpdir()
        o = make_orch(d, fixture_repo())
        verify(o, "w1")
        o.queue.add(Task(id="a", title="A", priority=1))
        o.queue.add(Task(id="b", title="B", priority=1, deps=["a"]))
        report = o.schedule()
        assigned = [t for t, _ in report["assigned"]]
        self.assertIn("a", assigned)
        self.assertNotIn("b", assigned)
        # Complete A -> B becomes eligible.
        o.queue.set_status("a", "completed")
        o.workers.release("w1")
        report = o.schedule()
        self.assertIn("b", [t for t, _ in report["assigned"]])

    def test_file_conflict_blocks_assignment(self) -> None:
        d = tmpdir()
        o = make_orch(d, fixture_repo())
        verify(o, "w1")
        verify(o, "w2")
        o.queue.add(Task(id="t1", title="one", priority=1,
                         expected_files=["src/world.ts"]))
        o.queue.add(Task(id="t2", title="two", priority=2,
                         expected_files=["src/world.ts"]))
        report = o.schedule()
        assigned = [t for t, _ in report["assigned"]]
        self.assertIn("t1", assigned)
        self.assertNotIn("t2", assigned)
        self.assertTrue(any("file conflict" in b["reason"]
                            for b in report["blocked"]))


class TestWorktrees(unittest.TestCase):
    def test_isolated_workspaces_no_overwrite(self) -> None:
        repo = fixture_repo()
        d = tmpdir()
        wt = WorktreeManager(repo, d)
        ok, _ = wt.supported()
        self.assertTrue(ok)
        ws1 = wt.create("w1", "t1")
        ws2 = wt.create("w2", "t2")
        try:
            self.assertTrue(ws1.worktree and ws2.worktree)
            self.assertNotEqual(ws1.path, ws2.path)
            # Each worker writes its own file in its own worktree.
            Path(ws1.path, "src", "w1.ts").write_text("// w1\n")
            Path(ws2.path, "src", "w2.ts").write_text("// w2\n")
            self.assertFalse(Path(ws1.path, "src", "w2.ts").exists())
            self.assertFalse(Path(ws2.path, "src", "w1.ts").exists())
            # Main checkout untouched.
            self.assertFalse((repo / "src" / "w1.ts").exists())
            f1 = wt.changed_files(ws1)
            self.assertIn("src/w1.ts", f1)
        finally:
            wt.remove(ws1)
            wt.remove(ws2)
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(d, ignore_errors=True)


class TestIntegrationGate(unittest.TestCase):
    def test_pass_merges_into_integration_branch(self) -> None:
        repo = fixture_repo(pass_tests=True)
        d = tmpdir()
        o = make_orch(d, repo)
        verify(o, "w1")
        o.queue.add(Task(id="t1", title="torch", expected_files=["src/torch.ts"]))
        o.schedule()
        wt = o.worktrees()
        assert wt is not None
        ws = Workspace("w1", o.workers.get("w1").workspace,
                       o.workers.get("w1").branch, True, 0)
        Path(ws.path, "src", "torch.ts").write_text("// torch\n")
        res = o.integrate_worker("w1")
        try:
            self.assertTrue(res.ok, res.detail)
            # Merged into bridge/integration, main untouched.
            branches = subprocess.run(
                ["git", "branch", "--list", "bridge/integration"],
                cwd=repo, capture_output=True, text=True).stdout
            self.assertIn("bridge/integration", branches)
            main_files = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "main"],
                cwd=repo, capture_output=True, text=True).stdout
            self.assertNotIn("torch.ts", main_files)
            integ_files = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "bridge/integration"],
                cwd=repo, capture_output=True, text=True).stdout
            self.assertIn("src/torch.ts", integ_files)
            self.assertEqual(o.queue.get("t1").status, "completed")
            # Worker branch preserved for forensics.
            self.assertIn(ws.branch, subprocess.run(
                ["git", "branch", "--list", ws.branch], cwd=repo,
                capture_output=True, text=True).stdout)
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(d, ignore_errors=True)

    def test_failed_tests_block_merge(self) -> None:
        repo = fixture_repo(pass_tests=False)
        d = tmpdir()
        o = make_orch(d, repo)
        verify(o, "w1")
        o.queue.add(Task(id="t1", title="bad", expected_files=["src/bad.ts"]))
        o.schedule()
        ws = Workspace("w1", o.workers.get("w1").workspace,
                       o.workers.get("w1").branch, True, 0)
        Path(ws.path, "src", "bad.ts").write_text("// bad\n")
        res = o.integrate_worker("w1")
        try:
            self.assertFalse(res.ok)
            self.assertEqual(o.workers.get("w1").state,
                             WorkerState.FAILED.value)
            # Task re-queued, worker branch preserved.
            self.assertEqual(o.queue.get("t1").status, "backlog")
            out = subprocess.run(["git", "branch", "--list", ws.branch],
                                 cwd=repo, capture_output=True,
                                 text=True).stdout
            self.assertIn(ws.branch, out)
            # Nothing merged.
            integ = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "bridge/integration"],
                cwd=repo, capture_output=True, text=True).stdout
            self.assertNotIn("bad.ts", integ)
        finally:
            shutil.rmtree(repo, ignore_errors=True)
            shutil.rmtree(d, ignore_errors=True)

    def test_overlapping_changes_blocked(self) -> None:
        repo = fixture_repo(pass_tests=True)
        d = tmpdir()
        own = OwnershipRegistry(d)
        gate = IntegrationGate(repo, own)
        own.claim("other-task", "w9", ["src/world.ts"])
        task = Task(id="t1", title="x")
        ws = Workspace("w1", "", "bridge/w1/t1", False, 0)
        res = gate.run(task, ws, ["src/world.ts"])
        self.assertFalse(res.passed)
        self.assertTrue(any("overlapping" in s for s in res.steps))
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)

    def test_no_changes_blocked(self) -> None:
        repo = fixture_repo(pass_tests=True)
        d = tmpdir()
        gate = IntegrationGate(repo, OwnershipRegistry(d))
        task = Task(id="t1", title="x")
        ws = Workspace("w1", "", "bridge/w1/t1", False, 0)
        res = gate.run(task, ws, [])
        self.assertFalse(res.passed)
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)


class TestRecoveryAndIsolation(unittest.TestCase):
    def test_restart_recovers_workers_and_queue(self) -> None:
        repo = fixture_repo()
        d = tmpdir()
        o = make_orch(d, repo)
        verify(o, "w1")
        verify(o, "w2")
        o.queue.add(Task(id="t1", title="one", expected_files=["src/a.ts"]))
        o.schedule()
        del o
        o2 = make_orch(d, repo)
        self.assertEqual(o2.workers.verified_count(), 2)
        slot = o2.workers.get("w1")
        self.assertEqual(slot.state, WorkerState.ASSIGNED.value)
        self.assertEqual(slot.task_id, "t1")
        self.assertEqual(o2.queue.get("t1").status, "active")
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)

    def test_disconnect_one_leaves_others(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        for wid in ("w1", "w2"):
            wm.bind_session(wid, "win")
            wm.verify_session(wid, f"Muse {wid[1]} is live")
        wm.set_state("w1", WorkerState.WORKING)
        wm.get("w1").task_id = "t1"
        wm.disconnect("w1")
        w1, w2 = wm.get("w1"), wm.get("w2")
        self.assertFalse(w1.session_verified)
        self.assertEqual(w1.state, WorkerState.BLOCKED.value)
        self.assertEqual(w1.task_id, "t1")  # preserved for reassignment
        self.assertTrue(w2.session_verified)
        self.assertEqual(w2.state, WorkerState.READY.value)
        self.assertEqual(wm.concurrency_limit(), 1)

    def test_never_claims_five_without_evidence(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        # Five role definitions exist, zero verified sessions.
        self.assertEqual(len(wm.slots), 5)
        self.assertEqual(wm.verified_count(), 0)
        self.assertEqual(wm.concurrency_limit(), 0)


if __name__ == "__main__":
    unittest.main()
