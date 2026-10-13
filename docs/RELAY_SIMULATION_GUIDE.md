# Relay Simulation Guide

The simulation lab (`tests/sim_lab/`) proves the relay's **control
logic** — orchestration, completion detection, extraction, retries,
crash recovery, pair isolation — without a Windows desktop or a live
ChatGPT session. It does NOT prove the pywinauto locators match the
real ChatGPT/Muse DOM; that gate is `relay_windows_check` on your PC.

## How it works

- `clock.py` — `VirtualClock`: time advances only through `sleep()`,
  so 1000s of streaming scenarios run in milliseconds, deterministically.
- `desktop.py` — `SimDesktop`/`SimWindow`: scripted windows with HWNDs,
  titles, PIDs, conversation URLs, message histories, composer state,
  streaming plans, and blocker pages (captcha/login/rate-limit/error).
- `agent.py` — `SimWindowAgent`: implements the production
  `WindowAgent` contract, but delegates the critical decisions to the
  REAL production code: `CompletionWatcher`, `extract_response`,
  `detect_challenge`, `message_in_text`, `wait_for_condition`, and the
  real `ChallengeDetected`. Faults live in the desktop; the agent
  behaves like the production UIA agent would against such a desktop.
- `scenarios.py` — 21 deterministic scenarios (SIM-A-01…SIM-F-03)
  covering window discovery, conversation identity, composer faults,
  streaming/extraction, delivery/retry/crash, and challenges. Each runs
  the REAL `RelayController` + real ledger transitions.
- `faults.py` — seeded fault-injection fuzzer. Each run builds 1–2
  pairs, injects 0–3 random faults, runs the real relay, and checks
  invariants INV1–INV5 (see below).

## Invariants (must hold on every run)

- **INV1** — no submission ever goes to a wrong or unverified conversation.
- **INV2** — no partial response is ever delivered as complete.
- **INV3** — no cross-pair delivery (pair A's answer never reaches pair B).
- **INV4** — identity/challenge faults end in a safe pause (ERROR +
  NEEDS_REVIEW), never a blind send or retry.
- **INV5** — no duplicate submissions of the same content.

## Running it

```bash
# deterministic scenarios + pure-function units + 200 fault runs + 100 e2e cycles
python -m unittest tests.test_relay_sim -v

# full 1000-run fault gate (also writes reports/relay_simulation_results.json)
python3 - <<'EOF'
from tests.sim_lab import faults
r = faults.run_many(1000, seed0=1, n_pairs=2)
print(r["passed"], "/", r["runs"])
EOF
```

## What the sim caught (real production defects)

1. **Stability-clock defect** (`CompletionWatcher`): when the
   "generating" indicator vanished without a text change, the quiet
   interval was computed from epoch (0.0) instead of restarting —
   a 10s streaming pause was declared "complete", delivering a partial
   answer. Fixed: `stable_since=None` until the first quiet poll.
2. **Restart amnesia**: pair baselines were in-memory only, so after a
   crash an in-flight message silently became history. Fixed: baselines
   persist in `relay_pairs.json`; ERROR state survives restart.
3. **Un-consumed hash not re-saved**: the ERROR un-consume restored the
   hash in memory but never persisted it — same silent-history bug.
4. **Adopt-path timeout**: after adopting an already-submitted message
   whose response had fully arrived, the watcher (baseline = current
   text) waited for changes that would never come. Fixed:
   `stable_response_to()` confirm-stability check.
5. **Test pollution**: relay state persisted to the real data dir during
   tests. Fixed: tests pass an isolated directory.

## Honest limits

- UIA tree shape, composer locators, and the address-bar/address-bar
  heuristics are scripted in the sim, not observed. Real-DOM
  verification happens only in `relay_windows_check --live`.
- Timing constants (poll 2s, stable 4s, settle 30s) are validated for
  logic, not tuned against real ChatGPT latency.
