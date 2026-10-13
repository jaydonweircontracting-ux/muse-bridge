"""Tkinter GUI for Muse Bridge: first-run wizard + control dashboard.

Keep it simple and readable: status, task, controls, logs. No charts.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path

from . import git_ops, muse_link
from .chatgpt import ChatGPTApi, ManualRelay, UsageLedger, DEFAULT_MODEL, PRICE_TABLE, PRICES_AS_OF
from .runner import Runner
from .state import ResponseState
from .store import Brief, Checkpoints, EventLog, Settings, Task, TaskQueue
from .project_state import ProjectState
import time


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
        self.project_state = ProjectState()
        self.ledger = UsageLedger(self.settings.dir / "usage.jsonl")
        self.session_start = time.time()
        self.runner = Runner(self.settings, self.log, self.queue,
                             self.checkpoints, self.brief,
                             project_state=self.project_state,
                             ledger=self.ledger,
                             session_start=self.session_start)
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
            api = self._make_api()
            self.runner.plan_task = lambda t: self._api_plan(api, t)
            self.runner.review = lambda ev: self._api_review(api, ev)
        else:
            self.runner.plan_task = lambda t: ("Manual relay: user provides criteria.", "manual")
            self.runner.review = lambda ev: (False, "Manual relay: user reviews in UI.")
        self.runner.send_to_muse = self._send_to_muse
        self.runner.wait_muse_done = self._wait_muse_done
        self.runner.verify_checks = self._verify_checks

    def _make_api(self) -> ChatGPTApi:
        return ChatGPTApi(
            self.settings.get("openai_api_key", ""),
            self.settings.get("openai_model", DEFAULT_MODEL),
            ledger=self.ledger,
            price_overrides=self.settings.get("price_overrides", {}))

    def _api_plan(self, api: ChatGPTApi, task: Task) -> tuple[str, str]:
        state = self.project_state.summary_prompt_block()
        r = api.complete(
            f"{state}\n\nDefine short, testable acceptance criteria for this game-dev task.\n"
            f"Task: {task.title}\n{task.brief}\nReply with 3-6 bullet criteria only.",
            instructions="You are a strict QA planner for a 2.5D isometric RPG codebase.",
            max_output_tokens=400, purpose="plan")
        return (r.text if r.ok else task.acceptance, "chatgpt-api" if r.ok else r.error)

    def _api_review(self, api: ChatGPTApi, evidence: str) -> tuple[bool, str]:
        r = api.complete(
            f"Review this development evidence. Reply 'APPROVE' or 'REJECT' first, then reasons.\n\n{evidence[:3000]}\n\n"
            + ProjectState.update_instruction(),
            instructions="You are a strict code reviewer. Approve only if tests/build pass, the diff is clean, and the acceptance criteria are met by the evidence.",
            max_output_tokens=600, purpose="review")
        if not r.ok:
            return False, f"Reviewer error: {r.error}"
        summary = ProjectState.extract_summary(r.text)
        if summary:
            self.project_state.save_summary(summary)
            self.log.append("state", "Project-state summary updated by reviewer.")
        txt = r.text.strip()
        return txt.upper().startswith("APPROVE"), txt[:300]

    def _send_to_muse(self, text: str) -> bool:
        """Level 1: Muse Code CLI. Level 2: window paste (fallback)."""
        mode = self.settings.get("muse_mode", "cli")
        repo = self.runner.repo()
        if mode == "cli" and repo is not None:
            cli = muse_link.MuseCodeCli(
                self.settings.get("muse_cli_template", ""))
            res = cli.run_task(text, str(repo),
                               timeout_s=int(self.settings.get("task_time_limit_s", 3600)))
            self.log.append("muse-cli",
                            f"exit={res.exit_code} timed_out={res.timed_out} "
                            f"stdout={len(res.stdout)} chars")
            # Stash completion + report where the runner's wait step finds it.
            self._cli_done = res.ok and not res.timed_out
            self._last_cli_report = res.stdout
            return self._cli_done
        # Fallback: window automation.
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
        # CLI mode: run_task already blocked until exit; completion known
        # from the exit code — reliable, unlike the window heuristic.
        if getattr(self, "_cli_done", None) is not None:
            done = self._cli_done
            del self._cli_done
            return "complete" if done else "timeout"
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

        # 2. Muse integration
        ttk.Label(f, text="2. Muse — the developer").pack(anchor="w", pady=(10, 0))
        self._muse_mode = tk.StringVar(value=self.settings.get("muse_mode", "cli"))
        ttk.Radiobutton(f, text="Muse Code CLI (recommended — reliable completion via exit code)",
                        variable=self._muse_mode, value="cli").pack(anchor="w")
        ttk.Radiobutton(f, text="Muse app window (fallback — completion detection is UNCERTAIN)",
                        variable=self._muse_mode, value="window").pack(anchor="w")
        row_cli = ttk.Frame(f); row_cli.pack(fill="x", pady=4)
        ttk.Button(row_cli, text="Probe Muse CLI…", command=self._probe_cli).pack(side="left")
        self._cli_status = ttk.Label(row_cli, text="")
        self._cli_status.pack(side="left", padx=6)
        row2 = ttk.Frame(f); row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="Window title (fallback):").pack(side="left")
        self._muse_var = tk.StringVar(value=self.settings.get("muse_window_title", ""))
        ttk.Entry(row2, textvariable=self._muse_var, width=40).pack(side="left", padx=6)
        ttk.Button(row2, text="Detect windows…", command=self._detect_windows).pack(side="left")
        caps = muse_link.capabilities()
        ttk.Label(f, text=f"Muse: {caps.detail}", wraplength=800,
                  foreground="green" if caps.completion_detection == "cli-exit-code"
                  else "orange").pack(anchor="w")

        # 3. ChatGPT — planning & review brain (official Responses API)
        ttk.Label(f, text="3. ChatGPT connection (official OpenAI API — billed separately from ChatGPT Plus)").pack(anchor="w", pady=(10, 0))
        self._gpt_mode = tk.StringVar(value=self.settings.get("chatgpt_mode", "api"))
        ttk.Radiobutton(f, text="Official OpenAI API (automatic)", variable=self._gpt_mode,
                        value="api").pack(anchor="w")
        ttk.Radiobutton(f, text="ChatGPT website (manual relay — you copy answers back)",
                        variable=self._gpt_mode, value="manual").pack(anchor="w")
        row3 = ttk.Frame(f); row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="API key:").pack(side="left")
        self._key_var = tk.StringVar(value=self.settings.get("openai_api_key", ""))
        ttk.Entry(row3, textvariable=self._key_var, width=40, show="•").pack(side="left", padx=6)
        ttk.Button(row3, text="Test", command=self._test_api).pack(side="left")
        self._api_status = ttk.Label(f, text=""); self._api_status.pack(anchor="w")
        row3b = ttk.Frame(f); row3b.pack(fill="x", pady=4)
        ttk.Label(row3b, text="Model:").pack(side="left")
        self._model_var = tk.StringVar(value=self.settings.get("openai_model", DEFAULT_MODEL))
        ttk.Combobox(row3b, textvariable=self._model_var,
                     values=sorted(PRICE_TABLE), width=18,
                     state="readonly").pack(side="left", padx=6)
        ttk.Label(row3b, text="Spend limit (USD/session):").pack(side="left", padx=(12, 0))
        self._spend_var = tk.StringVar(value=str(self.settings.get("spend_limit_usd", 5.0)))
        ttk.Entry(row3b, textvariable=self._spend_var, width=8).pack(side="left", padx=6)
        ttk.Label(f, text=f"Prices are estimates ({PRICES_AS_OF}).",
                  wraplength=800, foreground="gray").pack(anchor="w")

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

    def _probe_cli(self) -> None:
        probe = muse_link.MuseCodeCli().probe()
        color = "green" if probe.found else "red"
        self._cli_status.config(text=(probe.detail[:100]), foreground=color)
        if probe.found:
            self.settings.set("muse_mode", "cli")
            self._muse_mode.set("cli")
        messagebox.showinfo("Muse CLI probe", probe.detail)

    def _wizard_finish(self) -> None:
        self.settings.set("chatgpt_mode", self._gpt_mode.get())
        self.settings.set("openai_model", self._model_var.get())
        try:
            self.settings.set("spend_limit_usd", float(self._spend_var.get()))
        except ValueError:
            pass
        self.settings.set("muse_mode", self._muse_mode.get())
        if self._muse_var.get().strip():
            self.settings.set("muse_window_title", self._muse_var.get().strip())
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
        for key in ["Active task", "Queue", "Last checkpoint", "Cycles ok/failed",
                    "Muse link", "ChatGPT", "API spend (session)"]:
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
            muse_mode = self.settings.get("muse_mode", "cli")
            caps = muse_link.capabilities()
            self._info_vars["Muse link"].set(
                f"{muse_mode} [{caps.completion_detection}]")
            self._info_vars["ChatGPT"].set(
                f"{self.settings.get('chatgpt_mode', 'api')} / {self.settings.get('openai_model', DEFAULT_MODEL)}")
            spend = self.ledger.session_totals(self.session_start)
            limit = float(self.settings.get("spend_limit_usd", 0) or 0)
            self._info_vars["API spend (session)"].set(
                f"${spend['cost_usd']:.4f} / ${limit:.2f} limit "
                f"({int(spend['input_tokens'])} in / {int(spend['output_tokens'])} out)")
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
