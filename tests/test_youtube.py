"""YouTube client: parsing helpers, quota accounting, error hygiene."""
from datetime import datetime, timezone

import pytest
import requests

from yt_trends.youtube import (QuotaExceeded, YouTubeClient, YouTubeError, normalize_channel_ref,
                               parse_duration)

from conftest import FakeResponse, FakeSession


@pytest.mark.parametrize("iso,seconds", [
    ("PT1H2M3S", 3723), ("PT45S", 45), ("PT10M", 600), ("P1DT1S", 86401), ("P0D", 0), ("", 0), (None, 0),
    ("garbage", 0),
])
def test_parse_duration(iso, seconds):
    assert parse_duration(iso) == seconds


@pytest.mark.parametrize("ref,expected", [
    ("UC1234567890123456789012", ("UC1234567890123456789012", None)),
    ("https://www.youtube.com/channel/UC1234567890123456789012", ("UC1234567890123456789012", None)),
    ("@MrBeast", (None, "@MrBeast")),
    ("MrBeast", (None, "@MrBeast")),
    ("https://www.youtube.com/@MrBeast/videos", (None, "@MrBeast")),
    ("https://m.youtube.com/@MrBeast?si=x", (None, "@MrBeast")),
])
def test_normalize_channel_ref(ref, expected):
    assert normalize_channel_ref(ref) == expected


def test_search_costs_100_units_and_budget_is_enforced():
    session = FakeSession(FakeResponse(payload={"items": [{"id": {"videoId": "v1"}}]}))
    client = YouTubeClient("KEY", quota_budget=150, session=session)

    assert client.search_recent("q", datetime(2026, 1, 1, tzinfo=timezone.utc), 5) == ["v1"]
    assert client.units_used == 100
    with pytest.raises(QuotaExceeded):
        client.search_recent("q", datetime(2026, 1, 1, tzinfo=timezone.utc), 5)
    assert len(session.requests) == 1  # second call never sent


def test_network_error_does_not_leak_api_key():
    session = FakeSession(requests.ConnectionError("https://www.googleapis.com/youtube/v3/videos?key=SECRET"))
    client = YouTubeClient("SECRET", quota_budget=10, session=session)

    with pytest.raises(YouTubeError) as exc:
        client.videos(["a"])
    assert "SECRET" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_http_error_surfaces_api_reason():
    payload = {"error": {"message": "API key not valid", "errors": [{"reason": "badRequest"}]}}
    client = YouTubeClient("K", 10, FakeSession(FakeResponse(400, payload)))

    with pytest.raises(YouTubeError, match=r"HTTP 400: API key not valid \[badRequest\]"):
        client.channels(["UCx"])


def test_videos_parses_fields():
    item = {
        "id": "v1",
        "snippet": {"channelId": "UCa", "channelTitle": "A", "title": "T", "description": "D",
                    "tags": ["x"], "publishedAt": "2026-10-09T03:30:00Z"},
        "statistics": {"viewCount": "1500", "likeCount": "10"},  # commentCount hidden
        "contentDetails": {"duration": "PT5M"},
    }
    client = YouTubeClient("K", 10, FakeSession(FakeResponse(payload={"items": [item]})))

    [video] = client.videos(["v1", "v1"])
    assert (video.views, video.likes, video.comments, video.duration_sec) == (1500, 10, 0, 300)
    assert video.published_at == datetime(2026, 10, 9, 3, 30, tzinfo=timezone.utc)
    assert video.url == "https://www.youtube.com/watch?v=v1"


def test_comments_disabled_returns_empty_list():
    payload = {"error": {"message": "disabled", "errors": [{"reason": "commentsDisabled"}]}}
    client = YouTubeClient("K", 10, FakeSession(FakeResponse(403, payload)))

    assert client.top_comments("v1", 5) == []
