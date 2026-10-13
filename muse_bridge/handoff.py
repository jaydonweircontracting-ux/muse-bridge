"""Handoff preparation for Muse Bridge (free edition).

A Handoff is one guided step: a prepared prompt, where it goes, and what
the user must transfer back. The bot never claims a response exists until
the user pastes it into the bridge.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Handoff:
    id: str
    stage: str              # "plan" | "dev" | "review"
    task_id: str
    title: str
    prompt: str             # exact text to copy to clipboard
    target: str             # "chatgpt" | "muse"
    user_instruction: str   # plain-language guidance shown in the UI
    created_at: float = field(default_factory=time.time)
    response: str = ""      # filled ONLY by explicit user transfer
    done: bool = False


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def build_planning_prompt(master_prompt: str, project_summary: str,
                          repo_facts: str) -> str:
    return (
        f"{master_prompt.strip()}\n\n"
        f"---\nCURRENT PROJECT SUMMARY (maintained by you across sessions):\n"
        f"{project_summary.strip() or '(none yet — first planning cycle)'}\n\n"
        f"---\nREPOSITORY FACTS (read from the actual project, do not invent):\n"
        f"{repo_facts.strip() or '(not scanned yet)'}\n\n"
        f"---\nYOUR JOB NOW: propose the next development task(s).\n"
        f"Reply with each task on its own line in EXACTLY this format:\n"
        f"TASK: <short title> | <one-line brief>\n"
        f"Then add 2-4 bullet acceptance criteria per task.\n"
        f"Then update the project summary between "
        f"<STATE_SUMMARY> and </STATE_SUMMARY> tags.\n"
        f"Smallest safe change first. Preserve existing architecture."
    )


def build_dev_prompt(task_title: str, task_brief: str, acceptance: str,
                     master_prompt: str) -> str:
    return (
        f"Project rules (from the saved master prompt):\n"
        f"{master_prompt.strip()[:3000]}\n\n"
        f"TASK: {task_title}\n{task_brief}\n\n"
        f"Acceptance criteria:\n{acceptance}\n\n"
        f"Rules: preserve existing architecture; run tests and the production "
        f"build; report exact files changed, test results, and anything you "
        f"could NOT verify. Do not claim completion without evidence.\n\n"
        f"When finished, reply with a completion report between "
        f"<REPORT> and </REPORT> tags: files changed, tests run + results, "
        f"build result, and open risks."
    )


def build_review_prompt(task_title: str, acceptance: str, muse_report: str,
                        test_output: str, diff_summary: str) -> str:
    return (
        f"You are a strict code reviewer for a 2.5D isometric RPG codebase.\n\n"
        f"TASK: {task_title}\nAcceptance criteria:\n{acceptance}\n\n"
        f"DEVELOPER'S COMPLETION REPORT:\n{muse_report.strip() or '(no report transferred)'}\n\n"
        f"LOCAL TEST/BUILD OUTPUT:\n{test_output.strip() or '(not run)'}\n\n"
        f"GIT DIFF SUMMARY:\n{diff_summary.strip() or '(no changes detected)'}\n\n"
        f"Reply with APPROVE or REJECT first, then your reasons. "
        f"Approve ONLY if the evidence (not the claims) shows the acceptance "
        f"criteria are met. Call out regressions, incomplete work, and false "
        f"completion claims explicitly.\n"
        f"Then update the project summary between <STATE_SUMMARY> and "
        f"</STATE_SUMMARY> tags, and suggest the single next priority."
    )


TASK_LINE = re.compile(r"^\s*TASK\s*:\s*(.+?)\s*\|\s*(.+?)\s*$", re.M)


def parse_task_lines(text: str) -> list[tuple[str, str]]:
    """Extract (title, brief) pairs from a planning response.

    Only lines matching the exact TASK: title | brief convention count.
    Anything else is ignored — never invent tasks from prose.
    """
    return [(t.strip(), b.strip()) for t, b in TASK_LINE.findall(text)]


def extract_tagged(text: str, tag: str) -> str:
    start = text.find(f"<{tag}>")
    end = text.find(f"</{tag}>")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start + len(tag) + 2:end].strip()


def looks_like_approval(text: str) -> bool:
    return text.strip().upper().startswith("APPROVE")
