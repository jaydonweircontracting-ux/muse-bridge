"""Tkinter GUI for Muse Bridge: first-run wizard + control dashboard.

Keep it simple and readable: status, task, controls, logs. No charts.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path

from . import git_ops, muse_link
from .chatgpt import ChatGPTApi, ManualRelay
from .runner import Runner
from .state import ResponseState
from .store import Brief, Checkpoints, EventLog, Settings, Task, TaskQueue


class BridgeApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Muse Bridge — Autonomous RPG Development Bot")
        self.root.geometry("900x650")
        self.settings = Settings()
        self.log = EventLog()
        self.queue = TaskQueue()
        self.checkpoints = Checkpoints()
        self.brief = Brief()
        self.runner = Runner(self.settings, self.log, self.queue,
                             self.checkpoints, self.brief)
        self.manual = ManualRelay()
        self._stop_flag = False
        self._wire_integrations()
        if not self.settings.get("first_run_complete"):
            self.show_wizard()
        else:
            self.show_dashboard()

    # -- integrations --------------------------------------------------------
    def _wire_integrations(self) -> None:
        mode = self.settings.get("chatgpt_mode", "api")
        if mode == "api":
            api = ChatGPTApi(self.settings.get("openai_api_key", ""),
                             self.settings.get("openai_model", "gpt-4o-mini"))
            self.runner.plan_task = lambda t: self._api_plan(api, t)
            self.runner.review = lambda ev: self._api_review(api, ev)
        else:
            self.runner.plan_task = lambda t: ("Manual relay: user provides criteria.", "manual")
            self.runner.review = lambda ev: (False, "Manual relay: user reviews in UI.")
        self.runner.send_to_muse = self._send_to_muse
        self.runner.wait_muse_done = self._wait_muse_done
        self.runner.verify_checks = self._verify_checks

    def _api_plan(self, api: ChatGPTApi, task: Task) -> tuple[str, str]:
        r = api.complete(
            f"Define short, testable acceptance criteria for this game-dev task.\n"
            f"Task: {task.title}\n{task.brief}\nReply with 3-6 bullet criteria only.",
            system="You are a strict QA planner for a 2.5D isometric RPG codebase.",
            max_tokens=400)
        return (r.text if r.ok else task.acceptance, "chatgpt-api" if r.ok else r.error)

    def _api_review(self, api: ChatGPTApi, evidence: str) -> tuple[bool, str]:
        r = api.complete(
            f"Review this development evidence. Reply 'APPROVE' or 'REJECT' first, then reasons.\n\n{evidence[:3000]}",
            system="You are a strict code reviewer. Approve only if tests/build pass, the diff is clean, and the acceptance criteria are met by the evidence.",
            max_tokens=400)
        if not r.ok:
            return False, f"Reviewer error: {r.error}"
        txt = r.text.strip()
        return txt.upper().startswith("APPROVE"), txt[:300]

    def _send_to_muse(self, text: str) -> bool:
        title = self.settings.get("muse_window_title", "")
        if not title or not muse_link.focus_window(title):
            self.log.append("muse", "Could not focus Muse window.")
            return False
        ok = muse_link.paste_text(text)
        if ok:
            try:
                import pyautogui
                pyautogui.press("enter")
            except Exception:
                pass
        return ok

    def _wait_muse_done(self) -> str:
        watch = muse_link.CompletionWatch(
            quiet_s=25.0, poll_s=2.0)
        timeout = float(self.settings.get("response_timeout_s", 600))
        return watch.wait(timeout_s=timeout, should_stop=lambda: self._stop_flag)

    def _verify_checks(self, repo: Path) -> list[list[str]]:
        # Generic, safe defaults; the project brief can specialize later.
        return [[ "npx", "tsc", "--noEmit"], ["npm", "run", "build"]]

    # -- wizard ----------------------------------------------------------------
    def _clear(self) -> None:
        for w in self.root.winfo_children():
            w.destroy()

    def show_wizard(self) -> None:
        self._clear()
        f = ttk.Frame(self.root, padding=20)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Muse Bridge — first-run setup",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 10))

        # 1. Repo
        ttk.Label(f, text="1. Game repository folder").pack(anchor="w")
        row = ttk.Frame(f); row.pack(fill="x", pady=4)
        self._repo_var = tk.StringVar(value=self.settings.get("game_repo_path", ""))
        ttk.Entry(row, textvariable=self._repo_var, width=60).pack(side="left")
        ttk.Button(row, text="Browse…", command=self._browse_repo).pack(side="left", padx=6)
        self._repo_status = ttk.Label(f, text=""); self._repo_status.pack(anchor="w")

        # 2. Muse window
        ttk.Label(f, text="2. Muse window").pack(anchor="w", pady=(10, 0))
        row2 = ttk.Frame(f); row2.pack(fill="x", pady=4)
        self._muse_var = tk.StringVar(value=self.settings.get("muse_window_title", ""))
        ttk.Entry(row2, textvariable=self._muse_var, width=60).pack(side="left")
        ttk.Button(row2, text="Detect windows…", command=self._detect_windows).pack(side="left", padx=6)
        caps = muse_link.capabilities()
        ttk.Label(f, text=f"Muse completion detection: {caps.completion_detection.upper()} — {caps.detail}",
                  wraplength=800, foreground="orange" if caps.completion_detection != "api" else "green").pack(anchor="w")

        # 3. ChatGPT
        ttk.Label(f, text="3. ChatGPT connection").pack(anchor="w", pady=(10, 0))
        self._gpt_mode = tk.StringVar(value=self.settings.get("chatgpt_mode", "api"))
        ttk.Radiobutton(f, text="Official OpenAI API (automatic)", variable=self._gpt_mode,
                        value="api").pack(anchor="w")
        ttk.Radiobutton(f, text="ChatGPT website (manual relay — you copy answers back)",
                        variable=self._gpt_mode, value="manual").pack(anchor="w")
        row3 = ttk.Frame(f); row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="API key:").pack(side="left")
        self._key_var = tk.StringVar(value=self.settings.get("openai_api_key", ""))
        ttk.Entry(row3, textvariable=self._key_var, width=50, show="•").pack(side="left", padx=6)
        ttk.Button(row3, text="Test", command=self._test_api).pack(side="left")
        self._api_status = ttk.Label(f, text=""); self._api_status.pack(anchor="w")

        # 4. Brief
        ttk.Label(f, text="4. Paste your ONE starter prompt (project brief)").pack(anchor="w", pady=(10, 0))
        self._brief_text = scrolledtext.ScrolledText(f, height=10)
        self._brief_text.pack(fill="both", expand=True, pady=4)
        existing = self.brief.load().get("text", "")
        if existing:
            self._brief_text.insert("1.0", existing)

        btn = ttk.Frame(f); btn.pack(fill="x", pady=10)
        ttk.Button(btn, text="Save and open dashboard", command=self._wizard_finish).pack(side="right")

    def _browse_repo(self) -> None:
        d = filedialog.askdirectory(title="Select game repository folder")
        if not d:
            return
        self._repo_var.set(d)
        if git_ops.is_git_repo(d):
            br = git_ops.branch(d)
            self._repo_status.config(text=f"✓ Git repo, branch {br}", foreground="green")
            self.settings.set("game_repo_path", d)
            self.settings.set("game_repo_verified", True)
        else:
            self._repo_status.config(text="✗ Not a git repository.", foreground="red")
            self.settings.set("game_repo_verified", False)

    def _detect_windows(self) -> None:
        wins = muse_link.list_windows("Muse")
        if not wins:
            messagebox.showinfo("Windows", "No window with 'Muse' in the title found.\nOpen Muse first, then try again.")
            return
        pick = wins[0].title
        self._muse_var.set(pick)
        self.settings.set("muse_window_title", pick)
        messagebox.showinfo("Windows", f"Selected: {pick}\n\nUse TEST CONNECTIONS on the dashboard to verify paste.")

    def _test_api(self) -> None:
        api = ChatGPTApi(self._key_var.get().strip())
        self._api_status.config(text="Testing…")
        def run() -> None:
            r = api.test_connection()
            msg = "✓ API works." if r.ok else f"✗ {r.error}"
            self.root.after(0, lambda: self._api_status.config(
                text=msg, foreground="green" if r.ok else "red"))
        threading.Thread(target=run, daemon=True).start()

    def _wizard_finish(self) -> None:
        self.settings.set("chatgpt_mode", self._gpt_mode.get())
        if self._key_var.get().strip():
            self.settings.set("openai_api_key", self._key_var.get().strip())
        txt = self._brief_text.get("1.0", "end").strip()
        if txt:
            self.brief.save_text(txt)
        self.settings.set("first_run_complete", True)
        self._wire_integrations()
        self.log.append("app", "First-run setup complete.")
        self.show_dashboard()

    # -- dashboard ---------------------------------------------------------------
    def show_dashboard(self) -> None:
        self._clear()
        top = ttk.Frame(self.root, padding=10); top.pack(fill="x")
        ttk.Label(top, text="Muse Bridge", font=("TkDefaultFont", 14, "bold")).pack(side="left")
        self._state_lbl = ttk.Label(top, text="READY", font=("TkDefaultFont", 11, "bold"))
        self._state_lbl.pack(side="left", padx=20)

        btns = ttk.Frame(self.root, padding=10); btns.pack(fill="x")
        for label, cmd in [
            ("START", self._start), ("PAUSE", self._pause), ("RESUME", self._resume),
            ("STOP", self._stop), ("RUN ONE CYCLE", self._one_cycle),
            ("TEST CONNECTIONS", self._test_connections),
        ]:
            ttk.Button(btns, text=label, command=cmd).pack(side="left", padx=4)

        info = ttk.Frame(self.root, padding=10); info.pack(fill="x")
        self._info_vars: dict[str, tk.StringVar] = {}
        for key in ["Active task", "Queue", "Last checkpoint", "Cycles ok/failed", "Muse link", "ChatGPT"]:
            ttk.Label(info, text=key + ":").grid(sticky="w", padx=4)
            v = tk.StringVar(value="—")
            self._info_vars[key] = v
            ttk.Label(info, textvariable=v).grid(row=list(self._info_vars).index(key), column=1, sticky="w")

        ttk.Label(self.root, text="Log", padding=(10, 0)).pack(anchor="w")
        self._log_view = scrolledtext.ScrolledText(self.root, height=18, state="disabled")
        self._log_view.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._refresh()
        self.root.after(2000, self._tick)

    def _tick(self) -> None:
        try:
            self._refresh()
        finally:
            if self.root.winfo_exists():
                self.root.after(2000, self._tick)

    def _refresh(self) -> None:
        st = self.runner.tracker.state.value
        if hasattr(self, "_state_lbl") and self._state_lbl.winfo_exists():
            self._state_lbl.config(text=st)
            active = self.queue.active()
            counts = self.queue.counts()
            cp = self.checkpoints.latest()
            self._info_vars["Active task"].set(active.title if active else "none")
            self._info_vars["Queue"].set(str(counts))
            self._info_vars["Last checkpoint"].set(cp["commit"] if cp else "none")
            self._info_vars["Cycles ok/failed"].set(f"{self.runner.cycles_ok}/{self.runner.cycles_failed}")
            self._info_vars["Muse link"].set(
                f"{self.settings.get('muse_window_title', '—')} [{self.settings.get('muse_completion_mode')}]")
            self._info_vars["ChatGPT"].set(self.settings.get("chatgpt_mode", "api"))
            self._log_view.config(state="normal")
            self._log_view.delete("1.0", "end")
            for rec in self.log.tail(60):
                self._log_view.insert("end", f"{rec['kind']}: {rec['message']}\n")
            self._log_view.config(state="disabled")
            self._log_view.see("end")

    def _run_loop(self, max_cycles: int) -> None:
        def worker() -> None:
            n = 0
            while n < max_cycles and not self._stop_flag:
                res = self.runner.run_one_cycle(should_stop=lambda: self._stop_flag)
                if res is None:
                    break
                n += 1
                if not res.success and self.settings.get("pause_after_failure"):
                    break
            self._stop_flag = False
        self._stop_flag = False
        threading.Thread(target=worker, daemon=True).start()

    def _start(self) -> None:
        limit = int(self.settings.get("max_cycles_per_session", 10))
        self.log.append("app", f"START pressed; up to {limit} cycles.")
        self._run_loop(limit)

    def _one_cycle(self) -> None:
        threading.Thread(
            target=lambda: self.runner.run_one_cycle(should_stop=lambda: self._stop_flag),
            daemon=True).start()

    def _pause(self) -> None:
        self.runner.tracker.transition(ResponseState.PAUSED, "user pressed PAUSE")
        self.log.append("app", "Paused. No new tasks will start.")

    def _resume(self) -> None:
        self.runner.tracker.transition(ResponseState.READY, "user pressed RESUME")

    def _stop(self) -> None:
        self._stop_flag = True
        self.runner.tracker.transition(ResponseState.STOPPED, "user pressed STOP")
        self.log.append("app", "Stop requested.")

    def _test_connections(self) -> None:
        msgs = []
        repo = self.runner.repo()
        msgs.append(f"Repo: {'OK ' + str(repo) if repo else 'NOT CONFIGURED/VERIFIED'}")
        caps = muse_link.capabilities()
        if self.settings.get("muse_window_title"):
            ok = muse_link.focus_window(self.settings.get("muse_window_title", ""))
            msgs.append(f"Muse window focus: {'OK' if ok else 'FAILED'} "
                        f"(completion detection: {caps.completion_detection})")
        else:
            msgs.append("Muse window: not selected")
        if self.settings.get("chatgpt_mode") == "api":
            api = ChatGPTApi(self.settings.get("openai_api_key", ""))
            r = api.test_connection()
            msgs.append(f"ChatGPT API: {'OK' if r.ok else 'FAILED: ' + r.error}")
        else:
            msgs.append("ChatGPT: manual relay mode (you copy answers back)")
        messagebox.showinfo("Connection test", "\n".join(msgs))
        self.log.append("app", "Connection test: " + " | ".join(msgs))


def main() -> None:
    root = tk.Tk()
    BridgeApp(root)
    root.mainloop()
