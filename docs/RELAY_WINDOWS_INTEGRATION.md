# Relay Windows Integration — operator guide

Run on your Windows PC. The bridge must be installed
(`build_windows.bat`, then run the EXE or `python -m muse_bridge`).

## Step 1 — read-only inspection (safe, sends nothing)

```powershell
python -m muse_bridge.relay_windows_check
```

It will:

1. Refuse to run anywhere but Windows.
2. Report the automation backend (pywinauto version, comtypes).
3. Enumerate desktop windows with HWNDs.
4. Ask you to pick the ChatGPT window and the Muse window by number.
5. Inspect — without clicking or typing:
   - readable conversation text (first 500 chars shown),
   - challenge markers (login/CAPTCHA/rate-limit),
   - the address-bar conversation URL,
   - the composer control (found? enabled?),
   - the generating indicator.
6. Print `READ-ONLY VERDICT: READY` or list exactly what is wrong.

Fix what it lists (usually: log in, close the CAPTCHA, or re-map the
window), then re-run until READY.

## Step 2 — one live round trip (opt-in)

```powershell
python -m muse_bridge.relay_windows_check --live
```

After the read-only checks pass, it asks you to type `YES`. Then:

1. It places a unique harmless question in your Muse window
   (`RELAY-TEST-<id>`, asking ChatGPT to reply `RELAY-OK <id>`).
2. Runs the real `RelayController` for one round trip
   (240s response timeout).
3. Verifies the `RELAY-OK` marker arrived back in the Muse window.
4. Prints `LIVE ROUND TRIP: PASS/FAIL` and keeps the ledger dir.

It stops after one round trip. It never sends to a conversation whose
URL wasn't verified in step 1.

## Step 3 — multi-window (no cross-delivery)

```powershell
python -m muse_bridge.relay_windows_check --live --pairs 2
```

Pairs two independent Muse↔ChatGPT pairs (you pick all four windows)
and verifies each answer returns to its own Muse window. This is the
gate that proves "Muse 1 ↔ ChatGPT Window 1, Muse 2 ↔ ChatGPT Window 2"
with no cross-routing.

## What still needs your eyes

- The UIA locators (composer name "Message ChatGPT", address bar,
  "Stop generating") are heuristics. If the read-only inspection says
  `NOT FOUND`, paste the diagnostic output back — the locators get
  fixed, not worked around.
- First enable the relay for ONE pair in the bridge GUI (Relay button),
  press F1, and watch a full cycle before enabling the rest.

## Account-risk reminder

Automating ChatGPT's web UI is against OpenAI's terms for automated
access and could risk the ChatGPT account. The bridge pauses instead
of pushing through blocks, but the decision to run it is yours.
