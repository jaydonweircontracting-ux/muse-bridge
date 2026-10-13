"""Tests for bot-client run control: START/STOP, ticks, hotkeys, tiling.

Run: python -m unittest discover -s tests
"""

import unittest

from muse_bridge.hotkeys import DEFAULT_HOTKEYS, HotkeyManager
from muse_bridge.run_control import RunController, RunState
from muse_bridge import window_layout


class FakeSlot:
    def __init__(self, session_verified: bool):
        self.session_verified = session_verified


class FakeWorkers:
    def __init__(self, verified: int):
        self.slots = [FakeSlot(True)] * verified

    def verified_count(self) -> int:
        return sum(1 for s in self.slots if s.session_verified)


class FakeOrchestrator:
    def __init__(self, verified: int = 0):
        self.workers = FakeWorkers(verified)
        self.schedule_calls = 0
        self.shutdown_calls = 0
        self.fail_schedule = False
        self.next_report: dict = {"assigned": [], "blocked": []}

    def schedule(self) -> dict:
        self.schedule_calls += 1
        if self.fail_schedule:
            raise RuntimeError("boom")
        return self.next_report

    def safe_shutdown(self) -> None:
        self.shutdown_calls += 1


class TestRunController(unittest.TestCase):
    def test_start_refuses_without_verified_workers(self) -> None:
        ctl = RunController(orch=FakeOrchestrator(verified=0))
        ok, msg = ctl.start()
        self.assertFalse(ok)
        self.assertIn("verified", msg.lower())
        self.assertEqual(ctl.state, RunState.IDLE)

    def test_start_and_double_start(self) -> None:
        ctl = RunController(orch=FakeOrchestrator(verified=2))
        ok, msg = ctl.start()
        self.assertTrue(ok)
        self.assertEqual(ctl.state, RunState.RUNNING)
        ok2, _ = ctl.start()
        self.assertFalse(ok2)

    def test_tick_while_idle_does_nothing(self) -> None:
        orch = FakeOrchestrator(verified=1)
        ctl = RunController(orch=orch)
        self.assertEqual(ctl.tick(), [])
        self.assertEqual(orch.schedule_calls, 0)

    def test_tick_assigns_and_reports(self) -> None:
        orch = FakeOrchestrator(verified=2)
        orch.next_report = {"assigned": [("task-1", "w1")],
                            "blocked": ["task-2 waits on task-1"]}
        ctl = RunController(orch=orch)
        ctl.start()
        events = ctl.tick()
        self.assertEqual(orch.schedule_calls, 1)
        self.assertTrue(any("task-1" in e and "w1" in e for e in events))
        self.assertTrue(any("Blocked" in e for e in events))

    def test_graceful_stop_finishes_tick_then_idles(self) -> None:
        orch = FakeOrchestrator(verified=1)
        ctl = RunController(orch=orch)
        ctl.start()
        ok, _ = ctl.request_stop()
        self.assertTrue(ok)
        self.assertEqual(ctl.state, RunState.STOPPING)
        events = ctl.tick()
        self.assertEqual(ctl.state, RunState.IDLE)
        self.assertEqual(orch.shutdown_calls, 1)
        self.assertTrue(any("gracefully" in e.lower() for e in events))

    def test_force_stop_is_immediate(self) -> None:
        orch = FakeOrchestrator(verified=1)
        ctl = RunController(orch=orch)
        ctl.start()
        ok, _ = ctl.force_stop()
        self.assertTrue(ok)
        self.assertEqual(ctl.state, RunState.IDLE)
        self.assertEqual(orch.shutdown_calls, 1)

    def test_scheduler_error_does_not_kill_run(self) -> None:
        orch = FakeOrchestrator(verified=1)
        orch.fail_schedule = True
        ctl = RunController(orch=orch)
        ctl.start()
        events = ctl.tick()
        self.assertEqual(ctl.state, RunState.RUNNING)
        self.assertTrue(any("Scheduler error" in e for e in events))

    def test_toggle(self) -> None:
        ctl = RunController(orch=FakeOrchestrator(verified=1))
        ok, _ = ctl.toggle()
        self.assertTrue(ok)
        self.assertEqual(ctl.state, RunState.RUNNING)
        ok, _ = ctl.toggle()
        self.assertTrue(ok)
        self.assertEqual(ctl.state, RunState.STOPPING)


class TestHotkeys(unittest.TestCase):
    def test_default_bindings_present(self) -> None:
        names = {h.name for h in DEFAULT_HOTKEYS}
        self.assertIn("toggle_run", names)
        self.assertIn("stop_now", names)
        for i in range(1, 6):
            self.assertIn(f"focus_w{i}", names)

    def test_non_windows_is_safe_noop(self) -> None:
        mgr = HotkeyManager()
        # On Windows these may actually register; everywhere they must not
        # raise and stop() must be safe.
        try:
            mgr.register(DEFAULT_HOTKEYS[0], lambda: None)
            mgr.start()
        finally:
            mgr.stop()
        self.assertFalse(mgr.active)


class TestWindowLayout(unittest.TestCase):
    def test_tile_noop_off_windows(self) -> None:
        placed, msg = window_layout.tile_windows(["no such window"])
        self.assertEqual(placed, 0)
        self.assertIsInstance(msg, str)
        self.assertTrue(msg)


if __name__ == "__main__":
    unittest.main()
