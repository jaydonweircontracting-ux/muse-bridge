# Muse Bridge — Autonomous RPG Development Bot (Windows)

A Windows desktop app that runs the development loop for you:

**Download → Open EXE → Select Muse + ChatGPT → Paste one starter prompt → START → Let it work.**

It sends tasks to your Muse window, waits for *complete* responses (never guessing),
verifies every change against the actual repo (tests, build, diff), asks ChatGPT
(official API) to plan and review, commits only verified work, and keeps a full
task history so nothing is lost on crash or restart.

## Honest capability summary

| Feature | Status |
|---|---|
| ChatGPT via official OpenAI **Responses API** (automatic plan/review) | ✅ Fully automatic |
| ChatGPT usage/cost tracking + per-session spend limit | ✅ Enforced before every cycle |
| ChatGPT website (no API) | ✅ Manual relay mode — you copy the answer back; the app never scrapes the site |
| Muse via **Muse Code CLI** (`muse` command) | ✅ Preferred — completion = process exit code (reliable) |
| Muse via app-window automation | ⚠️ Fallback — completion detection **UNCERTAIN** (no official signal); the UI says so and asks you to confirm risky steps |
| Persistent project-state summary (maintained by ChatGPT) | ✅ Survives restarts |
| Git safety (no force-push, diff scan, checkpoints) | ✅ |
| Tests/build verification before commit | ✅ |
| Building the Windows EXE | ⚠️ Must be run on Windows (see below) |

## How the loop works

1. ChatGPT reads the master prompt + project-state summary and creates a task
   with measurable acceptance criteria.
2. The bot sends it to Muse (CLI preferred; window paste as fallback).
3. The bot waits until Muse has actually finished (CLI: exit code; window:
   quiet-screen heuristic + your confirmation).
4. The bot verifies: repo diff exists, tests + build pass, diff is clean.
5. ChatGPT reviews the real evidence (APPROVE/REJECT) and updates the
   project-state summary.
6. Fail → targeted correction task to Muse (bounded retries). Pass → git
   commit + checkpoint, ChatGPT picks the next task.
7. Repeat until paused, stopped, blocked, or the spend limit is reached.

Nothing starts a second task while one is in flight; every handoff and state
transition is logged so a restart recovers safely.

## Quick start (on your Windows PC)

1. Install Python 3.10+ from https://www.python.org/downloads/ (check **"Add python.exe to PATH"**).
2. Download/clone this repo, then double-click **`build_windows.bat`**.
3. Open **`dist\MuseBridge.exe`** — no Python needed after this.
4. Follow the first-run wizard:
   - Select your game repo folder (it verifies it's a git repo).
   - Select your Muse window (Detect windows…).
   - Choose ChatGPT API (paste key, Test) or manual-relay mode.
   - Paste your ONE starter prompt.
5. Press **TEST CONNECTIONS**, then **START**.

See [FIRST_RUN.md](FIRST_RUN.md) for the plain-English walkthrough.

## Project layout

```
main.py               Entry point
muse_bridge/
  state.py            Response-state machine (READY…STOPPED); illegal transitions rejected
  store.py            JSON persistence: settings, task queue, brief, log, checkpoints
  chatgpt.py          Official OpenAI Responses API + usage/cost ledger + manual-relay mode
  project_state.py    Persistent project summary maintained by ChatGPT across cycles
  muse_link.py        Muse Code CLI adapter (preferred) + window automation (fallback)
  git_ops.py          Safe git ops (no force-push), diff scanning, test/build runner
  runner.py           Headless loop engine: spend-limit gate, evidence-based completion
  gui.py              Tkinter wizard + dashboard (usage panel, spend limit, CLI probe)
tests/test_core.py    22 unit tests (run: python -m unittest discover -s tests)
build_windows.bat     The single packaging step (run on Windows)
```

## Muse integration detail

The bot inspects the actual environment at startup:

1. **Muse Code CLI** (`muse` on PATH, from https://dev.meta.ai): runs each task
   non-interactively in the game repo; the process exit code is the completion
   signal. The adapter auto-discovers the non-interactive flag from
   `muse --help` and accepts a custom `muse_cli_template` in settings.
2. **Window fallback**: focus + paste via pywinauto. Completion detection is
   honestly labelled UNCERTAIN.

Use the wizard's "Probe Muse CLI…" button to verify which level you have.

## Safety rules (enforced in code, not just docs)

- Never starts a second task while one is in flight (`StateTracker.busy()`).
- Never marks a task complete because a model said so — repo diff + tests + build + review must agree.
- Pauses on failure by default; bounded retries with backoff; per-task time limits.
- Commits only verified work; never force-pushes; scans diffs for secrets and build artifacts.
- All secrets stay on your disk; logs scrub credential-looking fields.

## Development

```bash
python -m unittest discover -s tests   # 10 tests, no deps
python main.py                          # run the GUI (needs a display)
```
