"""Tests for Muse Bridge — free existing-window edition.

Covers: explicit states, handoff preparation/parsing, no-duplicate guard,
recovery after restart, and a full simulated cycle (plan -> dev -> verify
-> review -> checkpoint) with canned transfers — no windows, no network.

Run: python -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muse_bridge import handoff as H
from muse_bridge.profile import ProjectProfile, scan_repo
from muse_bridge.runner import Coordinator
from muse_bridge.state import HandoffState, StateTracker
from muse_bridge.store import Brief, Checkpoints, EventLog, Settings, Task, TaskQueue


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def make_coord(d: Path) -> Coordinator:
    s = Settings(d)
    c = Coordinator(s, EventLog(d), TaskQueue(d), Checkpoints(d),
                    Brief(d), ProjectProfile(d))
    c.brief.save_text("MASTER: preserve architecture.")
    copied: list[str] = []
    c.copy_text = copied.append
    c.copied = copied  # type: ignore[attr-defined]
    return c


class TestStates(unittest.TestCase):
    def test_full_handoff_flow(self) -> None:
        t = StateTracker()
        flow = [HandoffState.PREPARED, HandoffState.WAITING_FOR_USER,
                HandoffState.MUSE_RUNNING, HandoffState.AWAITING_REPORT,
                HandoffState.REVIEW_PENDING, HandoffState.TASK_PASSED,
                HandoffState.READY]
        for s in flow:
            self.assertTrue(t.transition(s, "test"), f"failed at {s}")
        self.assertEqual(t.state, HandoffState.READY)

    def test_illegal_transitions_rejected(self) -> None:
        t = StateTracker()
        # Cannot jump READY -> MUSE_RUNNING (skips preparation).
        self.assertFalse(t.transition(HandoffState.MUSE_RUNNING))
        self.assertEqual(t.state, HandoffState.READY)
        # Cannot mark Muse done before it started.
        t.transition(HandoffState.PREPARED)
        self.assertFalse(t.transition(HandoffState.AWAITING_REPORT))

    def test_no_duplicate_while_busy(self) -> None:
        t = StateTracker()
        self.assertFalse(t.busy())
        t.transition(HandoffState.PREPARED)
        self.assertTrue(t.busy())
        t.transition(HandoffState.WAITING_FOR_USER)
        self.assertTrue(t.busy())
        t.transition(HandoffState.READY)
        self.assertFalse(t.busy())

    def test_pause_resume_stop(self) -> None:
        t = StateTracker()
        self.assertTrue(t.transition(HandoffState.PAUSED))
        self.assertTrue(t.transition(HandoffState.READY))
        self.assertTrue(t.transition(HandoffState.STOPPED))
        self.assertTrue(t.transition(HandoffState.READY))

    def test_state_persists(self) -> None:
        t = StateTracker()
        t.transition(HandoffState.PREPARED, "x")
        t2 = StateTracker.load(t.save())
        self.assertEqual(t2.state, HandoffState.PREPARED)


class TestHandoffParsing(unittest.TestCase):
    def test_parse_task_lines(self) -> None:
        text = ("Some prose.\n"
                "TASK: Fix roads | Roads render under buildings\n"
                "TASK: Add minimap | Circular minimap in corner\n"
                "not a task line\n"
                "TASK: missing separator\n")
        tasks = H.parse_task_lines(text)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0][0], "Fix roads")

    def test_parse_ignores_prose(self) -> None:
        self.assertEqual(H.parse_task_lines("We should fix roads someday."), [])

    def test_extract_tagged(self) -> None:
        self.assertEqual(H.extract_tagged("<REPORT>hi</REPORT> x", "REPORT"), "hi")
        self.assertEqual(H.extract_tagged("no tags", "REPORT"), "")

    def test_looks_like_approval(self) -> None:
        self.assertTrue(H.looks_like_approval("APPROVE — good"))
        self.assertTrue(H.looks_like_approval("approve\nreasons"))
        self.assertFalse(H.looks_like_approval("REJECT — broken"))


class TestCoordinator(unittest.TestCase):
    def test_planning_handoff_and_transfer(self) -> None:
        c = make_coord(tmpdir())
        res = c.prepare_planning()
        self.assertTrue(res.ok)
        self.assertEqual(c.tracker.state, HandoffState.WAITING_FOR_USER)
        self.assertEqual(len(c.copied), 1)  # prompt was copied
        # Second handoff while busy is refused — no duplicates.
        res2 = c.prepare_planning()
        self.assertFalse(res2.ok)
        # Transfer the plan.
        res3 = c.submit_planning_response(
            "TASK: Fix roads | Roads under buildings\n"
            "TASK: Add minimap | Corner minimap\n"
            "<STATE_SUMMARY>Roads first.</STATE_SUMMARY>")
        self.assertTrue(res3.ok)
        self.assertEqual(c.tracker.state, HandoffState.READY)
        self.assertEqual(c.queue.counts()["backlog"], 2)

    def test_empty_transfer_rejected(self) -> None:
        c = make_coord(tmpdir())
        c.prepare_planning()
        res = c.submit_planning_response("   ")
        self.assertFalse(res.ok)
        # Still waiting — nothing was faked.
        self.assertEqual(c.tracker.state, HandoffState.WAITING_FOR_USER)

    def test_dev_flow_needs_user_confirmation(self) -> None:
        c = make_coord(tmpdir())
        c.queue.add(Task(id="t1", title="Fix roads", brief="b"))
        res = c.prepare_dev()
        self.assertTrue(res.ok)
        self.assertEqual(c.tracker.state, HandoffState.WAITING_FOR_USER)
        # Cannot skip to "done" without the submitted step.
        self.assertFalse(c.mark_muse_done().ok)
        self.assertTrue(c.mark_muse_submitted().ok)
        self.assertEqual(c.tracker.state, HandoffState.MUSE_RUNNING)
        self.assertTrue(c.mark_muse_done().ok)
        self.assertEqual(c.tracker.state, HandoffState.AWAITING_REPORT)

    def test_muse_report_then_review_approve(self) -> None:
        c = make_coord(tmpdir())
        c.queue.add(Task(id="t1", title="Fix roads", brief="b",
                         acceptance="Roads render under buildings"))
        c.prepare_dev()
        c.mark_muse_submitted()
        c.mark_muse_done()
        res = c.submit_muse_report("<REPORT>Changed road renderer. Tests pass.</REPORT>")
        self.assertTrue(res.ok)
        # Fake verification results (no real repo here).
        c.last_test_output = "$ npm run build -> exit 0"
        c.last_diff_summary = "Changed files (1): ['src/roads.ts']"
        with mock.patch("muse_bridge.runner.git_ops.head_commit", return_value="abc123"), \
             mock.patch.object(c, "_repo", return_value=Path("/tmp")):
            rres = c.prepare_review()
            self.assertTrue(rres.ok)
            self.assertEqual(c.tracker.state, HandoffState.REVIEW_PENDING)
            sres = c.submit_review(
                "APPROVE — diff matches criteria.\n"
                "<STATE_SUMMARY>Roads fixed.</STATE_SUMMARY>")
        self.assertTrue(sres.ok)
        self.assertEqual(c.tracker.state, HandoffState.READY)
        self.assertEqual(c.queue.counts()["completed"], 1)
        self.assertEqual(c.checkpoints.latest()["commit"], "abc123")

    def test_review_reject_queues_correction(self) -> None:
        c = make_coord(tmpdir())
        c.queue.add(Task(id="t1", title="Fix roads", brief="b"))
        c.prepare_dev()
        c.mark_muse_submitted()
        c.mark_muse_done()
        c.submit_muse_report("<REPORT>done-ish</REPORT>")
        c.last_test_output = "$ npm run build -> exit 1\nFAIL"
        c.last_diff_summary = "Changed files (0): []"
        with mock.patch.object(c, "_repo", return_value=None):
            c.prepare_review()
            sres = c.submit_review("REJECT — build failed, no diff.")
        self.assertFalse(sres.ok)  # rejected
        self.assertEqual(c.tracker.state, HandoffState.READY)
        # Task back in backlog with feedback; correction via NEXT HANDOFF.
        self.assertEqual(c.queue.counts()["backlog"], 1)
        nres = c.next_handoff()
        self.assertTrue(nres.ok)
        self.assertEqual(c.tracker.state, HandoffState.WAITING_FOR_USER)

    def test_recovery_after_restart(self) -> None:
        d = tmpdir()
        c = make_coord(d)
        c.queue.add(Task(id="t1", title="Fix roads", brief="b"))
        c.prepare_dev()
        c.mark_muse_submitted()
        # Simulate app restart: brand-new Coordinator on the same directory.
        c2 = make_coord(d)
        self.assertEqual(c2.tracker.state, HandoffState.MUSE_RUNNING)
        status = c2.describe_status()
        self.assertIn("MUSE_RUNNING", status)
        # Queue survived too.
        self.assertEqual(c2.queue.counts().get("backlog", 0), 0)
        self.assertIsNotNone(c2.queue.active())

    def test_stop_preserves_history(self) -> None:
        c = make_coord(tmpdir())
        c.queue.add(Task(id="t1", title="x", brief="b"))
        c.prepare_dev()
        self.assertTrue(c.stop())
        self.assertEqual(c.tracker.state, HandoffState.STOPPED)
        self.assertGreater(len(c.tracker.history), 0)
        self.assertTrue(c.tracker.transition(HandoffState.READY))


class TestProfile(unittest.TestCase):
    def test_scan_repo_reads_facts(self) -> None:
        d = tmpdir()
        (d / "package.json").write_text(
            '{"name": "adventure", "dependencies": {"react": "1", "vite": "2"}}')
        src = d / "src" / "game"
        src.mkdir(parents=True)
        (src / "world.ts").write_text("// chunk system here\nlocalStorage save")
        facts = scan_repo(d)
        det = facts["detected"]
        self.assertTrue(det["react"])
        self.assertTrue(det["vite"])
        self.assertTrue(det["architecture_markers"]["chunk_system"])

    def test_scan_missing_repo_honest(self) -> None:
        facts = scan_repo(tmpdir() / "nope")
        self.assertEqual(facts["detected"]["package.json"], "not found")

    def test_profile_roundtrip(self) -> None:
        p = ProjectProfile(tmpdir())
        p.update_summary("Towns render.", "Combat next")
        data = p.load()
        self.assertEqual(data["project_summary"], "Towns render.")
        p.record_outcome("t1", "Fix roads", True)
        self.assertEqual(len(p.load()["completed"]), 1)


if __name__ == "__main__":
    unittest.main()
