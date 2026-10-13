"""Persistent RPG project profile for Muse Bridge.

Facts are READ from the actual repository when available — never invented.
Falls back to honest "(not detected)" placeholders otherwise.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


PROFILE_VERSION = 1


# Authoritative constraints every worker must respect. Updated only with
# verified facts — never invented.
ARCHITECTURE_CONSTRAINTS = [
    "React + Vite + TypeScript; GitHub Pages deployment; no backend.",
    "2.5D isometric renderer is the default view; 2D is the Settings fallback.",
    "Single grid/chunk coordinate system — never introduce a second one.",
    "World generation is deterministic from the world seed.",
    "NPC identities, quests, dialogue, saves, roads, authored locations preserved.",
    "Save schema is versioned; old saves must migrate, never be invalidated.",
    "Entities persist in saves: spawn-code changes must also handle save restore.",
    "No broad rewrites or replacement architecture without explicit approval.",
]


def scan_repo(repo: Path) -> dict:
    """Read real facts from the game repository. No invention."""
    facts: dict = {"path": str(repo), "detected": {}}
    d = facts["detected"]
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            d["name"] = data.get("name", "")
            deps = {**(data.get("dependencies", {})), **(data.get("devDependencies", {}))}
            d["react"] = "react" in deps
            d["vite"] = "vite" in deps
            d["typescript"] = "typescript" in deps
            scripts = data.get("scripts", {})
            d["scripts"] = {k: scripts[k] for k in ("build", "test") if k in scripts}
        except (json.JSONDecodeError, OSError):
            d["package.json"] = "unreadable"
    else:
        d["package.json"] = "not found"
    src = repo / "src"
    if src.is_dir():
        d["src_top_level"] = sorted(
            p.name for p in src.iterdir() if not p.name.startswith("."))[:20]
        game = src / "game"
        if game.is_dir():
            mods = sorted(p.stem for p in game.glob("*.ts"))[:30]
            d["game_modules_sample"] = mods
    # Architecture markers the bridge must preserve.
    markers = {
        "iso_renderer": bool(list((src).rglob("Iso*.tsx"))) if src.is_dir() else False,
        "chunk_system": False,
        "save_system": False,
    }
    if src.is_dir():
        text_hits: dict[str, bool] = {"chunk": False, "localStorage": False}
        for p in list(src.rglob("*.ts"))[:50]:
            try:
                t = p.read_text(encoding="utf-8", errors="ignore")[:20000]
            except OSError:
                continue
            if "chunk" in t.lower():
                text_hits["chunk"] = True
            if "localStorage" in t:
                text_hits["localStorage"] = True
            if all(text_hits.values()):
                break
        markers["chunk_system"] = text_hits["chunk"]
        markers["save_system"] = text_hits["localStorage"]
    d["architecture_markers"] = markers
    readme = repo / "README.md"
    if readme.exists():
        try:
            d["readme_head"] = readme.read_text(encoding="utf-8")[:500]
        except OSError:
            pass
    return facts


class ProjectProfile:
    def __init__(self, directory: Path | None = None) -> None:
        from .store import data_dir
        self.dir = directory or data_dir()
        self.path = self.dir / "project_profile.json"

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") == PROFILE_VERSION:
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return self.blank()

    def blank(self) -> dict:
        return {
            "version": PROFILE_VERSION,
            "repo_path": "",
            "branch": "",
            "repo_facts": {},
            "architecture_notes": "",
            "visual_goals": "",
            "gameplay_goals": "",
            "project_summary": "",
            "summary_updated_at": 0.0,
            "next_priority": "",
            "completed": [],
            "failed": [],
            # --- shared game memory (all workers read this) ---
            "architecture_constraints": list(ARCHITECTURE_CONSTRAINTS),
            "decision_log": [],        # {"at", "decision", "reason", "by"}
            "module_index": {},        # {"path": "one-line purpose"}
            "build_number": "",
            "current_commit": "",
        }

    def save(self, data: dict) -> None:
        data["version"] = PROFILE_VERSION
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    def refresh_repo(self, repo_path: str, branch: str = "") -> dict:
        """Re-scan the repo and merge fresh facts into the profile."""
        data = self.load()
        data["repo_path"] = repo_path
        data["branch"] = branch
        if repo_path and Path(repo_path).is_dir():
            data["repo_facts"] = scan_repo(Path(repo_path))
            data["repo_scanned_at"] = time.time()
        self.save(data)
        return data

    def repo_facts_text(self) -> str:
        data = self.load()
        facts = data.get("repo_facts", {}).get("detected", {})
        if not facts:
            return "(repository not scanned yet)"
        lines = []
        if facts.get("name"):
            lines.append(f"Project: {facts['name']}")
        stack = [k for k in ("react", "vite", "typescript") if facts.get(k)]
        if stack:
            lines.append("Stack: " + ", ".join(stack))
        if facts.get("scripts"):
            lines.append("Scripts: " + ", ".join(
                f"{k}={v}" for k, v in facts["scripts"].items()))
        markers = facts.get("architecture_markers", {})
        found = [k for k, v in markers.items() if v]
        if found:
            lines.append("Detected systems: " + ", ".join(found))
        if facts.get("game_modules_sample"):
            lines.append("Game modules (sample): " +
                         ", ".join(facts["game_modules_sample"][:12]))
        return "\n".join(lines) if lines else "(scan found nothing conclusive)"

    def record_outcome(self, task_id: str, title: str, passed: bool,
                       note: str = "") -> None:
        data = self.load()
        entry = {"id": task_id, "title": title, "at": time.time(), "note": note}
        data["completed" if passed else "failed"].append(entry)
        self.save(data)

    def update_summary(self, summary: str, next_priority: str = "") -> None:
        data = self.load()
        data["project_summary"] = summary
        data["summary_updated_at"] = time.time()
        if next_priority:
            data["next_priority"] = next_priority
        self.save(data)

    # --- shared game memory -------------------------------------------
    def log_decision(self, decision: str, reason: str = "",
                     by: str = "bridge") -> None:
        """Record an architectural decision. Verified facts only."""
        data = self.load()
        data["decision_log"].append({
            "at": time.time(), "decision": decision,
            "reason": reason, "by": by,
        })
        self.save(data)

    def set_module_index(self, index: dict[str, str]) -> None:
        data = self.load()
        data["module_index"] = index
        self.save(data)

    def record_build(self, build_number: str, commit: str) -> None:
        data = self.load()
        data["build_number"] = build_number
        data["current_commit"] = commit
        self.save(data)

    def worker_briefing(self) -> str:
        """The authoritative briefing every worker reads before coding."""
        data = self.load()
        lines = [
            "AUTHORITATIVE GAME BRIEF (read before changing code):",
            "",
            "Architecture constraints:",
            *[f"  - {c}" for c in data.get("architecture_constraints", [])],
            "",
            f"Project summary: {data.get('project_summary', '(none)')}",
            f"Next priority: {data.get('next_priority', '(none)')}",
        ]
        if data.get("decision_log"):
            lines.append("")
            lines.append("Key decisions:")
            for d in data["decision_log"][-8:]:
                lines.append(f"  - {d['decision']} ({d.get('reason', '')})")
        if data.get("module_index"):
            lines.append("")
            lines.append("Module index (purpose per file):")
            for path, purpose in list(data["module_index"].items())[:20]:
                lines.append(f"  - {path}: {purpose}")
        return "\n".join(lines)
