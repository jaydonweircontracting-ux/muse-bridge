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
  access and may risk the user's account. The relay is opt-in per pair
  with an explicit consent prompt; it still pauses rather than pushing
  through blocks.
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


class ChallengeDetected(RuntimeError):
    """A window agent found a login wall, CAPTCHA, rate-limit, or error
    page. Part of the WindowAgent contract: the relay pauses the pair
    instead of automating against the blocker."""


def message_in_text(text: str, message: str) -> bool:
    """Is our submitted message visible in the conversation text? Used
    for the adopt-don't-resend check. Lives here (not uia_agent) so both
    the orchestration and the agents share one implementation without a
    circular import."""
    marker = message.strip()
    return bool(marker) and marker[:500] in text


def _extract_delta(old: str, new: str) -> str:
    """Identify the new tail of `new` relative to `old`.

    Returns "" when nothing new is identifiable. Never returns the whole
    conversation: if `old` isn't recognizable as a prefix (UI re-render,
    reorder, cleared conversation), we refuse to guess rather than
    relaying everything as the question.
    """
    if not old:
        return new.strip()
    if new.startswith(old):
        return new[len(old):].strip()
    old_lines = [ln.strip() for ln in old.splitlines()]
    new_lines = [ln.strip() for ln in new.splitlines()]
    i = 0
    while (i < len(old_lines) and i < len(new_lines)
           and old_lines[i] == new_lines[i]):
        i += 1
    if i == 0:
        return ""
    return "\n".join(new_lines[i:]).strip()


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
                          baseline_hash: str,
                          submitted_text: str = "") -> str | None:
        """Return the COMPLETE new response text, or None on timeout /
        ambiguity. Must never return a partial streaming response.
        Raises ChallengeDetected on login walls / CAPTCHAs / errors."""
        raise NotImplementedError

    def conversation_id(self) -> str:
        """Current conversation URL/identifier, or '' if unreadable."""
        return ""

    def stable_response_to(self, submitted_text: str,
                           timeout_s: float = 10.0) -> str | None:
        """Adopt path: the message was already submitted by an earlier
        attempt and its response may already be sitting stable in the
        conversation. Return the complete response if the window is
        currently stable (no generating indicator, text unchanged for
        the timeout); otherwise None. Default: unsupported."""
        return None


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
                          baseline_hash: str,
                          submitted_text: str = "") -> str | None:
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
        # Pair baselines survive restarts: without them, an in-flight
        # message would silently become history after a crash instead of
        # being retried (ledger NEEDS_REVIEW is the backstop).
        from .store import data_dir
        self.state_path = (directory or data_dir()) / "relay_pairs.json"
        self._load_state()

    # -- persistence ------------------------------------------------------
    def _save_state(self) -> None:
        try:
            import json
            raw = {wid: {"enabled": p.enabled, "state": p.state,
                         "baselined": p.baselined,
                         "last_muse_hash": p.last_muse_hash,
                         "last_muse_text": p.last_muse_text[-4000:],
                         "last_delivered_hash": p.last_delivered_hash,
                         "error": p.error[:300]}
                   for wid, p in self.pairs.items()}
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception:
            pass

    def _load_state(self) -> None:
        try:
            import json
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for wid, rec in raw.items():
            p = self.pair_for(wid)
            p.enabled = bool(rec.get("enabled"))
            p.baselined = bool(rec.get("baselined"))
            p.last_muse_hash = rec.get("last_muse_hash", "")
            p.last_muse_text = rec.get("last_muse_text", "")
            p.last_delivered_hash = rec.get("last_delivered_hash", "")
            state = rec.get("state", RelayState.IDLE)
            if state not in (RelayState.IDLE, RelayState.ERROR):
                # Crashed mid-relay: the ledger entry is NEEDS_REVIEW;
                # the pair waits for an explicit retry.
                p.state = RelayState.ERROR
                p.error = "bridge restarted mid-relay"
            else:
                p.state = state
                p.error = rec.get("error", "")

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
        self._save_state()

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
        self._save_state()
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

    def poll_pair(self, worker_id: str) -> str:
        pair = self.pair_for(worker_id)
        try:
            muse = self.make_agent(worker_id, "muse")
        except Exception as e:
            return self._pause(pair, f"couldn't attach to Muse window: {e}")
        try:
            text = muse.read_text()
        except ChallengeDetected as e:
            return self._pause(pair, f"Muse window blocked ({e})")
        except Exception as e:
            return self._pause(pair, f"couldn't read Muse window: {e}")
        if not pair.baselined:
            # First poll: record the baseline silently. Relaying the
            # entire existing conversation history would be wrong.
            pair.baselined = True
            pair.last_muse_hash = _hash(text)
            pair.last_muse_text = text
            self._save_state()
            return f"{worker_id}: baseline established"
        old_hash, old_text = pair.last_muse_hash, pair.last_muse_text
        new_hash = _hash(text)
        if new_hash == old_hash:
            return f"{worker_id}: no new Muse content"
        delta = _extract_delta(old_text, text)
        # Echo guard: never relay our own delivered response back.
        if delta and _hash(delta) == pair.last_delivered_hash:
            delta = ""
        if not delta:
            # Content changed but nothing new is identifiable (UI
            # re-render, reorder, cleared conversation). Never relay the
            # whole conversation as a "question" — re-baseline and say
            # so; the user can trigger a missed message with Relay now.
            pair.last_muse_hash = new_hash
            pair.last_muse_text = text
            self._say(f"{worker_id}: Muse window changed in an "
                      f"unrecognized way — re-baselined, nothing relayed.")
            return f"{worker_id}: unrecognized change — re-baselined"
        pair.last_muse_hash = new_hash
        pair.last_muse_text = text
        self._save_state()
        result = self._relay(worker_id, pair, delta)
        if pair.state == RelayState.ERROR:
            # Un-consume: a later retry (after the user fixes the issue)
            # must see this content as new again. The ledger entry is
            # NEEDS_REVIEW, so nothing is lost or duplicated.
            pair.last_muse_hash, pair.last_muse_text = old_hash, old_text
            self._save_state()
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
        try:
            chat = self.make_agent(worker_id, "chatgpt")
        except Exception as e:
            msg0 = self.ledger.create(worker_id, "", "relay", message)
            return self._fail(pair, msg0.id,
                              f"couldn't attach to ChatGPT window: {e}")
        try:
            return self._do_relay_inner(worker_id, pair, message, chat)
        except ChallengeDetected as e:
            mid = (pair.pending_message_id
                   or self.ledger.create(
                       worker_id, "", "relay", message).id)
            return self._fail(pair, mid, f"blocked page ({e}) — NOT sent")
        except Exception as e:
            mid = (pair.pending_message_id
                   or self.ledger.create(
                       worker_id, "", "relay", message).id)
            return self._fail(pair, mid, f"relay error: {e}")

    def _verify_conversation(self, chat: WindowAgent,
                             worker_id: str) -> str:
        """Return the verified URL or raise. A matching window title is
        never sufficient proof of conversation identity."""
        try:
            url = chat.conversation_id()
        except ChallengeDetected:
            raise
        except Exception as e:
            raise RuntimeError(
                f"couldn't verify ChatGPT conversation: {e}")
        expected = self._expected_url(worker_id)
        if not expected:
            raise RuntimeError(
                "no expected conversation URL on file — re-map the "
                "ChatGPT window so its URL is captured. NOT sent.")
        if not url:
            raise RuntimeError(
                "couldn't read the ChatGPT conversation URL — refusing "
                "to send to an unverified conversation. NOT sent.")
        if url != expected:
            raise RuntimeError(
                f"ChatGPT window is on a different conversation "
                f"(expected …{expected[-24:]}, found …{url[-24:]}). "
                f"NOT sent.")
        return url

    def _do_relay_inner(self, worker_id: str, pair: RelayPair,
                        message: str, chat: WindowAgent) -> str:
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
        pair.pending_message_id = msg.id

        # Verify the destination conversation BEFORE anything is typed.
        self._verify_conversation(chat, worker_id)
        if not chat.focus():
            raise RuntimeError("couldn't focus ChatGPT window")
        self.ledger.transition(msg.id, COPIED, "ChatGPT window focused")

        if prior is None:
            before = chat.read_text()
            baseline_hash = _hash(before)
            # Adopt, don't resend: an earlier attempt may have submitted
            # while its verification was still uncertain.
            adopted = message_in_text(before, message)
            if adopted:
                self._say(f"{worker_id}: message already present from an "
                          f"earlier attempt — adopted, not resent.")
            elif not chat.type_and_submit(message):
                raise RuntimeError(
                    "typing into ChatGPT failed (composer missing, "
                    "disabled, or submit unverified)")
            self.ledger.transition(msg.id, SUBMITTED, "message submitted")
            self.ledger.set_relay_fields(
                msg.id, submit_baseline_hash=baseline_hash)
            submit_baseline = baseline_hash
        else:
            adopted = False
            submit_baseline = (prior.submit_baseline_hash
                               or _hash(chat.read_text()))
        pair.state = RelayState.WAIT_RESPONSE

        # Fast paths: an earlier attempt may already hold the response.
        response = prior.response_text if prior else ""
        if not response and adopted:
            # The message was submitted by the earlier attempt; its
            # answer may already be sitting stable in the conversation.
            # Confirm stability instead of waiting for new changes.
            try:
                response = chat.stable_response_to(message,
                                                   timeout_s=10.0) or ""
            except ChallengeDetected:
                raise
            except Exception:
                response = ""
        if not response:
            response = chat.wait_for_response(self.response_timeout_s,
                                              submit_baseline, message)
        if not response:
            raise RuntimeError(
                "no complete ChatGPT response within timeout — "
                "paused; check the window yourself")
        # The user could have switched conversations mid-wait: verify
        # AGAIN before the response goes anywhere.
        self._verify_conversation(chat, worker_id)
        self.ledger.transition(msg.id, RESPONSE_RECEIVED,
                               "complete response captured")
        self.ledger.set_relay_fields(msg.id, response_text=response)
        pair.state = RelayState.TO_MUSE

        muse = self.make_agent(worker_id, "muse")
        if not muse.focus():
            raise RuntimeError(
                "couldn't refocus Muse window — response kept "
                "in ledger, nothing lost")
        muse_text = muse.read_text()
        if response.strip() and response.strip() in muse_text:
            # Crash between delivery and the ledger write, then retry:
            # the response is already there — never retype it.
            self._say(f"{worker_id}: response already present in the "
                      f"Muse window — not retyped.")
        elif not muse.type_and_submit(response):
            raise RuntimeError(
                "delivering to Muse failed — response kept "
                "in ledger, nothing lost")
        pair.last_delivered_hash = _hash(response.strip())
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
        self._save_state()
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
        self._save_state()
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
