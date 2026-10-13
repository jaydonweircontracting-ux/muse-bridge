"""Acceptance walkthrough for Muse Bridge (free edition).

Simulates the user's side of the full 9-step cycle with canned transfers:
  1. Prepare planning prompt.
  2. "Hand to ChatGPT" (simulated) -> transfer plan back.
  3. Prepare dev task.
  4. "Hand to Muse" (simulated) -> user confirms done -> transfer report.
  5. Run local verification (against a tiny fixture repo).
  6. Prepare review prompt.
  7. "Hand to ChatGPT" (simulated) -> transfer APPROVE review.
  8. Save result + queue next task.
  9. Simulate app restart -> verify full recovery.

Run: python scripts/acceptance_walkthrough.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from muse_bridge.profile import ProjectProfile
from muse_bridge.runner import Coordinator
from muse_bridge.state import HandoffState
from muse_bridge.store import Brief, Checkpoints, EventLog, Settings, TaskQueue

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((PASS if cond else FAIL, f"{name} {detail}".strip()))
    if not cond:
        print(f"  !! FAILED: {name} {detail}")


def make_fixture_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="bridge-fixture-"))
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    (d / "package.json").write_text(
        '{"name": "adventure-game-demo-v2", "scripts": '
        '{"build": "node build.js", "test": "node test.js"}, '
        '"dependencies": {"react": "19", "vite": "6"}}')
    (d / "build.js").write_text('console.log("build ok");\n')
    (d / "test.js").write_text('console.log("3 passed, 0 failed");\n')
    (d / "src").mkdir()
    (d / "src" / "game.ts").write_text("// chunk system\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=d, check=True)
    return d


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="bridge-accept-"))
    repo = make_fixture_repo()
    copied: list[str] = []

    def new_coord() -> Coordinator:
        s = Settings(work)
        c = Coordinator(s, EventLog(work), TaskQueue(work), Checkpoints(work),
                        Brief(work), ProjectProfile(work))
        c.copy_text = copied.append
        return c

    print("== Acceptance walkthrough: free existing-window edition ==")
    c = new_coord()
    c.settings.set("game_repo_path", str(repo))
    c.settings.set("game_repo_verified", True)
    c.brief.save_text("MASTER: preserve the chunk architecture.")
    c.profile.refresh_repo(str(repo), "main")

    # 1. Prepare a task (planning).
    r = c.prepare_planning()
    check("1. planning prompt prepared", r.ok and r.handoff is not None)
    check("1. prompt copied to clipboard", len(copied) == 1)
    check("1. state=WAITING_FOR_USER", c.tracker.state == HandoffState.WAITING_FOR_USER)

    # 2. Transfer ChatGPT's plan back.
    r = c.submit_planning_response(
        "TASK: Add torch light | Torch items emit light radius\n"
        "TASK: Fix door sound | Door open/close sound effect\n"
        "<STATE_SUMMARY>Lighting next.</STATE_SUMMARY>")
    check("2. plan transferred, 2 tasks queued",
          r.ok and c.queue.counts().get("backlog", 0) == 2, r.detail)
    check("2. state=READY", c.tracker.state == HandoffState.READY)

    # 3. Hand the task to Muse.
    r = c.prepare_dev()
    check("3. dev prompt prepared", r.ok and r.handoff.stage == "dev")
    check("3. no duplicate handoff while busy", not c.prepare_dev().ok)
    check("3. prompt copied", len(copied) == 2)
    check("3a. cannot confirm done before submitted", not c.mark_muse_done().ok)
    check("3b. submitted -> MUSE_RUNNING",
          c.mark_muse_submitted().ok and c.tracker.state == HandoffState.MUSE_RUNNING)

    # 4. User confirms Muse finished; transfer the report.
    check("4. user-confirmed done -> AWAITING_REPORT",
          c.mark_muse_done().ok and c.tracker.state == HandoffState.AWAITING_REPORT)
    r = c.submit_muse_report("<REPORT>Added torch light radius. build ok, 3 tests pass.</REPORT>")
    check("4. report transferred (not faked)", r.ok)

    # 5. Local verification against the fixture repo.
    # Simulate Muse's change: a real file change in the fixture repo.
    (repo / "src" / "torch.ts").write_text("// torch light\n")
    r = c.run_verification()
    check("5. tests+build ran", r.ok, r.detail[:80])
    check("5. diff detected", "torch.ts" in c.last_diff_summary)

    # 6. Prepare the ChatGPT review.
    r = c.prepare_review()
    check("6. review prompt prepared", r.ok and r.handoff.stage == "review")
    check("6. state=REVIEW_PENDING", c.tracker.state == HandoffState.REVIEW_PENDING)
    check("6. review prompt contains evidence",
          "torch.ts" in r.handoff.prompt and "build ok" in r.handoff.prompt)

    # 7. Transfer the review back.
    r = c.submit_review("APPROVE — tests pass, diff is one clean file.\n"
                        "<STATE_SUMMARY>Torch light done.</STATE_SUMMARY>")
    check("7. review APPROVED -> checkpoint",
          r.ok and c.tracker.state == HandoffState.READY, r.detail)
    check("7. task completed", c.queue.counts().get("completed", 0) == 1)
    check("7. checkpoint recorded", c.checkpoints.latest() is not None)

    # 8. Queue the next task.
    r = c.next_handoff()
    check("8. next task prepared", r.ok and "Fix door sound" in (r.handoff.title if r.handoff else ""))

    # 9. Close and reopen: full recovery.
    del c
    c2 = new_coord()
    check("9. state recovered", c2.tracker.state == HandoffState.WAITING_FOR_USER,
          f"got {c2.tracker.state}")
    check("9. queue recovered", c2.queue.counts().get("active", 0) == 1)
    check("9. checkpoints recovered", c2.checkpoints.latest() is not None)
    check("9. profile summary recovered",
          "Torch light done" in c2.profile.load().get("project_summary", ""))
    print("\n" + c2.describe_status())

    print("\n== Results ==")
    failed = 0
    for status, name in results:
        print(f"  [{status}] {name}")
        if status == FAIL:
            failed += 1
    print(f"\n{len(results) - failed}/{len(results)} checks passed.")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(repo, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
