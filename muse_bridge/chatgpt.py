"""ChatGPT integration for Muse Bridge — official OpenAI Responses API.

Endpoint: POST https://api.openai.com/v1/responses
Body: {"model", "instructions", "input", "max_output_tokens"}
Reply: {"id": "resp_...", "status": "completed",
        "output_text": "...",
        "usage": {"input_tokens": N, "output_tokens": M}}

Completion is determined by the API itself (status == "completed"),
never by guessing from screen state. Every call records token usage
and estimated cost to the UsageLedger so the user can set a spend limit.

Two modes:
  "api"    — fully automatic via the Responses API (recommended).
  "manual" — ChatGPT website; the user relays the finished answer back.
             The bot never scrapes the website.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field


API_URL = "https://api.openai.com/v1/responses"


@dataclass
class ApiResult:
    ok: bool
    text: str = ""
    error: str = ""
    model: str = ""
    response_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


# ---------------------------------------------------------------- pricing
# Estimates only — verify current prices at platform.openai.com/pricing.
# Per 1M tokens, USD. User-overridable via settings["price_overrides"].

PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
}
PRICES_AS_OF = "2026-10-12 (estimates — check platform.openai.com/pricing)"
DEFAULT_MODEL = "gpt-4o-mini"


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  overrides: dict[str, list[float]] | None = None) -> float:
    table: dict[str, tuple[float, float]] = dict(PRICE_TABLE)
    if overrides:
        for k, v in overrides.items():
            try:
                table[k] = (float(v[0]), float(v[1]))
            except (IndexError, TypeError, ValueError):
                continue
    # Unknown model: fall back to the default model's rates, flagged.
    in_rate, out_rate = table.get(model, table[DEFAULT_MODEL])
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def _post(url: str, api_key: str, payload: dict, timeout: int = 180) -> dict:
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


def _extract_text(data: dict) -> str:
    """Pull assistant text out of a Responses API payload."""
    # Convenient top-level field when present.
    if isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    t = c.get("text", "")
                    if t:
                        parts.append(t)
    return "".join(parts)


class ChatGPTApi:
    """Official OpenAI Responses API client with usage/cost accounting."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL,
                 ledger: "UsageLedger | None" = None,
                 price_overrides: dict | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.ledger = ledger
        self.price_overrides = price_overrides or {}
        self.last_response_id: str = ""

    def test_connection(self) -> ApiResult:
        return self.complete("Reply with exactly the word: ok",
                             max_output_tokens=10)

    def complete(self, prompt: str, instructions: str = "",
                 max_output_tokens: int = 2000,
                 timeout: int = 180,
                 purpose: str = "general") -> ApiResult:
        if not self.api_key:
            return ApiResult(ok=False, error="No API key configured.")
        payload: dict = {
            "model": self.model,
            "instructions": instructions,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
        }
        # Chain onto the previous response for server-side context when
        # the API supports it; harmless if ignored.
        if self.last_response_id:
            payload["previous_response_id"] = self.last_response_id
        try:
            data = _post(API_URL, self.api_key, payload, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                detail = ""
            return ApiResult(ok=False, error=f"HTTP {e.code}: {detail}")
        except Exception as e:
            return ApiResult(ok=False, error=f"Network error: {e}")

        status = str(data.get("status", ""))
        if status and status != "completed":
            return ApiResult(ok=False,
                             error=f"Response status: {status} (not completed)")
        text = _extract_text(data)
        if not text:
            return ApiResult(ok=False, error="API returned no text output.")
        usage = data.get("usage", {}) or {}
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        cost = estimate_cost(self.model, in_tok, out_tok, self.price_overrides)
        rid = str(data.get("id", ""))
        if rid:
            self.last_response_id = rid
        if self.ledger is not None:
            self.ledger.record(self.model, in_tok, out_tok, cost, purpose)
        return ApiResult(ok=True, text=text, model=str(data.get("model", self.model)),
                         response_id=rid, input_tokens=in_tok,
                         output_tokens=out_tok, cost_usd=cost)


# ---------------------------------------------------------------- usage ledger

@dataclass
class UsageRecord:
    at: float
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    purpose: str = ""


class UsageLedger:
    """Append-only record of API spend. The runner enforces the limit."""

    def __init__(self, path) -> None:
        from pathlib import Path
        self.path = Path(path)
        self._records: list[UsageRecord] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raw = []
        for r in raw:
            try:
                self._records.append(UsageRecord(**r))
            except TypeError:
                continue

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([vars(r) for r in self._records],
                                 ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, model: str, input_tokens: int, output_tokens: int,
               cost_usd: float, purpose: str = "") -> None:
        self._records.append(UsageRecord(time.time(), model, input_tokens,
                                         output_tokens, cost_usd, purpose))
        self._save()

    def totals(self, since: float = 0.0) -> dict[str, float]:
        in_t = out_t = cost = 0.0
        n = 0
        for r in self._records:
            if r.at >= since:
                in_t += r.input_tokens
                out_t += r.output_tokens
                cost += r.cost_usd
                n += 1
        return {"calls": n, "input_tokens": in_t,
                "output_tokens": out_t, "cost_usd": cost}

    def session_totals(self, session_start: float) -> dict[str, float]:
        return self.totals(since=session_start)


# ---------------------------------------------------------------- manual relay

@dataclass
class ManualHandoff:
    prompt: str
    purpose: str
    created_at: float = field(default_factory=time.time)
    response: str = ""
    done: bool = False


class ManualRelay:
    """Manual-relay mode for the ChatGPT website.

    The bot prepares prompts; the human copies the finished answer back.
    The bot never claims the website is API-connected and never scrapes it.
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
