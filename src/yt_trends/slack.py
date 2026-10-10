"""Slack output: Block Kit message, plain-text rendering for dry runs, webhook sender."""
from __future__ import annotations

import requests

from .models import Topic

SECTION_LIMIT = 2900  # Slack hard limit is 3000 chars per text field


class SlackError(RuntimeError):
    """Raised when the webhook rejects a message."""


def esc(text: object) -> str:
    """Escape Slack mrkdwn control characters."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bullets(label: str, items: list[str]) -> list[str]:
    """Bold label + bullet lines, or nothing for an empty list."""
    return [f"*{label}:*", *(f"• {esc(x)}" for x in items)] if items else []


def _badge(topic: Topic) -> str:
    """NEW / ONGOING marker."""
    return "🆕 NEW" if topic.is_new else "🔁 ONGOING"


def build_message(niche: str, run_date: str, topics: list[Topic], stats: dict) -> dict:
    """Webhook payload with one section per topic."""
    blocks: list[dict] = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"🔥 {niche}: top {len(topics)} topics ({run_date})"[:150]},
    }]
    for rank, topic in enumerate(topics, start=1):
        d = topic.details
        lines = [f"*{rank}. {esc(topic.title)}*  `{_badge(topic)}`  score {topic.score:.2f}"]
        if d.get("why_trending"):
            lines.append(f"_{esc(d['why_trending'])}_")
        lines += _bullets("Video angles", d.get("video_angles", []))
        lines += _bullets("Title ideas", d.get("title_ideas", []))
        lines += _bullets("Viewers are asking", d.get("audience_questions", []))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:SECTION_LIMIT]}})

        evidence = " · ".join(
            f"<{sv.video.url}|{esc(sv.video.title[:60])}> ({sv.outlier:.1f}x)" for sv in topic.videos[:3]
        )
        meta = (f"Channels ({topic.coverage}): {esc(', '.join(topic.channels))}\n"
                f"Keywords: {esc(', '.join(topic.keywords))}\nEvidence: {evidence}")
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": meta[:SECTION_LIMIT]}]})
        blocks.append({"type": "divider"})

    footer = (f"{stats.get('candidates', 0)} candidate videos from {stats.get('channels', 0)} channels · "
              f"YouTube quota {stats.get('quota_used', 0)} units · topics by {stats.get('clustered_by', '?')}")
    if not stats.get("history"):
        footer += " · first run: growth uses lifetime averages until tomorrow's snapshot"
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": esc(footer)}]})
    return {"text": f"{niche}: top {len(topics)} YouTube topics for {run_date}", "blocks": blocks}


def render_text(niche: str, run_date: str, topics: list[Topic], stats: dict) -> str:
    """Human-readable version of the message (used by --dry-run)."""
    out = [f"== {niche}: top {len(topics)} topics ({run_date}) =="]
    for rank, topic in enumerate(topics, start=1):
        d = topic.details
        out.append(f"\n{rank}. {topic.title}  [{_badge(topic)}]  score={topic.score:.2f} "
                   f"(perf={topic.performance:.2f} channels={topic.coverage} "
                   f"novelty={topic.novelty:.2f} demand={topic.search_demand:.2f})")
        if d.get("why_trending"):
            out.append(f"   why: {d['why_trending']}")
        for key in ("video_angles", "title_ideas", "audience_questions"):
            for item in d.get(key, []):
                out.append(f"   {key}: {item}")
        for sv in topic.videos[:3]:
            out.append(f"   - {sv.video.title} | {sv.video.channel_title} | {sv.outlier:.1f}x | {sv.video.url}")
    out.append(f"\nstats: {stats}")
    return "\n".join(out)


def post(webhook_url: str, payload: dict, session: requests.Session) -> None:
    """Send a payload to an incoming webhook."""
    try:
        resp = session.post(webhook_url, json=payload, timeout=30)
    except requests.RequestException as exc:
        raise SlackError(f"network error ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise SlackError(f"HTTP {resp.status_code}: {resp.text[:200]}")
