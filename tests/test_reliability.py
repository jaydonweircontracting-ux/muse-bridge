"""Reliability requirements: ledger, conversation lock, destination
verification, watchdog, autonomy modes, queue backpressure."""
import tempfile
import unittest
from pathlib import Path

from muse_bridge.ledger import (ACKNOWLEDGED, COPIED, DELIVERED, DETECTED,
                                FAILED, NEEDS_REVIEW, QUEUED, RESPONSE_RECEIVED,
                                SUBMITTED, MessageLedger)
from muse_bridge.store import Task, TaskQueue
from muse_bridge.workers import WorkerManager


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="bridge-rel-"))


def _git(args: list[str], cwd: Path) -> None:
    import subprocess
    subprocess.run(["git"] + args, cwd=cwd, check=True,
                   capture_output=True)


def fixture_repo() -> Path:
    d = tmpdir()
    _git(["init", "-q"], d)
    _git(["config", "user.email", "t@t"], d)
    _git(["config", "user.name", "t"], d)
    _git(["checkout", "-q", "-b", "main"], d)
    (d / "package.json").write_text(
        '{"name": "game", "scripts": {"build": "node build.js", '
        '"test": "node test.js"}}')
    (d / "build.js").write_text('console.log("build ok");\n')
    (d / "test.js").write_text('console.log("ok");\n')
    src = d / "src"
    src.mkdir()
    (src / "world.ts").write_text("// world\n")
    _git(["add", "-A"], d)
    _git(["commit", "-qm", "init"], d)
    return d


def make_orch(d: Path, repo: Path):
    from muse_bridge.orchestrator import Orchestrator
    o = Orchestrator(d)
    o.settings.set("game_repo_path", str(repo))
    o.settings.set("game_repo_verified", True)
    o.copy_text = lambda s: None
    o.focus_window = lambda t: True
    return o


def verify(o, wid: str) -> None:
    o.workers.bind_session(wid, f"{wid}-window")
    ok, _ = o.workers.verify_session(wid, f"{o.workers.get(wid).label} is live")
    assert ok


def make_verified_orch(d: Path):
    o = make_orch(d, fixture_repo())
    verify(o, "w1")
    return o


class TestMessageLedger(unittest.TestCase):
    def test_create_assigns_unique_ids(self) -> None:
        d = tmpdir()
        led = MessageLedger(d)
        a = led.create("w1", "t1", "handoff", "prompt A")
        b = led.create("w1", "t1", "handoff", "prompt A")
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(a.text_sha256, b.text_sha256)  # same content

    def test_full_lifecycle(self) -> None:
        d = tmpdir()
        led = MessageLedger(d)
        m = led.create("w1", "t1", "handoff", "do the thing")
        for state in (QUEUED, COPIED, SUBMITTED, RESPONSE_RECEIVED,
                      DELIVERED, ACKNOWLEDGED):
            self.assertTrue(led.transition(m.id, state), state)
        self.assertEqual(led.get(m.id).state, ACKNOWLEDGED)
        self.assertEqual(led.pending(), [])

    def test_invalid_transition_rejected(self) -> None:
        d = tmpdir()
        led = MessageLedger(d)
        m = led.create("w1", "t1", "handoff", "x")
        self.assertFalse(led.transition(m.id, ACKNOWLEDGED))  # skip states
        self.assertEqual(led.get(m.id).state, DETECTED)

    def test_persists_across_restart(self) -> None:
        d = tmpdir()
        led = MessageLedger(d)
        m = led.create("w2", "t9", "handoff", "prompt")
        led.transition(m.id, QUEUED)
        led.transition(m.id, COPIED)
        led2 = MessageLedger(d)
        self.assertEqual(led2.get(m.id).state, COPIED)
        self.assertEqual(len(led2.get(m.id).history), 3)

    def test_recover_moves_midflight_to_needs_review(self) -> None:
        d = tmpdir()
        led = MessageLedger(d)
        mid = led.create("w1", "t1", "handoff", "a")
        led.transition(mid.id, QUEUED)
        led.transition(mid.id, COPIED)
        done = led.create("w1", "t2", "handoff", "b")
        for s in (QUEUED, COPIED, SUBMITTED, RESPONSE_RECEIVED,
                  DELIVERED, ACKNOWLEDGED):
            led.transition(done.id, s)
        moved = led.recover()
        self.assertEqual([m.id for m in moved], [mid.id])
        self.assertEqual(led.get(mid.id).state, NEEDS_REVIEW)
        self.assertEqual(led.get(done.id).state, ACKNOWLEDGED)

    def test_duplicate_protection(self) -> None:
        d = tmpdir()
        led = MessageLedger(d)
        m = led.create("w1", "t1", "handoff", "same prompt")
        led.transition(m.id, QUEUED)
        led.transition(m.id, COPIED)
        led.transition(m.id, SUBMITTED)
        dup = led.already_submitted("w1", m.text_sha256)
        self.assertIsNotNone(dup)
        self.assertEqual(dup.id, m.id)
        # Different worker: not a duplicate.
        self.assertIsNone(led.already_submitted("w2", m.text_sha256))

    def test_failed_can_retry(self) -> None:
        d = tmpdir()
        led = MessageLedger(d)
        m = led.create("w1", "t1", "handoff", "x")
        led.transition(m.id, QUEUED)
        self.assertTrue(led.transition(m.id, FAILED, "boom"))
        self.assertTrue(led.transition(m.id, QUEUED, "manual retry"))


class TestConversationLock(unittest.TestCase):
    def test_two_workers_cannot_share_chatgpt_window(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        wm.map_chatgpt("w1", "ChatGPT - convo A")
        owner = wm.chatgpt_owner("ChatGPT - convo A")
        self.assertIsNotNone(owner)
        assert owner is not None
        self.assertEqual(owner.id, "w1")
        # w1 itself is not its own conflict.
        self.assertIsNone(wm.chatgpt_owner("ChatGPT - convo A", exclude="w1"))
        self.assertIsNone(wm.chatgpt_owner("ChatGPT - convo B"))


class TestDestinationVerification(unittest.TestCase):
    def _orch(self, d: Path):
        return make_verified_orch(d)

    def test_handoff_blocked_when_window_missing(self) -> None:
        d = tmpdir()
        o = self._orch(d)
        from muse_bridge.store import Task
        o.queue.add(Task(id="t1", title="t", expected_files=["a.ts"]))
        o.schedule()
        # window_exists injected: window is gone
        o.window_exists = lambda title: False
        res = o.prepare_worker_handoff("w1")
        self.assertFalse(res.ok)
        self.assertIn("NOT sent", res.detail)
        # No ledger message was created for the blocked send.
        self.assertEqual(o.ledger.pending(), [])

    def test_handoff_proceeds_when_window_found(self) -> None:
        d = tmpdir()
        o = self._orch(d)
        from muse_bridge.store import Task
        o.queue.add(Task(id="t1", title="t", expected_files=["a.ts"]))
        o.schedule()
        o.window_exists = lambda title: True
        o.copy_text = lambda s: None
        o.focus_window = lambda t: True
        res = o.prepare_worker_handoff("w1")
        self.assertTrue(res.ok, res.detail)
        msgs = o.ledger.by_worker("w1")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].state, COPIED)

    def test_submitted_marks_ledger(self) -> None:
        d = tmpdir()
        o = self._orch(d)
        from muse_bridge.store import Task
        o.queue.add(Task(id="t1", title="t", expected_files=["a.ts"]))
        o.schedule()
        o.window_exists = lambda title: True
        o.copy_text = lambda s: None
        o.prepare_worker_handoff("w1")
        sr = o.worker_submitted("w1")
        self.assertTrue(sr.ok, sr.detail)
        msgs = o.ledger.by_worker("w1")
        self.assertEqual(msgs[0].state, SUBMITTED)
        # Double confirm is a harmless no-op, not a duplicate.
        dup = o.worker_submitted("w1")
        self.assertTrue(dup.ok, dup.detail)
        self.assertIn("already recorded", dup.detail)
        self.assertEqual(len(o.ledger.by_worker("w1")), 1)

    def test_report_and_done_advance_ledger(self) -> None:
        d = tmpdir()
        o = self._orch(d)
        from muse_bridge.store import Task
        o.queue.add(Task(id="t1", title="t", expected_files=["a.ts"]))
        o.schedule()
        o.window_exists = lambda title: True
        o.copy_text = lambda s: None
        o.prepare_worker_handoff("w1")
        o.worker_submitted("w1")
        # Real flow: user confirms the session finished, then transfers
        # the report.
        self.assertTrue(o.worker_done("w1").ok)
        self.assertEqual(o.ledger.by_worker("w1")[0].state, SUBMITTED)
        self.assertTrue(o.submit_worker_report("w1", "did stuff").ok)
        self.assertEqual(o.ledger.by_worker("w1")[0].state, DELIVERED)


class TestAutonomyModes(unittest.TestCase):
    def test_manual_worker_excluded_from_scheduling(self) -> None:
        d = tmpdir()
        o = make_orch(d, fixture_repo())
        verify(o, "w1")
        verify(o, "w2")
        o.workers.set_mode("w1", "manual")
        self.assertFalse(o.workers.get("w1").available())
        o.queue.add(Task(id="t0", title="t", expected_files=["a.ts"]))
        report = o.schedule()
        self.assertEqual(len(report["assigned"]), 1)
        self.assertEqual(report["assigned"][0][1], "w2")

    def test_invalid_mode_rejected(self) -> None:
        d = tmpdir()
        wm = WorkerManager(d)
        self.assertFalse(wm.set_mode("w1", "autonomous"))
        self.assertEqual(wm.get("w1").mode, "supervised")


class TestQueueBackpressure(unittest.TestCase):
    def test_queue_cap_refuses_overflow(self) -> None:
        d = tmpdir()
        q = TaskQueue(d, max_size=2)
        self.assertTrue(q.add(Task(id="a", title="a")))
        self.assertTrue(q.add(Task(id="b", title="b")))
        self.assertFalse(q.add(Task(id="c", title="c")))
        self.assertEqual(len(q.tasks), 2)

    def test_oversized_prompt_refused(self) -> None:
        d = tmpdir()
        o = make_orch(d, fixture_repo())
        verify(o, "w1")
        o.settings.set("max_prompt_chars", 10)
        o.queue.add(Task(id="t1", title="t", expected_files=["a.ts"]))
        o.schedule()
        o.window_exists = lambda title: True
        res = o.prepare_worker_handoff("w1")
        self.assertFalse(res.ok)
        self.assertIn("cap 10", res.detail)


if __name__ == "__main__":
    unittest.main()
