"""Plain data objects shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Channel:
    """A YouTube channel with the fields the pipeline needs."""

    id: str
    title: str
    uploads_playlist: str
    subscribers: int


@dataclass(frozen=True)
class Video:
    """A YouTube video with its current public statistics."""

    id: str
    channel_id: str
    channel_title: str
    title: str
    description: str
    tags: tuple[str, ...]
    published_at: datetime
    duration_sec: int
    views: int
    likes: int
    comments: int

    @property
    def url(self) -> str:
        """Public watch URL."""
        return f"https://www.youtube.com/watch?v={self.id}"


@dataclass(frozen=True)
class Snapshot:
    """A stored view count at a point in time."""

    views: int
    captured_at: datetime


@dataclass
class ScoredVideo:
    """A candidate video with its performance metrics."""

    video: Video
    outlier: float      # total views vs the channel's typical video
    views_24h: float    # views gained per 24h (true delta when history exists)
    has_history: bool   # True when views_24h came from a stored snapshot
    pace: float         # views_24h vs the channel's typical daily views
    score: float
    top_comments: list[str] = field(default_factory=list)


@dataclass
class Topic:
    """A trending topic: a group of videos plus ranking signals and LLM details."""

    title: str
    keywords: list[str]
    videos: list[ScoredVideo]
    performance: float = 0.0
    coverage: int = 0
    novelty: float = 1.0
    search_demand: float = 0.0
    score: float = 0.0
    is_new: bool = True
    details: dict = field(default_factory=dict)

    @property
    def channels(self) -> list[str]:
        """Distinct channel titles covering this topic, in video order."""
        return list(dict.fromkeys(sv.video.channel_title for sv in self.videos))
