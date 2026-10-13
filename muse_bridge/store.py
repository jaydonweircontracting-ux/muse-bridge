"""JSON persistence for Muse Bridge.

Everything the bot knows lives in one data directory so a crash or
restart loses nothing:
  settings.json   — window selections, API mode, limits, repo path
  brief.json      — the one starter prompt + project metadata
  queue.json      — backlog / active / completed / failed / blocked tasks
  log.jsonl       — append-only event log
  checkpoints.json — verified git checkpoints

On Windows the directory defaults to %APPDATA%\\MuseBridge,
elsewhere to ~/.muse-bridge. Overridable via MUSE_BRIDGE_HOME.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    override = os.environ.get("MUSE_BRIDGE_HOME")
    if override:
        p = Path(override)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", str(Path.home()))) / "MuseBridge"
    else:
        p = Path.home() / ".muse-bridge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic on both platforms


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS: dict[str, Any] = {
    "game_repo_path": "",
    "game_repo_verified": False,
    "muse_window_title": "",
    "muse_input_verified": False,
    "muse_completion_mode": "uncertain",  # honest default
    "chatgpt_mode": "api",  # or "manual"
    "openai_api_key": "",   # stored locally only, never logged
    "openai_model": "gpt-4o-mini",
    "max_cycles_per_session": 10,
    "max_retries": 3,
    "response_timeout_s": 600,
    "task_time_limit_s": 3600,
    "pause_after_failure": True,
    "require_approval_for_risky": True,
    "first_run_complete": False,
    # ChatGPT / spending (OpenAI API is billed separately from ChatGPT Plus)
    "spend_limit_usd": 5.00,
    "price_overrides": {},
    # Muse integration: "cli" preferred, "window" fallback
    "muse_mode": "cli",
    "muse_cli_template": "",
}


class Settings:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or data_dir()
        self.path = self.dir / "settings.json"
        self.data: dict[str, Any] = {**DEFAULT_SETTINGS, **_read_json(self.path, {})}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        # Never persist a key that looks like a secret into the log; the
        # settings file itself stays on the user's own disk.
        _write_json(self.path, self.data)


# ---------------------------------------------------------------- tasks

@dataclass
class Task:
    id: str
    title: str
    brief: str = ""
    acceptance: str = ""
    status: str = "backlog"  # backlog|active|completed|failed|blocked
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attempts: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


class TaskQueue:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or data_dir()
        self.path = self.dir / "queue.json"
        raw: list[dict[str, Any]] = _read_json(self.path, [])
        self.tasks: list[Task] = [Task(**t) for t in raw]

    def save(self) -> None:
        _write_json(self.path, [asdict(t) for t in self.tasks])

    def add(self, task: Task) -> None:
        self.tasks.append(task)
        self.save()

    def active(self) -> Task | None:
        for t in self.tasks:
            if t.status == "active":
                return t
        return None

    def next_backlog(self) -> Task | None:
        for t in self.tasks:
            if t.status == "backlog":
                return t
        return None

    def set_status(self, task_id: str, status: str, notes: str = "") -> bool:
        for t in self.tasks:
            if t.id == task_id:
                t.status = status
                t.updated_at = time.time()
                if notes:
                    t.notes = notes
                self.save()
                return True
        return False

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for t in self.tasks:
            c[t.status] = c.get(t.status, 0) + 1
        return c


# ---------------------------------------------------------------- log

class EventLog:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or data_dir()
        self.path = self.dir / "log.jsonl"

    def append(self, kind: str, message: str, **extra: Any) -> None:
        # Scrub anything that looks like a credential before writing.
        safe_extra = {
            k: ("***" if any(s in k.lower() for s in ("key", "token", "secret", "password")) else v)
            for k, v in extra.items()
        }
        rec = {"at": time.time(), "kind": kind, "message": message, **safe_extra}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def tail(self, n: int = 100) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


# ---------------------------------------------------------------- brief + checkpoints

class Brief:
    """The one starter prompt, saved once and reused forever."""

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or data_dir()
        self.path = self.dir / "brief.json"

    def save_text(self, text: str, project_name: str = "") -> None:
        _write_json(self.path, {
            "project_name": project_name,
            "text": text,
            "saved_at": time.time(),
        })

    def load(self) -> dict[str, Any]:
        return _read_json(self.path, {"project_name": "", "text": ""})


class Checkpoints:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or data_dir()
        self.path = self.dir / "checkpoints.json"

    def record(self, commit: str, task_id: str, note: str = "") -> None:
        items: list[dict[str, Any]] = _read_json(self.path, [])
        items.append({"at": time.time(), "commit": commit,
                      "task_id": task_id, "note": note})
        _write_json(self.path, items)

    def latest(self) -> dict[str, Any] | None:
        items: list[dict[str, Any]] = _read_json(self.path, [])
        return items[-1] if items else None
