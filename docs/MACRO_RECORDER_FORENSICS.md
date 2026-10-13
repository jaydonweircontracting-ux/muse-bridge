# Macro Recorder Pro — Technical Forensics Report

**Date:** 2026-10-13
**Analyst:** Muse Bridge project
**Subject file:** `96215AEE-62A8-436B-A4B1-751179C352D4-MacroRecorderSetup.exe`

> Clean-room note: this report documents only facts observable from the
> supplied installer file itself. No proprietary source code was accessed,
> no licensing was circumvented, and no runtime testing was performed.

## 1. File inventory (CONFIRMED FROM FILES)

| Fact | Value |
|---|---|
| File name | 96215AEE-62A8-436B-A4B1-751179C352D4-MacroRecorderSetup.exe |
| Size | 40,732,296 bytes (~38.8 MiB) |
| SHA-256 | `176c438974260b384392c81ea958b338087bd1301c40b77e7bb904c5763264bb` |
| File type | PE32 executable (GUI), Intel 80386, 11 sections |
| Product | MacroRecorder |
| Version | 5.0.40 |
| Publisher | Bartels Media GmbH |
| Copyright | (c) Bartels Media GmbH |
| Installer tech | Inno Setup 6.5.0 (setup data v7.0.0.3) |
| Installer stub | Built with Embarcadero Delphi (standard for Inno Setup) |
| Stub imports | advapi32, comctl32, oleaut32, user32, kernel32, ntdll, bcrypt (all standard Windows) |

## 2. What could NOT be inspected (UNKNOWN / NOT ACCESSIBLE)

- **Installed file list:** the Inno data section (v7.0.0.3) cannot be
  extracted with available tools — `innoextract` 1.9 supports only up to
  Inno 6.0.5 and fails with "could not determine setup data version".
  The embedded file table is compressed; string-level carving of the
  binary yields only OS DLL names, no application file names.
- **Application architecture, hotkey implementation, macro file format,
  configuration storage:** not observable without executing the
  installer on Windows.
- **Recording/playback internals:** not present in the installer stub.

## 3. Design patterns worth reusing (INFERRED from product category)

These are standard, well-documented Windows automation patterns — not
claims about MacroRecorder's internals:

1. **Global hotkeys via `RegisterHotKey`** — the same API Muse Bridge
   already uses. Standard practice: one hotkey per action, mod+VK,
   thread-owned message loop. (Already implemented in `hotkeys.py`.)
2. **Window targeting by handle, not title** — robust macro tools resolve
   a window once (by title/process at selection time) then keep the
   `HWND`; they re-validate with `IsWindow()` before acting. Titles are
   for humans; handles are for machines. This is the correct upgrade
   for the bridge's title-based focus.
3. **Input via `SendInput`, not keystroke logs** — synthetic input goes
   through the OS input queue so the target app can't distinguish it
   from a real keyboard. Playback timing uses explicit delays between
   events, not wall-clock sleeps alone.
4. **Focus verification before typing** — check `GetForegroundWindow()
   == target HWND` after focusing; abort the send if focus didn't land.
5. **Bounded waits** — every "wait for X" has a timeout and a failure
   path; nothing blocks forever on a window that will never appear.
6. **Profiles as data files** — macros/settings serialized to local
   files, versioned, never mixed with the executable.

## 4. Feature comparison: MacroRecorder vs Muse Bridge

| Capability | MacroRecorder (category) | Muse Bridge (today) |
|---|---|---|
| Global hotkeys | yes (core feature) | yes — F1/F2/F3, configurable, no mouse, debounce, conflict detect |
| Window targeting | HWND-based | title-based (weaker — see §5) |
| Record/playback input | yes | no — out of scope by design |
| Per-pair routing | n/a (single-stream tool) | yes — worker↔ChatGPT pair registry |
| Message ledger / dedup | n/a | yes — `ledger.py` |
| Destination verification | focus check | exact-title re-check before send |
| Crash recovery | session restore | ledger NEEDS_REVIEW + recovery.json |

## 5. Recommended bridge improvements from this analysis

1. **Resolve windows to HWNDs at map time** (`muse_link`): keep the
   title for display, but store the window handle and re-validate with
   it; fall back to title re-search when the handle dies. This is the
   single biggest robustness win and directly addresses "don't rely on
   titles alone".
2. **Focus verification before typing**: after `focus_window`, confirm
   the foreground window is the target before any clipboard/focus step.
3. **Bounded waits everywhere**: any future "wait for response" step
   gets an explicit timeout + NEEDS_REVIEW fallback, never an open wait.
4. Nothing in this analysis suggests copying MacroRecorder's
   record/playback engine — the bridge's job is routing and state, not
   input macros.

## 6. Tests still required on the user's PC (BLOCKED here)

- Install MacroRecorder in a sandbox and observe: hotkey registration
  behavior with conflicts, what happens when a target window closes
  mid-playback, config file format/location, tray behavior.
- For the bridge: real UIA reads of ChatGPT/Muse windows (see relay
  implementation notes).

## 7. Risk assessment

- **Static analysis risk:** none — read-only inspection of a file the
  user supplied.
- **Clean-room status:** no proprietary code was extracted or reproduced;
  only public Windows API patterns are referenced.
- **Account risk (bridge relay):** automating ChatGPT's web UI is
  against OpenAI's terms for automated access; the user's account could
  be flagged. The user has been warned and explicitly accepted this.
  The relay therefore pauses (never retries blindly) on login walls,
  CAPTCHAs, or any challenge.
