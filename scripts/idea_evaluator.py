"""
idea_evaluator.py

Two-pass AI evaluation pipeline for ideas:
  Pass 1 (Perplexity): Market research -- competitors, trends, gaps
  Pass 2 (Claude): Structured scorecard -- scores, verdict, recommendation

Falls back to Claude-only if Perplexity is unavailable.
"""

import json
import os
from typing import Optional

import anthropic
from openai import OpenAI

PERPLEXITY_MODEL = os.getenv("PERPLEXITY_MODEL", "sonar-pro")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


def _get_perplexity_client() -> Optional[OpenAI]:
    key = os.getenv("PERPLEXITY_API_KEY")
    if not key:
        return None
    return OpenAI(api_key=key, base_url="https://api.perplexity.ai")


def _get_anthropic_client() -> Optional[anthropic.Anthropic]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


# ── Pass 1: Perplexity market research ────────────────────────────────────────

RESEARCH_PROMPT = """Research the following idea/concept for market viability.

IDEA: {idea_text}

Provide a structured research report covering:

1. **Existing Products/Projects**: List specific products, tools, or projects that do something similar. Include names, URLs, and brief descriptions.
2. **Key Competitors**: For each competitor, note their traction (GitHub stars, users, funding rounds, revenue if known).
3. **Market Size & Trends**: Is demand growing? What market segments care about this? Any recent industry reports or trends?
4. **Gaps & Opportunities**: What are existing solutions missing? Where could a new entrant differentiate?

Be specific with names, numbers, and URLs. Focus on the last 12-18 months of data."""


def _research_idea(idea_text: str) -> Optional[str]:
    """Run Perplexity research. Returns raw research text or None on failure."""
    client = _get_perplexity_client()
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model=PERPLEXITY_MODEL,
            messages=[
                {"role": "system", "content": "You are a market research analyst. Provide factual, data-driven research."},
                {"role": "user", "content": RESEARCH_PROMPT.format(idea_text=idea_text)},
            ],
            temperature=0.1,
        )
        return resp.choices[0].message.content
    except Exception:
        return None


# ── Pass 2: Claude analysis and scoring ───────────────────────────────────────

ANALYSIS_PROMPT_WITH_RESEARCH = """You are evaluating an idea for market viability, competitiveness, and content potential.

IDEA: {idea_text}

MARKET RESEARCH:
{research}

Based on the research above, produce a JSON evaluation scorecard. Score each dimension from 1-10:

- market_demand: Is there demonstrated need? Are people actively searching for or paying for this?
- competition_level: How crowded is the space? (10 = extremely crowded, very hard to stand out)
- uniqueness: How differentiated is this idea from existing solutions?
- feasibility: How realistic is it to build this or write about it, given typical engineering skills?
- content_potential: How well does this work as a blog post, tutorial, or content piece?

Calculate overall_score as the weighted average: (market_demand * 0.25) + ((10 - competition_level) * 0.2) + (uniqueness * 0.2) + (feasibility * 0.15) + (content_potential * 0.2)

Determine verdict based on overall_score:
- "strong_yes" if >= 8
- "yes" if >= 6
- "maybe" if >= 4
- "no" if < 4

Return ONLY valid JSON in this exact structure (no markdown fences):
{{
  "scores": {{
    "market_demand": <int>,
    "competition_level": <int>,
    "uniqueness": <int>,
    "feasibility": <int>,
    "content_potential": <int>
  }},
  "overall_score": <float>,
  "verdict": "<string>",
  "competitors": [
    {{ "name": "<string>", "url": "<string>", "notes": "<string>" }}
  ],
  "market_summary": "<1-2 sentences>",
  "recommendation": "<1-2 sentences on whether to pursue and how>",
  "risks": ["<risk 1>", "<risk 2>"]
}}"""

ANALYSIS_PROMPT_NO_RESEARCH = """You are evaluating an idea for market viability, competitiveness, and content potential.

IDEA: {idea_text}

You do NOT have market research data, so use your general knowledge. Be conservative with scores when unsure.

Score each dimension from 1-10:

- market_demand: Is there likely demonstrated need?
- competition_level: How crowded is the space likely to be? (10 = extremely crowded)
- uniqueness: How differentiated does this idea seem from common solutions?
- feasibility: How realistic is it to build this or write about it?
- content_potential: How well does this work as a blog post, tutorial, or content piece?

Calculate overall_score as the weighted average: (market_demand * 0.25) + ((10 - competition_level) * 0.2) + (uniqueness * 0.2) + (feasibility * 0.15) + (content_potential * 0.2)

Determine verdict based on overall_score:
- "strong_yes" if >= 8
- "yes" if >= 6
- "maybe" if >= 4
- "no" if < 4

Return ONLY valid JSON in this exact structure (no markdown fences):
{{
  "scores": {{
    "market_demand": <int>,
    "competition_level": <int>,
    "uniqueness": <int>,
    "feasibility": <int>,
    "content_potential": <int>
  }},
  "overall_score": <float>,
  "verdict": "<string>",
  "competitors": [
    {{ "name": "<string>", "url": "<string or empty>", "notes": "<string>" }}
  ],
  "market_summary": "<1-2 sentences>",
  "recommendation": "<1-2 sentences on whether to pursue and how>",
  "risks": ["<risk 1>", "<risk 2>"]
}}"""


def _analyze_idea(idea_text: str, research: Optional[str]) -> Optional[dict]:
    """Run Claude analysis. Returns parsed scorecard dict or None on failure."""
    client = _get_anthropic_client()
    if not client:
        return None

    if research:
        prompt = ANALYSIS_PROMPT_WITH_RESEARCH.format(idea_text=idea_text, research=research)
    else:
        prompt = ANALYSIS_PROMPT_NO_RESEARCH.format(idea_text=idea_text)

    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except (json.JSONDecodeError, IndexError, Exception):
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def evaluate_idea(idea_text: str) -> Optional[dict]:
    """
    Run the full evaluation pipeline on an idea.

    Returns a scorecard dict with scores, verdict, competitors, etc.
    Returns None only if Claude is also unavailable.
    """
    research = _research_idea(idea_text)
    scorecard = _analyze_idea(idea_text, research)
    if scorecard:
        scorecard["research_source"] = "perplexity" if research else "claude_only"
    return scorecard


def format_scorecard_slack(idea_text: str, scorecard: dict) -> str:
    """Format a scorecard as a Slack message with visual progress bars."""
    scores = scorecard.get("scores", {})

    def bar(score: int) -> str:
        filled = score
        empty = 10 - score
        return "\u2588" * filled + "\u2591" * empty

    lines = [
        f":bar_chart: *Evaluation: {idea_text}*",
        "",
        f"  :chart_with_upwards_trend: Market Demand    {bar(scores.get('market_demand', 0))}  {scores.get('market_demand', 0)}/10",
        f"  :crossed_swords: Competition      {bar(scores.get('competition_level', 0))}  {scores.get('competition_level', 0)}/10",
        f"  :gem: Uniqueness         {bar(scores.get('uniqueness', 0))}  {scores.get('uniqueness', 0)}/10",
        f"  :wrench: Feasibility       {bar(scores.get('feasibility', 0))}  {scores.get('feasibility', 0)}/10",
        f"  :memo: Content Potential  {bar(scores.get('content_potential', 0))}  {scores.get('content_potential', 0)}/10",
        "",
    ]

    overall = scorecard.get("overall_score", 0)
    verdict = scorecard.get("verdict", "maybe")
    verdict_icons = {
        "strong_yes": ":large_green_circle: *STRONG YES*",
        "yes": ":white_check_mark: *YES*",
        "maybe": ":large_yellow_circle: *MAYBE*",
        "no": ":red_circle: *NO*",
    }
    lines.append(f"  *Overall: {overall}/10* \u2014 {verdict_icons.get(verdict, verdict)}")
    lines.append("")

    competitors = scorecard.get("competitors", [])
    if competitors:
        comp_parts = [f"{c['name']}" + (f" ({c['notes']})" if c.get("notes") else "") for c in competitors[:4]]
        lines.append(f"  *Competitors:* {', '.join(comp_parts)}")

    if scorecard.get("market_summary"):
        lines.append(f"  *Market:* {scorecard['market_summary']}")
    if scorecard.get("recommendation"):
        lines.append(f"  *Recommendation:* {scorecard['recommendation']}")

    risks = scorecard.get("risks", [])
    if risks:
        lines.append(f"  *Risks:* {'; '.join(risks[:3])}")

    source = scorecard.get("research_source", "unknown")
    if source == "claude_only":
        lines.append("\n  _:warning: Perplexity unavailable \u2014 evaluation based on Claude's general knowledge only._")

    return "\n".join(lines)
