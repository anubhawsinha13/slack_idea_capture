"""
idea_store.py

CRUD operations for ideas (data/ideas.json) and projects (data/projects.json).
All file I/O is isolated here so slack_bot.py stays focused on Slack handlers.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
IDEAS_PATH = DATA_DIR / "ideas.json"
PROJECTS_PATH = DATA_DIR / "projects.json"

_lock = threading.Lock()


# ── Generic JSON helpers ──────────────────────────────────────────────────────

def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Ideas ─────────────────────────────────────────────────────────────────────

def _load_ideas() -> dict:
    data = _load(IDEAS_PATH)
    data.setdefault("ideas", [])
    data.setdefault("next_id", 1)
    return data


def add_idea(
    text: str,
    category: str = "blog",
    tags: Optional[list[str]] = None,
    source: str = "slack_command",
    slack_user: str = "",
    slack_channel: str = "",
) -> dict:
    """Add a new idea and return it."""
    with _lock:
        data = _load_ideas()
        idea = {
            "id": data["next_id"],
            "text": text,
            "category": category,
            "tags": tags or [],
            "status": "captured",
            "source": source,
            "slack_user": slack_user,
            "slack_channel": slack_channel,
            "created_at": _now_iso(),
            "promotions": [],
            "evaluation": None,
        }
        data["ideas"].append(idea)
        data["next_id"] += 1
        _save(IDEAS_PATH, data)
        return idea


def get_idea(idea_id: int) -> Optional[dict]:
    data = _load_ideas()
    for idea in data["ideas"]:
        if idea["id"] == idea_id:
            return idea
    return None


def list_ideas(
    tag: Optional[str] = None,
    status: Optional[str] = None,
    show_all: bool = False,
) -> list[dict]:
    """Return ideas matching the filters. Default: only 'captured' status."""
    data = _load_ideas()
    results = data["ideas"]

    if not show_all and status is None:
        results = [i for i in results if i["status"] in ("captured", "promoted")]
    elif status:
        results = [i for i in results if i["status"] == status]

    if tag:
        tag_lower = tag.lower()
        results = [
            i for i in results
            if i["category"] == tag_lower or tag_lower in [t.lower() for t in i.get("tags", [])]
        ]

    return results


def update_idea_status(idea_id: int, status: str) -> Optional[dict]:
    """Set idea status. Returns updated idea or None if not found."""
    with _lock:
        data = _load_ideas()
        for idea in data["ideas"]:
            if idea["id"] == idea_id:
                idea["status"] = status
                _save(IDEAS_PATH, data)
                return idea
        return None


def delete_idea(idea_id: int) -> Optional[dict]:
    """Remove idea entirely. Returns deleted idea or None."""
    with _lock:
        data = _load_ideas()
        for i, idea in enumerate(data["ideas"]):
            if idea["id"] == idea_id:
                removed = data["ideas"].pop(i)
                _save(IDEAS_PATH, data)
                return removed
        return None


def add_promotion(idea_id: int, promotion: dict) -> Optional[dict]:
    """Append a promotion record to an idea. Sets status to 'promoted'."""
    promotion.setdefault("at", _now_iso())
    with _lock:
        data = _load_ideas()
        for idea in data["ideas"]:
            if idea["id"] == idea_id:
                idea["promotions"].append(promotion)
                if idea["status"] == "captured":
                    idea["status"] = "promoted"
                _save(IDEAS_PATH, data)
                return idea
        return None


def save_evaluation(idea_id: int, evaluation: dict) -> Optional[dict]:
    """Store evaluation results on an idea (overwrites previous)."""
    evaluation.setdefault("evaluated_at", _now_iso())
    with _lock:
        data = _load_ideas()
        for idea in data["ideas"]:
            if idea["id"] == idea_id:
                idea["evaluation"] = evaluation
                _save(IDEAS_PATH, data)
                return idea
        return None


# ── Projects ──────────────────────────────────────────────────────────────────

def _load_projects() -> dict:
    data = _load(PROJECTS_PATH)
    data.setdefault("projects", [])
    data.setdefault("next_id", 1)
    return data


def add_project(
    name: str,
    description: str,
    year: Optional[str] = None,
    tags: Optional[list[str]] = None,
    github: str = "",
    from_idea_id: Optional[int] = None,
) -> dict:
    """Add a new project and return it."""
    with _lock:
        data = _load_projects()
        project = {
            "id": data["next_id"],
            "name": name,
            "description": description,
            "year": year or str(datetime.now(timezone.utc).year),
            "tags": tags or [],
            "github": github,
            "status": "active",
            "from_idea_id": from_idea_id,
            "created_at": _now_iso(),
        }
        data["projects"].append(project)
        data["next_id"] += 1
        _save(PROJECTS_PATH, data)
        return project


def get_project(project_id: int) -> Optional[dict]:
    data = _load_projects()
    for proj in data["projects"]:
        if proj["id"] == project_id:
            return proj
    return None


def list_projects() -> list[dict]:
    return _load_projects()["projects"]
