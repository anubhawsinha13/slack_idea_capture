"""
sync_projects_to_anuva.py

Syncs data/projects.json to anuva.blog's content/projects.json.

Two modes:
  - Local: writes directly to ANUVA_BLOG_DIR/content/projects.json
  - CI:    pushes via GitHub contents API using ANUVA_BLOG_REPO + ANUVA_BLOG_PAT
"""

import json
import os
from base64 import b64encode, b64decode
from pathlib import Path

import requests

from idea_store import list_projects

GITHUB_API = "https://api.github.com"


def _projects_for_anuva() -> list[dict]:
    """Convert internal project records to anuva.blog's format."""
    return [
        {
            "name": p["name"],
            "description": p["description"],
            "year": p["year"],
            "tags": p["tags"],
            "github": p["github"],
        }
        for p in list_projects()
        if p.get("status") == "active"
    ]


def sync_local() -> str:
    """Write projects to the local anuva.blog directory. Returns the file path."""
    anuva_dir = os.getenv("ANUVA_BLOG_DIR", "")
    if not anuva_dir:
        raise EnvironmentError("ANUVA_BLOG_DIR is not set.")

    target = Path(anuva_dir) / "content" / "projects.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(_projects_for_anuva(), indent=2, ensure_ascii=False) + "\n"
    target.write_text(payload, encoding="utf-8")
    return str(target)


def sync_github() -> str:
    """Push projects to anuva.blog via GitHub contents API. Returns the file URL."""
    repo = os.getenv("ANUVA_BLOG_REPO", "")
    pat = os.getenv("ANUVA_BLOG_PAT", "")
    if not repo or not pat:
        raise EnvironmentError("ANUVA_BLOG_REPO and ANUVA_BLOG_PAT must be set for CI sync.")

    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    file_path = "content/projects.json"
    url = f"{GITHUB_API}/repos/{repo}/contents/{file_path}"

    existing = requests.get(url, headers=headers, timeout=15)
    sha = existing.json().get("sha") if existing.status_code == 200 else None

    content = json.dumps(_projects_for_anuva(), indent=2, ensure_ascii=False) + "\n"
    payload = {
        "message": "chore: sync projects from slack_idea_capture",
        "content": b64encode(content.encode()).decode(),
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()["content"]["html_url"]


def sync() -> str:
    """
    Sync projects to anuva.blog. Tries local first, then GitHub API.
    Returns a description of what was synced.
    """
    anuva_dir = os.getenv("ANUVA_BLOG_DIR", "")
    if anuva_dir and Path(anuva_dir).is_dir():
        path = sync_local()
        return f"Synced to local: {path}"

    return f"Synced to GitHub: {sync_github()}"


if __name__ == "__main__":
    print(sync())
