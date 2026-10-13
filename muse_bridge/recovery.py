"""Minimal local recovery file — links and IDs only, never a message store.

``recovery.json`` lives in the bridge data directory. It records just enough
for a human or a fresh process to reconnect after a restart:

- bridge version
- game repo URL + local path
- ChatGPT window title
- worker IDs -> window titles + account bindings
- last checkpoint (id + time)
- data directory location

It never stores secrets, message contents, or conversation history — the
edition stores no secrets at all, and the full state already persists in
the existing JSON stores (workers, queue, accounts, settings, checkpoints).
This file is the lightweight index card: if a worker forgets a link or the
app restarts, read this file first.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

RECOVERY_VERSION = 1
RECOVERY_FILENAME = "recovery.json"

# Keys that must never appear in the recovery file, defence in depth.
_FORBIDDEN_SUBSTRINGS = ("api_key", "apikey", "secret", "password", "token",
                         "cookie", "credential")


def _scrub(obj: Any) -> Any:
    """Refuse secret-shaped data rather than writing it."""
    if isinstance(obj, dict):
        for k in obj:
            if any(s in str(k).lower() for s in _FORBIDDEN_SUBSTRINGS):
                raise ValueError(f"refusing to write secret-shaped key: {k}")
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def recovery_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / RECOVERY_FILENAME


def write_recovery(orch: Any, bridge_version: str = "1") -> Path:
    """Snapshot the reconnect essentials from a live Orchestrator."""
    data_dir = Path(orch.settings.dir)
    workers: dict[str, dict[str, str]] = {}
    try:
        for s in orch.workers.slots:
            workers[s.id] = {
                "label": s.label,
                "window": s.window_title,
                "chatgpt_window": s.chatgpt_window,
                "account": s.account_id,
                "paused": s.paused,
                "state": s.state,
                "task": s.task_id,
            }
    except Exception:
        pass
    try:
        cp = orch.checkpoints.latest() or {}
        last_checkpoint = {
            "at": cp.get("at", 0),
            "task": cp.get("task_id", ""),
            "commit": cp.get("commit", ""),
        }
    except Exception:
        last_checkpoint = {}
    try:
        repo_url = orch.settings.get("game_repo_url", "")
        repo_path = orch.settings.get("game_repo_path", "")
        chatgpt_window = orch.settings.get("chatgpt_window_title", "")
    except Exception:
        repo_url = repo_path = chatgpt_window = ""
    payload = {
        "version": RECOVERY_VERSION,
        "bridge": bridge_version,
        "written_at": time.time(),
        "repo_url": repo_url,
        "repo_path": repo_path,
        "chatgpt_window": chatgpt_window,
        "workers": workers,
        "last_checkpoint": last_checkpoint,
        "data_dir": str(data_dir),
        "how_to_reconnect": (
            "1) Open Muse Bridge. 2) It re-reads this file and looks for "
            "each worker window by title. 3) Re-bind any window it cannot "
            "find, then re-run the probe. 4) Press START to resume."
        ),
    }
    _scrub(payload)
    path = recovery_path(data_dir)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_recovery(data_dir: Path | str) -> dict[str, Any] | None:
    """Read the recovery file. Returns None when absent or unreadable."""
    path = recovery_path(data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != RECOVERY_VERSION:
        return None
    return data


def reconnect(orch: Any) -> list[str]:
    """Re-find bound worker windows by title after a restart.

    Non-intrusive: lists windows and matches titles, never steals focus.
    Returns human-readable per-worker results.
    """
    from . import muse_link
    results: list[str] = []
    try:
        slots = orch.workers.slots
    except Exception:
        return ["Worker list unavailable."]
    if not slots:
        return ["No workers registered."]
    try:
        titles = {w.title for w in muse_link.list_windows()}
    except Exception:
        titles = set()
    for s in slots:
        if not s.window_title:
            results.append(f"{s.label}: no window bound — bind one in Workers.")
        elif s.window_title in titles:
            results.append(f"{s.label}: reconnected ('{s.window_title}').")
        else:
            results.append(
                f"{s.label}: window not found ('{s.window_title}') — "
                f"re-bind and re-run the probe.")
    return results
