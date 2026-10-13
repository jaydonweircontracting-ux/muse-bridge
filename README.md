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
| ChatGPT via official OpenAI API (automatic plan/review) | ✅ Fully automatic |
| ChatGPT website (no API) | ✅ Manual relay mode — you copy the answer back; the app never scrapes the site |
| Muse window: find, focus, paste prompt | ✅ Via pywinauto/pyautogui on Windows |
| Muse window: detecting "task finished" | ⚠️ **UNCERTAIN** — no official signal exists. The app uses a no-new-text timeout heuristic and asks you to confirm before risky steps. The UI labels this clearly. |
| Git safety (no force-push, diff scan, checkpoints) | ✅ |
| Tests/build verification before commit | ✅ |
| Building the Windows EXE | ⚠️ Must be run on Windows (see below) |

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
  chatgpt.py          Official OpenAI API client + manual-relay mode (never scrapes)
  muse_link.py        Windows window automation + honest capability report
  git_ops.py          Safe git ops (no force-push), diff scanning, test/build runner
  runner.py           Headless autonomous loop engine (unit-tested)
  gui.py              Tkinter wizard + dashboard
tests/test_core.py    10 unit tests (run: python -m unittest discover -s tests)
build_windows.bat     The single packaging step (run on Windows)
```

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
