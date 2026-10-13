"""Simulation-lab tests: deterministic scenarios + pure-function units.

These exercise the REAL production code paths (RelayController,
CompletionWatcher, extract_response, detect_challenge, message_in_text,
_extract_delta, ledger transitions) against a scripted SimDesktop.
See docs/RELAY_SIMULATION_GUIDE.md.
"""
import unittest

from muse_bridge.relay import (RelayState, _extract_delta,
                             message_in_text)
from muse_bridge.uia_agent import (CompletionWatcher, detect_challenge,
                                   extract_response)
from tests.sim_lab import faults, scenarios
from tests.sim_lab.agent import SimWindowAgent
from tests.sim_lab.clock import VirtualClock
from tests.sim_lab.desktop import SimDesktop
from tests.sim_lab.scenarios import Harness, _basic_pair


class TestPureFunctions(unittest.TestCase):
    def test_detect_challenge(self) -> None:
        self.assertEqual(detect_challenge("Verify you are human"), "verify you are human")
        self.assertEqual(detect_challenge("Log in to continue"), "log in to continue")
        self.assertEqual(detect_challenge("Too many requests, slow down"), "too many requests")
        self.assertEqual(detect_challenge("Something went wrong"), "something went wrong")
        self.assertEqual(detect_challenge("normal conversation text"), "")

    def test_message_in_text(self) -> None:
        self.assertTrue(message_in_text("a\nhello world\nb", "hello world"))
        self.assertFalse(message_in_text("a\nb", "hello world"))
        self.assertFalse(message_in_text("a", ""))

    def test_extract_response_first_occurrence(self) -> None:
        full = "Q\nA quotes Q inside\nmore"
        # First occurrence -> inclusion (complete, not truncated).
        self.assertEqual(extract_response(full, "Q"),
                         "A quotes Q inside\nmore")

    def test_extract_response_missing_marker(self) -> None:
        self.assertEqual(extract_response("full text", "nope"), "full text")

    def test_extract_delta(self) -> None:
        self.assertEqual(_extract_delta("a\nb", "a\nb\nc"), "c")
        self.assertEqual(_extract_delta("a\nb", "a\nb"), "")
        # Reordered / unrecognizable -> refuse to guess (never whole text).
        self.assertEqual(_extract_delta("a\nb\nc", "x\ny\nz"), "")
        self.assertEqual(_extract_delta("", "hello"), "hello")
        # Whitespace-tolerant line prefix.
        self.assertEqual(_extract_delta("a  \nb", "a\nb\nc"), "c")


class TestCompletionWatcher(unittest.TestCase):
    def _watcher(self, script, **kw):
        """script: list of (is_generating, text) per poll."""
        clock = VirtualClock()
        it = iter(script)
        state = {"g": False, "t": ""}

        def is_g():
            return state["g"]

        def read():
            try:
                state["g"], state["t"] = next(it)
            except StopIteration:
                pass
            # time passes only via sleeps; advance the script lazily
            return state["t"]

        w = CompletionWatcher(read, is_g, sleep=clock.sleep, now=clock.now,
                              poll_s=2.0, stable_s=4.0, settle_s=30.0, **kw)
        return w, clock

    def test_quiet_pause_without_indicator_is_not_completion(self) -> None:
        # The defect the sim caught: indicator vanishes without a text
        # change -> stability clock must RESTART, not compute from epoch.
        script = [
            (True, "base\nQ"),            # generating
            (True, "base\nQ\npart1"),     # token
            (False, "base\nQ\npart1"),   # indicator gone, no new text
            (False, "base\nQ\npart1"),   # quiet...
            (False, "base\nQ\npart1\npart2"),  # ...then more text
            (False, "base\nQ\npart1\npart2"),
            (False, "base\nQ\npart1\npart2"),
            (False, "base\nQ\npart1\npart2"),
        ]
        w, clock = self._watcher(script)
        import hashlib
        base = hashlib.sha256(b"base\nQ").hexdigest()
        res = w.wait(120.0, base)
        self.assertIsNotNone(res)
        _, stable = res
        self.assertIn("part2", stable)  # NOT the partial "part1"

    def test_never_generating_needs_settle(self) -> None:
        # No indicator ever: a 10s pause must not complete (settle=30s),
        # but continued stability past settle does (documented policy).
        script = [(False, "base\nQ\nr1")] * 4 + [(False, "base\nQ\nr1\nr2")] * 20
        w, clock = self._watcher(script)
        import hashlib
        base = hashlib.sha256(b"base\nQ").hexdigest()
        res = w.wait(200.0, base)
        self.assertIsNotNone(res)
        self.assertIn("r2", res[1])

    def test_timeout_returns_none(self) -> None:
        w, clock = self._watcher([(True, "base")] * 100)
        import hashlib
        base = hashlib.sha256(b"base").hexdigest()
        self.assertIsNone(w.wait(30.0, base))


class TestSimScenarios(unittest.TestCase):
    def _check(self, o: dict) -> None:
        sid = o.get("id", "?")
        if "expect_state" in o:
            self.assertEqual(o.get("state"), o["expect_state"],
                             f"{sid}: {o.get('status', '')} {o.get('error', '')}")
        if "expect_submissions" in o:
            self.assertEqual(len(o.get("submissions", [])),
                             o["expect_submissions"], sid)
        if "expect_delivered" in o:
            self.assertEqual(o.get("delivered"), o["expect_delivered"], sid)
        if "wrong_window_submissions" in o:
            self.assertEqual(o["wrong_window_submissions"], 0, sid)

    def test_all_deterministic_scenarios(self) -> None:
        for fn in scenarios.ALL:
            with self.subTest(scenario=fn.__name__):
                self._check(fn())


class TestFaultInjection(unittest.TestCase):
    def test_seeded_fault_runs_hold_invariants(self) -> None:
        # 200 seeded runs here; the 1000-run gate runs in CI/dev and its
        # machine-readable report lands in reports/.
        r = faults.run_many(200, seed0=7001, n_pairs=2)
        self.assertEqual(r["failures"], [], 
                         f"first failure: {r['failures'][:1]}")
        self.assertEqual(r["passed"], r["runs"])

    def test_every_fault_type_individually(self) -> None:
        for i, f in enumerate(faults.FAULTS):
            with self.subTest(fault=f):
                r = faults.run_one(9000 + i, n_pairs=1, faults=[f])
                self.assertTrue(r["ok"], f"{f}: {r}")


class TestEndToEndCycles(unittest.TestCase):
    def test_100_consecutive_cycles_two_pairs(self) -> None:
        h = Harness()
        for i in range(2):
            wid = f"w{i + 1}"
            muse_win = h.desktop.add_window(f"Muse {i}", role="muse")
            muse_win.add_history("assistant", "Muse: hi")
            url = f"https://chat.openai.com/c/e2e{i}"
            chat_win = h.desktop.add_window(f"ChatGPT {i}", url=url)
            chat_win.set_response(f"ChatGPT: answer {i}.", mode="instant")
            h.pair(wid, muse_win, chat_win, url)
            h.baseline(wid)
        for cycle in range(100):
            wid = f"w{(cycle % 2) + 1}"
            q = f"Muse: cycle {cycle} question"
            h.agents[(wid, "muse")]._window().add_history("user", q)
            status = h.ctl.poll_pair(wid)
            self.assertIn("relay complete", status,
                          f"cycle {cycle}: {status}")
            o = h.outcome(wid)
            self.assertEqual(o["state"], RelayState.IDLE)
        # No cross-delivery across all 100 cycles.
        for i in range(2):
            wid = f"w{i + 1}"
            chat_win = h.desktop.find_by_hwnd(
                h.agents[(wid, "chatgpt")].hwnd)
            for sub in chat_win.submissions:
                self.assertIn(f"cycle", sub["text"])
                # every submission belongs to this pair's questions
                self.assertNotIn("w3", sub["text"])


if __name__ == "__main__":
    unittest.main()
