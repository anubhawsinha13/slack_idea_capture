"""
slack_bot.py

Slack bot for capturing ideas, evaluating them against the market, and
promoting them to blog posts or projects.

Commands:
  /idea <text> [--tag blog|project|thought]   Capture an idea (auto-evaluates)
  /note <text>                                 Shorthand: /idea --tag thought
  /ideas [--tag <tag>] [--status <s>] [--all]  List/filter ideas
  /idea_evaluate <id>                          Re-evaluate an idea
  /idea_to_blog <id> [--live]                  Promote to blog post
  /idea_to_project <id> [--to anuva|github]    Promote to project
  /idea_done <id>                              Mark as done
  /idea_archive <id>                           Archive
  /idea_delete <id>                            Delete

Setup:
  - Create a Slack App at https://api.slack.com/apps
  - Enable Socket Mode -> App-Level Token (xapp-...) with connections:write
  - Register the slash commands listed above
  - OAuth scopes: commands, chat:write
  - Optional (emoji capture): reactions:read, channels:history, groups:history
  - Install to workspace, copy tokens into .env
"""

import json
import os
import re
import sys
import threading
import time
from base64 import b64encode
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from idea_store import (
    add_idea,
    get_idea,
    list_ideas,
    update_idea_status,
    delete_idea,
    add_promotion,
    save_evaluation,
    add_project,
    list_projects,
)
from idea_evaluator import evaluate_idea, format_scorecard_slack


# ── Environment ───────────────────────────────────────────────────────────────

def _load_env() -> str:
    """Locate and load .env from the project root."""
    script_dir = Path(__file__).resolve().parent
    for search_dir in (script_dir.parent, script_dir.parent.parent):
        try:
            entries = os.listdir(str(search_dir))
        except OSError:
            continue
        if ".env" in entries:
            env_path = os.path.join(str(search_dir), ".env")
            load_dotenv(dotenv_path=env_path)
            return env_path
    load_dotenv()
    return ""


_ENV_PATH = _load_env()

_REQUIRED = {
    "SLACK_BOT_TOKEN": "Bot OAuth token (xoxb-...) from Slack App -> OAuth & Permissions",
    "SLACK_APP_TOKEN": "App-level token (xapp-...) from Slack App -> Basic Information -> App-Level Tokens",
    "GITHUB_TOKEN": "Personal Access Token with repo + workflow scopes",
    "GITHUB_REPO": "This repo in owner/repo format, e.g. anubhawsinha/slack_idea_capture",
}

_missing = [f"  {k}  ({hint})" for k, hint in _REQUIRED.items() if not os.getenv(k)]
if _missing:
    print("ERROR: Missing required variables in .env:\n")
    print("\n".join(_missing))
    print(f"\nLooked for .env at: {_ENV_PATH}")
    sys.exit(1)


# ── Slack app ─────────────────────────────────────────────────────────────────

app = App(token=os.environ["SLACK_BOT_TOKEN"])


# ── GitHub helpers ────────────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"


def _github_headers(token: Optional[str] = None) -> dict:
    t = token or os.getenv("GITHUB_TOKEN", "")
    return {
        "Authorization": f"Bearer {t}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _commit_data_to_github() -> Optional[str]:
    """Commit data/ideas.json to this project's GitHub repo."""
    repo = os.environ["GITHUB_REPO"]
    branch = os.getenv("GITHUB_BRANCH", "main")
    data_path = Path(__file__).resolve().parent.parent / "data" / "ideas.json"
    if not data_path.exists():
        return None

    content_b64 = b64encode(data_path.read_bytes()).decode()
    url = f"{GITHUB_API}/repos/{repo}/contents/data/ideas.json"
    headers = _github_headers()

    existing = requests.get(url, headers=headers, params={"ref": branch}, timeout=10)
    sha = existing.json().get("sha") if existing.status_code == 200 else None

    payload = {
        "message": "chore: update ideas.json",
        "content": content_b64,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    try:
        resp = requests.put(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()["content"]["html_url"]
    except Exception:
        return None


def _trigger_blog_workflow(topic: str, live: bool = False) -> Optional[str]:
    """Trigger blog_post_workflow's publish-content.yml with a topic."""
    repo = os.getenv("BLOG_WORKFLOW_REPO", "")
    branch = os.getenv("BLOG_WORKFLOW_BRANCH", "main")
    if not repo:
        return None

    url = f"{GITHUB_API}/repos/{repo}/actions/workflows/publish-content.yml/dispatches"
    payload = {
        "ref": branch,
        "inputs": {
            "post_file": "",
            "topic": topic,
            "draft": "false" if live else "true",
            "categories": "Engineering",
        },
    }
    try:
        resp = requests.post(url, json=payload, headers=_github_headers(), timeout=15)
        resp.raise_for_status()
        return f"https://github.com/{repo}/actions/workflows/publish-content.yml"
    except Exception:
        return None


def _get_latest_run_url(repo: str) -> Optional[str]:
    """Poll for the most recent Actions run URL."""
    url = f"{GITHUB_API}/repos/{repo}/actions/runs"
    for _ in range(5):
        time.sleep(2)
        try:
            resp = requests.get(url, headers=_github_headers(), params={"per_page": 3}, timeout=10)
            if resp.status_code == 200:
                for run in resp.json().get("workflow_runs", []):
                    if run.get("name") == "Publish Blog Post":
                        return run["html_url"]
        except Exception:
            pass
    return None


def _create_github_repo(name: str, description: str) -> Optional[str]:
    """Create a new GitHub repo and return its URL."""
    url = f"{GITHUB_API}/user/repos"
    payload = {
        "name": name,
        "description": description,
        "private": False,
        "auto_init": True,
    }
    try:
        resp = requests.post(url, json=payload, headers=_github_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()["html_url"]
    except Exception:
        return None


def _generate_readme_with_claude(idea_text: str, repo_name: str) -> Optional[str]:
    """Generate a project README using Claude."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": (
                    f"Generate a README.md for a new project called '{repo_name}' based on this idea:\n\n"
                    f"{idea_text}\n\n"
                    "Include: project description, features/goals, getting started section, "
                    "and a roadmap. Keep it concise and professional. "
                    "Return only the markdown content, no code fences."
                ),
            }],
        )
        return resp.content[0].text
    except Exception:
        return None


def _commit_readme_to_repo(repo_url: str, readme_content: str) -> bool:
    """Push a README.md to a newly created GitHub repo."""
    parts = repo_url.rstrip("/").split("/")
    owner, repo_name = parts[-2], parts[-1]
    url = f"{GITHUB_API}/repos/{owner}/{repo_name}/contents/README.md"

    existing = requests.get(url, headers=_github_headers(), timeout=10)
    sha = existing.json().get("sha") if existing.status_code == 200 else None

    payload = {
        "message": "docs: add project README",
        "content": b64encode(readme_content.encode()).decode(),
    }
    if sha:
        payload["sha"] = sha

    try:
        resp = requests.put(url, json=payload, headers=_github_headers(), timeout=15)
        return resp.status_code in (200, 201)
    except Exception:
        return False


# ── Evaluation background runner ──────────────────────────────────────────────

def _run_evaluation_async(idea_id: int, idea_text: str, channel: str, client):
    """Evaluate in a background thread and post the result to Slack."""
    def _worker():
        scorecard = evaluate_idea(idea_text)
        if scorecard:
            save_evaluation(idea_id, scorecard)
            _commit_data_to_github()
            msg = format_scorecard_slack(idea_text, scorecard)
            try:
                client.chat_postMessage(channel=channel, text=msg)
            except Exception as exc:
                print(f"[eval] Failed to post scorecard to {channel}: {exc}", flush=True)
        else:
            try:
                client.chat_postMessage(
                    channel=channel,
                    text=f":warning: Could not evaluate idea #{idea_id} — check API keys (PERPLEXITY_API_KEY, ANTHROPIC_API_KEY).",
                )
            except Exception as exc:
                print(f"[eval] Failed to post warning to {channel}: {exc}", flush=True)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


# ── Argument parser helper ────────────────────────────────────────────────────

def _parse_flags(text: str, flags: dict[str, str]) -> tuple[str, dict[str, str]]:
    """
    Extract --flag value pairs from text.
    flags: { "flag_name": "default_value" }
    Returns (remaining_text, parsed_values).
    """
    values = dict(flags)
    for flag_name in flags:
        pattern = rf"--{flag_name}\s+(\S+)"
        match = re.search(pattern, text)
        if match:
            values[flag_name] = match.group(1)
            text = text[:match.start()] + text[match.end():]
    # Also handle boolean flags (no value)
    for flag_name in flags:
        if f"--{flag_name}" in text and values[flag_name] == flags[flag_name]:
            text = text.replace(f"--{flag_name}", "")
            values[flag_name] = "true"
    return text.strip(), values


# ── /idea ─────────────────────────────────────────────────────────────────────

@app.command("/idea")
def handle_idea(ack, respond, command, client):
    ack()

    text = command.get("text", "").strip()
    if not text:
        respond(
            ":warning: Please provide an idea.\n"
            "  `/idea Build a CLI for LLM benchmarking`\n"
            "  `/idea RAG pipeline tutorial --tag blog`\n"
            "  `/idea New dashboard project --tag project`"
        )
        return

    remaining, flags = _parse_flags(text, {"tag": "blog"})
    category = flags["tag"]
    if category not in ("blog", "project", "thought"):
        category = "blog"

    if not remaining:
        respond(":warning: Idea text cannot be empty after parsing flags.")
        return

    idea = add_idea(
        text=remaining,
        category=category,
        source="slack_command",
        slack_user=command.get("user_id", ""),
        slack_channel=command.get("channel_id", ""),
    )

    _commit_data_to_github()

    respond(
        f":bulb: *Idea #{idea['id']} captured!*\n\n"
        f"  *Text:* {idea['text']}\n"
        f"  *Category:* {idea['category']}\n\n"
        f"_Evaluating against the market — scorecard arriving shortly …_"
    )

    _run_evaluation_async(idea["id"], idea["text"], command.get("channel_id", ""), client)


# ── /note ─────────────────────────────────────────────────────────────────────

@app.command("/note")
def handle_note(ack, respond, command, client):
    ack()

    text = command.get("text", "").strip()
    if not text:
        respond(":warning: Please provide a note. `/note Quick thought about RAG latency`")
        return

    idea = add_idea(
        text=text,
        category="thought",
        source="slack_command",
        slack_user=command.get("user_id", ""),
        slack_channel=command.get("channel_id", ""),
    )

    _commit_data_to_github()

    respond(
        f":thought_balloon: *Note #{idea['id']} captured!*\n\n"
        f"  *Text:* {idea['text']}\n\n"
        f"_Evaluating — scorecard arriving shortly …_"
    )

    _run_evaluation_async(idea["id"], idea["text"], command.get("channel_id", ""), client)


# ── /ideas ────────────────────────────────────────────────────────────────────

@app.command("/ideas")
def handle_ideas(ack, respond, command):
    ack()

    text = command.get("text", "").strip()
    show_all = "--all" in text
    text = text.replace("--all", "").strip()

    _, flags = _parse_flags(text, {"tag": "", "status": ""})
    tag = flags["tag"] or None
    status = flags["status"] or None

    ideas = list_ideas(tag=tag, status=status, show_all=show_all)

    if not ideas:
        respond(":information_source: No ideas found matching your filters.")
        return

    category_icons = {"blog": ":memo:", "project": ":hammer_and_wrench:", "thought": ":thought_balloon:"}
    status_icons = {
        "captured": ":white_circle:",
        "promoted": ":large_green_circle:",
        "done": ":white_check_mark:",
        "archived": ":file_cabinet:",
    }

    lines = [f":bulb: *Ideas* ({len(ideas)} total)\n"]
    for idea in ideas:
        cat_icon = category_icons.get(idea["category"], ":grey_question:")
        st_icon = status_icons.get(idea["status"], ":grey_question:")
        verdict = ""
        if idea.get("evaluation"):
            v = idea["evaluation"].get("verdict", "")
            score = idea["evaluation"].get("overall_score", 0)
            verdict_labels = {"strong_yes": "STRONG YES", "yes": "YES", "maybe": "MAYBE", "no": "NO"}
            verdict = f" · _{score}/10 {verdict_labels.get(v, v)}_"

        promos = ""
        if idea.get("promotions"):
            types = [p["type"] for p in idea["promotions"]]
            promos = f" · [{', '.join(types)}]"

        lines.append(
            f"  {st_icon} `{idea['id']}` {cat_icon} {idea['text'][:80]}"
            f"{'…' if len(idea['text']) > 80 else ''}"
            f"{verdict}{promos}"
        )

    lines.append(
        "\n_Commands:_ `/idea_evaluate <id>` · `/idea_to_blog <id>` · "
        "`/idea_to_project <id>` · `/idea_done <id>`"
    )
    respond("\n".join(lines))


# ── /idea_evaluate ────────────────────────────────────────────────────────────

@app.command("/idea_evaluate")
def handle_evaluate(ack, respond, command, client):
    ack()

    text = command.get("text", "").strip()
    if not text:
        respond(":warning: Usage: `/idea_evaluate 3`")
        return

    try:
        idea_id = int(text.split()[0])
    except ValueError:
        respond(f":x: Invalid idea ID: `{text}`. Use a number.")
        return

    idea = get_idea(idea_id)
    if not idea:
        respond(f":x: Idea #{idea_id} not found. Run `/ideas --all` to see all ideas.")
        return

    respond(f":mag: Re-evaluating idea #{idea_id}: _{idea['text']}_ …")
    _run_evaluation_async(idea_id, idea["text"], command.get("channel_id", ""), client)


# ── /idea_to_blog ─────────────────────────────────────────────────────────────

@app.command("/idea_to_blog")
def handle_to_blog(ack, respond, command):
    ack()

    text = command.get("text", "").strip()
    live = "--live" in text
    text = text.replace("--live", "").strip()

    if not text:
        respond(":warning: Usage: `/idea_to_blog 3` or `/idea_to_blog 3 --live`")
        return

    try:
        idea_id = int(text.split()[0])
    except ValueError:
        respond(f":x: Invalid idea ID: `{text}`")
        return

    idea = get_idea(idea_id)
    if not idea:
        respond(f":x: Idea #{idea_id} not found.")
        return

    blog_repo = os.getenv("BLOG_WORKFLOW_REPO", "")
    if not blog_repo:
        respond(":x: `BLOG_WORKFLOW_REPO` is not configured in `.env`.")
        return

    mode_label = ":rocket: *live*" if live else ":pencil: *draft*"
    respond(f":gear: Promoting idea #{idea_id} to blog ({mode_label}) …\n_{idea['text']}_")

    actions_url = _trigger_blog_workflow(idea["text"], live=live)
    if not actions_url:
        respond(":x: Failed to trigger blog workflow. Check `BLOG_WORKFLOW_REPO` and `GITHUB_TOKEN`.")
        return

    run_url = _get_latest_run_url(blog_repo) or actions_url

    add_promotion(idea_id, {"type": "blog", "actions_run": run_url})
    _commit_data_to_github()

    outcome = "go live on your site" if live else "appear as a WordPress draft"
    respond(
        f":white_check_mark: Blog pipeline triggered!\n\n"
        f"*Idea:* #{idea_id} — {idea['text']}\n"
        f"*Mode:* {'LIVE' if live else 'Draft'}\n"
        f"*Actions run:* {run_url}\n\n"
        f"The post will {outcome} once the pipeline finishes."
    )


# ── /idea_to_project ──────────────────────────────────────────────────────────

@app.command("/idea_to_project")
def handle_to_project(ack, respond, command):
    ack()

    text = command.get("text", "").strip()
    remaining, flags = _parse_flags(text, {"to": "anuva", "tags": ""})

    if not remaining:
        respond(
            ":warning: Usage:\n"
            "  `/idea_to_project 3`                     — add to anuva.blog projects\n"
            "  `/idea_to_project 3 --to github`          — create a new GitHub repo\n"
            "  `/idea_to_project 3 --to github --tags Python,AI`"
        )
        return

    try:
        idea_id = int(remaining.split()[0])
    except ValueError:
        respond(f":x: Invalid idea ID: `{remaining}`")
        return

    idea = get_idea(idea_id)
    if not idea:
        respond(f":x: Idea #{idea_id} not found.")
        return

    target = flags["to"]
    extra_tags = [t.strip() for t in flags["tags"].split(",") if t.strip()] if flags["tags"] else []

    if target == "github":
        respond(f":gear: Creating GitHub repo for idea #{idea_id} …")

        slug = re.sub(r"[^a-z0-9]+", "-", idea["text"].lower())[:50].strip("-")
        repo_url = _create_github_repo(slug, idea["text"])
        if not repo_url:
            respond(":x: Failed to create GitHub repo. Check `GITHUB_TOKEN` permissions.")
            return

        respond(f":pencil: Generating README with Claude …")
        readme = _generate_readme_with_claude(idea["text"], slug)
        if readme:
            _commit_readme_to_repo(repo_url, readme)

        add_promotion(idea_id, {"type": "github_repo", "repo_url": repo_url})
        _commit_data_to_github()

        respond(
            f":white_check_mark: GitHub repo created!\n\n"
            f"*Idea:* #{idea_id} — {idea['text']}\n"
            f"*Repo:* {repo_url}\n"
            f"{'*README:* generated with Claude' if readme else '*README:* default (Claude unavailable)'}"
        )

    else:
        respond(f":gear: Adding idea #{idea_id} to anuva.blog projects …")

        tags = extra_tags or idea.get("tags", [])
        project = add_project(
            name=idea["text"][:80],
            description=idea["text"],
            tags=tags,
            from_idea_id=idea_id,
        )

        try:
            from sync_projects_to_anuva import sync
            sync_result = sync()
        except Exception as exc:
            sync_result = f"Sync failed: {exc}"

        add_promotion(idea_id, {"type": "anuva_project", "project_id": project["id"]})
        _commit_data_to_github()

        respond(
            f":white_check_mark: Project added!\n\n"
            f"*Idea:* #{idea_id} — {idea['text']}\n"
            f"*Project ID:* {project['id']}\n"
            f"*Sync:* {sync_result}"
        )


# ── /idea_done ────────────────────────────────────────────────────────────────

@app.command("/idea_done")
def handle_done(ack, respond, command):
    ack()
    text = command.get("text", "").strip()
    if not text:
        respond(":warning: Usage: `/idea_done 3`")
        return
    try:
        idea_id = int(text.split()[0])
    except ValueError:
        respond(f":x: Invalid idea ID: `{text}`")
        return

    idea = update_idea_status(idea_id, "done")
    if not idea:
        respond(f":x: Idea #{idea_id} not found.")
        return

    _commit_data_to_github()
    respond(f":white_check_mark: Idea #{idea_id} marked as *done*.")


# ── /idea_archive ─────────────────────────────────────────────────────────────

@app.command("/idea_archive")
def handle_archive(ack, respond, command):
    ack()
    text = command.get("text", "").strip()
    if not text:
        respond(":warning: Usage: `/idea_archive 3`")
        return
    try:
        idea_id = int(text.split()[0])
    except ValueError:
        respond(f":x: Invalid idea ID: `{text}`")
        return

    idea = update_idea_status(idea_id, "archived")
    if not idea:
        respond(f":x: Idea #{idea_id} not found.")
        return

    _commit_data_to_github()
    respond(f":file_cabinet: Idea #{idea_id} *archived*.")


# ── /idea_delete ──────────────────────────────────────────────────────────────

@app.command("/idea_delete")
def handle_delete(ack, respond, command):
    ack()
    text = command.get("text", "").strip()
    if not text:
        respond(":warning: Usage: `/idea_delete 3`")
        return
    try:
        idea_id = int(text.split()[0])
    except ValueError:
        respond(f":x: Invalid idea ID: `{text}`")
        return

    idea = delete_idea(idea_id)
    if not idea:
        respond(f":x: Idea #{idea_id} not found.")
        return

    _commit_data_to_github()
    respond(f":wastebasket: Idea #{idea_id} *deleted* — _{idea['text'][:60]}_")


# ── Emoji reaction capture (lightbulb) ────────────────────────────────────────

@app.event("reaction_added")
def handle_reaction(event, client):
    """Capture a message as an idea when someone reacts with :bulb:."""
    if event.get("reaction") != "bulb":
        return

    channel = event.get("item", {}).get("channel", "")
    ts = event.get("item", {}).get("ts", "")
    user = event.get("user", "")

    if not channel or not ts:
        return

    try:
        result = client.conversations_history(
            channel=channel, latest=ts, inclusive=True, limit=1,
        )
        messages = result.get("messages", [])
        if not messages:
            return
        text = messages[0].get("text", "").strip()
        if not text:
            return
    except Exception:
        return

    idea = add_idea(
        text=text,
        category="blog",
        source="slack_reaction",
        slack_user=user,
        slack_channel=channel,
    )
    _commit_data_to_github()

    try:
        client.chat_postMessage(
            channel=channel,
            text=(
                f":bulb: *Idea #{idea['id']} captured from reaction!*\n"
                f"  {idea['text'][:100]}{'…' if len(idea['text']) > 100 else ''}\n\n"
                f"_Evaluating — scorecard arriving shortly …_"
            ),
        )
    except Exception:
        pass

    _run_evaluation_async(idea["id"], idea["text"], channel, client)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise SystemExit(
            "SLACK_APP_TOKEN is not set. "
            "Generate an App-Level Token with connections:write in your Slack App settings."
        )

    print("Starting Idea Capture bot in Socket Mode …")
    handler = SocketModeHandler(app, app_token)
    handler.start()
