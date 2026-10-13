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
        self._orig_ask = messagebox.askyesno
        messagebox.showwarning = lambda t, m: self.warnings.append(str(m))  # type: ignore
        messagebox.showinfo = lambda t, m: self.infos.append(str(m))  # type: ignore
        messagebox.askyesno = lambda t, m: True  # type: ignore[assignment]

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
        messagebox.askyesno = self._orig_ask  # type: ignore
        try:
            self.app.hotkeys.stop()
        except Exception:
            pass
        try:
            if self.root is not None:
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

    def test_dashboard_writes_recovery_file(self) -> None:
        import json
        from muse_bridge.recovery import recovery_path
        from muse_bridge.store import data_dir
        # data_dir() resolves from the fake HOME set in setUp.
        path = recovery_path(data_dir())
        self.assertTrue(path.exists(), f"missing: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertIn("workers", data)
        self.assertIn("how_to_reconnect", data)

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
        handler = self.app.hotkeys._handlers.get("run_toggle")
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

    def test_pause_button_cycle(self) -> None:
        from muse_bridge.run_control import RunState
        self._verify_worker("w1")
        # Pause disabled while idle.
        self.assertEqual(str(self.app._pause_btn.cget("state")), "disabled")
        self.app._run_btn.invoke()
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.RUNNING)
        self.assertEqual(str(self.app._pause_btn.cget("state")), "normal")
        # Pause via the real button.
        self.app._pause_btn.invoke()
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.PAUSED)
        self.assertIn("PAUSED", self.app._run_status.cget("text"))
        self.assertIn("RESUME", self.app._pause_btn.cget("text"))
        # Pause hotkey (F3 default) toggles back to running.
        handler = self.app.hotkeys._handlers.get("run_pause")
        self.assertIsNotNone(handler)
        assert handler is not None
        handler()
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.RUNNING)

    def test_hotkey_settings_dialog_lists_actions(self) -> None:
        self.app._show_hotkey_settings()
        self.root.update()
        dialogs = [w for w in self.root.winfo_children()
                   if type(w).__name__ == "Toplevel"]
        self.assertTrue(dialogs, "settings dialog did not open")
        dlg = dialogs[-1]
        labels = [w.cget("text") for w in dlg.winfo_children()
                  if type(w).__name__ == "Label"]
        self.assertTrue(any("Start / Go" in t for t in labels), labels)
        dlg.destroy()

    def test_keysym_to_vk_mapping(self) -> None:
        conv = self.app._keysym_to_vk
        self.assertEqual(conv("F1"), 0x70)
        self.assertEqual(conv("F3"), 0x72)
        self.assertEqual(conv("a"), 0x41)
        self.assertEqual(conv("5"), 0x35)
        self.assertIsNone(conv("Shift_L"))   # modifiers not assignable alone
        self.assertIsNone(conv("Control_L"))
        self.assertIsNone(conv("weird_key_xyz"))

    def _open_capture_dialog(self):
        from muse_bridge.hotkey_config import binding_for
        self.app._show_hotkey_settings()
        self.root.update()
        dlg = [w for w in self.root.winfo_children()
               if type(w).__name__ == "Toplevel"][-1]
        binding = binding_for(self.app.settings, "run_toggle")
        self.app._assign_hotkey(dlg, binding, {})
        self.root.update()
        caps = [w for w in dlg.winfo_children()
                if type(w).__name__ == "Toplevel"]
        self.assertTrue(caps, "capture dialog did not open")
        return dlg, caps[-1]

    def test_capture_assigns_plain_key(self) -> None:
        from muse_bridge.hotkeys import KeyCapture
        cap = KeyCapture(self.app._keysym_to_vk)
        self.assertEqual(cap.key("F5"), ("assign", 0, 0x74))

    def test_capture_assigns_chord(self) -> None:
        from muse_bridge.hotkeys import MOD_CONTROL, MOD_SHIFT, KeyCapture
        cap = KeyCapture(self.app._keysym_to_vk)
        self.assertEqual(cap.key("Control_L"), ("modifier",))
        self.assertEqual(cap.key("Shift_L"), ("modifier",))
        kind, mods, vk = cap.key("r")
        self.assertEqual(kind, "assign")
        self.assertEqual(mods, MOD_CONTROL | MOD_SHIFT)
        self.assertEqual(vk, 0x52)

    def test_capture_escape_cancels(self) -> None:
        from muse_bridge.hotkeys import KeyCapture
        cap = KeyCapture(self.app._keysym_to_vk)
        self.assertEqual(cap.key("Escape"), ("cancel",))
        self.assertEqual(cap.key("F1"), ("ignore",))  # finished

    def test_capture_ignores_repeat_and_rejects_junk(self) -> None:
        from muse_bridge.hotkeys import KeyCapture
        cap = KeyCapture(self.app._keysym_to_vk)
        self.assertEqual(cap.key("F1")[0], "assign")
        cap2 = KeyCapture(self.app._keysym_to_vk)
        cap2.key("F1")
        # finish a fresh capture to test repeat on the same instance:
        cap3 = KeyCapture(self.app._keysym_to_vk)
        cap3.key("a")
        self.assertEqual(cap3.key("a"), ("ignore",))
        cap4 = KeyCapture(self.app._keysym_to_vk)
        kind, ks = cap4.key("weird_key_xyz")
        self.assertEqual(kind, "rejected")

    def test_capture_release_clears_modifiers(self) -> None:
        from muse_bridge.hotkeys import KeyCapture
        cap = KeyCapture(self.app._keysym_to_vk)
        cap.key("Control_L")
        cap.release("Control_L")
        kind, mods, vk = cap.key("r")
        self.assertEqual((kind, mods), ("assign", 0))

    def test_assignment_dialog_has_no_mouse_bindings(self) -> None:
        # Mouse clicks can never be assigned: the capture dialog binds
        # only keyboard events.
        dlg, cap = self._open_capture_dialog()
        for seq in ("<Button-1>", "<Button-2>", "<Button-3>",
                    "<ButtonRelease-1>"):
            self.assertEqual(cap.bind(seq), "",
                             f"mouse binding {seq} must not exist")
        dlg.destroy()

    def test_worker_pause_toggle(self) -> None:
        self._verify_worker("w1")
        slot = self.app.orch.workers.get("w1")
        assert slot is not None
        self.app._w_pause_toggle("w1")
        self.assertTrue(slot.paused)
        self.assertFalse(slot.available())
        self.app._w_pause_toggle("w1")
        self.assertFalse(slot.paused)
        self.assertTrue(slot.available())

    def test_verify_pairs_no_windows(self) -> None:
        # Off Windows there are no windows: must report gracefully.
        self._verify_worker("w1")
        self.app._w_verify_pairs()
        self.root.update()
        self.assertTrue(any("MISSING" in i for i in self.infos), self.infos)

    def test_map_chatgpt_assigns_window(self) -> None:
        import muse_bridge.gui as gui_mod
        from muse_bridge import muse_link
        orig_ask = gui_mod._ask_choice
        orig_list = muse_link.list_windows
        try:
            muse_link.list_windows = lambda substring="": [
                muse_link.WindowInfo(title="ChatGPT - convo A")]
            gui_mod._ask_choice = lambda *a, **k: 0
            self.app._w_map_chatgpt("w1")
            slot = self.app.orch.workers.get("w1")
            assert slot is not None
            self.assertEqual(slot.chatgpt_window, "ChatGPT - convo A")
            # Persisted across reload.
            from muse_bridge.workers import WorkerManager
            from muse_bridge.store import data_dir
            wm2 = WorkerManager(data_dir())
            assert wm2.get("w1") is not None
            self.assertEqual(wm2.get("w1").chatgpt_window,
                             "ChatGPT - convo A")
        finally:
            gui_mod._ask_choice = orig_ask
            muse_link.list_windows = orig_list

    def test_hotkey_during_wizard_does_not_crash(self) -> None:
        """Ctrl+Shift+S before the dashboard exists: no run bar yet, but the
        hotkey must not crash — start just reports no verified workers."""
        import tkinter as tk
        from muse_bridge.gui import BridgeApp
        # Fresh HOME so this app really is on its first run (wizard showing).
        wiz_home = tempfile.mkdtemp()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = wiz_home
        root = tk.Tk()
        try:
            app = BridgeApp(root)  # wizard showing, no dashboard/run bar
            root.update()
            self.assertFalse(hasattr(app, "_run_dot"))
            handler = app.hotkeys._handlers.get("run_toggle")
            assert handler is not None
            handler()  # must not raise
            root.update()
            self.assertTrue(any("verified" in w.lower() for w in self.warnings))
        finally:
            try:
                app.hotkeys.stop()
            except Exception:
                pass
            root.destroy()
            if old_home is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = old_home

    def test_on_close_persists_cleanly(self) -> None:
        self._verify_worker("w1")
        self.app._on_close()
        # State file written; no exception. (Root is destroyed; mark it so
        # tearDown doesn't double-destroy.)
        self.root = None  # type: ignore

    def _make_git_repo(self) -> str:
        import subprocess
        d = tempfile.mkdtemp()
        def git(*args: str) -> None:
            subprocess.run(["git", "-C", d, *args], check=True,
                           capture_output=True, timeout=60)
        git("init", "-b", "main")
        git("config", "user.email", "sim@test.local")
        git("config", "user.name", "Sim")
        with open(os.path.join(d, "README.md"), "w") as f:
            f.write("# sim repo\n")
        git("add", "-A")
        git("commit", "-m", "init")
        return d

    def test_full_worker_lifecycle(self) -> None:
        """End-to-end through a real temp git repo: verify -> run -> handoff
        -> submitted -> done -> report -> review packet -> integrate gate ->
        auto-stop on drain."""
        from muse_bridge.workers import WorkerState
        orch = self.app.orch
        repo = self._make_git_repo()
        orch.settings.set("game_repo_path", repo)
        orch.settings.set("game_repo_verified", True)

        self._verify_worker("w1")
        orch.queue.add(Task(id="task-9", title="Lifecycle test"))

        # START, one tick: assign + auto handoff (worktree created).
        self.app._run_btn.invoke()
        self.root.update()
        self.app.run_ctl.tick()
        self.root.update()
        slot = orch.workers.get("w1")
        assert slot is not None
        self.assertEqual(slot.state, WorkerState.WORKING.value)
        self.assertTrue(slot.branch.startswith("bridge/w1/"))

        # Simulate the worker's work: a change in its worktree.
        ws_path = slot.workspace or repo
        with open(os.path.join(ws_path, "feature.txt"), "w") as f:
            f.write("worker did the thing\n")

        # User confirms submit -> done.
        self.app._w_submitted("w1")
        self.root.update()
        self.assertEqual(slot.state, WorkerState.WAITING_FOR_RESPONSE.value)
        self.app._w_done("w1")
        self.root.update()
        self.assertEqual(slot.state, WorkerState.WAITING_FOR_USER.value)

        # Transfer the completion report (bypass the paste dialog).
        res = orch.submit_worker_report("w1", "<REPORT>did the thing</REPORT>")
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(slot.state, WorkerState.REVIEW.value)

        # Review packet builds against the real repo.
        ok, packet = orch.prepare_review_packet("w1")
        self.assertTrue(ok, packet)

        # Integration gate: real merge into bridge/integration.
        res = orch.integrate_worker("w1")
        self.assertTrue(res.ok, res.detail)
        self.assertEqual(orch.queue.get("task-9").status, "completed")
        self.assertEqual(slot.state, WorkerState.READY.value)

        # Queue drained, workers idle -> run stops itself.
        events = self.app.run_ctl.tick()
        self.root.update()
        self.assertEqual(self.app.run_ctl.state, RunState.IDLE)
        self.assertTrue(any("Run complete" in e for e in events),
                        f"events: {events}")

    def test_integrate_without_repo_fails_gracefully(self) -> None:
        """No repo configured: integrate must fail gracefully, not crash."""
        orch = self.app.orch
        self._verify_worker("w1")
        orch.queue.add(Task(id="task-8", title="No repo test"))
        self.app._run_btn.invoke()
        self.root.update()
        self.app.run_ctl.tick()
        self.root.update()
        res = orch.submit_worker_report("w1", "report text")
        self.assertTrue(res.ok, res.detail)
        res = orch.integrate_worker("w1")
        self.assertFalse(res.ok)
        self.assertIn("repo", res.detail.lower())


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
