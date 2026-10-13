"""Tests for the automatic chat relay (fake window agents)."""
import tempfile
import unittest
from pathlib import Path

from muse_bridge.ledger import ACKNOWLEDGED, NEEDS_REVIEW, MessageLedger
from muse_bridge.relay import (FakeWindowAgent, RelayController, RelayState)


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="bridge-relay-"))


class Agents:
    """Routes (worker_id, role) -> FakeWindowAgent."""

    def __init__(self) -> None:
        self.map: dict[tuple[str, str], FakeWindowAgent] = {}

    def set(self, wid: str, role: str, agent: FakeWindowAgent) -> None:
        self.map[(wid, role)] = agent

    def __call__(self, wid: str, role: str) -> FakeWindowAgent:
        return self.map[(wid, role)]


def make_controller(agents: Agents, **kw):
    led = MessageLedger(tmpdir())
    ctl = RelayController(led, agents, **kw)
    ctl.get_worker = lambda wid: None  # no URL expectations
    return ctl


class TestRelayRoundTrip(unittest.TestCase):
    def test_full_round_trip(self) -> None:
        agents = Agents()
        muse = FakeWindowAgent(text="Muse: old history",
                               url="https://muse.ai/chat/1")
        agents.set("w1", "muse", muse)
        agents.set("w1", "chatgpt",
                   FakeWindowAgent(text="ChatGPT: ready",
                                   url="https://chat.openai.com/c/abc",
                                   response="ChatGPT: check the doorway rect."))
        ctl = make_controller(agents, poll_interval_s=0)
        ctl.set_enabled("w1", True)

        self.assertIn("baseline", ctl.poll_pair("w1"))
        # History is not relayed. New question appears...
        muse.text += "\nMuse: how do I fix the door bug?"
        statuses = ctl.poll()
        self.assertTrue(any("relay complete" in s for s in statuses),
                        statuses)

        chat = agents.map[("w1", "chatgpt")]
        # The Muse question reached ChatGPT...
        self.assertEqual(chat.typed, ["Muse: how do I fix the door bug?"])
        # ...and the answer reached the Muse window.
        self.assertEqual(muse.typed,
                         ["ChatGPT: check the doorway rect."])
        # Ledger shows the completed round trip.
        msgs = ctl.ledger.by_worker("w1")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].state, ACKNOWLEDGED)

    def test_no_new_content_is_idle(self) -> None:
        agents = Agents()
        agents.set("w1", "muse", FakeWindowAgent(text="hello"))
        agents.set("w1", "chatgpt", FakeWindowAgent(text="hi"))
        ctl = make_controller(agents, poll_interval_s=0)
        ctl.set_enabled("w1", True)
        ctl.poll_pair("w1")  # baseline
        status = ctl.poll_pair("w1")
        self.assertIn("no new", status)
        self.assertEqual(ctl.ledger.by_worker("w1"), [])

    def test_no_echo_loop(self) -> None:
        agents = Agents()
        muse = FakeWindowAgent(text="history")
        agents.set("w1", "muse", muse)
        agents.set("w1", "chatgpt",
                   FakeWindowAgent(text="x", response="ChatGPT: a."))
        ctl = make_controller(agents, poll_interval_s=0)
        ctl.set_enabled("w1", True)
        ctl.poll_pair("w1")  # baseline
        muse.text += "\nMuse: q?"
        ctl.poll_pair("w1")  # relay
        # Our delivery is now in the Muse window text; next poll must
        # not relay it back.
        status = ctl.poll_pair("w1")
        self.assertIn("no new", status)
        self.assertEqual(len(ctl.ledger.by_worker("w1")), 1)


class TestRelayIsolation(unittest.TestCase):
    def test_pairs_never_cross_route(self) -> None:
        agents = Agents()
        m1 = FakeWindowAgent(text="Muse1 history")
        m2 = FakeWindowAgent(text="Muse2 history")
        agents.set("w1", "muse", m1)
        agents.set("w1", "chatgpt",
                   FakeWindowAgent(text="", response="ChatGPT: alpha answer"))
        agents.set("w2", "muse", m2)
        agents.set("w2", "chatgpt",
                   FakeWindowAgent(text="", response="ChatGPT: beta answer"))
        ctl = make_controller(agents, poll_interval_s=0)
        ctl.set_enabled("w1", True)
        ctl.set_enabled("w2", True)
        ctl.poll()  # baselines
        m1.text += "\nMuse1: alpha question"
        m2.text += "\nMuse2: beta question"
        ctl.poll()
        c1 = agents.map[("w1", "chatgpt")].typed
        c2 = agents.map[("w2", "chatgpt")].typed
        self.assertEqual(c1, ["Muse1: alpha question"])
        self.assertEqual(c2, ["Muse2: beta question"])
        self.assertEqual(m1.typed, ["ChatGPT: alpha answer"])
        self.assertEqual(m2.typed, ["ChatGPT: beta answer"])


class TestRelayFailures(unittest.TestCase):
    def test_wrong_conversation_blocks_send(self) -> None:
        agents = Agents()
        muse = FakeWindowAgent(text="history")
        agents.set("w1", "muse", muse)
        agents.set("w1", "chatgpt",
                   FakeWindowAgent(text="", url="https://chat.openai.com/c/WRONG",
                                   response="r"))
        ctl = make_controller(agents, poll_interval_s=0)

        class Slot:
            chatgpt_url = "https://chat.openai.com/c/RIGHT"
        ctl.get_worker = lambda wid: Slot()
        ctl.set_enabled("w1", True)
        ctl.poll_pair("w1")  # baseline
        muse.text += "\nMuse: q?"
        status = ctl.poll_pair("w1")
        self.assertIn("PAUSED", status)
        self.assertIn("different conversation", status)
        # Nothing was typed anywhere.
        self.assertEqual(agents.map[("w1", "chatgpt")].typed, [])
        msgs = ctl.ledger.by_worker("w1")
        self.assertEqual(msgs[0].state, NEEDS_REVIEW)

    def test_response_timeout_pauses(self) -> None:
        agents = Agents()
        muse = FakeWindowAgent(text="history")
        agents.set("w1", "muse", muse)
        agents.set("w1", "chatgpt",
                   FakeWindowAgent(text="waiting...", response=None))
        ctl = make_controller(agents, poll_interval_s=0,
                              response_timeout_s=0.01)
        ctl.set_enabled("w1", True)
        ctl.poll_pair("w1")  # baseline
        muse.text += "\nMuse: q?"
        status = ctl.poll_pair("w1")
        self.assertIn("PAUSED", status)
        pair = ctl.pair_for("w1")
        self.assertEqual(pair.state, RelayState.ERROR)
        msgs = ctl.ledger.by_worker("w1")
        self.assertEqual(msgs[0].state, NEEDS_REVIEW)

    def test_failed_relay_unconsumes_for_retry(self) -> None:
        agents = Agents()
        muse = FakeWindowAgent(text="history")
        agents.set("w1", "muse", muse)
        chat = FakeWindowAgent(text="x", response=None)
        agents.set("w1", "chatgpt", chat)
        ctl = make_controller(agents, poll_interval_s=0,
                              response_timeout_s=0.01)
        ctl.set_enabled("w1", True)
        ctl.poll_pair("w1")  # baseline
        muse.text += "\nMuse: q?"
        ctl.poll_pair("w1")  # fails on timeout — but WAS submitted
        self.assertEqual(ctl.pair_for("w1").state, RelayState.ERROR)
        self.assertEqual(chat.typed, ["Muse: q?"])
        # User fixes it: response now available. Retry must NOT resend.
        chat.response = "ChatGPT: fixed answer"
        ctl.retry_pair("w1")
        status = ctl.poll_pair("w1")
        self.assertIn("relay complete", status)
        self.assertEqual(chat.typed, ["Muse: q?"])  # still exactly once
        self.assertEqual(muse.typed, ["ChatGPT: fixed answer"])
        self.assertEqual(ctl.ledger.by_worker("w1")[0].state,
                         ACKNOWLEDGED)

    def test_muse_read_failure_pauses(self) -> None:
        class Broken(FakeWindowAgent):
            def read_text(self) -> str:
                raise RuntimeError("UIA gone")
        agents = Agents()
        agents.set("w1", "muse", Broken())
        agents.set("w1", "chatgpt", FakeWindowAgent())
        ctl = make_controller(agents, poll_interval_s=0)
        ctl.set_enabled("w1", True)
        status = ctl.poll_pair("w1")
        self.assertIn("PAUSED", status)


if __name__ == "__main__":
    unittest.main()
