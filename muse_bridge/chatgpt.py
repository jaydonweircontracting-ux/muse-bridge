"""ChatGPT side of Muse Bridge — free existing-window edition.

There is NO API client here by design: this edition coordinates the user's
existing ChatGPT browser window. The bot prepares prompts, the user pastes
them into ChatGPT, and the user transfers the completed response back into
the bridge. The bot NEVER scrapes the ChatGPT website, uses hidden page
APIs, or injects into the browser, and it never claims a response exists
until the user has transferred it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ManualHandoff:
    """One prompt waiting for the user to relay ChatGPT's answer."""
    prompt: str
    purpose: str
    created_at: float = field(default_factory=time.time)
    response: str = ""
    done: bool = False


class ManualRelay:
    """Manual-relay coordination for the ChatGPT website."""

    def __init__(self) -> None:
        self.pending: ManualHandoff | None = None

    def prepare(self, prompt: str, purpose: str) -> ManualHandoff:
        self.pending = ManualHandoff(prompt=prompt, purpose=purpose)
        return self.pending

    def submit_response(self, text: str) -> ManualHandoff | None:
        """Record a user-transferred response. Nothing else creates one."""
        if self.pending is None or not text.strip():
            return None
        self.pending.response = text
        self.pending.done = True
        done = self.pending
        self.pending = None
        return done

    def cancel(self) -> None:
        self.pending = None
