"""Headless GUI simulation for bot-client run control.

Runs the REAL tkinter BridgeApp under Xvfb: clicks the actual START/STOP
button, drives the tick loop, and fires the hotkey handlers exactly the way
the Win32 hotkey thread would (through the registered handler map).

Run: xvfb-run -a python -m unittest tests.test_gui_run
Skips gracefully when no display is available.
"""

import os
import tempfile
import unittest

try:
    import tkinter as tk
    from tkinter import messagebox
    _TK = True
except ImportError:
    _TK = False

from muse_bridge.run_control import RunState
from muse_bridge.store import Task
from muse_bridge.window_layout import grid_layout

HAS_DISPLAY = bool(os.environ.get("DISPLAY"))


@unittest.skipUnless(_TK and HAS_DISPLAY, "needs tkinter + display (xvfb-run)")
class TestGuiRunControl(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate all data files in a fake HOME.
        self._old_home = os.environ.get("HOME")
        self.tmp_home = tempfile.mkdtemp()
        os.environ["HOME"] = self.tmp_home
        # Some platforms use APPDATA; point it away too.
        self._old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.tmp_home

        self.warnings: list[str] = []
        self.infos: list[str] = []
        self._orig_warn = messagebox.showwarning
        self._orig_info = messagebox.showinfo
        messagebox.showwarning = lambda t, m: self.warnings.append(str(m))  # type: ignore
        messagebox.showinfo = lambda t, m: self.infos.append(str(m))  # type: ignore

        from muse_bridge.gui import BridgeApp
        self.root = tk.Tk()
        self.app = BridgeApp(self.root)
        # Skip the first-run wizard: jump straight to the dashboard.
        self.app.settings.set("first_run_complete", True)
        self.app.show_dashboard()
        self.root.update()

    def tearDown(self) -> None:
        messagebox.showwarning = self._orig_warn  # type: ignore
        messagebox.showinfo = self._orig_info  # type: ignore
        try:
            self.app.hotkeys.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        if self._old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = self._old_home
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata

    # -- helpers ---------------------------------------------------------

    def _verify_worker(self, wid: str = "w1") -> None:
        orch = self.app.orch
        self.assertTrue(orch.workers.bind_session(wid, f"Fake window {wid}"))
        slot = orch.workers.get(wid)
        assert slot is not None
        ok, msg = orch.workers.verify_session(wid, f"probe reply from {slot.label}")
        self.assertTrue(ok, msg)

    # -- tests ------------------------------------------------------------

    def test_run_bar_initial_state(self) -> None:
        self.assertEqual(self.app.run_ctl.state, RunState.IDLE)
        self.assertIn("IDLE", self.app._run_status.cget("text"))
        self.assertIn("START", self.app._run_btn.cget("text"))

    def test_start_refuses_without_verified_workers(self) -> None:
        self.app._run_btn.invoke()
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.IDLE)
        self.assertTrue(any("verified" in w.lower() for w in self.warnings),
                        f"warnings: {self.warnings}")

    def test_full_run_cycle_through_real_button(self) -> None:
        self._verify_worker("w1")
        # Queue a task so the scheduler has something to assign.
        self.app.orch.queue.add(Task(id="task-1", title="Build a thing"))

        # START via the real button.
        self.app._run_btn.invoke()
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.RUNNING)
        self.assertIn("STOP", self.app._run_btn.cget("text"))
        self.assertIn("RUNNING", self.app._run_status.cget("text"))

        # Drive one tick manually: scheduler should assign task-1 -> w1.
        events = self.app.run_ctl.tick()
        self.root.update()
        self.assertTrue(any("task-1" in e for e in events),
                        f"events: {events}")

        # Simulate the Ctrl+Shift+S hotkey exactly as the Win32 thread would.
        handler = self.app.hotkeys._handlers.get("toggle_run")
        self.assertIsNotNone(handler)
        assert handler is not None
        handler()  # -> request_stop
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.STOPPING)

        # Next tick settles to IDLE gracefully.
        events = self.app.run_ctl.tick()
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.IDLE)
        self.assertIn("START", self.app._run_btn.cget("text"))
        self.assertTrue(any("gracefully" in e.lower() for e in events))

    def test_hotkey_stop_now_is_immediate(self) -> None:
        self._verify_worker("w1")
        self.app._run_btn.invoke()
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.RUNNING)

        handler = self.app.hotkeys._handlers.get("stop_now")
        self.assertIsNotNone(handler)
        assert handler is not None
        handler()  # -> force_stop
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.IDLE)

    def test_hotkey_focus_worker_without_window(self) -> None:
        # w2 has no bound window: handler must not raise, just logs.
        handler = self.app.hotkeys._handlers.get("focus_w2")
        self.assertIsNotNone(handler)
        assert handler is not None
        handler()
        self.root.update()

    def test_tile_windows_button_no_windows(self) -> None:
        # No bound windows on Linux: must show info, not crash.
        self.app._tile_worker_windows()
        self.root.update()
        self.assertTrue(self.infos, "expected an info dialog")


class TestGridLayout(unittest.TestCase):
    def test_single_window_gets_full_area(self) -> None:
        rects = grid_layout(1, 1920, 1080)
        self.assertEqual(rects, [(0, 0, 1920, 1080)])

    def test_five_windows_use_three_columns(self) -> None:
        rects = grid_layout(5, 1920, 1080)
        self.assertEqual(len(rects), 5)
        xs = sorted({r[0] for r in rects})
        self.assertEqual(len(xs), 3)  # 3 columns
        # No overlaps.
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                overlap = (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                           and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])
                self.assertFalse(overlap, f"overlap: {a} vs {b}")

    def test_empty(self) -> None:
        self.assertEqual(grid_layout(0, 1920, 1080), [])


if __name__ == "__main__":
    unittest.main()
