"""Multi-account manager for Muse Bridge (BUILD 2).

Each account is a Meta login (Facebook, Instagram, email, or phone backed —
per Meta's own sign-in options) that can reach a Muse session. The bridge:

  - lets the user add/label accounts,
  - launches an ISOLATED browser profile per account for the normal
    Meta login flow (the user signs in themselves),
  - never stores passwords, tokens, or cookies — only labels, statuses,
    and profile-directory paths,
  - binds verified sessions to worker slots (one session per slot),
  - never copies authentication state between accounts.

Session isolation method: separate browser user-data directories. This is
a supported, ordinary browser feature — each profile keeps its own
cookies, so two accounts' sessions cannot leak into each other.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# Keys that must NEVER appear in the persisted account file.
FORBIDDEN_KEYS = {"password", "passwd", "token", "cookie", "cookies",
                  "secret", "auth", "credential", "session_key"}


@dataclass
class Account:
    id: str
    label: str
    provider: str = "facebook"   # facebook | instagram | email | phone
    status: str = "added"        # added | signed_in | connected | disconnected
    profile_dir: str = ""        # isolated browser profile directory
    worker_slot: str = ""        # bound worker slot id, e.g. "w2"
    note: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class AccountManager:
    def __init__(self, directory: Path | None = None) -> None:
        from .store import data_dir
        self.dir = directory or data_dir()
        self.path = self.dir / "accounts.json"
        self.profiles_base = self.dir / "browser_profiles"
        self.profiles_base.mkdir(parents=True, exist_ok=True)
        self.accounts: list[Account] = self._load()

    def _load(self) -> list[Account]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        out = []
        for r in raw:
            try:
                # Defensive: refuse to load anything secret-shaped.
                if FORBIDDEN_KEYS & {k.lower() for k in r}:
                    continue
                out.append(Account(**{k: v for k, v in r.items()
                                      if k in Account.__dataclass_fields__}))
            except TypeError:
                continue
        return out

    def _save(self) -> None:
        data = [asdict(a) for a in self.accounts]
        for d in data:
            assert not (FORBIDDEN_KEYS & {k.lower() for k in d}), \
                "refusing to persist secret-shaped key"
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    # -- CRUD ---------------------------------------------------------------
    def add(self, label: str, provider: str = "facebook") -> Account:
        acc_id = "acc_" + uuid.uuid4().hex[:8]
        profile_dir = str(self.profiles_base / acc_id)
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        acc = Account(id=acc_id, label=label.strip() or acc_id,
                      provider=provider, profile_dir=profile_dir)
        self.accounts.append(acc)
        self._save()
        return acc

    def get(self, account_id: str) -> Account | None:
        for a in self.accounts:
            if a.id == account_id:
                return a
        return None

    def remove(self, account_id: str, delete_profile: bool = False) -> bool:
        acc = self.get(account_id)
        if not acc:
            return False
        if delete_profile:
            import shutil
            shutil.rmtree(acc.profile_dir, ignore_errors=True)
        self.accounts = [a for a in self.accounts if a.id != account_id]
        self._save()
        return True

    def set_status(self, account_id: str, status: str,
                   note: str = "") -> bool:
        acc = self.get(account_id)
        if not acc:
            return False
        acc.status = status
        acc.updated_at = time.time()
        if note:
            acc.note = note
        self._save()
        return True

    # -- worker binding -------------------------------------------------------
    def bind_worker(self, account_id: str, worker_id: str) -> tuple[bool, str]:
        """Bind an account to a worker slot (session still needs probing)."""
        acc = self.get(account_id)
        if not acc:
            return False, "Unknown account."
        if acc.status not in ("signed_in", "connected"):
            return False, (f"Account '{acc.label}' is '{acc.status}'. "
                           f"Sign in first (Open login), then bind.")
        # One account per slot; a slot holds at most one account.
        for other in self.accounts:
            if other.id != account_id and other.worker_slot == worker_id:
                return False, (f"Slot {worker_id} is already bound to "
                               f"'{other.label}'. Unbind it first.")
        acc.worker_slot = worker_id
        acc.updated_at = time.time()
        self._save()
        return True, f"'{acc.label}' bound to slot {worker_id}. Verify the session next."

    def unbind_worker(self, account_id: str) -> bool:
        acc = self.get(account_id)
        if not acc:
            return False
        acc.worker_slot = ""
        acc.updated_at = time.time()
        self._save()
        return True

    def account_for_slot(self, worker_id: str) -> Account | None:
        for a in self.accounts:
            if a.worker_slot == worker_id:
                return a
        return None

    # -- status ---------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        return [asdict(a) for a in self.accounts]

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for a in self.accounts:
            by_status[a.status] = by_status.get(a.status, 0) + 1
        return {"total": len(self.accounts), "by_status": by_status,
                "bound_slots": sum(1 for a in self.accounts if a.worker_slot)}
