"""Tkinter GUI for Muse Bridge — free existing-window edition.

First-run wizard: select ChatGPT window, Muse window, game repo, master prompt.
Dashboard: big readable buttons, guided handoffs, response-transfer box.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from pathlib import Path

from . import git_ops, muse_link
from .profile import ProjectProfile
from .runner import Coordinator, StepResult
from .state import HandoffState
from .store import Brief, Checkpoints, EventLog, Settings, TaskQueue


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
        if not self.settings.get("first_run_complete"):
            self.show_wizard()
        else:
            self.show_dashboard()
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
        top = ttk.Frame(self.root, padding=10); top.pack(fill="x")
        ttk.Label(top, text="Muse Bridge",
                  font=("TkDefaultFont", 14, "bold")).pack(side="left")
        self._state_lbl = ttk.Label(top, text="READY",
                                    font=("TkDefaultFont", 12, "bold"))
        self._state_lbl.pack(side="left", padx=20)

        self._next_action = ttk.Label(self.root, text="", wraplength=920,
                                      font=("TkDefaultFont", 10, "bold"),
                                      foreground="navy", padding=(10, 4))
        self._next_action.pack(fill="x")

        btns = ttk.Frame(self.root, padding=8); btns.pack(fill="x")
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
        prog = ttk.LabelFrame(self.root, text="Muse progress (you confirm — never guessed)",
                              padding=8)
        prog.pack(fill="x", padx=10, pady=4)
        ttk.Button(prog, text="MUSE TASK SUBMITTED",
                   command=self._muse_submitted).pack(side="left", padx=4)
        ttk.Button(prog, text="MUSE DONE — I CONFIRM",
                   command=self._muse_done).pack(side="left", padx=4)
        ttk.Button(prog, text="RUN TESTS + BUILD",
                   command=self._run_verify).pack(side="left", padx=4)

        # Response transfer
        tr = ttk.LabelFrame(self.root, text="Transfer a response (paste, don't retype)",
                            padding=8)
        tr.pack(fill="x", padx=10, pady=4)
        self._transfer = scrolledtext.ScrolledText(tr, height=4)
        self._transfer.pack(fill="x", pady=2)
        ttk.Button(tr, text="SAVE TRANSFERRED RESPONSE",
                   command=self._save_transfer).pack(anchor="e")

        info = ttk.Frame(self.root, padding=(10, 4)); info.pack(fill="x")
        self._info_vars: dict[str, tk.StringVar] = {}
        for key in ["Current task", "Current handoff", "Waiting on",
                    "Last completed", "Queue", "Saved progress"]:
            ttk.Label(info, text=key + ":").grid(sticky="w", padx=4)
            v = tk.StringVar(value="—")
            self._info_vars[key] = v
            ttk.Label(info, textvariable=v, wraplength=700).grid(
                row=list(self._info_vars).index(key), column=1, sticky="w")

        ttk.Label(self.root, text="Log", padding=(10, 0)).pack(anchor="w")
        self._log_view = scrolledtext.ScrolledText(self.root, height=10,
                                                   state="disabled")
        self._log_view.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._refresh()
        self.root.after(2000, self._tick)

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
