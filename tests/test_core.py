"""Unit tests for Muse Bridge core (state machine, store, runner safety).

Run: python -m pytest tests/  (or python -m unittest)
No Windows, no API keys, no GUI needed.
"""

import tempfile
import unittest
from pathlib import Path

from muse_bridge.state import ResponseState, StateTracker
from muse_bridge.store import EventLog, Settings, Task, TaskQueue, Checkpoints
from muse_bridge.runner import Runner
from muse_bridge.store import Brief


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


class TestStateMachine(unittest.TestCase):
    def test_happy_path(self) -> None:
        t = StateTracker()
        self.assertTrue(t.transition(ResponseState.TASK_SENT))
        self.assertTrue(t.transition(ResponseState.WAITING_FOR_RESPONSE))
        self.assertTrue(t.transition(ResponseState.RESPONSE_IN_PROGRESS))
        self.assertTrue(t.transition(ResponseState.RESPONSE_COMPLETE))
        self.assertTrue(t.transition(ResponseState.REVIEWING))
        self.assertTrue(t.transition(ResponseState.SENDING_TO_MUSE))
        self.assertTrue(t.transition(ResponseState.MUSE_WORKING))
        self.assertTrue(t.transition(ResponseState.VERIFYING))
        self.assertTrue(t.transition(ResponseState.CHECKPOINTING))
        self.assertTrue(t.transition(ResponseState.READY))
        self.assertEqual(t.state, ResponseState.READY)

    def test_illegal_transition_rejected(self) -> None:
        t = StateTracker()
        # READY -> VERIFYING skips the whole pipeline: must be rejected.
        self.assertFalse(t.transition(ResponseState.VERIFYING))
        self.assertEqual(t.state, ResponseState.READY)
        # No double-send: TASK_SENT cannot go straight back to TASK_SENT.
        t.transition(ResponseState.TASK_SENT)
        self.assertFalse(t.transition(ResponseState.TASK_SENT))

    def test_never_double_task_while_busy(self) -> None:
        t = StateTracker()
        self.assertFalse(t.busy())
        t.transition(ResponseState.TASK_SENT)
        self.assertTrue(t.busy())
        t.transition(ResponseState.ERROR)
        self.assertFalse(t.busy())


class TestStore(unittest.TestCase):
    def test_settings_roundtrip(self) -> None:
        d = tmpdir()
        s = Settings(d)
        s.set("muse_window_title", "Muse")
        s2 = Settings(d)
        self.assertEqual(s2.get("muse_window_title"), "Muse")

    def test_queue(self) -> None:
        d = tmpdir()
        q = TaskQueue(d)
        q.add(Task(id="t1", title="Fix roads"))
        self.assertEqual(q.next_backlog().id, "t1")
        q.set_status("t1", "completed")
        self.assertIsNone(q.next_backlog())
        self.assertEqual(q.counts()["completed"], 1)

    def test_log_scrubs_secrets(self) -> None:
        d = tmpdir()
        log = EventLog(d)
        log.append("test", "hello", api_key="sk-secret", other="fine")
        tail = log.tail(1)[0]
        self.assertEqual(tail["api_key"], "***")
        self.assertEqual(tail["other"], "fine")

    def test_checkpoints(self) -> None:
        d = tmpdir()
        c = Checkpoints(d)
        self.assertIsNone(c.latest())
        c.record("abc123", "t1", "roads")
        self.assertEqual(c.latest()["commit"], "abc123")


class TestRunnerSafety(unittest.TestCase):
    def _runner(self, d: Path) -> Runner:
        s = Settings(d)
        return Runner(s, EventLog(d), TaskQueue(d), Checkpoints(d), Brief(d))

    def test_no_repo_no_cycle(self) -> None:
        r = self._runner(tmpdir())
        r.queue.add(Task(id="t1", title="x"))
        res = r.run_one_cycle()
        self.assertIsNotNone(res)
        assert res is not None
        self.assertFalse(res.success)
        self.assertIn("repo", res.detail.lower())

    def test_no_double_cycle_while_busy(self) -> None:
        r = self._runner(tmpdir())
        r.tracker.transition(ResponseState.TASK_SENT)
        self.assertIsNone(r.run_one_cycle())

    def test_handoff_summary(self) -> None:
        r = self._runner(tmpdir())
        s = r.handoff_summary()
        self.assertIn("State:", s)
        self.assertIn("Tasks:", s)


if __name__ == "__main__":
    unittest.main()
