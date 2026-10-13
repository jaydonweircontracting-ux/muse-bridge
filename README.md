# Muse Bridge — RPG Development Coordinator (Windows)

**Free existing-window edition. No API keys. No fees. No Python needed to run it.**

A Windows desktop app that coordinates your existing ChatGPT and Muse windows
while you develop your Adventure Game V2 RPG:

**Open EXE → select windows once → paste master prompt → guided handoffs → keep developing.**

The bridge prepares every prompt, copies it to your clipboard, focuses the
right window, and tells you exactly what to do. You paste into ChatGPT/Muse,
and paste their completed responses back into the bridge — never retyping.
It runs your local tests/builds, assembles the evidence, and only marks a
task passed when ChatGPT's review approves the real evidence.

## What it does NOT do (on purpose)

- No scraping of the ChatGPT website, no hidden APIs, no browser injection.
- Never claims a response was received until you transfer it.
- Never guesses that Muse finished — you confirm completion explicitly.
- Never rewrites game files itself; all code changes stay with Muse.
- Never force-pushes, deletes branches, or deploys untested changes.
- Stores no passwords and no secrets (there are none to store).

## The guided loop

1. **Plan** — bridge prepares a planning prompt from your master prompt +
   project summary → you run it in ChatGPT → paste the plan back →
   tasks are queued automatically from `TASK: title | brief` lines.
2. **Develop** — bridge copies the exact task → focuses Muse → you submit →
   you confirm when Muse is actually done → paste Muse's report back.
3. **Verify** — bridge runs your repo's real `build`/`test` scripts (only the
   tools that exist), inspects the git diff, and bundles the evidence.
4. **Review** — bridge prepares a review prompt (task + report + test output +
   diff) → you run it in ChatGPT → paste the review back.
5. **Decide** — APPROVE → checkpoint saved, next task queued. REJECT →
   correction task prepared (bounded retries), or blocked for your decision.

Explicit states throughout: READY, PREPARED, WAITING_FOR_USER, MUSE_RUNNING,
AWAITING_REPORT, REVIEW_PENDING, TASK_PASSED, TASK_FAILED, PAUSED, BLOCKED,
STOPPED. State is saved after every handoff — close the app or restart
Windows and it tells you exactly where you left off.

## Quick start (on your Windows PC)

1. Install Python 3.10+ from https://www.python.org/downloads/
   (check **"Add python.exe to PATH"** — only needed to *build* the app).
2. Download this repository (Code → Download ZIP) and unzip it.
3. Double-click **`build_windows.bat`**.
4. Open **`dist\MuseBridge.exe`** — Python is never needed again.
5. One-time setup: select your ChatGPT window, your Muse window, and your
   Adventure Game V2 folder; paste your master prompt.
6. Press **START** and follow the guided handoffs.

See [FIRST_RUN.md](FIRST_RUN.md) for the plain-English walkthrough.

## Project layout

```
main.py                  Entry point
muse_bridge/
  state.py               Explicit handoff states; illegal transitions rejected
  handoff.py             Prompt builders, TASK-line parser, <TAG> extraction
  runner.py              Headless handoff coordinator (unit-tested)
  store.py               JSON persistence: settings, queue, brief, log, checkpoints
  profile.py             RPG project profile; repo facts READ from your files
  muse_link.py           Window listing + focus (pywinauto); no scraping, no guessing
  chatgpt.py             Manual-relay coordination (no API in this edition)
  git_ops.py             Safe git reads, diff scan, local test/build runner
  gui.py                 Tkinter wizard + control panel
tests/test_core.py       19 unit tests (python -m unittest discover -s tests)
scripts/acceptance_walkthrough.py   25-check end-to-end walkthrough (25/25 pass)
build_windows.bat        The single packaging step (run on Windows)
```

## Verification status

- 19/19 unit tests pass (states, parsing, no-duplicate guard, recovery).
- 25/25 acceptance-walkthrough checks pass: full plan→dev→verify→review→
  checkpoint cycle plus close/reopen recovery, against a fixture repo.
- The Windows EXE itself is **not yet built** — this environment is Linux and
  PyInstaller only builds for its host OS. Run `build_windows.bat` on a
  Windows PC for the one remaining packaging step, then use the wizard's
  Test buttons to verify window targeting on your machine.
