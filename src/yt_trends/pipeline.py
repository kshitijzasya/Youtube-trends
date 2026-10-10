"""One niche, end to end: collect -> snapshot -> score -> cluster -> rank -> enrich -> save."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Protocol

from .config import AppConfig, NicheConfig
from .llm import JsonLlm, LlmError
from .models import Channel, Topic, Video
from .scoring import channel_baselines, rank_topics, score_videos, words
from .store import HistoryStore
from .topics import cluster_by_keywords, cluster_with_llm, enrich_with_llm, load_prompt
from .youtube import QuotaExceeded, normalize_channel_ref

log = logging.getLogger(__name__)

SNAPSHOT_MIN_AGE = timedelta(hours=12)  # growth is measured against a snapshot at least this old
COMMENTED_VIDEOS_PER_TOPIC = 3


class PipelineError(RuntimeError):
    """Raised when a niche cannot produce a report."""


class YouTubeApi(Protocol):
    """The subset of YouTubeClient the pipeline uses (lets tests pass a fake)."""

    units_used: int

    def channel_id_for_handle(self, handle: str) -> str | None: ...
    def channels(self, ids: Iterable[str]) -> list[Channel]: ...
    def recent_upload_ids(self, uploads_playlist: str, max_results: int = 50) -> list[str]: ...
    def videos(self, ids: Iterable[str]) -> list[Video]: ...
    def search_recent(self, query: str, published_after: datetime, max_results: int,
                      region_code: str | None = None, language: str | None = None) -> list[str]: ...
    def top_comments(self, video_id: str, max_results: int) -> list[str]: ...


@dataclass
class Deps:
    """External collaborators for a run."""

    youtube: YouTubeApi
    store: HistoryStore
    llm: JsonLlm | None
    suggest: Callable[[list[str], str | None], list[str]]
    prompts_dir: Path
    now: datetime


@dataclass
class NicheResult:
    """Ranked topics plus run statistics."""

    run_date: str
    topics: list[Topic]
    stats: dict = field(default_factory=dict)


def resolve_channel_ids(refs: Iterable[str], youtube: YouTubeApi, store: HistoryStore) -> list[str]:
    """Config entries -> channel ids, caching handle lookups in the store."""
    ids = []
    for ref in refs:
        channel_id, handle = normalize_channel_ref(ref)
        if handle:
            channel_id = store.channel_id_for_handle(handle)
            if not channel_id:
                channel_id = youtube.channel_id_for_handle(handle)
                if not channel_id:
                    raise PipelineError(f"no channel found for {handle!r}; copy the handle from the channel page")
                store.save_handle(handle, channel_id)
        ids.append(channel_id)
    return list(dict.fromkeys(ids))


def fetch_uploads(youtube: YouTubeApi, channels: Iterable[Channel]) -> dict[str, list[Video]]:
    """Latest uploads (with stats) for each channel."""
    channels = list(channels)
    upload_ids = [vid for ch in channels for vid in youtube.recent_upload_ids(ch.uploads_playlist)]
    by_channel: dict[str, list[Video]] = {ch.id: [] for ch in channels}
    for video in youtube.videos(upload_ids):
        by_channel.setdefault(video.channel_id, []).append(video)
    return by_channel


def _search(niche: NicheConfig, youtube: YouTubeApi, since: datetime, known: set[str]) -> list[Video]:
    """Keyword search for breakout videos outside the tracked channels."""
    found: list[str] = []
    for keyword in niche.search_keywords:
        try:
            found += youtube.search_recent(keyword, since, niche.search_results_per_keyword,
                                           niche.region_code, niche.language)
        except QuotaExceeded as exc:
            log.warning("search stopped early: %s", exc)
            break
    new_ids = [vid for vid in dict.fromkeys(found) if vid not in known]
    return youtube.videos(new_ids) if new_ids else []


def _single_video_topics(videos, limit: int) -> list[Topic]:
    """Last-resort topics: each top video stands alone."""
    return [Topic(title=sv.video.title, keywords=words(sv.video.title)[:5], videos=[sv]) for sv in videos[:limit]]


def run_niche(niche: NicheConfig, config: AppConfig, deps: Deps) -> NicheResult:
    """Produce today's ranked, enriched topics for one niche and record history."""
    yt, store, now = deps.youtube, deps.store, deps.now
    run_date = now.strftime("%Y-%m-%d")
    since = now - timedelta(hours=niche.lookback_hours)
    units_before = yt.units_used

    # 1. Collect: tracked channels' uploads, keyword search, uploads of discovered channels (baselines).
    tracked = yt.channels(resolve_channel_ids(niche.channels, yt, store)) if niche.channels else []
    uploads = fetch_uploads(yt, tracked)
    known_ids = {v.id for vs in uploads.values() for v in vs}
    searched = _search(niche, yt, since, known_ids)

    discovered = sorted({v.channel_id for v in searched} - set(uploads),
                        key=lambda cid: max(v.views for v in searched if v.channel_id == cid), reverse=True)
    try:
        uploads.update(fetch_uploads(yt, yt.channels(discovered[:niche.max_discovered_channels])))
    except QuotaExceeded as exc:
        log.warning("skipping discovered-channel baselines: %s", exc)

    # 2. Snapshot: read yesterday's numbers before writing today's.
    candidates: dict[str, Video] = {}
    for video in [v for vs in uploads.values() for v in vs] + searched:
        if video.published_at >= since:
            candidates.setdefault(video.id, video)
    previous = store.previous_snapshots(candidates, before=now - SNAPSHOT_MIN_AGE)
    store.save_snapshots(candidates.values(), now)

    # 3. Score videos against their own channel's normal.
    scored = score_videos(candidates.values(), channel_baselines(uploads, now), previous, now,
                          niche.min_duration_sec)
    if not scored:
        raise PipelineError(f"no long-form videos published in the last {niche.lookback_hours}h; "
                            "check channels/search_keywords or raise lookback_hours")
    pool = scored[:niche.candidate_videos]

    # 4. Cluster into candidate topics (twice as many as needed, so ranking has a choice).
    suggestions = deps.suggest(list(niche.search_keywords), niche.language)
    topics, clustered_by = [], "keywords"
    if deps.llm:
        try:
            topics = cluster_with_llm(deps.llm, pool, niche.name, niche.topics * 2,
                                      load_prompt(deps.prompts_dir, "cluster"))
            clustered_by = deps.llm.name
        except LlmError as exc:
            log.warning("LLM clustering failed, using keywords: %s", exc)
    if not topics:
        topics = cluster_by_keywords(pool, niche.topics * 2)
    if not topics:
        topics, clustered_by = _single_video_topics(pool, niche.topics), "top-videos"

    # 5. Rank with history (novelty), coverage and search demand.
    recent = store.recent_topics(niche.name, now - timedelta(days=config.novelty_days), run_date)
    ranked = rank_topics(topics, niche.weights, recent, suggestions)[:niche.topics]

    # 6. Enrich: comments on evidence videos, then LLM details.
    try:
        for topic in ranked:
            for sv in topic.videos[:COMMENTED_VIDEOS_PER_TOPIC]:
                sv.top_comments = yt.top_comments(sv.video.id, niche.comments_per_video)
    except QuotaExceeded as exc:
        log.warning("comments stopped early: %s", exc)
    if deps.llm:
        try:
            enrich_with_llm(deps.llm, ranked, niche.name, suggestions, load_prompt(deps.prompts_dir, "details"))
        except LlmError as exc:
            log.warning("LLM enrichment failed, posting without details: %s", exc)

    store.save_report(run_date, niche.name, ranked)
    store.commit()
    stats = {
        "candidates": len(scored),
        "channels": len(uploads),
        "searched_videos": len(searched),
        "suggestions": len(suggestions),
        "quota_used": yt.units_used - units_before,
        "clustered_by": clustered_by,
        "history": any(sv.has_history for sv in scored),
    }
    return NicheResult(run_date=run_date, topics=ranked, stats=stats)
