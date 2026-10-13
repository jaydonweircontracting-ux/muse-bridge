"""Tests for the multi-account manager (BUILD 2).

Covers: add/remove/persist, no secrets ever stored, bind rules,
one-slot-one-account, distinct isolated profiles, removal isolation,
orchestrator slot linkage.

Run: python -m unittest discover -s tests
"""

import json
import tempfile
import unittest
from pathlib import Path

from muse_bridge.accounts import AccountManager, FORBIDDEN_KEYS
from muse_bridge.orchestrator import Orchestrator


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


class TestAccountManager(unittest.TestCase):
    def test_add_persists(self) -> None:
        d = tmpdir()
        m = AccountManager(d)
        acc = m.add("Personal FB")
        self.assertEqual(acc.label, "Personal FB")
        self.assertEqual(acc.status, "added")
        m2 = AccountManager(d)
        self.assertEqual(len(m2.accounts), 1)
        self.assertEqual(m2.accounts[0].label, "Personal FB")

    def test_no_secrets_stored(self) -> None:
        d = tmpdir()
        m = AccountManager(d)
        m.add("Alt FB 2")
        raw = json.loads((d / "accounts.json").read_text(encoding="utf-8"))
        for rec in raw:
            keys = {k.lower() for k in rec}
            self.assertFalse(keys & FORBIDDEN_KEYS,
                             f"secret-shaped key persisted: {keys & FORBIDDEN_KEYS}")
        # Profile dir holds no credentials either (bridge never writes any).
        acc = m.accounts[0]
        self.assertTrue(Path(acc.profile_dir).is_dir())

    def test_distinct_profiles_per_account(self) -> None:
        d = tmpdir()
        m = AccountManager(d)
        a1 = m.add("One")
        a2 = m.add("Two")
        self.assertNotEqual(a1.profile_dir, a2.profile_dir)

    def test_bind_requires_signed_in(self) -> None:
        d = tmpdir()
        m = AccountManager(d)
        acc = m.add("FB")
        ok, msg = m.bind_worker(acc.id, "w1")
        self.assertFalse(ok)
        self.assertIn("Sign in first", msg)
        m.set_status(acc.id, "signed_in")
        ok, msg = m.bind_worker(acc.id, "w1")
        self.assertTrue(ok)
        self.assertEqual(m.get(acc.id).worker_slot, "w1")

    def test_one_slot_one_account(self) -> None:
        d = tmpdir()
        m = AccountManager(d)
        a1 = m.add("One")
        a2 = m.add("Two")
        m.set_status(a1.id, "signed_in")
        m.set_status(a2.id, "signed_in")
        self.assertTrue(m.bind_worker(a1.id, "w1")[0])
        ok, msg = m.bind_worker(a2.id, "w1")
        self.assertFalse(ok)
        self.assertIn("already bound", msg)

    def test_remove_does_not_touch_others(self) -> None:
        d = tmpdir()
        m = AccountManager(d)
        a1 = m.add("One")
        a2 = m.add("Two")
        m.set_status(a1.id, "signed_in")
        m.bind_worker(a1.id, "w1")
        self.assertTrue(m.remove(a1.id))
        self.assertIsNone(m.get(a1.id))
        self.assertIsNotNone(m.get(a2.id))
        self.assertEqual(m.get(a2.id).status, "added")

    def test_status_flow(self) -> None:
        d = tmpdir()
        m = AccountManager(d)
        acc = m.add("FB")
        for s in ("signed_in", "connected", "disconnected"):
            self.assertTrue(m.set_status(acc.id, s))
            self.assertEqual(m.get(acc.id).status, s)


class TestAccountSlotLinkage(unittest.TestCase):
    def _orch(self, d: Path) -> Orchestrator:
        o = Orchestrator(d)
        o.copy_text = lambda t: None
        o.focus_window = lambda t: True
        return o

    def test_bind_links_both_ways(self) -> None:
        d = tmpdir()
        o = self._orch(d)
        acc = o.accounts.add("FB")
        o.accounts.set_status(acc.id, "signed_in")
        ok, msg = o.bind_account_to_slot(acc.id, "w2")
        self.assertTrue(ok, msg)
        self.assertEqual(o.workers.get("w2").account_id, acc.id)
        self.assertEqual(o.accounts.account_for_slot("w2").id, acc.id)

    def test_dashboard_shows_account_per_slot(self) -> None:
        d = tmpdir()
        o = self._orch(d)
        acc = o.accounts.add("FB")
        o.accounts.set_status(acc.id, "signed_in")
        o.bind_account_to_slot(acc.id, "w2")
        dash = o.dashboard()
        row = next(r for r in dash["workers"] if r["id"] == "w2")
        self.assertEqual(row["account_label"], "FB")
        row1 = next(r for r in dash["workers"] if r["id"] == "w1")
        self.assertEqual(row1["account_label"], "")

    def test_unbind_clears_both(self) -> None:
        d = tmpdir()
        o = self._orch(d)
        acc = o.accounts.add("FB")
        o.accounts.set_status(acc.id, "signed_in")
        o.bind_account_to_slot(acc.id, "w2")
        ok, _ = o.unbind_account_from_slot(acc.id)
        self.assertTrue(ok)
        self.assertEqual(o.workers.get("w2").account_id, "")
        self.assertIsNone(o.accounts.account_for_slot("w2"))

    def test_verified_session_count_unchanged_by_accounts(self) -> None:
        # Accounts don't create sessions: the verified count still comes
        # from probe-verified sessions only.
        d = tmpdir()
        o = self._orch(d)
        acc = o.accounts.add("FB")
        o.accounts.set_status(acc.id, "signed_in")
        o.bind_account_to_slot(acc.id, "w1")
        self.assertEqual(o.workers.verified_count(), 0)
        self.assertEqual(o.workers.concurrency_limit(), 0)


if __name__ == "__main__":
    unittest.main()
