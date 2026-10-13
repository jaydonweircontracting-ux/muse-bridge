# Relay UIA Audit — 2026-10-13

Starting commit: `581121b`. Inspected `muse_bridge/relay.py`,
`muse_bridge/uia_agent.py`, `muse_bridge/muse_link.py`,
`muse_bridge/ledger.py`, `muse_bridge/workers.py`, `muse_bridge/gui.py`
before changing anything. The F1 call chain:

```
F1 → RunController.tick → BridgeApp._run_tick → RelayController.poll
  → poll_pair(wid) → _new_content (delta+echo guard)
  → _relay → _do_relay:
      make_agent(chatgpt) → conversation_id() → _expected_url check
      → focus → _prior_submission (ledger dedup) → type_and_submit
      → SUBMITTED → read_text (baseline) → wait_for_response
      → RESPONSE_RECEIVED → make_agent(muse) → focus
      → type_and_submit(response) → DELIVERED → ACKNOWLEDGED
```

## Component classification

| # | Component | Location | Status |
|---|---|---|---|
| 1 | Window discovery | `muse_link.list_windows` / `find_handle` | PRODUCTION IMPLEMENTED (win32-only; no-op on Linux) |
| 2 | HWND obtain/retain | `workers.map_chatgpt` / `bind_session` → slot handles | PRODUCTION IMPLEMENTED |
| 3 | Re-identify on invalid handle | `gui._make_relay_agent` (title re-search) | PARTIALLY IMPLEMENTED — no check that the recovered window is the same one; HWND reuse by another window undetected |
| 4 | Conversation URL verification | `relay._do_relay` + `_expected_url` | PARTIALLY IMPLEMENTED — GUI never captures `chatgpt_url` (`map_chatgpt` called without `url=`), so verification is dead in practice; unreadable URL proceeds silently |
| 5 | Reading conversation content | `uia_agent.read_text` | UNVERIFIED ON WINDOWS (heuristic tree walk) |
| 6 | Composer identification | `uia_agent._chatgpt_input` / `_muse_input` | UNVERIFIED ON WINDOWS |
| 7 | Typing + submitting | `uia_agent.type_and_submit` | UNVERIFIED ON WINDOWS; no verification that text was entered or submitted |
| 8 | Submission detection | — | MISSING — no post-submit confirmation |
| 9 | Response completion detection | `uia_agent.wait_for_response` | UNVERIFIED ON WINDOWS; stability heuristic has logic gaps (see D6) |
| 10 | Return response to Muse | `relay._do_relay` delivery block | PRODUCTION IMPLEMENTED (fake-tested only); delivery-time dedup MISSING |
| 11 | Ledger recording | `relay` + `ledger` | PRODUCTION IMPLEMENTED |
| 12 | Timeout/retry/dup/UI-change/challenge handling | `relay` | PARTIALLY IMPLEMENTED — timeout→NEEDS_REVIEW works; no challenge detection; retry can double-deliver; no conversation re-verification after the wait |

## Defects found by inspection (to confirm via simulation)

- **D1.** Unreadable conversation URL proceeds silently. `_do_relay`
  only blocks on `expected and url and url != expected`. If the address
  bar can't be read (`url == ""`), the send goes ahead unverified.
- **D2.** `_extract_new` returns the last 6000 chars of the whole
  conversation, not the new response — prior history leaks into delivery.
- **D3.** No login/CAPTCHA/rate-limit/error detection. The relay would
  attempt automation against a challenge page.
- **D4.** `UiaWindowAgent._window()` caches the resolved window forever.
  After close/reopen or HWND reuse, operations target a stale object.
- **D5.** `type_and_submit` returns True blindly — no check that the
  paste landed or that Enter submitted (vs. inserting a newline).
- **D6.** `wait_for_response` stability logic: a long streaming pause
  with no "Stop generating" indicator (renamed button, poll gap) is
  declared complete after 4 quiet seconds — a partial answer delivered
  as complete. Quiet interval alone must not establish completion.
- **D7.** Uncaught exceptions in `_do_relay` (`make_agent` for chatgpt,
  `chat.read_text`, `muse.read_text`) propagate instead of pausing.
- **D8.** Retry after a crash between delivery and the DELIVERED ledger
  transition re-delivers the same response (no delivery-time dedup).
- **D9.** No conversation re-verification after the response wait —
  the user could switch tabs mid-wait and we'd deliver the wrong
  conversation's answer.
- **D10.** GUI `_w_map_chatgpt` never captures the conversation URL,
  so the URL check (the only conversation-identity signal) never fires.
- **D11.** `read_text` dedups repeated text spans — legitimately
  repeated content is dropped, corrupting deltas.
- **D12.** `_new_content` falls back to "whole text is the delta" when
  the old text isn't a character prefix — a UI re-render would relay
  the entire conversation as the question.
- **D13.** `type_and_submit` silently truncates at 8000 chars.
- **D14.** No composer readiness check (disabled / read-only composer).

## Macro Recorder relationship (Phase 7)

The relay does **not** use Macro Recorder. It drives windows via
pywinauto UIA directly (`uia_agent.py`). Macro Recorder Pro 5.0.40 was
forensics-only (see `docs/MACRO_RECORDER_FORENSICS.md`): its window-
identity and focus-verification patterns informed the HWND-based design,
but there is no runtime dependency on Macro Recorder internals.

## What simulation can and cannot prove

The sim lab (see `docs/RELAY_SIMULATION_GUIDE.md`) exercises the real
`RelayController`, the real `CompletionWatcher` completion algorithm,
the real extraction/challenge/delta pure functions, and the real ledger
transitions against a scripted desktop. It **cannot** prove that
pywinauto locators match the real ChatGPT/Muse DOM — that requires
`python -m muse_bridge.relay_windows_check` on the user's PC
(see `docs/RELAY_WINDOWS_INTEGRATION.md`).

## Defects confirmed and fixed by the simulation (2026-10-13)

The sim lab confirmed D1, D3, D4, D6, D7, D8, D9, D10, D12, D13, D14
and fixed them in production code (see `docs/RELAY_SIMULATION_GUIDE.md`
"What the sim caught"). D2 (tail-6000 extraction) was replaced with
anchored `extract_response`; D5 (blind True) with verified
`type_and_submit` + adopt-on-retry; D11 (dedup dropping repeats) no
longer matters because deltas are line-prefix based and extraction is
anchored on the submitted message.

Remaining honest gaps (Windows-only, by construction):
- pywinauto locator names ("Message ChatGPT", address bar, "Stop
  generating") vs. the live DOM — `relay_windows_check` read-only mode.
- Timing constants (poll 2s / stable 4s / settle 30s) are logic-
  validated, not latency-tuned against real ChatGPT.
