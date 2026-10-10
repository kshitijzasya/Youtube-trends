"""Shared fixtures: video factory, fake YouTube API, fake LLM, fake HTTP session."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from yt_trends.models import Channel, Video
from yt_trends.store import SqliteStore

NOW = datetime(2026, 10, 10, 3, 30, tzinfo=timezone.utc)


def make_video(vid: str, channel: str = "UCa", views: int = 1000, age_hours: float = 24,
               title: str = "Some video", tags=(), duration: int = 600, now: datetime = NOW) -> Video:
    """Build a Video with sensible defaults."""
    return Video(id=vid, channel_id=channel, channel_title=f"Channel {channel}", title=title,
                 description="desc", tags=tuple(tags), published_at=now - timedelta(hours=age_hours),
                 duration_sec=duration, views=views, likes=views // 20, comments=views // 100)


class FakeYouTube:
    """In-memory stand-in for YouTubeClient."""

    def __init__(self, channels: dict[str, Channel], uploads: dict[str, list[Video]],
                 search: dict[str, list[Video]] | None = None, handles: dict[str, str] | None = None):
        self._channels = channels
        self._uploads = uploads            # channel id -> videos
        self._search = search or {}        # keyword -> videos
        self._handles = handles or {}
        self._videos = {v.id: v for vs in [*uploads.values(), *self._search.values()] for v in vs}
        self.units_used = 0
        self.calls: list[str] = []

    def channel_id_for_handle(self, handle):
        self.units_used += 1
        self.calls.append(f"handle:{handle}")
        return self._handles.get(handle)

    def channels(self, ids):
        self.units_used += 1
        return [self._channels[i] for i in ids if i in self._channels]

    def recent_upload_ids(self, uploads_playlist, max_results=50):
        self.units_used += 1
        channel_id = uploads_playlist.replace("UU", "UC", 1)
        return [v.id for v in self._uploads.get(channel_id, [])][:max_results]

    def videos(self, ids):
        self.units_used += 1
        return [self._videos[i] for i in ids if i in self._videos]

    def search_recent(self, query, published_after, max_results, region_code=None, language=None):
        self.units_used += 100
        return [v.id for v in self._search.get(query, []) if v.published_at >= published_after][:max_results]

    def top_comments(self, video_id, max_results):
        self.units_used += 1
        return [f"How do I start with {video_id}?"]

    def replace_views(self, vid: str, views: int):
        """Simulate growth between runs."""
        old = self._videos[vid]
        new = Video(**{**old.__dict__, "views": views})
        self._videos[vid] = new
        for vs in self._uploads.values():
            for i, v in enumerate(vs):
                if v.id == vid:
                    vs[i] = new


class FakeLlm:
    """Returns queued responses; records prompts."""

    name = "fake"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete_json(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, status: int = 200, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    """Records requests and returns queued responses (or raises queued exceptions)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict]] = []
        self.headers = {}

    def _next(self, method, url, kwargs):
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


def channel(cid: str) -> Channel:
    """Channel whose uploads playlist follows YouTube's UC->UU convention."""
    return Channel(id=cid, title=f"Channel {cid}", uploads_playlist="UU" + cid[2:], subscribers=10000)


@pytest.fixture
def now():
    return NOW


@pytest.fixture(params=["sqlite", "postgres"])
def store(request):
    """Each storage test runs on SQLite and, when TEST_DATABASE_URL is set, on a throwaway Postgres schema."""
    if request.param == "sqlite":
        backend = SqliteStore(":memory:")
        yield backend
        backend.close()
        return

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set (run tests via docker compose to include Postgres)")
    import psycopg
    from yt_trends.postgres_store import PostgresStore

    schema = f"test_{uuid.uuid4().hex[:12]}"
    backend = PostgresStore(url, schema=schema)
    yield backend
    backend.close()
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
