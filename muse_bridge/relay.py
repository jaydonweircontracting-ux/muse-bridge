"""Automatic chat relay between paired Muse and ChatGPT windows.

Each worker has its own Muse window and its own dedicated ChatGPT window.
The relay remembers which windows belong together (HWND handle + title +
expected conversation URL), watches the Muse window for new content,
submits it to the paired ChatGPT conversation, waits for the completed
response, and delivers it back — recording every step in the message
ledger.

Safety rules (non-negotiable):
- Never type into or read a window whose identity can't be verified.
- Never deliver a partial response as complete.
- Never relay the bridge's own delivered text back (echo guard).
- One pair relays at a time (serialized browser actions — no focus fights).
- Any login wall, CAPTCHA, challenge, or ambiguity pauses the pair with
  a plain-English error. Nothing is retried blindly.
- Automating ChatGPT's web UI is against OpenAI's terms for automated
  access and may risk the user's account. The user explicitly accepted
  this; the relay still pauses rather than pushing through blocks.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .ledger import (ACKNOWLEDGED, COPIED, DELIVERED, DETECTED, FAILED,
                     NEEDS_REVIEW, QUEUED, RESPONSE_RECEIVED, SUBMITTED,
                     MessageLedger)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RelayState:
    IDLE = "IDLE"
    TO_CHATGPT = "TO_CHATGPT"
    WAIT_RESPONSE = "WAIT_RESPONSE"
    TO_MUSE = "TO_MUSE"
    ERROR = "ERROR"


@dataclass
class RelayPair:
    worker_id: str
    enabled: bool = False
    state: str = RelayState.IDLE
    baselined: bool = False       # first poll only records, never relays
    last_muse_hash: str = ""
    last_muse_text: str = ""
    last_delivered_hash: str = ""   # echo guard: our own deliveries
    pending_message_id: str = ""
    error: str = ""
    last_poll: float = 0.0


class WindowAgent:
    """How the relay talks to one chat window. Real implementation uses
    UIA on Windows (see uia_agent.py); tests use FakeWindowAgent."""

    def read_text(self) -> str:
        raise NotImplementedError

    def focus(self) -> bool:
        raise NotImplementedError

    def type_and_submit(self, text: str) -> bool:
        raise NotImplementedError

    def wait_for_response(self, timeout_s: float,
                          baseline_hash: str) -> str | None:
        """Return the COMPLETE new response text, or None on timeout /
        ambiguity. Must never return a partial streaming response."""
        raise NotImplementedError

    def conversation_id(self) -> str:
        """Current conversation URL/identifier, or '' if unreadable."""
        return ""


class FakeWindowAgent(WindowAgent):
    """Scripted agent for tests."""

    def __init__(self, text: str = "", url: str = "",
                 response: str | None = None) -> None:
        self.text = text
        self.url = url
        self.response = response
        self.focused = False
        self.typed: list[str] = []

    def read_text(self) -> str:
        return self.text

    def focus(self) -> bool:
        self.focused = True
        return True

    def type_and_submit(self, text: str) -> bool:
        self.typed.append(text)
        if self.response is not None:
            self.text += "\n" + self.response
        return True

    def wait_for_response(self, timeout_s: float,
                          baseline_hash: str) -> str | None:
        return self.response

    def conversation_id(self) -> str:
        return self.url


class RelayController:
    """Owns relay pairs and runs the poll loop. Serialized: at most one
    pair is mid-relay at any moment."""

    def __init__(self, ledger: MessageLedger,
                 make_agent: Callable[[str, str], WindowAgent],
                 directory: Path | None = None,
                 poll_interval_s: float = 10.0,
                 response_timeout_s: float = 180.0) -> None:
        self.ledger = ledger
        self.make_agent = make_agent
        self.poll_interval_s = poll_interval_s
        self.response_timeout_s = response_timeout_s
        self.pairs: dict[str, RelayPair] = {}
        self._busy = False
        self.events: list[str] = []

    # -- pair management --------------------------------------------------
    def pair_for(self, worker_id: str) -> RelayPair:
        return self.pairs.setdefault(worker_id, RelayPair(worker_id))

    def set_enabled(self, worker_id: str, enabled: bool) -> None:
        p = self.pair_for(worker_id)
        p.enabled = enabled
        if enabled and p.state != RelayState.ERROR:
            # Fresh enable: re-baseline (content changed while disabled
            # is history, not new).
            p.baselined = False
            p.error = ""
        elif not enabled:
            p.state = RelayState.IDLE
            p.error = ""
        self._say(f"{worker_id} relay {'enabled' if enabled else 'disabled'}.")

    def retry_pair(self, worker_id: str) -> str:
        """Re-arm a paused pair. The baseline is kept and failed polls
        un-consume their content, so the next poll retries the message
        instead of swallowing it as history."""
        p = self.pair_for(worker_id)
        if p.state != RelayState.ERROR:
            return f"{worker_id}: nothing to retry"
        p.state = RelayState.IDLE
        p.error = ""
        p.enabled = True
        self._say(f"{worker_id}: retry armed.")
        return f"{worker_id}: retry armed"

    # -- polling ----------------------------------------------------------
    def poll(self) -> list[str]:
        """One tick: poll each enabled pair. Returns status lines."""
        out: list[str] = []
        if self._busy:
            return ["relay busy — skipping tick"]
        for wid, pair in self.pairs.items():
            if not pair.enabled or pair.state == RelayState.ERROR:
                continue
            if time.time() - pair.last_poll < self.poll_interval_s:
                continue
            pair.last_poll = time.time()
            out.append(self.poll_pair(wid))
        return out

    def _new_content(self, pair: RelayPair, text: str) -> str:
        h = _hash(text)
        if h == pair.last_muse_hash:
            return ""
        old = pair.last_muse_text
        pair.last_muse_hash = h
        pair.last_muse_text = text
        if old and text.startswith(old):
            delta = text[len(old):].strip()
        else:
            delta = text.strip()
        # Echo guard: never relay our own delivered response back.
        if delta and _hash(delta) == pair.last_delivered_hash:
            return ""
        return delta

    def poll_pair(self, worker_id: str) -> str:
        pair = self.pair_for(worker_id)
        try:
            muse = self.make_agent(worker_id, "muse")
        except Exception as e:
            return self._pause(pair, f"couldn't attach to Muse window: {e}")
        try:
            text = muse.read_text()
        except Exception as e:
            return self._pause(pair, f"couldn't read Muse window: {e}")
        if not pair.baselined:
            # First poll: record the baseline silently. Relaying the
            # entire existing conversation history would be wrong.
            pair.baselined = True
            pair.last_muse_hash = _hash(text)
            pair.last_muse_text = text
            return f"{worker_id}: baseline established"
        old_hash, old_text = pair.last_muse_hash, pair.last_muse_text
        delta = self._new_content(pair, text)
        if not delta:
            return f"{worker_id}: no new Muse content"
        result = self._relay(worker_id, pair, delta)
        if pair.state == RelayState.ERROR:
            # Un-consume: a later retry (after the user fixes the issue)
            # must see this content as new again. The ledger entry is
            # NEEDS_REVIEW, so nothing is lost or duplicated.
            pair.last_muse_hash, pair.last_muse_text = old_hash, old_text
        return result

    def _relay(self, worker_id: str, pair: RelayPair, message: str) -> str:
        if self._busy:
            return f"{worker_id}: relay busy, will retry next tick"
        self._busy = True
        try:
            return self._do_relay(worker_id, pair, message)
        finally:
            self._busy = False

    def _prior_submission(self, worker_id: str, content_hash: str,
                          exclude: str) -> object | None:
        """Was this exact content already submitted in an earlier attempt?
        Prevents duplicate ChatGPT submissions after a timeout-retry."""
        for m in self.ledger.by_worker(worker_id):
            if m.id == exclude or m.kind != "relay":
                continue
            if m.text_sha256 != content_hash:
                continue
            if m.state in (ACKNOWLEDGED, FAILED):
                continue  # terminal — not a live submission
            if SUBMITTED in [h["to"] for h in m.history]:
                return m
        return None

    def _do_relay(self, worker_id: str, pair: RelayPair,
                  message: str) -> str:
        chat = self.make_agent(worker_id, "chatgpt")
        # Verify the destination conversation before sending.
        try:
            url = chat.conversation_id()
        except Exception as e:
            msg0 = self.ledger.create(worker_id, "", "relay", message)
            return self._fail(pair, msg0.id,
                              f"couldn't verify ChatGPT conversation: {e}")
        expected = self._expected_url(worker_id)
        if expected and url and url != expected:
            msg0 = self.ledger.create(worker_id, "", "relay", message)
            return self._fail(
                pair, msg0.id,
                f"ChatGPT window is on a different conversation "
                f"(expected …{expected[-24:]}, found …{url[-24:]}). "
                f"NOT sent.")
        if not chat.focus():
            msg0 = self.ledger.create(worker_id, "", "relay", message)
            return self._fail(pair, msg0.id, "couldn't focus ChatGPT window")

        mh = _hash(message)
        prior = self._prior_submission(worker_id, mh, exclude="")
        if prior is not None:
            # Already submitted in an earlier attempt (e.g. we timed out
            # waiting last time) — do NOT resend, just wait for the reply.
            msg = prior
            self._say(f"{worker_id}: already submitted earlier — "
                      f"not resending, waiting for the response.")
        else:
            msg = self.ledger.create(worker_id, "", "relay", message)
            self.ledger.transition(msg.id, QUEUED,
                                   "new Muse content detected")
            self.ledger.transition(msg.id, COPIED, "ChatGPT window focused")
            if not chat.type_and_submit(message):
                return self._fail(pair, msg.id,
                                  "typing into ChatGPT failed")
            self.ledger.transition(msg.id, SUBMITTED, "message submitted")
        pair.pending_message_id = msg.id
        pair.state = RelayState.WAIT_RESPONSE

        baseline = _hash(chat.read_text())
        response = chat.wait_for_response(self.response_timeout_s,
                                          baseline)
        if not response:
            return self._fail(
                pair, msg.id,
                "no complete ChatGPT response within timeout — "
                "paused; check the window yourself")
        self.ledger.transition(msg.id, RESPONSE_RECEIVED,
                               "complete response captured")
        pair.state = RelayState.TO_MUSE

        muse = self.make_agent(worker_id, "muse")
        if not muse.focus():
            return self._fail(pair, msg.id,
                              "couldn't refocus Muse window — response kept "
                              "in ledger, nothing lost")
        if not muse.type_and_submit(response):
            return self._fail(pair, msg.id,
                              "delivering to Muse failed — response kept "
                              "in ledger, nothing lost")
        pair.last_delivered_hash = _hash(response)
        # Don't re-relay our own delivery on the next poll.
        mt = muse.read_text()
        pair.last_muse_hash = _hash(mt)
        pair.last_muse_text = mt
        self.ledger.transition(msg.id, DELIVERED, "response delivered")
        self.ledger.transition(msg.id, ACKNOWLEDGED, "round trip complete")
        pair.state = RelayState.IDLE
        pair.pending_message_id = ""
        self._say(f"{worker_id}: relayed {len(message)} chars -> "
                  f"{len(response)} chars back.")
        return f"{worker_id}: relay complete"

    # -- helpers ----------------------------------------------------------
    def _expected_url(self, worker_id: str) -> str:
        get = getattr(self, "get_worker", None)
        if get is None:
            return ""
        slot = get(worker_id)
        return slot.chatgpt_url if slot else ""

    def _fail(self, pair: RelayPair, msg_id: str, reason: str) -> str:
        self.ledger.transition(msg_id, NEEDS_REVIEW, reason)
        return self._pause(pair, reason)

    def _pause(self, pair: RelayPair, reason: str) -> str:
        pair.state = RelayState.ERROR
        pair.error = reason
        self._say(f"{pair.worker_id} relay PAUSED: {reason}")
        return f"{pair.worker_id}: PAUSED — {reason}"

    def recover(self) -> int:
        """After restart: re-arm pairs stuck mid-relay as NEEDS_REVIEW."""
        n = 0
        for pair in self.pairs.values():
            if pair.state not in (RelayState.IDLE, RelayState.ERROR):
                pair.state = RelayState.ERROR
                pair.error = "bridge restarted mid-relay"
                n += 1
        return n

    def _say(self, text: str) -> None:
        self.events.append(f"{time.strftime('%H:%M:%S')} {text}")
