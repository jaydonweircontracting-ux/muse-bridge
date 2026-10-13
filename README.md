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

## Run control (bot-client style)

The run bar above the dashboard works like an old-school bot client or
screen recorder:

- **▶ START** — begins the run loop (every 5s): assigns eligible tasks to
  verified idle workers, copies each handoff prompt to the clipboard, and
  focuses that worker's window. You still paste the prompt and confirm
  every step — the loop just never leaves a worker waiting.
- **⏸ PAUSE / ▶ RESUME** — halts the loop without losing state; workers
  keep their assignments and you resume exactly where you left off.
- **⏹ STOP** — graceful: finishes the current tick, then stands down.
  Everything is persisted; in-flight handoffs keep their branches.
- The loop **stops itself** when the queue is drained (or when remaining
  tasks are all blocked and no workers are active — it tells you which).

Configurable global hotkeys (work even when the bridge window isn't
focused). Defaults follow the recorder workflow:

| Action | Default | Notes |
|---|---|---|
| Start / Go | `F1` | starts the run loop |
| Stop | `F2` | graceful stop (acts only when a run is active) |
| Pause / Resume | `F3` | toggles |
| Stop immediately | `Ctrl+Shift+X` | no grace period |
| Focus worker 1–5 | `Ctrl+Shift+1`–`5` | brings that Muse window forward |

**Hotkeys…** opens the settings screen: every action with its current
binding, click **Assign…**, then press any key (`Esc` cancels). Bindings
persist across restarts; **Reset to Defaults** restores F1/F2/F3. Conflicts
ask before replacing; mouse clicks can never be assigned; holding a key
does not re-trigger (debounced).

**Tile windows** arranges all bound worker windows in a grid so you can
watch the fleet at once.

Deliberate boundary: hotkeys drive the bridge's own loop and window focus
only — they never type into or click inside Muse/ChatGPT windows, and run
hotkeys never blast commands at individual workers. Handoffs stay explicit
clipboard copy/paste with your confirmation.

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
  accounts.py            Multi-account manager (BUILD 2); no secrets stored
  browser_profiles.py    Isolated browser profiles per account (BUILD 2)
  workspaces.py          Git worktree isolation per worker (branch fallback)
  ownership.py           File ownership registry + conflict detection
  scheduler.py           Dependency-aware assignment, honest concurrency limit
  integrate.py           Integration gate: evidence-based merge into bridge/integration
  store.py               JSON persistence: settings, queue, brief, log, checkpoints
  profile.py             Shared game memory: brief, constraints, decisions, module index
  muse_link.py           Window listing + focus (pywinauto); no scraping, no guessing
  chatgpt.py             Manual-relay coordination (no API in this edition)
  git_ops.py             Safe git reads, diff scan, local test/build runner
  gui.py                 Tkinter wizard + control panel (Handoffs + Workers + Accounts + GitHub tabs)
tests/test_core.py       19 unit tests
tests/test_orchestration.py  14 orchestration tests
tests/test_accounts.py   11 account-manager tests
tests/test_github.py     11 GitHub + review-packet tests
scripts/acceptance_walkthrough.py      25-check handoff cycle (25/25 pass)
scripts/acceptance_orchestration.py    19-check multi-worker cycle (19/19 pass)
build_windows.bat        The single packaging step (run on Windows)
```

## GitHub integration (GitHub tab, read-only by default)

- **Connection:** local git always works for reads; authenticated access via
  the official GitHub CLI (`gh auth login` once — the bridge never touches
  your token). Status, user, and repo visibility shown honestly.
- **Panel:** repository, branch, HEAD, latest commit, 20-commit history,
  branch list (worker branches flagged), one-click branch compare
  (worker branch vs integration/main) with diff stats.
- **Review packets:** per completed worker task — description, worker/branch,
  files, diff, tests, typecheck, build, limitations, recommended action.
  One click copies it for ChatGPT; the returned review attaches to the task
  as advisory (automated tests remain authoritative).
- **ChatGPT prompts** now include live repository context (branch, HEAD,
  recent commits, worker branches, uncommitted changes) read at prompt time.
- ChatGPT's own GitHub connector is separate and gives this app nothing;
  the bridge's integration is its own, and clipboard handoff remains the
  no-API path — only tested automation is ever claimed.

## Multi-account access (Accounts tab)

Each Facebook account (or Instagram/email/phone-backed Meta account — per
Meta's sign-in options) gets its own **isolated browser profile**, so
sessions cannot leak into each other:

1. **ADD ACCOUNT** — label it (e.g. "Personal FB").
2. **Open login** — launches Chrome/Edge with that account's private profile
   at muse.ai. Sign in through Meta's normal login flow yourself.
3. **Mark signed in** — confirm muse.ai loads in that window.
4. **Bind to slot** — link the account to a worker slot (w1–w5).
5. On the **Workers tab**: bind the slot's Muse window, then **Verify
   session** with the probe prompt. Only verified sessions count toward
   the concurrency limit.

The bridge never stores passwords, tokens, or cookies — only labels,
statuses, and profile paths. Disconnecting or removing one account never
touches the others.

**Verified platform facts (Oct 2026):** Muse signs in with a Meta account,
which can be backed by Facebook, Instagram, email, or phone — so separate
Facebook accounts can back separate Muse logins. There is currently no
native Windows Muse app (Microsoft announced one is coming); on Windows,
Muse runs at muse.ai in the browser, which is exactly why isolated
browser profiles are the supported session-separation method.

**Not yet verified from here:** the real Facebook→Meta→Muse login flow on
your PC (no Windows/browser here). The account manager is explicit about
what it can and cannot detect — follow the on-screen steps and it will
tell you what needs your confirmation.

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
- **Worker↔ChatGPT pairs.** Each slot maps to its dedicated ChatGPT
  conversation window (**Map ChatGPT**; **Discover Windows** lists what's
  open — one window per conversation, since tabs can't be enumerated
  without a browser extension). Mappings persist, appear in
  `recovery.json`, and **Verify pairs** re-finds every mapped window by
  title. Message exchange itself stays user-confirmed copy/paste — the
  bridge never types into or reads those windows on its own.
- **Per-worker pause.** **Pause/Resume** on a worker row excludes that slot
  from scheduling (it keeps its state, branch, and task); the run loop's
  F1/F2/F3 control the loop only, never individual workers.
- **Autonomy modes.** Each worker is **Manual** (you drive it from the row
  buttons; the run loop never auto-schedules it) or **Supervised** (the
  run loop may schedule it, but every message exchange is still
  user-confirmed). There is deliberately no fully-autonomous mode.
- **Pre-send destination verification.** Before any handoff prompt or
  review packet is copied, the bridge re-checks that the mapped window
  exists under its exact title. If not, the send is blocked with a
  plain-English explanation — never sent to an ambiguous destination.
- **One-to-one conversation lock.** Two workers can't accidentally share
  one ChatGPT conversation; remapping asks for explicit confirmation.
- **Durable message ledger.** Every handoff and review packet gets a
  unique ID and explicit states (DETECTED → QUEUED → COPIED → SUBMITTED
  → RESPONSE_RECEIVED → DELIVERED → ACKNOWLEDGED, plus FAILED and
  NEEDS_REVIEW). Crash recovery moves anything mid-flight to
  NEEDS_REVIEW instead of blindly repeating it; double-confirms are
  harmless no-ops, not duplicates.
- **Watchdog.** Every ~30s the bridge checks mapped windows still exist;
  a vanished window pauses only that worker (queue/branch preserved)
  with a plain-English explanation in the log.
- **Backpressure.** Configurable queue cap (default 50) and prompt-size
  cap (default 8000 chars); oversized items are refused with guidance,
  not silently truncated.
- **Diagnostics & backup.** One-click diagnostics (windows, pair
  verification, ledger counts, queue, errors, version) with a redacted
  export that hashes window titles; config backup/restore as a zip.

## Verification status

- 55/55 unit tests pass (handoff + orchestration + accounts + GitHub).
- 25/25 handoff acceptance checks pass; 19/19 orchestration checks pass.
- GitHub panel: all logic tested against fixture repos (identity, history,
  file reads, branch compare, packet assembly, review attachment).
  `gh` auth detection tested with mocks; real `gh auth login` must happen
  on your PC.
- The Windows EXE itself is **not yet built** — this environment is Linux and
  PyInstaller only builds for its host OS. Run `build_windows.bat` on a
  Windows PC for the one remaining packaging step.
