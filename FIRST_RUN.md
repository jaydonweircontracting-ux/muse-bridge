# Muse Bridge — First-Run Guide (plain English)

## What you need
- A Windows 10/11 PC (64-bit).
- Your ChatGPT window open in your browser (signed in as usual).
- Your Muse window open (signed in as usual).
- Your Adventure Game V2 folder on the PC.
- No API keys. No new accounts. No fees.

## Step 1 — Build the app (one time)
1. Install Python 3.10+ from https://www.python.org/downloads/
   - On the first installer screen, check **"Add python.exe to PATH"**.
   - (Python is only needed to *build* the app, never to run it.)
2. Download this repository (Code → Download ZIP) and unzip it.
3. Double-click **`build_windows.bat`**. It builds the app.
4. When it says DONE, open the **`dist`** folder and double-click **`MuseBridge.exe`**.

## Step 2 — One-time setup
1. **ChatGPT window:** click Detect, pick your ChatGPT browser window, then
   Test focus to prove the bridge can bring it forward.
2. **Game folder:** Browse to your Adventure Game V2 folder. The bridge
   verifies it's a git repo and reads its real details (React/Vite,
   scripts, detected systems) into your project profile.
3. **Master prompt:** paste your one big RPG instruction. The bridge saves it
   — you never paste it again.

## Step 2b — Add your Muse accounts (Accounts tab)
1. Open the **Accounts** tab → **ADD ACCOUNT** → label it (e.g. "Personal FB").
2. **Open login** — a browser window opens with a private, isolated profile.
   Sign in through Meta's normal login (Facebook/Instagram/email), then open
   muse.ai in that same window and check Muse loads.
3. **Mark signed in** — confirm it yourself; the bridge can't detect logins.
4. **Bind to slot** — link the account to a worker slot (w1–w5).
5. Repeat for each Facebook account you want as a worker.
6. On the **Workers tab**: for each slot, **Bind window** (pick its Muse
   window), then **Verify session** (paste the probe prompt into that
   session, paste its reply back). Only verified sessions receive tasks.

Tip: one account = one worker slot in practice. The bridge counts verified
*sessions*, so if you genuinely have two independent Muse windows verified,
you get two workers — never more than what's real.

## Step 3 — The daily routine
1. Open `MuseBridge.exe`.
2. Press **START** (or **NEXT HANDOFF**).
3. The bridge copies a prompt and tells you what to do, e.g.:
   *"Paste this into ChatGPT, submit, then paste the completed response below."*
4. Paste the response into the bridge's transfer box → **SAVE TRANSFERRED RESPONSE**.
5. For Muse tasks: paste the task into Muse, press **MUSE TASK SUBMITTED**,
   and only press **MUSE DONE — I CONFIRM** when Muse has actually finished.
   The bridge never guesses.
6. Paste Muse's report back → the bridge runs your tests/build automatically.
7. Press **COPY RESPONSE FOR REVIEW** → run it in ChatGPT → paste the review back.
8. APPROVE → checkpoint saved, next task queued. REJECT → correction prepared.

## If something goes wrong
- **VIEW TASK QUEUE** / **VIEW HISTORY** — see everything.
- **EXPORT DIAGNOSTICS** — saves a file you can send for help.
  (The app stores no passwords or keys, so there's nothing to leak.)
- Close and reopen any time: the bridge restores exactly where you left off
  and tells you what was happening.
- **PAUSE** stops new handoffs. **STOP** stands down without deleting history.

## The one thing the bridge can't do for you
Transferring ChatGPT's response through the bridge is manual — that's the
price of the free edition, and it's deliberate: the bridge must never claim
to have read a response you haven't transferred. The transfer box means
copy + paste, never retyping.
