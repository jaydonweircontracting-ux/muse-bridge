"""ChatGPT connection for Muse Bridge.

Two modes, exactly as specified:

1. "api"    — official OpenAI API. Fully automatic: send a prompt, poll
              the documented completion mechanism, return the complete
              response. This is the recommended mode.
2. "manual" — the ordinary ChatGPT website. The bot prepares the prompt,
              the USER copies the completed response back in (or approves
              a handoff). The bot NEVER scrapes the website.

Uses only the standard library (urllib) so there are no dependencies
to install for the API path.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass


API_URL = "https://api.openai.com/v1/chat/completions"


@dataclass
class ApiResult:
    ok: bool
    text: str = ""
    error: str = ""
    model: str = ""
    usage: dict | None = None


def _post(url: str, api_key: str, payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ChatGPTApi:
    """Official OpenAI API client with honest completion semantics.

    The API only returns when the model has finished (or the connection
    failed), so RESPONSE_COMPLETE is determined by the API itself —
    never by guessing from screen state.
    """

    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        self.api_key = api_key
        self.model = model

    def test_connection(self) -> ApiResult:
        """Harmless test message; proves the key works end-to-end."""
        return self.complete(
            "Reply with exactly the word: ok",
            max_tokens=10,
        )

    def complete(self, prompt: str, system: str = "",
                 max_tokens: int = 2000,
                 timeout: int = 180) -> ApiResult:
        if not self.api_key:
            return ApiResult(ok=False, error="No API key configured.")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        try:
            data = _post(API_URL, self.api_key, payload, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                detail = ""
            return ApiResult(ok=False, error=f"HTTP {e.code}: {detail}")
        except Exception as e:  # network down, DNS, timeout...
            return ApiResult(ok=False, error=f"Network error: {e}")
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            finish = choice.get("finish_reason", "")
            if finish == "length":
                text += "\n\n[NOTE: response was truncated by max_tokens]"
            return ApiResult(ok=True, text=text, model=data.get("model", self.model),
                             usage=data.get("usage"))
        except (KeyError, IndexError, TypeError) as e:
            return ApiResult(ok=False, error=f"Unexpected API response: {e}")


# ---------------------------------------------------------------- manual relay

@dataclass
class ManualHandoff:
    """A prompt waiting for the user to relay the website's answer."""
    prompt: str
    purpose: str
    created_at: float = 0.0
    response: str = ""
    done: bool = False

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()


class ManualRelay:
    """Manual-relay mode for the ChatGPT website.

    The bot prepares prompts; the human copies the finished answer back.
    The bot never claims the website is API-connected.
    """

    def __init__(self) -> None:
        self.pending: ManualHandoff | None = None

    def prepare(self, prompt: str, purpose: str) -> ManualHandoff:
        self.pending = ManualHandoff(prompt=prompt, purpose=purpose)
        return self.pending

    def submit_response(self, text: str) -> ManualHandoff | None:
        if self.pending is None:
            return None
        self.pending.response = text
        self.pending.done = True
        done = self.pending
        self.pending = None
        return done
