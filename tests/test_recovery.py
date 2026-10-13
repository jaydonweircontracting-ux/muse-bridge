"""Tests for the minimal recovery file (links/IDs only, never secrets).

Run: python -m unittest discover -s tests
"""

import json
import tempfile
import unittest
from pathlib import Path

from muse_bridge import recovery
from muse_bridge.recovery import (
    RECOVERY_FILENAME, read_recovery, reconnect, recovery_path,
    write_recovery,
)


class FakeSettings:
    def __init__(self, d: Path):
        self.dir = d
        self._vals = {
            "game_repo_url": "https://github.com/example/game",
            "game_repo_path": "C:\\game",
            "chatgpt_window_title": "ChatGPT",
        }

    def get(self, key: str, default=None):
        return self._vals.get(key, default)


class FakeSlot:
    def __init__(self, wid, label, window="", account="", state="READY",
                 task=""):
        self.id = wid
        self.label = label
        self.window_title = window
        self.chatgpt_window = ""
        self.account_id = account
        self.paused = False
        self.state = state
        self.task_id = task


class FakeWorkers:
    def __init__(self, slots):
        self.slots = slots


class FakeCheckpoints:
    def latest(self):
        return {"at": 1234.0, "task_id": "task-1", "commit": "abc123"}


class FakeOrch:
    def __init__(self, d: Path, slots):
        self.settings = FakeSettings(d)
        self.workers = FakeWorkers(slots)
        self.checkpoints = FakeCheckpoints()


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


class TestRecoveryFile(unittest.TestCase):
    def test_write_creates_expected_shape(self) -> None:
        d = tmpdir()
        orch = FakeOrch(d, [FakeSlot("w1", "Muse 1", "Win 1", "acc-1",
                                     "WORKING", "task-9")])
        path = write_recovery(orch)
        self.assertEqual(path, recovery_path(d))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["repo_url"], "https://github.com/example/game")
        self.assertEqual(data["chatgpt_window"], "ChatGPT")
        self.assertEqual(data["workers"]["w1"]["window"], "Win 1")
        self.assertEqual(data["workers"]["w1"]["account"], "acc-1")
        self.assertEqual(data["workers"]["w1"]["task"], "task-9")
        self.assertIn("chatgpt_window", data["workers"]["w1"])
        self.assertIn("paused", data["workers"]["w1"])
        self.assertEqual(data["last_checkpoint"]["commit"], "abc123")
        self.assertIn("how_to_reconnect", data)

    def test_no_message_content_stored(self) -> None:
        d = tmpdir()
        orch = FakeOrch(d, [])
        path = write_recovery(orch)
        raw = path.read_text(encoding="utf-8").lower()
        for word in ("report", "prompt", "conversation", "message"):
            self.assertNotIn(word, raw)

    def test_refuses_secret_shaped_keys(self) -> None:
        with self.assertRaises(ValueError):
            recovery._scrub({"api_key": "xxx"})

    def test_read_round_trip(self) -> None:
        d = tmpdir()
        orch = FakeOrch(d, [FakeSlot("w2", "Muse 2")])
        write_recovery(orch)
        data = read_recovery(d)
        assert data is not None
        self.assertIn("w2", data["workers"])

    def test_read_missing_or_corrupt(self) -> None:
        d = tmpdir()
        self.assertIsNone(read_recovery(d))
        recovery_path(d).write_text("not json", encoding="utf-8")
        self.assertIsNone(read_recovery(d))
        recovery_path(d).write_text('{"version": 999}', encoding="utf-8")
        self.assertIsNone(read_recovery(d))

    def test_reconnect_no_workers(self) -> None:
        d = tmpdir()
        orch = FakeOrch(d, [])
        self.assertEqual(reconnect(orch), ["No workers registered."])

    def test_reconnect_unbound_worker(self) -> None:
        d = tmpdir()
        orch = FakeOrch(d, [FakeSlot("w1", "Muse 1")])
        lines = reconnect(orch)
        self.assertTrue(any("no window bound" in l for l in lines), lines)

    def test_reconnect_window_not_found_off_windows(self) -> None:
        d = tmpdir()
        orch = FakeOrch(d, [FakeSlot("w1", "Muse 1", "Some Window")])
        lines = reconnect(orch)
        # Off Windows, list_windows() is empty -> not found, re-bind advice.
        self.assertTrue(any("not found" in l for l in lines), lines)


if __name__ == "__main__":
    unittest.main()
