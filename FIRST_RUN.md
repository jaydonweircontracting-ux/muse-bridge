# Muse Bridge — First-Run Guide (plain English)

## What you need
- A Windows 10/11 PC (64-bit).
- Your Muse window open (the app with your RPG project).
- Either an OpenAI API key (https://platform.openai.com/api-keys) **or** the ChatGPT website open in your browser.

## Step 1 — Build the app (one time)
1. Install Python 3.10+ from https://www.python.org/downloads/
   - On the first installer screen, check **"Add python.exe to PATH"**.
2. Download this repository (Code → Download ZIP) and unzip it.
3. Double-click **`build_windows.bat`**. It installs what it needs and builds the app.
4. When it says DONE, open the **`dist`** folder and double-click **`MuseBridge.exe`**.
   You never need Python again after this.

## Step 2 — First-run wizard
1. **Game repository:** click Browse and select your Adventure Game V2 folder.
   The app checks it is a real git repo and shows the branch.
2. **Muse window:** open Muse, then click "Detect windows…" and pick it.
   > Note: the app can paste prompts into Muse, but detecting *when Muse
   > finishes* has no perfect signal. The app watches for the response to
   > go quiet and will ask you to confirm before anything risky.
3. **ChatGPT:** choose one:
   - **API (recommended):** paste your OpenAI API key, click Test. Planning and
     code review then run fully automatically. Your key never leaves your PC.
   - **Website (manual):** the app shows you each prompt; you paste ChatGPT's
     finished answer back in. Slower, but no key needed.
4. **Starter prompt:** paste your one big RPG instruction (goals, architecture
   rules, what "done" means). The app saves it — you never paste it again.

## Step 3 — Go
1. Click **TEST CONNECTIONS** — all green before real work starts.
2. Click **START**. The app will:
   - pick one small task from the backlog,
   - ask ChatGPT to define what "done" looks like,
   - paste the task into Muse and wait for the *complete* response,
   - check the repo actually changed, run tests and the build,
   - ask ChatGPT to review the real evidence,
   - commit only if everything passes, then move to the next task.
3. **PAUSE** stops new tasks. **STOP** halts everything. **RUN ONE CYCLE** does a single task.

## If something goes wrong
- **VIEW LOGS** shows everything the app did, with timestamps.
- **EXPORT DIAGNOSTICS** (in the logs view) saves a file you can send for help —
  it never includes your API key.
- Close and reopen the app any time: tasks, history, and checkpoints are saved
  automatically. Use the handoff summary in the logs to see where it left off.

## Limits you control (Settings)
- Max development cycles per session (default 10)
- Max retries per task (default 3)
- Response timeout (default 10 minutes)
- Pause after a failure (default on)
- Manual approval for risky diffs (default on)
