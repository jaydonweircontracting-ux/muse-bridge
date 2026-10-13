# Relay Simulation Results — 2026-10-13

Starting commit: `581121b`.
Machine-readable: `reports/relay_simulation_results.json`.

> **What this proves:** the relay's control logic — orchestration,
> completion detection, extraction, retries, crash recovery, pair
> isolation — against 21 deterministic scenarios, 1000 seeded
> fault-injection runs, and 100 consecutive end-to-end cycles.
>
> **What it does NOT prove:** that the pywinauto locators match the
> real ChatGPT/Muse DOM. That gate is `python -m
> muse_bridge.relay_windows_check --live` on a Windows PC.

## Gate results

| Gate | Result |
|---|---|
| Existing test suite (160 tests) | **PASS** |
| Deterministic scenarios (21) | **21/21 PASS** |
| Fault-injection runs (1000, 2 pairs each) | **1000/1000 PASS** |
| Consecutive end-to-end cycles (100, 2 pairs) | **100/100 PASS** |
| Wrong-conversation submissions | **0** |
| Cross-pair deliveries | **0** |
| Blind resubmissions of uncertain in-flight messages | **0** |
| Challenge/identity faults ending in safe pause | **all** |
| Ledger recoverable after simulated crash | **yes** (baselines + ERROR state persist) |
| Silently skipped tests | **none** |

## Production defects found by the simulation (all fixed, all with regression tests)

1. **CompletionWatcher stability-clock defect** — when the generating
   indicator vanished without a text change, quiet time was computed
   from epoch instead of restarting the clock: a 10s streaming pause
   was declared complete and a partial answer delivered. Fixed with
   `stable_since=None` semantics; regression: `test_quiet_pause_without_indicator_is_not_completion`.
2. **Restart amnesia** — pair baselines were in-memory only; after a
   crash an in-flight NEEDS_REVIEW message silently became history.
   Fixed: `relay_pairs.json` persistence (baselines, enablement, ERROR
   state); regression: `SIM-E-01`.
3. **Un-consume not re-saved** — the ERROR path restored the Muse hash
   in memory but never persisted it (same silent-history outcome).
   Fixed; covered by `SIM-E-01`.
4. **Adopt-path livelock** — adopting an already-submitted message whose
   response had fully arrived led the watcher to wait for changes that
   would never come (timeout). Fixed: `stable_response_to()` confirm-
   stability check; regression: `SIM-C-03`.
5. **Test isolation** — relay state leaked into the real data dir during
   tests, poisoning later tests. Fixed: isolated directory per test.

## Status by category

- **SIMULATION PASS** — yes (this report).
- **WINDOWS UIA INSPECTION PASS** — not yet (needs your PC).
- **LIVE CHATGPT ROUND TRIP PASS** — not yet (needs your PC).
- **MULTI-WINDOW LIVE PASS** — not yet (needs your PC).

## Exact command to run next on your PC

```powershell
python -m muse_bridge.relay_windows_check --live --pairs 2
```

Read-only first if you prefer: omit `--live`.
