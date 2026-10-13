"""Tkinter GUI for Muse Bridge — free existing-window edition.

First-run wizard: select ChatGPT window, Muse window, game repo, master prompt.
Dashboard: big readable buttons, guided handoffs, response-transfer box.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path
from typing import Callable

from . import git_ops, muse_link
from .hotkeys import (HotkeyBinding, HotkeyManager, find_conflict, vk_label)
from .hotkey_config import (binding_for, load_bindings, reset_bindings,
                            save_binding)
from .orchestrator import Orchestrator
from .profile import ProjectProfile
from .run_control import RunController, RunState
from .runner import Coordinator, StepResult
from .state import HandoffState
from .store import Brief, Checkpoints, EventLog, Settings, TaskQueue
from .window_layout import tile_windows
from .workers import WorkerState


def _open_path(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])
    except Exception:
        pass


class BridgeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Muse Bridge — RPG Development Coordinator")
        self.root.geometry("960x700")
        self.settings = Settings()
        self.log = EventLog()
        self.queue = TaskQueue()
        self.checkpoints = Checkpoints()
        self.brief = Brief()
        self.profile = ProjectProfile()
        self.coord = Coordinator(self.settings, self.log, self.queue,
                                 self.checkpoints, self.brief, self.profile)
        self.coord.copy_text = self._copy
        self.orch = Orchestrator()
        self.orch.focus_window = muse_link.focus_window
        self.orch.copy_text = self._copy
        # Bot-client run control: START/STOP + global hotkeys.
        self.run_ctl = RunController(orch=self.orch)
        self.run_ctl.on_event = lambda msg: self.log.append("run", msg)
        self.hotkeys = HotkeyManager(dispatch=lambda cb: self.root.after(0, cb))
        self._run_bar_widgets: dict = {}
        self._setup_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if not self.settings.get("first_run_complete"):
            self.show_wizard()
        else:
            self.show_dashboard()

    # -- helpers -------------------------------------------------------------
    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()  # keep clipboard after window closes

    def _clear(self) -> None:
        for w in self.root.winfo_children():
            w.destroy()

    # -- wizard ----------------------------------------------------------------
    def show_wizard(self) -> None:
        self._clear()
        f = ttk.Frame(self.root, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Muse Bridge — one-time setup",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(f, text="No API keys. No fees. Just point the bridge at your windows and repo.",
                  wraplength=860).pack(anchor="w", pady=(0, 10))

        # 1. ChatGPT window
        ttk.Label(f, text="1. Your existing ChatGPT browser window",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        r1 = ttk.Frame(f); r1.pack(fill="x", pady=4)
        self._cg_var = tk.StringVar(value=self.settings.get("chatgpt_window_title", ""))
        ttk.Entry(r1, textvariable=self._cg_var, width=58).pack(side="left")
        ttk.Button(r1, text="Detect…", command=lambda: self._detect("chatgpt")).pack(side="left", padx=6)
        ttk.Button(r1, text="Test focus", command=lambda: self._test_focus("chatgpt")).pack(side="left")

        # 2. Muse window
        ttk.Label(f, text="2. Your existing Muse window",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(8, 0))
        r2 = ttk.Frame(f); r2.pack(fill="x", pady=4)
        self._muse_var = tk.StringVar(value=self.settings.get("muse_window_title", ""))
        ttk.Entry(r2, textvariable=self._muse_var, width=58).pack(side="left")
        ttk.Button(r2, text="Detect…", command=lambda: self._detect("muse")).pack(side="left", padx=6)
        ttk.Button(r2, text="Test focus", command=lambda: self._test_focus("muse")).pack(side="left")
        caps = muse_link.capabilities()
        ttk.Label(f, text=caps.detail, wraplength=860,
                  foreground="green" if caps.can_focus else "orange").pack(anchor="w")

        # 3. Repo
        ttk.Label(f, text="3. Adventure Game V2 repository folder",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(8, 0))
        r3 = ttk.Frame(f); r3.pack(fill="x", pady=4)
        self._repo_var = tk.StringVar(value=self.settings.get("game_repo_path", ""))
        ttk.Entry(r3, textvariable=self._repo_var, width=58).pack(side="left")
        ttk.Button(r3, text="Browse…", command=self._browse_repo).pack(side="left", padx=6)
        self._repo_status = ttk.Label(f, text="")
        self._repo_status.pack(anchor="w")

        # 4. Master prompt
        ttk.Label(f, text="4. Paste your ONE master prompt (saved; never pasted again)",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(8, 0))
        self._brief_text = scrolledtext.ScrolledText(f, height=9)
        self._brief_text.pack(fill="both", expand=True, pady=4)
        existing = self.brief.load().get("text", "")
        if existing:
            self._brief_text.insert("1.0", existing)

        ttk.Button(f, text="Save and open the control panel",
                   command=self._wizard_finish).pack(anchor="e", pady=10)

    def _detect(self, which: str) -> None:
        hint = "Chrome" if which == "chatgpt" else "Muse"
        wins = muse_link.list_windows(hint)
        if not wins:
            wins = muse_link.list_windows("")
        if not wins:
            messagebox.showinfo("Detect windows",
                                "No windows found. Open the window first, then try again.")
            return
        # Simple picker: choose the first match, show full list in a dialog.
        names = "\n".join(f"{i+1}. {w.title[:80]}" for i, w in enumerate(wins[:15]))
        choice = _ask_choice(self.root, "Select window",
                             f"Windows matching '{hint}':\n\n{names}\n\nType the number:",
                             len(wins))
        if choice is not None:
            var = self._cg_var if which == "chatgpt" else self._muse_var
            var.set(wins[choice].title)
            self.log.append("app", f"{which} window selected: {wins[choice].title[:60]}")

    def _test_focus(self, which: str) -> None:
        var = self._cg_var if which == "chatgpt" else self._muse_var
        title = var.get().strip()
        if not title:
            messagebox.showinfo("Test focus", "Select a window first.")
            return
        ok = muse_link.focus_window(title)
        messagebox.showinfo("Test focus",
                            f"{'✓ Focused the window.' if ok else '✗ Could not focus it. Is it open?'}")
        self.log.append("app", f"Focus test ({which}): {'ok' if ok else 'FAILED'}")

    def _browse_repo(self) -> None:
        d = filedialog.askdirectory(title="Select Adventure Game V2 folder")
        if not d:
            return
        self._repo_var.set(d)
        if git_ops.is_git_repo(d):
            br = git_ops.branch(d)
            self._repo_status.config(
                text=f"✓ Git repo, branch {br}", foreground="green")
            self.settings.set("game_repo_path", d)
            self.settings.set("game_repo_verified", True)
            prof = self.profile.refresh_repo(d, br)
            n = len(prof.get("repo_facts", {}).get("detected", {}))
            self._repo_status.config(
                text=f"✓ Git repo, branch {br} — profile scanned ({n} fact groups)",
                foreground="green")
        else:
            self._repo_status.config(text="✗ Not a git repository.", foreground="red")
            self.settings.set("game_repo_verified", False)

    def _wizard_finish(self) -> None:
        self.settings.set("chatgpt_window_title", self._cg_var.get().strip())
        self.settings.set("muse_window_title", self._muse_var.get().strip())
        txt = self._brief_text.get("1.0", "end").strip()
        if txt:
            self.brief.save_text(txt)
        else:
            messagebox.showwarning("Master prompt",
                                   "Paste your master prompt before continuing — "
                                   "the bridge needs it to prepare handoffs.")
            return
        self.settings.set("first_run_complete", True)
        self.log.append("app", "First-run setup complete.")
        self.show_dashboard()

    # -- dashboard ---------------------------------------------------------------
    def show_dashboard(self) -> None:
        self._clear()
        self._build_run_bar(self.root)
        # Recovery: refresh the lightweight reconnect file and re-find
        # worker windows by title (non-intrusive, never steals focus).
        try:
            from .recovery import reconnect, write_recovery
            write_recovery(self.orch)
            for line in reconnect(self.orch):
                self.log.append("recovery", line)
        except Exception as exc:
            self.log.append("recovery", f"Recovery check failed: {exc}")
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        hf = ttk.Frame(nb)
        wf = ttk.Frame(nb)
        af = ttk.Frame(nb)
        ghf = ttk.Frame(nb)
        nb.add(hf, text="Handoffs")
        nb.add(wf, text="Workers (1–5)")
        nb.add(af, text="Accounts")
        nb.add(ghf, text="GitHub")
        self._build_handoff_tab(hf)
        self._build_workers_tab(wf)
        self._build_accounts_tab(af)
        self._build_github_tab(ghf)
        # Restart recovery: tell the user what was happening.
        st = self.coord.tracker.state
        if st not in (HandoffState.READY, HandoffState.STOPPED):
            self.log.append("app", "Recovered after restart: " + self.coord.describe_status().replace("\n", " | "))
            messagebox.showinfo(
                "Recovered after restart",
                "Muse Bridge restored your saved state:\n\n"
                + self.coord.describe_status()
                + "\n\nNothing was lost. Press NEXT HANDOFF to continue, "
                  "or STOP to stand down.")

    # -- run control bar (bot-client style START/STOP + hotkeys) ------------------

    def _build_run_bar(self, parent) -> None:
        bar = ttk.Frame(parent, padding=(8, 6))
        bar.pack(fill="x")
        self._run_dot = tk.Canvas(bar, width=14, height=14,
                                  highlightthickness=0)
        self._run_dot.pack(side="left", padx=(0, 6))
        self._run_status = ttk.Label(bar, text="IDLE", width=34)
        self._run_status.pack(side="left")
        self._run_btn = ttk.Button(bar, text="▶  START",
                                   command=self._run_toggle, width=18)
        self._run_btn.pack(side="left", padx=8)
        self._pause_btn = ttk.Button(bar, text="⏸  PAUSE",
                                     command=self._run_pause_toggle, width=14)
        self._pause_btn.pack(side="left")
        ttk.Button(bar, text="Tile windows",
                   command=self._tile_worker_windows).pack(side="left",
                                                           padx=(8, 0))
        ttk.Button(bar, text="Hotkeys…",
                   command=self._show_hotkey_settings).pack(side="left",
                                                            padx=(4, 0))
        self._hotkey_hint = ttk.Label(bar, text="", foreground="gray")
        self._hotkey_hint.pack(side="right", padx=8)
        self._refresh_run_bar()
        # Drive the run loop on the UI thread (schedule once).
        if not getattr(self, "_run_tick_scheduled", False):
            self._run_tick_scheduled = True
            self.root.after(int(self.run_ctl.tick_interval_s * 1000),
                            self._run_tick)

    def _hk_label(self, name: str) -> str:
        try:
            return binding_for(self.settings, name).label
        except Exception:
            return "?"

    def _refresh_run_bar(self) -> None:
        # The bar may not exist yet (e.g. hotkey pressed during the
        # first-run wizard) — never crash, just skip the visual update.
        if not hasattr(self, "_run_dot"):
            return
        st = self.run_ctl.state
        colors = {RunState.IDLE: "gray", RunState.RUNNING: "green",
                  RunState.PAUSED: "blue", RunState.STOPPING: "orange"}
        self._run_dot.delete("all")
        self._run_dot.create_oval(2, 2, 12, 12,
                                  fill=colors.get(st, "gray"), outline="")
        self._run_status.config(text=self.run_ctl.describe().upper())
        go_label, stop_label, pause_label = (self._hk_label("run_toggle"),
                                            self._hk_label("run_stop"),
                                            self._hk_label("run_pause"))
        self._run_btn.config(
            text=(f"⏹  STOP  ({stop_label})" if st == RunState.RUNNING
                  else f"▶  START  ({go_label})"))
        self._pause_btn.config(
            text=(f"▶  RESUME  ({pause_label})" if st == RunState.PAUSED
                  else f"⏸  PAUSE  ({pause_label})"),
            state=("normal" if st in (RunState.RUNNING, RunState.PAUSED)
                   else "disabled"))
        self._hotkey_hint.config(
            text=(f"{go_label} start · {stop_label} stop · "
                  f"{pause_label} pause · Hotkeys… to reassign"))

    def _run_toggle(self) -> None:
        ok, msg = self.run_ctl.toggle()
        self.log.append("run", msg)
        if not ok:
            messagebox.showwarning("Run control", msg)
        self._refresh_run_bar()

    def _run_tick(self) -> None:
        try:
            events = self.run_ctl.tick()
            if events:
                self._refresh_workers_tab_if_visible()
        except Exception as exc:
            self.log.append("run", f"Tick error: {exc}")
        self._refresh_run_bar()
        self.root.after(int(self.run_ctl.tick_interval_s * 1000),
                        self._run_tick)

    def _refresh_workers_tab_if_visible(self) -> None:
        try:
            self._refresh_workers()
        except Exception:
            pass

    # -- global hotkeys --------------------------------------------------------

    def _setup_hotkeys(self) -> None:
        """Register the user's configured bindings (defaults: F1/F2/F3)."""
        self._hk_last_fire: dict[str, float] = {}
        bound = 0
        for binding in load_bindings(self.settings):
            handler = self._hotkey_handler_for(binding.name)
            if handler is None:
                continue
            # Debounce: holding the key must not re-trigger the action.
            handler = self._debounced(binding.name, handler)
            if self.hotkeys.register(binding, handler):
                bound += 1
        if self.hotkeys.start():
            self.log.append("app", f"Global hotkeys active ({bound} bound).")
        else:
            self.log.append("app",
                            "Global hotkeys unavailable on this platform.")

    def _debounced(self, name: str,
                   fn: Callable[[], None]) -> Callable[[], None]:
        def wrapper() -> None:
            now = time.time()
            if now - self._hk_last_fire.get(name, 0.0) < 0.4:
                return  # key repeat — ignore
            self._hk_last_fire[name] = now
            fn()
        return wrapper

    def _hotkey_handler_for(self, name: str) -> Callable[[], None] | None:
        if name == "run_toggle":
            return self._run_toggle
        if name == "run_stop":
            return self._run_toggle_stop_only
        if name == "run_pause":
            return self._run_pause_toggle
        if name == "stop_now":
            return self._hk_stop_now
        if name.startswith("focus_w"):
            wid = "w" + name[-1]
            return lambda w=wid: self._focus_worker(w)
        return None

    def _run_toggle_stop_only(self) -> None:
        # F2 means Stop (not toggle): only act when something is running.
        if self.run_ctl.state in (RunState.RUNNING, RunState.PAUSED):
            self._run_toggle()
        else:
            self.log.append("run", "Hotkey: nothing running to stop.")

    def _run_pause_toggle(self) -> None:
        ok, msg = self.run_ctl.toggle_pause()
        self.log.append("run", "Hotkey: " + msg)
        if not ok:
            messagebox.showinfo("Run control", msg)
        self._refresh_run_bar()

    def _hk_stop_now(self) -> None:
        ok, msg = self.run_ctl.force_stop()
        self.log.append("run", "Hotkey: " + msg)
        self._refresh_run_bar()
        if not ok:
            messagebox.showinfo("Run control", msg)

    # -- hotkey configuration (reassignable, persisted) ------------------------

    def _show_hotkey_settings(self) -> None:
        from .hotkeys import IS_WINDOWS
        dlg = tk.Toplevel(self.root)
        dlg.title("Hotkeys")
        dlg.transient(self.root)
        dlg.grab_set()
        ttk.Label(dlg,
                  text=("Click Assign, then press any key. Esc cancels. "
                        "Mouse clicks cannot be assigned.\n"
                        "Run hotkeys control the run loop only — "
                        "never individual workers."),
                  foreground="gray").grid(row=0, column=0, columnspan=3,
                                           padx=10, pady=(10, 6), sticky="w")
        label_vars: dict[str, tk.StringVar] = {}

        def rebuild_rows() -> None:
            for w in dlg.grid_slaves():
                if int(w.grid_info()["row"]) > 0:
                    w.destroy()
            for i, b in enumerate(load_bindings(self.settings), start=1):
                ttk.Label(dlg, text=b.description).grid(
                    row=i, column=0, padx=(10, 4), pady=3, sticky="w")
                var = tk.StringVar(value=b.label)
                label_vars[b.name] = var
                ttk.Label(dlg, textvariable=var, width=20,
                          font=("TkDefaultFont", 10, "bold")).grid(
                              row=i, column=1, padx=4, pady=3)
                ttk.Button(dlg, text="Assign…",
                           command=lambda b=b: self._assign_hotkey(
                               dlg, b, label_vars)).grid(
                                   row=i, column=2, padx=(4, 10), pady=3)
            n = len(load_bindings(self.settings)) + 1
            ttk.Button(dlg, text="Reset to Defaults",
                       command=lambda: self._reset_hotkeys(dlg)).grid(
                           row=n, column=0, columnspan=3, pady=10)
            if not IS_WINDOWS:
                ttk.Label(dlg, text="Global hotkeys need Windows.",
                          foreground="gray").grid(row=n + 1, column=0,
                                                   columnspan=3, pady=(0, 8))

        rebuild_rows()
        self._hotkey_dialog_rebuild = rebuild_rows

    def _reset_hotkeys(self, dlg) -> None:
        reset_bindings(self.settings)
        # Re-register everything from defaults.
        for b in load_bindings(self.settings):
            self._reregister_hotkey(b.name, quiet=True)
        self.log.append("app", "Hotkeys reset to defaults.")
        if hasattr(self, "_hotkey_dialog_rebuild"):
            self._hotkey_dialog_rebuild()
        self._refresh_run_bar()

    def _reregister_hotkey(self, name: str, quiet: bool = False) -> bool:
        from .hotkeys import IS_WINDOWS
        binding = binding_for(self.settings, name)
        handler = self._hotkey_handler_for(name)
        if handler is None:
            return False
        self.hotkeys.unregister(name)
        ok = self.hotkeys.register(binding, self._debounced(name, handler))
        if not ok and IS_WINDOWS and not quiet:
            messagebox.showwarning(
                "Hotkey",
                f"Could not register {binding.label} globally — it is "
                f"reserved by Windows or taken by another app. "
                f"Pick a different key.")
        return ok

    @staticmethod
    def _keysym_to_vk(keysym: str) -> int | None:
        """Map a tkinter keysym to a Win32 virtual-key code (None = reject)."""
        if len(keysym) == 1:
            ch = keysym.upper()
            if "A" <= ch <= "Z" or "0" <= ch <= "9":
                return ord(ch)
        if keysym.startswith("F") and keysym[1:].isdigit():
            n = int(keysym[1:])
            if 1 <= n <= 24:
                return 0x70 + n - 1
        return {"space": 0x20, "Return": 0x0D, "Tab": 0x09,
                "BackSpace": 0x08, "Delete": 0x2E, "Insert": 0x2D,
                "Home": 0x24, "End": 0x23, "Prior": 0x21, "Next": 0x22,
                "Up": 0x26, "Down": 0x28, "Left": 0x25,
                "Right": 0x27}.get(keysym)

    def _assign_hotkey(self, parent, binding: HotkeyBinding,
                       label_vars: dict) -> None:
        from .hotkeys import KeyCapture
        # Unregister first: pressing the old key during capture must not
        # fire the action.
        self.hotkeys.unregister(binding.name)
        cap = tk.Toplevel(parent)
        cap.title(f"Assign: {binding.description}")
        cap.transient(parent)
        cap.grab_set()
        ttk.Label(cap,
                  text=("Press any keyboard key to assign.\n"
                        "Esc cancels — mouse clicks are not assignable."),
                  justify="center").pack(padx=24, pady=20)
        capture = KeyCapture(self._keysym_to_vk)

        def finish(mods: int, vk: int) -> None:
            label = vk_label(mods, vk)
            conflict = find_conflict(load_bindings(self.settings),
                                     binding.name, mods, vk)
            if conflict and not messagebox.askyesno(
                    "Hotkey conflict",
                    f"{label} is already assigned to "
                    f"'{conflict.description}'.\n\nReplace it?"):
                capture.held_mods.clear()
                capture.pressed.clear()
                capture.done = False
                return  # keep waiting for another key
            save_binding(self.settings, binding.name, mods, vk)
            cap.destroy()
            ok = self._reregister_hotkey(binding.name)
            var = label_vars.get(binding.name)
            if var is not None:
                var.set(label if ok else label + " (not registered)")
            self.log.append("app",
                            f"Hotkey '{binding.description}' -> {label}.")
            self._refresh_run_bar()

        def on_key(ev) -> None:
            outcome = capture.key(ev.keysym)
            kind = outcome[0]
            if kind == "cancel":
                cap.destroy()
                self._reregister_hotkey(binding.name, quiet=True)
            elif kind == "rejected":
                messagebox.showinfo("Hotkey",
                                    f"'{outcome[1]}' cannot be assigned. "
                                    f"Use a letter, digit, function key, "
                                    f"or arrow/navigation key.")
            elif kind == "assign":
                finish(outcome[1], outcome[2])
            # ("modifier",) and ("ignore",): keep waiting.

        def on_key_release(ev) -> None:
            capture.release(ev.keysym)

        cap.bind("<KeyPress>", on_key)
        cap.bind("<KeyRelease>", on_key_release)
        # No mouse bindings at all: clicks can never be assigned.
        def on_close() -> None:
            if not capture.done:
                fake_escape = type("E", (), {"keysym": "Escape"})()
                on_key(fake_escape)
        cap.protocol("WM_DELETE_WINDOW", on_close)

        def _focus() -> None:
            try:
                cap.focus_force()
            except Exception:
                pass
        cap.after(50, _focus)

    def _focus_worker(self, worker_id: str) -> None:
        try:
            slot = self.orch.workers.get(worker_id)
            title = getattr(slot, "window_title", "") if slot else ""
        except Exception:
            title = ""
        if title and muse_link.focus_window(title):
            self.log.append("run", f"Hotkey: focused worker {worker_id}.")
        else:
            self.log.append(
                "run", f"Hotkey: no bound window for worker {worker_id}.")

    def _tile_worker_windows(self) -> None:
        try:
            titles = [s.window_title for s in self.orch.workers.slots
                      if getattr(s, "window_title", "")]
        except Exception:
            titles = []
        placed, msg = tile_windows(titles)
        self.log.append("run", "Tile windows: " + msg)
        messagebox.showinfo("Tile windows", msg)

    def _on_close(self) -> None:
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        try:
            from .recovery import write_recovery
            write_recovery(self.orch)
        except Exception:
            pass
        try:
            self.orch.safe_shutdown()
        except Exception:
            pass
        self.root.destroy()

    def _build_handoff_tab(self, parent) -> None:
        top = ttk.Frame(parent, padding=10); top.pack(fill="x")
        ttk.Label(top, text="Muse Bridge",
                  font=("TkDefaultFont", 14, "bold")).pack(side="left")
        self._state_lbl = ttk.Label(top, text="READY",
                                    font=("TkDefaultFont", 12, "bold"))
        self._state_lbl.pack(side="left", padx=20)

        self._next_action = ttk.Label(parent, text="", wraplength=920,
                                      font=("TkDefaultFont", 10, "bold"),
                                      foreground="navy", padding=(10, 4))
        self._next_action.pack(fill="x")

        btns = ttk.Frame(parent, padding=8); btns.pack(fill="x")
        buttons = [
            ("START", self._start, 0, 0), ("PAUSE", self._pause, 0, 1),
            ("RESUME", self._resume, 0, 2), ("STOP", self._stop, 0, 3),
            ("NEXT HANDOFF", self._next_handoff, 0, 4),
            ("COPY NEXT PROMPT", self._copy_prompt, 1, 0),
            ("COPY RESPONSE FOR REVIEW", self._copy_review, 1, 1),
            ("OPEN CHATGPT", lambda: self._open_win("chatgpt"), 1, 2),
            ("OPEN MUSE", lambda: self._open_win("muse"), 1, 3),
            ("OPEN GAME REPOSITORY", self._open_repo, 1, 4),
            ("VIEW TASK QUEUE", self._view_queue, 2, 0),
            ("VIEW HISTORY", self._view_history, 2, 1),
            ("EXPORT DIAGNOSTICS", self._export_diag, 2, 2),
        ]
        for label, cmd, r, c in buttons:
            ttk.Button(btns, text=label, command=cmd, width=22).grid(
                row=r, column=c, padx=4, pady=3)

        # Muse progress confirmations (explicit — never guessed)
        prog = ttk.LabelFrame(parent, text="Muse progress (you confirm — never guessed)",
                              padding=8)
        prog.pack(fill="x", padx=10, pady=4)
        ttk.Button(prog, text="MUSE TASK SUBMITTED",
                   command=self._muse_submitted).pack(side="left", padx=4)
        ttk.Button(prog, text="MUSE DONE — I CONFIRM",
                   command=self._muse_done).pack(side="left", padx=4)
        ttk.Button(prog, text="RUN TESTS + BUILD",
                   command=self._run_verify).pack(side="left", padx=4)

        # Response transfer
        tr = ttk.LabelFrame(parent, text="Transfer a response (paste, don't retype)",
                            padding=8)
        tr.pack(fill="x", padx=10, pady=4)
        self._transfer = scrolledtext.ScrolledText(tr, height=4)
        self._transfer.pack(fill="x", pady=2)
        trow = ttk.Frame(tr); trow.pack(anchor="e")
        ttk.Button(trow, text="SAVE TRANSFERRED RESPONSE",
                   command=self._save_transfer).pack(side="left", padx=4)
        ttk.Button(trow, text="ATTACH AS CHATGPT REVIEW",
                   command=self._attach_review).pack(side="left")

        info = ttk.Frame(parent, padding=(10, 4)); info.pack(fill="x")
        self._info_vars: dict[str, tk.StringVar] = {}
        for key in ["Current task", "Current handoff", "Waiting on",
                    "Last completed", "Queue", "Saved progress"]:
            ttk.Label(info, text=key + ":").grid(sticky="w", padx=4)
            v = tk.StringVar(value="—")
            self._info_vars[key] = v
            ttk.Label(info, textvariable=v, wraplength=700).grid(
                row=list(self._info_vars).index(key), column=1, sticky="w")

        ttk.Label(parent, text="Log", padding=(10, 0)).pack(anchor="w")
        self._log_view = scrolledtext.ScrolledText(parent, height=10,
                                                   state="disabled")
        self._log_view.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._refresh()
        self.root.after(2000, self._tick)

    # -- workers tab ---------------------------------------------------------------
    def _build_workers_tab(self, parent) -> None:
        top = ttk.Frame(parent, padding=10); top.pack(fill="x")
        ttk.Label(top, text="Worker orchestration",
                  font=("TkDefaultFont", 12, "bold")).pack(side="left")
        self._sess_lbl = ttk.Label(top, text="", font=("TkDefaultFont", 10, "bold"))
        self._sess_lbl.pack(side="left", padx=16)
        ttk.Button(top, text="SCHEDULE WORK",
                   command=self._w_schedule).pack(side="left", padx=4)
        ttk.Button(top, text="SAFE SHUTDOWN",
                   command=self._w_safe_shutdown).pack(side="left", padx=4)
        ttk.Label(parent, wraplength=920, padding=(10, 2),
                  text="A worker is only real when its session is verified. "
                       "Slots without a verified session never receive tasks — "
                       "the remaining work stays queued.").pack(fill="x")

        self._worker_rows: dict[str, dict] = {}
        for slot in self.orch.workers.slots:
            fr = ttk.LabelFrame(parent, text=f"{slot.label} — {slot.role}",
                                padding=6)
            fr.pack(fill="x", padx=10, pady=3)
            info = tk.StringVar(value="")
            ttk.Label(fr, textvariable=info, wraplength=880,
                      font=("TkDefaultFont", 9)).pack(anchor="w")
            btns = ttk.Frame(fr); btns.pack(anchor="w", pady=2)
            wid = slot.id
            for label, cmd in [
                ("Bind window", lambda w=wid: self._w_bind(w)),
                ("Verify session", lambda w=wid: self._w_verify(w)),
                ("Prepare handoff", lambda w=wid: self._w_handoff(w)),
                ("Submitted", lambda w=wid: self._w_submitted(w)),
                ("Done — confirm", lambda w=wid: self._w_done(w)),
                ("Transfer report", lambda w=wid: self._w_report(w)),
                ("Integrate", lambda w=wid: self._w_integrate(w)),
                ("Review packet", lambda w=wid: self._w_packet(w)),
                ("Release", lambda w=wid: self._w_release(w)),
            ]:
                ttk.Button(btns, text=label, command=cmd, width=14).pack(
                    side="left", padx=2)
            self._worker_rows[wid] = {"frame": fr, "info": info,
                                      "label": slot.label}
        ttk.Label(parent, text="Integration log", padding=(10, 0)).pack(anchor="w")
        self._gate_view = scrolledtext.ScrolledText(parent, height=8,
                                                    state="disabled")
        self._gate_view.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _w_say(self, res) -> None:
        self.log.append("ui", res.detail)
        if not res.ok:
            messagebox.showinfo("Workers", res.detail)
        self._refresh_workers()

    def _w_schedule(self) -> None:
        report = self.orch.schedule()
        lines = [f"Verified sessions: {report['verified_sessions']}/5 "
                 f"(concurrency limit {report['concurrency_limit']})"]
        for tid, wid in report["assigned"]:
            lines.append(f"  {tid} -> {wid}")
        for b in report["blocked"]:
            lines.append(f"  BLOCKED {b['task']}: {b['reason']}")
        self.log.append("orchestrator", "Schedule: " + " | ".join(lines))
        messagebox.showinfo("Schedule", "\n".join(lines))
        self._refresh_workers()

    def _w_bind(self, wid: str) -> None:
        wins = muse_link.list_windows("")
        if not wins:
            messagebox.showinfo("Bind window", "No windows found. Open the Muse window first.")
            return
        names = "\n".join(f"{i+1}. {w.title[:80]}" for i, w in enumerate(wins[:15]))
        choice = _ask_choice(self.root, "Select window",
                             f"Pick the Muse window for this worker:\n\n{names}\n\nType the number:",
                             len(wins))
        if choice is not None:
            self.orch.workers.bind_session(wid, wins[choice].title)
            ok = muse_link.focus_window(wins[choice].title)
            self.log.append("app", f"{wid} bound to '{wins[choice].title[:50]}' (focus {'ok' if ok else 'FAILED'})")
            messagebox.showinfo("Bind window",
                                f"Bound. Now press 'Verify session' and follow the probe step.")
        self._refresh_workers()

    def _w_verify(self, wid: str) -> None:
        slot = self.orch.workers.get(wid)
        if not slot or not slot.window_title:
            messagebox.showinfo("Verify session", "Bind a window first.")
            return
        probe = f"Reply with exactly this line and nothing else: {slot.label} is live"
        self._copy(probe)
        muse_link.focus_window(slot.window_title)
        reply = _ask_text(self.root, "Verify session",
                          f"1. The probe prompt was copied and {slot.label}'s window focused.\n"
                          f"2. Paste it into that Muse session and submit.\n"
                          f"3. Paste the session's reply below:\n")
        if reply is None:
            return
        ok, msg = self.orch.workers.verify_session(wid, reply)
        self.log.append("app", f"Verify {wid}: {msg}")
        messagebox.showinfo("Verify session", msg)
        self._refresh_workers()

    def _w_handoff(self, wid: str) -> None:
        self._w_say(self.orch.prepare_worker_handoff(wid))

    def _w_submitted(self, wid: str) -> None:
        self._w_say(self.orch.worker_submitted(wid))

    def _w_done(self, wid: str) -> None:
        slot = self.orch.workers.get(wid)
        if slot and not messagebox.askyesno(
                "Confirm", f"Has {slot.label}'s session ACTUALLY finished?\n\nOnly say Yes if you can see the completed work."):
            self.log.append("app", f"{wid}: completion not confirmed.")
            return
        self._w_say(self.orch.worker_done(wid))

    def _w_report(self, wid: str) -> None:
        text = _ask_text(self.root, "Transfer report",
                         "Paste the worker's completion report below (don't retype):")
        if text is None:
            return
        self._w_say(self.orch.submit_worker_report(wid, text))

    def _w_integrate(self, wid: str) -> None:
        slot = self.orch.workers.get(wid)
        if not slot:
            return
        if not messagebox.askyesno("Integrate",
                                    f"Run the integration gate for {slot.label} ({slot.task_id})?\n\n"
                                    f"This inspects the diff, runs tests/build, and merges "
                                    f"into the integration branch on success."):
            return
        self.log.append("gate", f"Gate started for {slot.task_id}…")
        def work() -> None:
            res = self.orch.integrate_worker(wid)
            self.root.after(0, lambda: self._w_say(res))
        threading.Thread(target=work, daemon=True).start()

    def _w_packet(self, wid: str) -> None:
        ok, packet = self.orch.prepare_review_packet(wid)
        if ok:
            slot = self.orch.workers.get(wid)
            _popup(self.root, f"Review packet — {slot.task_id}", packet)
            messagebox.showinfo("Review packet",
                                "Packet copied to clipboard. Paste it into ChatGPT, then use "
                                "'Attach ChatGPT review' on the Handoffs tab.")
        else:
            messagebox.showinfo("Review packet", packet)

    def _w_release(self, wid: str) -> None:
        slot = self.orch.workers.get(wid)
        if slot and slot.task_id:
            # Return the task to the queue; keep ownership clean.
            t = self.orch.queue.get(slot.task_id)
            if t:
                t.status = "backlog"; t.owner = ""; self.orch.queue.save()
            self.orch.ownership.release_task(slot.task_id)
        self.orch.workers.release(wid, "released by user")
        self.log.append("app", f"{wid} released.")
        self._refresh_workers()

    def _w_safe_shutdown(self) -> None:
        self.orch.safe_shutdown()
        messagebox.showinfo("Safe shutdown",
                            "All worker state, queues, and ownership saved.\n"
                            "Worker branches are preserved. You can close the app.")

    def _refresh_workers(self) -> None:
        if not hasattr(self, "_worker_rows"):
            return
        dash = self.orch.dashboard()
        self._sess_lbl.config(
            text=f"Verified sessions: {dash['verified_sessions']}/5   "
                 f"Concurrency limit: {dash['concurrency_limit']}")
        for row in dash["workers"]:
            wid = row["id"]
            if wid not in self._worker_rows:
                continue
            idle = row["idle_for_s"]
            idle_s = "never" if idle < 0 else f"{int(idle)}s ago"
            waiting = "  *** WAITING FOR YOU ***" if row["waiting_for_user"] else ""
            files = ", ".join(row["files"][:4]) + ("…" if len(row["files"]) > 4 else "")
            acct = row.get("account_label", "")
            acct_s = f"  account={acct} ({row.get('account_status','')})" if acct else "  account=—"
            self._worker_rows[wid]["info"].set(
                f"[{row['connection']}] state={row['state']}  task={row['task'] or '—'}  "
                f"branch={row['branch'] or '—'}{acct_s}{waiting}\n"
                f"files: {files or '—'}   last test: {row['last_test'] or '—'}   "
                f"errors={row['errors']} retries={row['retries']}   last activity: {idle_s}"
                + (f"   note: {row['note']}" if row["note"] else ""))
            # Keep the frame title in sync with label/role edits.
            slot = self.orch.workers.get(wid)
            if slot:
                self._worker_rows[wid]["frame"].config(
                    text=f"{slot.label} — {slot.role}")
        if hasattr(self, "_gate_view") and self._gate_view.winfo_exists():
            self._gate_view.config(state="normal")
            self._gate_view.delete("1.0", "end")
            for rec in self.log.tail(30):
                if rec["kind"] in ("gate", "orchestrator"):
                    self._gate_view.insert("end", f"{rec['kind']}: {rec['message']}\n")
            self._gate_view.config(state="disabled")
            self._gate_view.see("end")

    # -- accounts tab --------------------------------------------------------------
    def _build_accounts_tab(self, parent) -> None:
        top = ttk.Frame(parent, padding=10); top.pack(fill="x")
        ttk.Label(top, text="Muse accounts",
                  font=("TkDefaultFont", 12, "bold")).pack(side="left")
        self._acc_summary_lbl = ttk.Label(top, text="")
        self._acc_summary_lbl.pack(side="left", padx=16)
        ttk.Button(top, text="ADD ACCOUNT",
                   command=self._acc_add).pack(side="left", padx=4)
        ttk.Label(parent, wraplength=920, padding=(10, 2),
                  text="Each account opens in its own isolated browser profile, so logins "
                       "cannot leak into each other. You sign in through Meta's normal "
                       "login flow — the bridge never sees or stores passwords. "
                       "Then bind the account to a worker slot and verify its Muse "
                       "session on the Workers tab.").pack(fill="x")
        ttk.Label(parent, wraplength=920, padding=(10, 0), foreground="gray",
                  text="Note: the bridge cannot detect your login by itself. "
                       "After signing in, press 'Mark signed in'.").pack(fill="x")

        self._acc_rows_frame = ttk.Frame(parent, padding=6)
        self._acc_rows_frame.pack(fill="both", expand=True)
        self._acc_row_vars: dict[str, dict] = {}

    def _acc_add(self) -> None:
        label = _ask_text(self.root, "Add account",
                          "Label for this account (e.g. 'Personal FB', 'Alt FB 2'):\n"
                          "(one line)")
        if label is None:
            return
        label = label.strip().splitlines()[0] if label.strip() else ""
        if not label:
            messagebox.showinfo("Add account", "A label is required.")
            return
        acc = self.orch.accounts.add(label, provider="facebook")
        self.log.append("accounts", f"Added account '{label}' ({acc.id}).")
        self._refresh_accounts()

    def _acc_open_login(self, acc_id: str) -> None:
        from . import browser_profiles
        acc = self.orch.accounts.get(acc_id)
        if not acc:
            return
        ok, msg = browser_profiles.launch_profile(acc.profile_dir)
        self.log.append("accounts", f"Open login for '{acc.label}': {msg}")
        if ok:
            messagebox.showinfo(
                "Sign in",
                f"A browser window opened with an isolated profile for '{acc.label}'.\n\n"
                "1. Go through Meta's normal login (Facebook/Instagram/email).\n"
                "2. Open muse.ai in that same window and confirm Muse loads.\n"
                "3. Come back here and press 'Mark signed in'.")
        else:
            messagebox.showinfo("Open login", msg)

    def _acc_mark_signed_in(self, acc_id: str) -> None:
        acc = self.orch.accounts.get(acc_id)
        if not acc:
            return
        if messagebox.askyesno("Confirm",
                               f"Have you signed into '{acc.label}' in its browser window\n"
                               "and confirmed muse.ai loads there?"):
            self.orch.accounts.set_status(acc_id, "signed_in")
            self.log.append("accounts", f"'{acc.label}' marked signed in (user-confirmed).")
        self._refresh_accounts()

    def _acc_bind(self, acc_id: str) -> None:
        acc = self.orch.accounts.get(acc_id)
        if not acc:
            return
        slots = self.orch.workers.slots
        names = "\n".join(f"{i+1}. {s.id} — {s.label} ({s.role})"
                          for i, s in enumerate(slots))
        choice = _ask_choice(self.root, "Bind to worker slot",
                             f"Bind '{acc.label}' to which worker slot?\n\n{names}\n\nType the number:",
                             len(slots))
        if choice is None:
            return
        ok, msg = self.orch.bind_account_to_slot(acc_id, slots[choice].id)
        self.log.append("accounts", msg)
        messagebox.showinfo("Bind", msg + ("\n\nNext: on the Workers tab, bind the "
                                           "Muse window for that slot and Verify session."))
        self._refresh_accounts()
        self._refresh_workers()

    def _acc_unbind(self, acc_id: str) -> None:
        ok, msg = self.orch.unbind_account_from_slot(acc_id)
        self.log.append("accounts", msg)
        self._refresh_accounts()
        self._refresh_workers()

    def _acc_disconnect(self, acc_id: str) -> None:
        acc = self.orch.accounts.get(acc_id)
        if not acc:
            return
        # Disconnecting one account never touches the others.
        if acc.worker_slot:
            self.orch.workers.disconnect(acc.worker_slot)
        self.orch.accounts.set_status(acc_id, "disconnected",
                                      "user disconnected; others unaffected")
        self.log.append("accounts", f"'{acc.label}' disconnected.")
        self._refresh_accounts()
        self._refresh_workers()

    def _acc_reconnect(self, acc_id: str) -> None:
        self._acc_open_login(acc_id)
        self.orch.accounts.set_status(acc_id, "added",
                                      "re-login launched; mark signed in when done")

    def _acc_remove(self, acc_id: str) -> None:
        acc = self.orch.accounts.get(acc_id)
        if not acc:
            return
        if not messagebox.askyesno("Remove account",
                                   f"Remove '{acc.label}'?\n\nThis unlinks it from any worker slot. "
                                   "Other accounts are not affected."):
            return
        if acc.worker_slot:
            self.orch.unbind_account_from_slot(acc_id)
            self.orch.workers.unlink_account(acc.worker_slot)
        keep = messagebox.askyesno("Browser profile",
                                   "Also delete its isolated browser profile?")
        self.orch.accounts.remove(acc_id, delete_profile=keep)
        self.log.append("accounts", f"'{acc.label}' removed.")
        self._refresh_accounts()
        self._refresh_workers()

    def _refresh_accounts(self) -> None:
        if not hasattr(self, "_acc_rows_frame"):
            return
        for w in self._acc_rows_frame.winfo_children():
            w.destroy()
        summary = self.orch.accounts.summary()
        self._acc_summary_lbl.config(
            text=f"{summary['total']} account(s) — "
                 + ", ".join(f"{k}: {v}" for k, v in summary["by_status"].items()))
        for acc in self.orch.accounts.accounts:
            fr = ttk.LabelFrame(self._acc_rows_frame,
                                text=f"{acc.label}  [{acc.provider}]",
                                padding=6)
            fr.pack(fill="x", padx=6, pady=3)
            status_line = (f"status={acc.status}   "
                           f"slot={acc.worker_slot or '—'}")
            if acc.note:
                status_line += f"   note: {acc.note}"
            ttk.Label(fr, text=status_line, wraplength=880).pack(anchor="w")
            btns = ttk.Frame(fr); btns.pack(anchor="w", pady=2)
            aid = acc.id
            for label, cmd in [
                ("Open login", lambda a=aid: self._acc_open_login(a)),
                ("Mark signed in", lambda a=aid: self._acc_mark_signed_in(a)),
                ("Bind to slot", lambda a=aid: self._acc_bind(a)),
                ("Unbind", lambda a=aid: self._acc_unbind(a)),
                ("Disconnect", lambda a=aid: self._acc_disconnect(a)),
                ("Reconnect", lambda a=aid: self._acc_reconnect(a)),
                ("Remove", lambda a=aid: self._acc_remove(a)),
            ]:
                ttk.Button(btns, text=label, command=cmd, width=14).pack(
                    side="left", padx=2)

    # -- github tab ----------------------------------------------------------------
    def _build_github_tab(self, parent) -> None:
        top = ttk.Frame(parent, padding=10); top.pack(fill="x")
        ttk.Label(top, text="GitHub — read-only",
                  font=("TkDefaultFont", 12, "bold")).pack(side="left")
        self._gh_status_lbl = ttk.Label(top, text="", wraplength=600)
        self._gh_status_lbl.pack(side="left", padx=16)
        ttk.Button(top, text="REFRESH",
                   command=self._gh_refresh).pack(side="left", padx=4)
        ttk.Label(parent, wraplength=920, padding=(10, 2), foreground="gray",
                  text="Read-only by default. Authenticated access uses the official "
                       "GitHub CLI (gh) — run `gh auth login` once; the bridge never "
                       "handles your token. ChatGPT's own GitHub connector is separate "
                       "and does not give this app any access.").pack(fill="x")

        self._gh_info = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self._gh_info, wraplength=920,
                  padding=(10, 2), font=("TkDefaultFont", 9)).pack(fill="x")

        mid = ttk.Frame(parent, padding=(10, 0)); mid.pack(fill="both", expand=True)
        left = ttk.LabelFrame(mid, text="Commit history", padding=6)
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self._gh_history = tk.Listbox(left, height=12)
        self._gh_history.pack(fill="both", expand=True)
        right = ttk.LabelFrame(mid, text="Branches", padding=6)
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self._gh_branches = tk.Listbox(right, height=12)
        self._gh_branches.pack(fill="both", expand=True)

        bot = ttk.Frame(parent, padding=10); bot.pack(fill="x")
        ttk.Button(bot, text="COMPARE SELECTED BRANCH",
                   command=self._gh_compare).pack(side="left", padx=4)
        ttk.Button(bot, text="REVIEW PACKET FOR SELECTED",
                   command=self._gh_packet).pack(side="left", padx=4)
        self._gh_diff = scrolledtext.ScrolledText(parent, height=10,
                                                  state="disabled")
        self._gh_diff.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _gh_conn(self):
        from .github import GitHubConnection
        repo = self.orch.repo()
        return GitHubConnection(repo) if repo else None

    def _gh_refresh(self) -> None:
        from .github import gh_auth_status
        conn = self._gh_conn()
        auth = gh_auth_status()
        if conn is None:
            self._gh_status_lbl.config(text="No game repo configured.")
            return
        ident = conn.identity()
        latest = conn.latest_commit()
        remote = conn.remote_info()
        status_txt = (f"{'✓ ' if auth.connected else '○ '}{auth.detail}")
        self._gh_status_lbl.config(text=status_txt)
        info = (f"{ident['owner']}/{ident['repo'] or '(local only)'}   "
                f"branch={ident['branch']}   HEAD={ident['head']}")
        if remote.get("visibility"):
            info += f"   visibility={remote['visibility']}"
        self._gh_info.set(info)
        self._gh_history.delete(0, "end")
        for c in conn.commit_history(20):
            self._gh_history.insert("end",
                                    f"{c['short']} {c['date']} {c['subject'][:70]}")
        self._gh_branches.delete(0, "end")
        br = conn.branches()
        self._bridge_branches = br["local"]
        for b in br["local"]:
            mark = "  [worker]" if b.startswith("bridge/") else ""
            self._gh_branches.insert("end", b + mark)
        self.log.append("github", f"Refreshed: {ident['branch']}@{ident['head']}")

    def _gh_compare(self) -> None:
        conn = self._gh_conn()
        if conn is None or not hasattr(self, "_bridge_branches"):
            return
        sel = self._gh_branches.curselection()
        if not sel:
            messagebox.showinfo("Compare", "Select a branch first.")
            return
        branch = self._bridge_branches[sel[0]]
        base = "bridge/integration" if branch.startswith("bridge/") else "main"
        # If the integration branch doesn't exist yet, compare against main.
        diff = conn.compare_branch(branch, base)
        if not diff["files"] and base != "main":
            diff = conn.compare_branch(branch, "main")
            base = "main"
        self._gh_diff.config(state="normal")
        self._gh_diff.delete("1.0", "end")
        self._gh_diff.insert("end", f"{branch} vs {base}\n\n{diff['stat']}\n")
        self._gh_diff.config(state="disabled")
        self.log.append("github", f"Compared {branch} vs {base}: {len(diff['files'])} files")

    def _gh_packet(self) -> None:
        if not hasattr(self, "_bridge_branches"):
            return
        sel = self._gh_branches.curselection()
        if not sel:
            messagebox.showinfo("Review packet", "Select a worker branch first.")
            return
        branch = self._bridge_branches[sel[0]]
        # Find the task that owns this branch.
        task = next((t for t in self.orch.queue.tasks if t.branch == branch), None)
        slot = next((s for s in self.orch.workers.slots if s.branch == branch), None)
        if not task:
            messagebox.showinfo("Review packet", "No task found for that branch.")
            return
        ok, packet = self.orch.prepare_review_packet(slot.id if slot else "w1")
        if ok:
            _popup(self.root, f"Review packet — {task.id}", packet)
            self.log.append("github", f"Review packet prepared for {task.id} (copied).")
        else:
            messagebox.showinfo("Review packet", packet)

    # -- dashboard actions -------------------------------------------------------
    def _say(self, res: StepResult) -> None:
        self.log.append("ui", res.detail)
        if not res.ok:
            messagebox.showinfo("Muse Bridge", res.detail)

    def _start(self) -> None:
        self._say(self.coord.next_handoff())

    def _next_handoff(self) -> None:
        self._say(self.coord.next_handoff())

    def _pause(self) -> None:
        self.coord.pause()
        self.log.append("app", "Paused. No new handoffs will start.")

    def _resume(self) -> None:
        self.coord.resume()

    def _stop(self) -> None:
        self.coord.stop()
        self.log.append("app", "Stopped. History preserved.")

    def _copy_prompt(self) -> None:
        h = self.coord.current
        if h:
            self._copy(h.prompt)
            self.log.append("ui", f"Prompt re-copied ({h.title}).")
        else:
            messagebox.showinfo("Muse Bridge", "No prepared prompt right now — press NEXT HANDOFF.")

    def _copy_review(self) -> None:
        res = self.coord.prepare_review()
        self._say(res)

    def _open_win(self, which: str) -> None:
        key = "chatgpt_window_title" if which == "chatgpt" else "muse_window_title"
        title = self.settings.get(key, "")
        if not title:
            messagebox.showinfo("Muse Bridge", "No window saved — run setup again.")
            return
        ok = muse_link.focus_window(title)
        self.log.append("app", f"Focus {which}: {'ok' if ok else 'FAILED'}")
        if not ok:
            messagebox.showinfo("Muse Bridge", f"Could not focus the {which} window. Is it open?")

    def _open_repo(self) -> None:
        p = self.settings.get("game_repo_path", "")
        if p:
            _open_path(p)
        else:
            messagebox.showinfo("Muse Bridge", "No repo folder saved.")

    def _muse_submitted(self) -> None:
        self._say(self.coord.mark_muse_submitted())

    def _muse_done(self) -> None:
        res = self.coord.mark_muse_done()
        if res.ok and not messagebox.askyesno(
                "Confirm", "Has Muse ACTUALLY finished the task?\n\n"
                           "Only say Yes if you can see the completed work."):
            # User wasn't sure — roll back to MUSE_RUNNING.
            self.coord.tracker.transition(HandoffState.MUSE_RUNNING,
                                          "user unsure; back to running")
            self.log.append("app", "Completion not confirmed; still MUSE_RUNNING.")
            return
        self._say(res)

    def _run_verify(self) -> None:
        self.log.append("app", "Running tests + build…")
        def work() -> None:
            res = self.coord.run_verification()
            self.root.after(0, lambda: self._say(res))
        threading.Thread(target=work, daemon=True).start()

    def _save_transfer(self) -> None:
        text = self._transfer.get("1.0", "end")
        h = self.coord.current
        if not h:
            messagebox.showinfo("Muse Bridge", "Nothing is waiting for a response right now.")
            return
        if h.stage == "plan":
            res = self.coord.submit_planning_response(text)
        elif h.stage == "dev":
            res = self.coord.submit_muse_report(text)
            if res.ok:
                # Chain verification automatically, then guide to review.
                vres = self.coord.run_verification()
                res = StepResult(
                    vres.ok,
                    res.detail + "\n\nVerification:\n" + vres.detail[:800]
                    + "\n\nPress COPY RESPONSE FOR REVIEW when ready.")
        elif h.stage == "review":
            res = self.coord.submit_review(text)
        else:
            res = StepResult(False, "Unknown handoff stage.")
        self._transfer.delete("1.0", "end")
        self._say(res)

    def _attach_review(self) -> None:
        text = self._transfer.get("1.0", "end")
        if not text.strip():
            messagebox.showinfo("Attach review", "Paste the ChatGPT review first.")
            return
        # Attach to the current handoff's task, else ask which task.
        task_id = self.coord.current.task_id if self.coord.current else ""
        if not task_id:
            cands = [t for t in self.orch.queue.tasks
                     if t.status in ("active", "completed")]
            if not cands:
                messagebox.showinfo("Attach review", "No active/completed tasks.")
                return
            names = "\n".join(f"{i+1}. {t.id} — {t.title[:60]}"
                              for i, t in enumerate(cands))
            choice = _ask_choice(self.root, "Attach review",
                                 f"Attach to which task?\n\n{names}\n\nType the number:",
                                 len(cands))
            if choice is None:
                return
            task_id = cands[choice].id
        ok, msg = self.orch.attach_chatgpt_review(task_id, text)
        self.log.append("ui", msg)
        if ok:
            self._transfer.delete("1.0", "end")
            messagebox.showinfo("Attach review", msg)
        else:
            messagebox.showinfo("Attach review", msg)

    def _view_queue(self) -> None:
        lines = ["TASK QUEUE", ""]
        for t in self.coord.queue.all():
            lines.append(f"[{t.status}] {t.id} — {t.title}")
            if t.brief:
                lines.append(f"    {t.brief[:100]}")
        _popup(self.root, "Task queue", "\n".join(lines))

    def _view_history(self) -> None:
        lines = ["HISTORY (state transitions + checkpoints)", ""]
        for tr in self.coord.tracker.history[-40:]:
            lines.append(f"{tr.from_state} -> {tr.to_state}  ({tr.reason})")
        lines.append("")
        for cp in self.coord.checkpoints.all()[-10:]:
            lines.append(f"checkpoint {cp['commit'][:8]} — {cp['note']}")
        _popup(self.root, "History", "\n".join(lines))

    def _export_diag(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile="muse-bridge-diagnostics.txt",
            title="Export diagnostics")
        if not path:
            return
        prof = self.profile.load()
        with open(path, "w", encoding="utf-8") as f:
            f.write("MUSE BRIDGE DIAGNOSTICS (no secrets are stored by this app)\n\n")
            f.write(self.coord.describe_status() + "\n\n")
            f.write("PROJECT SUMMARY:\n" + prof.get("project_summary", "(none)") + "\n\n")
            f.write("QUEUE:\n")
            for t in self.queue.all():
                f.write(f"  [{t.status}] {t.id} {t.title}\n")
            f.write("\nLOG (last 200):\n")
            for rec in self.log.tail(200):
                f.write(f"  {rec['kind']}: {rec['message']}\n")
        self.log.append("app", f"Diagnostics exported to {path}")
        messagebox.showinfo("Muse Bridge", f"Diagnostics saved to:\n{path}")

    # -- refresh loop --------------------------------------------------------------
    def _tick(self) -> None:
        try:
            self._refresh()
            self._refresh_workers()
            self._refresh_accounts()
        finally:
            if self.root.winfo_exists():
                self.root.after(2000, self._tick)

    def _guidance(self) -> str:
        s = self.coord.tracker.state
        h = self.coord.current
        guide = {
            HandoffState.READY: "Press NEXT HANDOFF to prepare the next step.",
            HandoffState.PREPARED: f"Prompt ready: {h.title if h else ''} — copied to clipboard.",
            HandoffState.WAITING_FOR_USER: (h.user_instruction if h else ""),
            HandoffState.MUSE_RUNNING: "Muse is working. Press MUSE DONE — I CONFIRM only when it has actually finished.",
            HandoffState.AWAITING_REPORT: "Paste Muse's completion report below, then press SAVE TRANSFERRED RESPONSE.",
            HandoffState.REVIEW_PENDING: "Review prompt copied — paste it into ChatGPT, then transfer the review back below.",
            HandoffState.TASK_PASSED: "Task passed and checkpointed. Press NEXT HANDOFF for the next task.",
            HandoffState.TASK_FAILED: "Task failed. Press NEXT HANDOFF for the correction task.",
            HandoffState.PAUSED: "Paused. Press RESUME to continue.",
            HandoffState.BLOCKED: "Blocked — needs your decision. Check the log.",
            HandoffState.STOPPED: "Stopped. History preserved. Press START when ready.",
        }
        return guide.get(s, "")

    def _refresh(self) -> None:
        if not (hasattr(self, "_state_lbl") and self._state_lbl.winfo_exists()):
            return
        st = self.coord.tracker.state.value
        self._state_lbl.config(text=st)
        self._next_action.config(text="Next: " + self._guidance())
        active = self.queue.active()
        counts = self.queue.counts()
        cps = self.coord.checkpoints.all()
        h = self.coord.current
        waiting = {
            HandoffState.WAITING_FOR_USER: "you (paste into ChatGPT/Muse)",
            HandoffState.MUSE_RUNNING: "Muse",
            HandoffState.AWAITING_REPORT: "you (transfer Muse's report)",
            HandoffState.REVIEW_PENDING: "you (transfer ChatGPT's review)",
        }.get(self.coord.tracker.state, "—")
        self._info_vars["Current task"].set(active.title if active else "none")
        self._info_vars["Current handoff"].set(
            f"{h.stage}: {h.title}" if h else "none")
        self._info_vars["Waiting on"].set(waiting)
        self._info_vars["Last completed"].set(
            cps[-1]["note"] if cps else "none")
        self._info_vars["Queue"].set(
            f"{counts.get('backlog',0)} queued, {counts.get('active',0)} active, "
            f"{counts.get('completed',0)} done, {counts.get('failed',0)} failed")
        self._info_vars["Saved progress"].set(
            f"{len(cps)} checkpoint(s) · state saved after every handoff")
        self._log_view.config(state="normal")
        self._log_view.delete("1.0", "end")
        for rec in self.log.tail(40):
            self._log_view.insert("end", f"{rec['kind']}: {rec['message']}\n")
        self._log_view.config(state="disabled")
        self._log_view.see("end")


def _popup(root: tk.Tk, title: str, text: str) -> None:
    win = tk.Toplevel(root)
    win.title(title)
    win.geometry("640x420")
    st = scrolledtext.ScrolledText(win, state="normal")
    st.pack(fill="both", expand=True, padx=8, pady=8)
    st.insert("1.0", text)
    st.config(state="disabled")


def _ask_text(root: tk.Tk, title: str, prompt: str) -> str | None:
    """Multi-line paste dialog. Returns text or None if cancelled."""
    result: dict[str, str | None] = {"v": None}
    win = tk.Toplevel(root)
    win.title(title)
    win.geometry("600x400")
    ttk.Label(win, text=prompt, wraplength=560, padding=10).pack(anchor="w")
    st = scrolledtext.ScrolledText(win, height=10)
    st.pack(fill="both", expand=True, padx=10)
    def ok() -> None:
        result["v"] = st.get("1.0", "end")
        win.destroy()
    def cancel() -> None:
        win.destroy()
    row = ttk.Frame(win); row.pack(pady=8)
    ttk.Button(row, text="Save", command=ok).pack(side="left", padx=6)
    ttk.Button(row, text="Cancel", command=cancel).pack(side="left")
    win.transient(root)
    win.grab_set()
    root.wait_window(win)
    return result["v"]


def _ask_choice(root: tk.Tk, title: str, prompt: str, n: int) -> int | None:
    """Tiny numbered-choice dialog. Returns 0-based index or None."""
    result: dict[str, int | None] = {"v": None}
    win = tk.Toplevel(root)
    win.title(title)
    win.geometry("560x380")
    ttk.Label(win, text=prompt, wraplength=520,
              padding=10).pack(anchor="w")
    var = tk.StringVar()
    ttk.Entry(win, textvariable=var, width=10).pack(anchor="w", padx=10)
    def ok() -> None:
        try:
            i = int(var.get().strip()) - 1
            result["v"] = i if 0 <= i < n else None
        except ValueError:
            result["v"] = None
        win.destroy()
    ttk.Button(win, text="Select", command=ok).pack(pady=10)
    win.transient(root)
    win.grab_set()
    root.wait_window(win)
    return result["v"]


def main() -> None:
    root = tk.Tk()
    BridgeApp(root)
    root.mainloop()
