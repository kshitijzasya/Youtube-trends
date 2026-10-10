"""Turning scored videos into topics: LLM clustering, keyword fallback, LLM enrichment."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from string import Template

from .llm import JsonLlm, LlmError
from .models import ScoredVideo, Topic
from .scoring import words

log = logging.getLogger(__name__)


def load_prompt(prompts_dir: Path, name: str) -> Template:
    """Read a $placeholder prompt template from the prompts directory."""
    return Template((prompts_dir / f"{name}.md").read_text(encoding="utf-8"))


def cluster_with_llm(llm: JsonLlm, videos: list[ScoredVideo], niche: str, max_topics: int,
                     template: Template) -> list[Topic]:
    """Ask the LLM to group videos into topics; unknown or duplicate video ids are dropped."""
    by_id = {sv.video.id: sv for sv in videos}
    payload = [{
        "id": sv.video.id,
        "title": sv.video.title,
        "channel": sv.video.channel_title,
        "score": round(sv.score, 2),
        "views": sv.video.views,
        "tags": list(sv.video.tags[:8]),
        "description": sv.video.description[:200],
    } for sv in videos]
    prompt = template.substitute(niche=niche, max_topics=max_topics,
                                 videos=json.dumps(payload, ensure_ascii=False))
    data = llm.complete_json(prompt)

    used: set[str] = set()
    topics = []
    for raw in data.get("topics") or []:
        if not isinstance(raw, dict) or not str(raw.get("topic", "")).strip():
            continue
        members = []
        for vid in raw.get("video_ids") or []:
            if vid in by_id and vid not in used:
                used.add(vid)
                members.append(by_id[vid])
        if members:
            keywords = [str(k) for k in raw.get("keywords") or [] if str(k).strip()][:6]
            topics.append(Topic(title=str(raw["topic"]).strip(), keywords=keywords, videos=members))
    if not topics:
        raise LlmError("LLM clustering returned no usable topics")
    return topics[:max_topics]


def cluster_by_keywords(videos: list[ScoredVideo], max_topics: int) -> list[Topic]:
    """No-AI fallback: rank words/phrases from titles+tags by summed video score.

    A term needs at least two videos; terms whose words overlap a picked term entirely are skipped.
    """
    weight: dict[str, float] = defaultdict(float)
    members: dict[str, list[ScoredVideo]] = defaultdict(list)
    for sv in videos:
        title_words = words(sv.video.title)
        tag_words = words(" ".join(sv.video.tags))
        terms = set(title_words) | set(tag_words)
        terms |= {f"{a} {b}" for a, b in zip(title_words, title_words[1:]) if a != b}
        for term in terms:
            weight[term] += sv.score
            members[term].append(sv)

    # Ties go to longer (more specific) phrases, then alphabetical, so output is deterministic.
    ranked = sorted((t for t in weight if len(members[t]) >= 2),
                    key=lambda t: (-weight[t], -len(t.split()), t))
    picked: list[str] = []
    for term in ranked:
        if len(picked) >= max_topics:
            break
        term_words = set(term.split())
        if not any(term_words <= set(p.split()) or set(p.split()) <= term_words for p in picked):
            picked.append(term)
    return [Topic(title=t, keywords=[t], videos=members[t]) for t in picked]


def enrich_with_llm(llm: JsonLlm, topics: list[Topic], niche: str, suggestions: list[str],
                    template: Template) -> None:
    """Fill topic.details (why_trending, questions, angles, titles) in place."""
    payload = [{
        "index": i,
        "topic": t.title,
        "status": "NEW" if t.is_new else "ONGOING",
        "channels_covering": t.channels,
        "videos": [{
            "title": sv.video.title,
            "channel": sv.video.channel_title,
            "views": sv.video.views,
            "views_last_24h": int(sv.views_24h),
            "times_channel_normal": round(sv.outlier, 1),
            "top_comments": sv.top_comments[:5],
        } for sv in t.videos[:4]],
    } for i, t in enumerate(topics)]
    prompt = template.substitute(niche=niche,
                                 suggestions=json.dumps(suggestions[:40], ensure_ascii=False),
                                 topics=json.dumps(payload, ensure_ascii=False))
    data = llm.complete_json(prompt)
    for raw in data.get("topics") or []:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        if isinstance(index, int) and 0 <= index < len(topics):
            topics[index].details = {
                "why_trending": str(raw.get("why_trending", "")).strip(),
                "audience_questions": [str(x) for x in raw.get("audience_questions") or []][:3],
                "video_angles": [str(x) for x in raw.get("video_angles") or []][:3],
                "title_ideas": [str(x) for x in raw.get("title_ideas") or []][:3],
            }
