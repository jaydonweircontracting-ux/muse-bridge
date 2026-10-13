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
from muse_bridge.chatgpt import (_extract_text, estimate_cost, UsageLedger,
                                 DEFAULT_MODEL)
from muse_bridge.project_state import ProjectState
from muse_bridge import muse_link


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


class TestResponsesApi(unittest.TestCase):
    def test_extract_output_text_field(self) -> None:
        data = {"status": "completed", "output_text": "hello world",
                "usage": {"input_tokens": 5, "output_tokens": 2}}
        self.assertEqual(_extract_text(data), "hello world")

    def test_extract_from_output_items(self) -> None:
        data = {"status": "completed", "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [
                {"type": "output_text", "text": "APPROVE"},
                {"type": "output_text", "text": " — looks good"},
            ]},
        ]}
        self.assertEqual(_extract_text(data), "APPROVE — looks good")

    def test_extract_empty(self) -> None:
        self.assertEqual(_extract_text({"status": "completed"}), "")

    def test_cost_estimation(self) -> None:
        # 1M in + 1M out of gpt-4o-mini at (0.15, 0.60) = $0.75
        self.assertAlmostEqual(
            estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000), 0.75)
        # Unknown model falls back to default rates, never crashes.
        self.assertGreaterEqual(estimate_cost("nope-9", 10, 10), 0.0)

    def test_usage_ledger(self) -> None:
        d = tmpdir()
        ledger = UsageLedger(d / "usage.jsonl")
        ledger.record(DEFAULT_MODEL, 100, 50, 0.0001, "plan")
        totals = ledger.totals()
        self.assertEqual(totals["calls"], 1)
        self.assertEqual(totals["input_tokens"], 100)
        self.assertAlmostEqual(totals["cost_usd"], 0.0001)
        # Persists across instances.
        ledger2 = UsageLedger(d / "usage.jsonl")
        self.assertEqual(ledger2.totals()["calls"], 1)


class TestProjectState(unittest.TestCase):
    def test_roundtrip(self) -> None:
        ps = ProjectState(tmpdir())
        self.assertEqual(ps.load()["summary"], "")
        ps.save_summary("Towns render; combat untested.")
        self.assertIn("Towns render", ps.load()["summary"])
        self.assertIn("Towns render", ps.summary_prompt_block())

    def test_extract_summary_tags(self) -> None:
        text = "APPROVE\n<STATE_SUMMARY>All green. Next: audio.</STATE_SUMMARY>\nbye"
        self.assertEqual(ProjectState.extract_summary(text),
                         "All green. Next: audio.")
        self.assertEqual(ProjectState.extract_summary("no tags"), "")


class TestSpendLimit(unittest.TestCase):
    def _runner(self, d: Path) -> Runner:
        s = Settings(d)
        ledger = UsageLedger(d / "usage.jsonl")
        return Runner(s, EventLog(d), TaskQueue(d), Checkpoints(d), Brief(d),
                      ledger=ledger)

    def test_no_limit_no_block(self) -> None:
        d = tmpdir()
        r = self._runner(d)
        r.settings.set("spend_limit_usd", 0)
        ok, _ = r.spend_ok()
        self.assertTrue(ok)

    def test_limit_enforced(self) -> None:
        d = tmpdir()
        r = self._runner(d)
        r.settings.set("spend_limit_usd", 0.01)
        assert r.ledger is not None
        r.ledger.record(DEFAULT_MODEL, 1_000_000, 1_000_000, 0.75, "plan")
        ok, msg = r.spend_ok()
        self.assertFalse(ok)
        self.assertIn("Spend limit", msg)
        # A cycle must refuse to start when over budget.
        r.queue.add(Task(id="t1", title="x"))
        res = r.run_one_cycle()
        self.assertIsNotNone(res)
        assert res is not None
        self.assertFalse(res.success)


class TestMuseCli(unittest.TestCase):
    def test_probe_reports_gracefully_when_missing(self) -> None:
        # Must never crash; reports found=False with install guidance.
        probe = muse_link.MuseCodeCli().probe()
        self.assertIsInstance(probe.found, bool)
        if not probe.found:
            self.assertIn("muse", probe.detail)

    def test_run_task_fails_cleanly_without_binary(self) -> None:
        import shutil
        if shutil.which("muse"):
            self.skipTest("muse CLI present; manual test needed")
        res = muse_link.MuseCodeCli().run_task("hi", "/tmp", timeout_s=5)
        self.assertFalse(res.ok)

    def test_capabilities_never_claims_unverified(self) -> None:
        caps = muse_link.capabilities()
        self.assertIn(caps.completion_detection,
                      ("cli-exit-code", "uncertain", "unavailable"))


if __name__ == "__main__":
    unittest.main()
