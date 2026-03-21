# Slack Idea Capture

A Slack bot that captures ideas, evaluates them against the market using AI, and promotes them to blog posts or projects.

## What It Does

- **Capture** ideas and notes from Slack (`/idea`, `/note`)
- **Evaluate** every idea automatically using Perplexity (market research) + Claude (analysis) to produce a viability scorecard
- **Promote to blog** -- triggers the [blog_post_workflow](https://github.com/anubhawsinha/blog_post_workflow) GitHub Actions pipeline (`/idea_to_blog`)
- **Promote to project** -- adds to the [anuva.blog](https://anuva.blog) projects page or scaffolds a new GitHub repo (`/idea_to_project`)
- Same idea can become **both** a blog post and a project

## Setup

1. **Create a Slack App** at https://api.slack.com/apps
   - Enable Socket Mode and generate an App-Level Token (`xapp-...`) with `connections:write`
   - Add slash commands: `/idea`, `/note`, `/ideas`, `/idea_to_blog`, `/idea_to_project`, `/idea_evaluate`, `/idea_done`, `/idea_archive`, `/idea_delete`
   - OAuth scopes: `commands`, `chat:write`
   - Optional (emoji capture): `reactions:read`, `channels:history`, `groups:history`
   - Install the app to your workspace

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Fill in credentials
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Run**
   ```bash
   ./start_bot.sh
   # or: python scripts/slack_bot.py
   ```

## Slash Commands

| Command | Description |
|---------|-------------|
| `/idea <text> [--tag blog\|project\|thought]` | Capture an idea (auto-evaluates) |
| `/note <text>` | Shorthand for `/idea <text> --tag thought` |
| `/ideas [--tag <tag>] [--status <status>] [--all]` | List and filter ideas |
| `/idea_evaluate <id>` | Re-evaluate an idea against the market |
| `/idea_to_blog <id> [--live]` | Promote idea to a blog post |
| `/idea_to_project <id> [--to anuva\|github]` | Promote idea to a project |
| `/idea_done <id>` | Mark idea as done |
| `/idea_archive <id>` | Archive an idea |
| `/idea_delete <id>` | Delete an idea |

## Evaluation Scorecard

Every idea is scored on five dimensions (1-10):
- **Market Demand** -- Is there demonstrated need?
- **Competition Level** -- How crowded is the space?
- **Uniqueness** -- How differentiated is this idea?
- **Feasibility** -- How realistic to build/write?
- **Content Potential** -- How well does this work as content?

Verdict: **strong_yes** (8+), **yes** (6+), **maybe** (4+), **no** (<4)

## Architecture

```
slack_idea_capture/
├── scripts/
│   ├── slack_bot.py              # Slack Bolt app (Socket Mode)
│   ├── idea_store.py             # CRUD for ideas + projects JSON
│   ├── idea_evaluator.py         # Perplexity research + Claude analysis
│   └── sync_projects_to_anuva.py # Push projects to anuva.blog
├── data/
│   ├── ideas.json                # All captured ideas
│   └── projects.json             # Projects (synced to anuva.blog)
├── .env.example
├── requirements.txt
└── start_bot.sh
```

Connects to sibling projects via APIs:
- **blog_post_workflow** -- triggers `publish-content.yml` via GitHub Actions API
- **anuva.blog** -- writes to `content/projects.json` locally or via GitHub contents API
