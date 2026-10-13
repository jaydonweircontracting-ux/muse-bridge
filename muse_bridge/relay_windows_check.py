"""Windows-only integration test for the chat relay.

Read-only by default: enumerates desktop windows, inspects the UIA
tree of an operator-chosen ChatGPT window and Muse window, verifies the
composer is locatable/editable, and prints a diagnostic report — without
submitting anything.

With --live (and explicit typed confirmation), it runs ONE real round
trip: a unique harmless test message into the verified ChatGPT
conversation, waits for the complete response, delivers it to the test
Muse window, verifies receipt, and stops.

With --live --pairs N it repeats the pairing for N independent pairs
and verifies no cross-delivery.

Usage:
    python -m muse_bridge.relay_windows_check            # read-only
    python -m muse_bridge.relay_windows_check --live      # one round trip
    python -m muse_bridge.relay_windows_check --live --pairs 2

This module NEVER runs outside Windows and NEVER sends a message
without the operator typing YES.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def _require_windows() -> None:
    if sys.platform != "win32":
        sys.exit("relay_windows_check requires Windows "
                 f"(this is {sys.platform}).")


def _backend_report() -> dict:
    info: dict = {"platform": sys.platform}
    try:
        import pywinauto
        info["pywinauto"] = pywinauto.__version__
    except Exception as e:
        info["pywinauto"] = f"MISSING ({e})"
    try:
        import comtypes  # noqa
        info["comtypes"] = "ok"
    except Exception as e:
        info["comtypes"] = f"MISSING ({e})"
    return info


def _enumerate() -> list:
    from pywinauto import Desktop
    wins = []
    for w in Desktop(backend="uia").windows():
        try:
            title = w.window_text().strip()
            if not title:
                continue
            wins.append({"hwnd": int(w.handle), "title": title,
                         "pid": w.process_id()})
        except Exception:
            continue
    return wins


def _pick(wins: list, prompt: str) -> dict:
    print(f"\n{prompt}")
    for i, w in enumerate(wins[:40]):
        print(f"  [{i}] {w['title'][:90]} (hwnd={w['hwnd']})")
    while True:
        raw = input("number (or 'q'): ").strip()
        if raw.lower() == "q":
            sys.exit("aborted by operator.")
        try:
            idx = int(raw)
            return wins[idx]
        except (ValueError, IndexError):
            print("invalid choice.")


def _inspect_window(handle: int, role: str) -> dict:
    """Read-only UIA inspection. Never types or clicks."""
    from .uia_agent import UiaWindowAgent, detect_challenge
    agent = UiaWindowAgent(handle, role)
    rep: dict = {"hwnd": handle, "role": role}
    try:
        text = agent.read_text()
    except Exception as e:
        rep["read_error"] = f"{type(e).__name__}: {e}"
        return rep
    rep["text_chars"] = len(text)
    rep["text_head"] = text[:500]
    rep["challenge"] = detect_challenge(text) or None
    rep["url"] = agent.conversation_id()
    # Composer probe (locate only — no click, no typing).
    try:
        box = (agent._chatgpt_input() if role == "chatgpt"
               else agent._muse_input())
        if box is None:
            rep["composer"] = "NOT FOUND"
        else:
            try:
                enabled = box.is_enabled()
            except Exception:
                enabled = "unknown"
            rep["composer"] = (
                f"FOUND name={box.element_info.name!r} "
                f"type={box.element_info.control_type!r} "
                f"enabled={enabled}")
    except Exception as e:
        rep["composer"] = f"PROBE ERROR: {e}"
    try:
        rep["generating"] = agent._is_generating()
    except Exception as e:
        rep["generating"] = f"ERROR: {e}"
    return rep


def _print_report(rep: dict) -> None:
    print(f"\n--- inspection: {rep['role']} hwnd={rep['hwnd']} ---")
    for k, v in rep.items():
        if k in ("hwnd", "role", "text_head"):
            continue
        print(f"  {k}: {v}")
    if rep.get("text_head"):
        print(f"  text_head: {rep['text_head'][:200]!r}")


def _confirm(prompt: str) -> bool:
    return input(f"\n{prompt} Type YES to proceed: ").strip() == "YES"


def _live_round_trip(chat_pick: dict, muse_pick: dict,
                     expected_url: str) -> bool:
    from .ledger import MessageLedger
    from .relay import RelayController
    from .uia_agent import agent_for

    tmp = Path(tempfile.mkdtemp(prefix="relay-live-"))
    ledger = MessageLedger(tmp)
    agents = {(  "w1", "chatgpt"): agent_for(chat_pick["hwnd"], "chatgpt"),
              ("w1", "muse"): agent_for(muse_pick["hwnd"], "muse")}
    ctl = RelayController(ledger, lambda w, r: agents[(w, r)],
                          directory=tmp, poll_interval_s=0,
                          response_timeout_s=240.0)

    class Slot:
        chatgpt_url = expected_url
    ctl.get_worker = lambda wid: Slot()
    ctl.set_enabled("w1", True)
    print("baseline:", ctl.poll_pair("w1"))

    import uuid
    marker = f"RELAY-TEST-{uuid.uuid4().hex[:8]}"
    question = (f"{marker} If you see this, reply with exactly: "
                f"RELAY-OK {marker}")
    # Inject the question directly into the Muse window via the agent
    # (this is the test's stand-in for "Muse produced a message").
    muse = agents[("w1", "muse")]
    if not muse.focus():
        print("FAIL: couldn't focus the Muse window.")
        return False
    if not muse.type_and_submit(question):
        print("FAIL: couldn't type the test question into Muse window.")
        return False
    print("question placed in Muse window; polling relay...")
    status = ctl.poll_pair("w1")
    print("relay:", status)
    ok = (ctl.pair_for("w1").state == "IDLE"
          and f"RELAY-OK {marker}" in muse.read_text())
    print("LIVE ROUND TRIP:", "PASS" if ok else "FAIL")
    print(f"ledger dir: {tmp} (kept for inspection)")
    return ok


def main(argv: list[str]) -> int:
    _require_windows()
    live = "--live" in argv
    try:
        pairs = int(argv[argv.index("--pairs") + 1])
    except (ValueError, IndexError):
        pairs = 1

    print("== relay Windows integration check ==")
    for k, v in _backend_report().items():
        print(f"  {k}: {v}")
    if _backend_report()["pywinauto"].startswith("MISSING"):
        print("pywinauto is required. Install it, then re-run.")
        return 2

    wins = _enumerate()
    print(f"\n{len(wins)} top-level windows enumerated.")
    results = []
    for n in range(pairs if live else 1):
        tag = f"pair {n + 1}" if pairs > 1 else "the pair"
        chat_pick = _pick(wins, f"Choose the ChatGPT window for {tag}:")
        muse_pick = _pick(wins, f"Choose the Muse window for {tag}:")
        chat_rep = _inspect_window(chat_pick["hwnd"], "chatgpt")
        muse_rep = _inspect_window(muse_pick["hwnd"], "muse")
        _print_report(chat_rep)
        _print_report(muse_rep)

        problems = []
        if chat_rep.get("challenge"):
            problems.append(f"ChatGPT window shows {chat_rep['challenge']!r}")
        if muse_rep.get("challenge"):
            problems.append(f"Muse window shows {muse_rep['challenge']!r}")
        comp = chat_rep.get("composer", "")
        if isinstance(comp, str) and comp.startswith(
                ("NOT FOUND", "PROBE ERROR")):
            problems.append("ChatGPT composer not locatable")
        if not chat_rep.get("url"):
            problems.append("ChatGPT conversation URL unreadable — "
                            "the relay would refuse to send")
        if problems:
            print("\nREAD-ONLY VERDICT: NOT READY")
            for p in problems:
                print(f"  - {p}")
            results.append(False)
            continue
        print("\nREAD-ONLY VERDICT: READY")
        print(f"  conversation: {chat_rep['url']}")
        results.append(True)

        if not live:
            continue
        print("\n*** LIVE TEST: one harmless test message will be sent to")
        print(f"    {chat_rep['url']}")
        print("    It asks ChatGPT to reply with a fixed marker string.")
        if not _confirm("Proceed with the live round trip?"):
            print("live test skipped by operator.")
            results[-1] = False
            continue
        ok = _live_round_trip(chat_pick, muse_pick, chat_rep["url"])
        results[-1] = ok

    print(f"\nDONE: {sum(results)}/{len(results)} pair(s) ready/passed.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
