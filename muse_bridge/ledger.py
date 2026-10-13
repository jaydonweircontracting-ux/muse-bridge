"""Durable message ledger for Muse Bridge.

Every relay step (handoff prompt, review packet) gets a unique ID and an
explicit state. The ledger is the bridge's memory of what was handed to
the user, what was confirmed, and what still needs attention — it is
what makes restarts and crashes recoverable without losing or duplicating
work.

Honest scope: in this edition the bridge never submits into ChatGPT or
Muse windows on its own. "Sent" states therefore mean "handed to the user
via clipboard + window focus"; confirmations are the user's explicit
button presses, which is also what makes duplicate detection reliable.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# Explicit lifecycle. Persisted before the action they describe, never after.
DETECTED = "DETECTED"            # bridge created the message
QUEUED = "QUEUED"                # assigned to a worker, awaiting handoff
COPIED = "COPIED"                # prompt copied to clipboard, window focused
SUBMITTED = "SUBMITTED"          # user confirmed they pasted/submitted it
RESPONSE_RECEIVED = "RESPONSE_RECEIVED"  # user transferred the report back
DELIVERED = "DELIVERED"          # report attached to the task
ACKNOWLEDGED = "ACKNOWLEDGED"    # task done confirmed — terminal
FAILED = "FAILED"                # gave up (retry limit) — terminal-ish
NEEDS_REVIEW = "NEEDS_REVIEW"    # ambiguous (e.g. crash mid-flight)

TERMINAL = {ACKNOWLEDGED, FAILED}

TRANSITIONS: dict[str, set[str]] = {
    DETECTED: {QUEUED, FAILED},
    QUEUED: {COPIED, FAILED},
    COPIED: {SUBMITTED, NEEDS_REVIEW, FAILED},
    SUBMITTED: {RESPONSE_RECEIVED, NEEDS_REVIEW, FAILED},
    RESPONSE_RECEIVED: {DELIVERED, NEEDS_REVIEW, FAILED},
    DELIVERED: {ACKNOWLEDGED, NEEDS_REVIEW, FAILED},
    NEEDS_REVIEW: {QUEUED, ACKNOWLEDGED, FAILED},
    FAILED: {QUEUED},            # manual retry only
    ACKNOWLEDGED: set(),
}


@dataclass
class Message:
    id: str
    worker_id: str
    task_id: str
    kind: str                    # "handoff" | "review_packet"
    text: str                    # local only — never committed to git
    text_sha256: str = ""
    created_at: float = 0.0
    state: str = DETECTED
    attempts: int = 0
    last_error: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex
        if not self.text_sha256:
            self.text_sha256 = hashlib.sha256(
                self.text.encode("utf-8")).hexdigest()
        if not self.created_at:
            self.created_at = time.time()


class MessageLedger:
    """Owns message records; persists to messages.json."""

    def __init__(self, directory: Path | None = None) -> None:
        from .store import data_dir
        self.dir = directory or data_dir()
        self.path = self.dir / "messages.json"
        self.messages: dict[str, Message] = self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> dict[str, Message]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        known = {f.name for f in Message.__dataclass_fields__.values()}
        out: dict[str, Message] = {}
        for mid, rec in raw.items():
            try:
                out[mid] = Message(
                    **{k: v for k, v in rec.items() if k in known})
            except TypeError:
                continue
        return out

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {mid: asdict(m) for mid, m in self.messages.items()},
            indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # -- lifecycle --------------------------------------------------------
    def create(self, worker_id: str, task_id: str, kind: str,
               text: str) -> Message:
        m = Message(id="", worker_id=worker_id, task_id=task_id,
                    kind=kind, text=text)
        m.history.append({"t": time.time(), "from": "",
                          "to": DETECTED, "note": "created"})
        self.messages[m.id] = m
        self._save()
        return m

    def transition(self, msg_id: str, new_state: str,
                   note: str = "") -> bool:
        m = self.messages.get(msg_id)
        if not m or new_state not in TRANSITIONS.get(m.state, set()):
            return False
        old = m.state
        m.state = new_state
        if new_state in (QUEUED, COPIED):
            m.attempts += 1
        if note:
            m.last_error = note if new_state in (FAILED, NEEDS_REVIEW) else ""
        m.history.append({"t": time.time(), "from": old,
                          "to": new_state, "note": note})
        self._save()
        return True

    def get(self, msg_id: str) -> Message | None:
        return self.messages.get(msg_id)

    def pending(self) -> list[Message]:
        """Not in a terminal state — work the bridge still owes someone."""
        return [m for m in self.messages.values()
                if m.state not in TERMINAL]

    def by_worker(self, worker_id: str) -> list[Message]:
        return [m for m in self.messages.values()
                if m.worker_id == worker_id]

    def already_submitted(self, worker_id: str,
                          text_sha256: str) -> Message | None:
        """Duplicate protection: was this exact prompt already confirmed
        submitted for this worker? Prevents double-confirm after a crash."""
        for m in self.by_worker(worker_id):
            if (m.text_sha256 == text_sha256
                    and m.state in (SUBMITTED, RESPONSE_RECEIVED,
                                    DELIVERED, ACKNOWLEDGED)):
                return m
        return None

    def recover(self) -> list[Message]:
        """Crash/restart recovery: anything left mid-flight (copied but
        never confirmed, submitted but no report) becomes NEEDS_REVIEW
        instead of being blindly repeated."""
        moved = []
        for m in self.pending():
            if m.state in (COPIED, SUBMITTED, RESPONSE_RECEIVED):
                if self.transition(m.id, NEEDS_REVIEW,
                                   "bridge restarted mid-flight"):
                    moved.append(m)
        return moved

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for m in self.messages.values():
            c[m.state] = c.get(m.state, 0) + 1
        return c
