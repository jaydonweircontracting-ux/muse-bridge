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
  runner.py              Headless single-handoff coordinator (unit-tested)
  orchestrator.py        Multi-worker engine: schedule, handoff, integrate
  workers.py             5 worker slots; verified sessions only, never simulated
  workspaces.py          Git worktree isolation per worker (branch fallback)
  ownership.py           File ownership registry + conflict detection
  scheduler.py           Dependency-aware assignment, honest concurrency limit
  integrate.py           Integration gate: evidence-based merge into bridge/integration
  store.py               JSON persistence: settings, queue, brief, log, checkpoints
  profile.py             Shared game memory: brief, constraints, decisions, module index
  muse_link.py           Window listing + focus (pywinauto); no scraping, no guessing
  chatgpt.py             Manual-relay coordination (no API in this edition)
  git_ops.py             Safe git reads, diff scan, local test/build runner
  gui.py                 Tkinter wizard + control panel (Handoffs + Workers tabs)
tests/test_core.py       19 unit tests (python -m unittest discover -s tests)
tests/test_orchestration.py  14 orchestration tests
scripts/acceptance_walkthrough.py      25-check handoff cycle (25/25 pass)
scripts/acceptance_orchestration.py    19-check multi-worker cycle (19/19 pass)
build_windows.bat        The single packaging step (run on Windows)
```

## Multi-worker orchestration (Workers tab)

Up to five Muse workers collaborate on the **same** Adventure Game V2 repo:

- **Real sessions only.** Each of the 5 slots (Muse 1–5, configurable roles)
  binds a Muse window and is verified with a probe prompt whose reply must
  contain the slot's label. The dashboard shows "N/5 sessions verified" and
  the scheduler never exceeds that count — with fewer sessions, work queues
  instead of pretending to run in parallel.
- **Isolated workspaces.** Each assigned worker gets its own Git worktree on
  its own branch (`bridge/w1/<task>`). Workers cannot overwrite each other;
  the main checkout is never touched by workers.
- **File ownership.** Tasks declare expected files; overlapping claims block
  assignment. Core files (App.tsx, package.json, …) are exclusive.
- **Dependency-aware scheduling.** Tasks wait for prerequisites; priorities
  order the queue.
- **Integration gate.** Before merging: diff inspection, ownership/overlap
  check, tests + typecheck + production build in the worker's workspace,
  then merge into the single `bridge/integration` branch. Failed gates roll
  back the integration branch and preserve the worker's branch — failed
  tests can never be marked passed.
- **Shared game memory.** One authoritative brief, architecture constraints,
  decision log, and module index — every worker reads it before coding, and
  only verified facts update it.
- **Recovery & isolation.** All state persists; restarting restores workers,
  queue, and ownership. Disconnecting one account never touches the others.

## Verification status

- 33/33 unit tests pass (handoff engine + orchestration engine).
- 25/25 handoff acceptance checks pass: full plan→dev→verify→review→
  checkpoint cycle plus close/reopen recovery, against a fixture repo.
- 19/19 orchestration acceptance checks pass: two concurrent tasks in
  separate worktrees, conflict blocking, evidence-based integration,
  failed-test merge block with worker branch preserved, restart recovery,
  honest 2/5 concurrency reporting.
- The Windows EXE itself is **not yet built** — this environment is Linux and
  PyInstaller only builds for its host OS. Run `build_windows.bat` on a
  Windows PC for the one remaining packaging step, then use the wizard's
  Test buttons to verify window targeting on your machine.
