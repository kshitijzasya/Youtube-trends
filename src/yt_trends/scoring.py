"""Video and topic scoring. Pure functions, no I/O."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable, Mapping

from .config import Weights
from .models import ScoredVideo, Snapshot, Topic, Video

MIN_BASELINE_VIEWS = 100       # floor so tiny channels don't produce absurd ratios
MIN_SNAPSHOT_GAP_HOURS = 1.0   # ignore snapshots too close together to measure growth
NEW_TOPIC_SIMILARITY = 0.5     # below this overlap with past topics => "new"

STOPWORDS = frozenset((
    "a an the and or of to in on for with is are was were how why what when who i you my your "
    "this that it its vs from at by be we our us new best top ever just get got will can do does "
    "did not no yes all any more most very so than then into about over after before up down out "
    "video videos youtube watch full part episode ep official live"
).split())


def words(text: str) -> list[str]:
    """Lowercase content words in order (len > 2, no stopwords)."""
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS]


def tokens(text: str) -> set[str]:
    """Distinct content words."""
    return set(words(text))


def channel_baselines(uploads_by_channel: Mapping[str, Iterable[Video]], now: datetime) -> dict[str, float]:
    """Typical total views per channel: median of uploads aged 7-90 days.

    Falls back to the median of all fetched uploads when too few are in that window.
    """
    baselines = {}
    for channel_id, uploads in uploads_by_channel.items():
        uploads = list(uploads)
        settled = [v.views for v in uploads if timedelta(days=7) <= now - v.published_at <= timedelta(days=90)]
        pool = settled if len(settled) >= 3 else [v.views for v in uploads]
        if pool:
            baselines[channel_id] = float(median(pool))
    return baselines


def score_video(video: Video, baseline: float, previous: Snapshot | None, now: datetime) -> ScoredVideo:
    """Score one video against its channel's normal.

    - outlier: total views / typical video views ("3x their usual").
    - views_24h: real growth since the last snapshot (scaled to 24h) when history exists,
      otherwise the lifetime daily average (age clamped to >= 24h so new uploads aren't extrapolated).
    - pace: views_24h / (typical views / 7), i.e. today's gain vs a typical video's first-week daily rate.
    """
    baseline = max(baseline, MIN_BASELINE_VIEWS)
    age_hours = max((now - video.published_at).total_seconds() / 3600, 0.0)

    gap_hours = (now - previous.captured_at).total_seconds() / 3600 if previous else 0.0
    if previous and gap_hours >= MIN_SNAPSHOT_GAP_HOURS and video.views >= previous.views:
        views_24h = (video.views - previous.views) / gap_hours * 24
        has_history = True
    else:
        views_24h = video.views / max(age_hours, 24.0) * 24
        has_history = False

    outlier = video.views / baseline
    pace = views_24h / (baseline / 7)
    score = 0.5 * math.log1p(outlier) + 0.5 * math.log1p(pace)
    return ScoredVideo(video=video, outlier=outlier, views_24h=views_24h,
                       has_history=has_history, pace=pace, score=score)


def score_videos(candidates: Iterable[Video], baselines: Mapping[str, float],
                 previous: Mapping[str, Snapshot], now: datetime, min_duration_sec: int) -> list[ScoredVideo]:
    """Score candidates (skipping Shorts below min_duration_sec), best first."""
    scored = [
        score_video(v, baselines.get(v.channel_id, 0.0), previous.get(v.id), now)
        for v in candidates
        if v.duration_sec >= min_duration_sec
    ]
    return sorted(scored, key=lambda s: s.score, reverse=True)


def _jaccard(a: set[str], b: set[str]) -> float:
    """Set overlap in [0, 1]."""
    return len(a & b) / len(a | b) if a and b else 0.0


def _normalize(values: list[float]) -> list[float]:
    """Scale to [0, 1] by the max (all zeros stay zeros)."""
    top = max(values, default=0.0)
    return [v / top if top > 0 else 0.0 for v in values]


def rank_topics(topics: list[Topic], weights: Weights, recent_topics: Iterable[Iterable[str]],
                suggestions: Iterable[str]) -> list[Topic]:
    """Compute ranking signals for each topic and return them best first.

    - performance: mean score of the topic's best 3 videos
    - coverage: distinct channels
    - novelty: 1 - max token overlap with topics reported in the novelty window
    - search_demand: share of autocomplete suggestions that mention the topic
    """
    history = [tokens(" ".join(t)) for t in recent_topics]
    suggestion_tokens = [tokens(s) for s in suggestions]

    for topic in topics:
        best = sorted((sv.score for sv in topic.videos), reverse=True)[:3]
        topic.performance = sum(best) / len(best) if best else 0.0
        topic.coverage = len({sv.video.channel_id for sv in topic.videos})

        topic_tokens = tokens(" ".join([topic.title, *topic.keywords]))
        similarity = max((_jaccard(topic_tokens, past) for past in history), default=0.0)
        topic.novelty = 1.0 - similarity
        topic.is_new = similarity < NEW_TOPIC_SIMILARITY

        hits = sum(1 for st in suggestion_tokens if st & topic_tokens)
        topic.search_demand = hits / len(suggestion_tokens) if suggestion_tokens else 0.0

    perf = _normalize([t.performance for t in topics])
    cov = _normalize([float(t.coverage) for t in topics])
    demand = _normalize([t.search_demand for t in topics])
    for i, topic in enumerate(topics):
        topic.score = (weights.performance * perf[i] + weights.coverage * cov[i]
                       + weights.novelty * topic.novelty + weights.search_demand * demand[i])
    return sorted(topics, key=lambda t: t.score, reverse=True)
