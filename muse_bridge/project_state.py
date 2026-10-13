"""Persistent project-state summary for Muse Bridge.

ChatGPT (the planning/review brain) maintains this: after every review
it writes a compact summary of what works, what's broken, and the next
priority. The summary is fed into every planning prompt so context is
never lost — even across restarts or context-limit handoffs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class ProjectState:
    def __init__(self, directory: Path | None = None) -> None:
        from .store import data_dir
        self.dir = directory or data_dir()
        self.path = self.dir / "project_state.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"summary": "", "updated_at": 0.0, "updates": 0}

    def save_summary(self, summary: str) -> None:
        prev = self.load()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "summary": summary,
            "updated_at": time.time(),
            "updates": int(prev.get("updates", 0)) + 1,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def summary_prompt_block(self) -> str:
        """Short block prepended to planning prompts."""
        s = self.load().get("summary", "")
        if not s:
            return "(No project-state summary yet — this is the first cycle.)"
        return f"Current project state (maintained across cycles):\n{s[:1500]}"

    @staticmethod
    def update_instruction() -> str:
        return (
            "After your review, write an updated project-state summary "
            "(max 300 words): what currently works, what is broken or risky, "
            "and the single next priority. Put it between "
            "<STATE_SUMMARY> and </STATE_SUMMARY> tags."
        )

    @staticmethod
    def extract_summary(text: str) -> str:
        start = text.find("<STATE_SUMMARY>")
        end = text.find("</STATE_SUMMARY>")
        if start == -1 or end == -1 or end <= start:
            return ""
        return text[start + len("<STATE_SUMMARY>"):end].strip()
