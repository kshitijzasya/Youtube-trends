"""Minimal YouTube Data API v3 client (REST + API key) with quota accounting."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Iterable, Iterator

import requests

from .models import Channel, Video

log = logging.getLogger(__name__)

BASE_URL = "https://www.googleapis.com/youtube/v3/"
SEARCH_COST = 100  # search.list; every other call used here costs 1 unit
_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
_CHANNEL_ID = re.compile(r"(UC[\w-]{22})")


class YouTubeError(RuntimeError):
    """Raised for API or network failures (never includes the API key)."""


class QuotaExceeded(YouTubeError):
    """Raised before a call that would exceed the per-run quota budget."""


def parse_duration(iso: str | None) -> int:
    """Convert an ISO 8601 duration (e.g. PT1H2M3S) to seconds; 0 if unparseable."""
    match = _DURATION.match(iso or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_timestamp(value: str) -> datetime:
    """Parse an RFC 3339 timestamp from the API into an aware UTC datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def normalize_channel_ref(ref: str) -> tuple[str | None, str | None]:
    """Split a config channel entry into (channel_id, handle); exactly one is set."""
    value = ref.strip()
    match = _CHANNEL_ID.search(value)
    if match:
        return match.group(1), None
    value = re.sub(r"^https?://(www\.|m\.)?youtube\.com/", "", value)
    handle = re.split(r"[/?#]", value.lstrip("@"))[0]
    return None, f"@{handle}"


def _chunks(items: list[str], size: int = 50) -> Iterator[list[str]]:
    """Yield fixed-size slices (the API accepts at most 50 ids per call)."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


class YouTubeClient:
    """Thin wrapper over the endpoints the pipeline uses."""

    def __init__(self, api_key: str, quota_budget: int, session: requests.Session | None = None,
                 timeout: int = 30) -> None:
        self._key = api_key
        self._budget = quota_budget
        self._session = session or requests.Session()
        self._timeout = timeout
        self.units_used = 0

    def _get(self, resource: str, params: dict) -> dict:
        """GET a resource, charging quota first and hiding the key from errors."""
        cost = SEARCH_COST if resource == "search" else 1
        if self.units_used + cost > self._budget:
            raise QuotaExceeded(f"quota budget {self._budget} reached ({self.units_used} used)")
        query = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._session.get(BASE_URL + resource, params={**query, "key": self._key},
                                     timeout=self._timeout)
        except requests.RequestException as exc:
            # The exception text can contain the full URL (with key), so drop it.
            raise YouTubeError(f"{resource}: network error ({type(exc).__name__})") from None
        self.units_used += cost
        if resp.status_code != 200:
            try:
                error = resp.json().get("error", {})
                reason = (error.get("errors") or [{}])[0].get("reason", "")
                message = f"{error.get('message', '')} [{reason}]"
            except ValueError:
                message = resp.text[:200]
            raise YouTubeError(f"{resource} HTTP {resp.status_code}: {message}")
        return resp.json()

    def channel_id_for_handle(self, handle: str) -> str | None:
        """Resolve a @handle to a channel id (1 unit)."""
        items = self._get("channels", {"part": "id", "forHandle": handle}).get("items") or []
        return items[0]["id"] if items else None

    def channels(self, ids: Iterable[str]) -> list[Channel]:
        """Fetch channel metadata (1 unit per 50 channels)."""
        result = []
        for batch in _chunks(list(dict.fromkeys(ids))):
            data = self._get("channels", {"part": "snippet,contentDetails,statistics",
                                          "id": ",".join(batch), "maxResults": 50})
            for item in data.get("items") or []:
                result.append(Channel(
                    id=item["id"],
                    title=item["snippet"]["title"],
                    uploads_playlist=item["contentDetails"]["relatedPlaylists"]["uploads"],
                    subscribers=int(item.get("statistics", {}).get("subscriberCount") or 0),
                ))
        return result

    def recent_upload_ids(self, uploads_playlist: str, max_results: int = 50) -> list[str]:
        """Latest video ids from a channel's uploads playlist (1 unit)."""
        try:
            data = self._get("playlistItems", {"part": "contentDetails", "playlistId": uploads_playlist,
                                               "maxResults": max_results})
        except YouTubeError as exc:
            if "HTTP 404" in str(exc):  # channel with no public uploads
                return []
            raise
        return [i["contentDetails"]["videoId"] for i in data.get("items") or []]

    def videos(self, ids: Iterable[str]) -> list[Video]:
        """Fetch full video details and stats (1 unit per 50 videos)."""
        result = []
        for batch in _chunks(list(dict.fromkeys(ids))):
            data = self._get("videos", {"part": "snippet,statistics,contentDetails",
                                        "id": ",".join(batch), "maxResults": 50})
            for item in data.get("items") or []:
                snippet, stats = item["snippet"], item.get("statistics", {})
                result.append(Video(
                    id=item["id"],
                    channel_id=snippet["channelId"],
                    channel_title=snippet.get("channelTitle", ""),
                    title=snippet.get("title", ""),
                    description=(snippet.get("description") or "")[:500],
                    tags=tuple(snippet.get("tags") or ()),
                    published_at=parse_timestamp(snippet["publishedAt"]),
                    duration_sec=parse_duration(item.get("contentDetails", {}).get("duration")),
                    views=int(stats.get("viewCount") or 0),
                    likes=int(stats.get("likeCount") or 0),
                    comments=int(stats.get("commentCount") or 0),
                ))
        return result

    def search_recent(self, query: str, published_after: datetime, max_results: int,
                      region_code: str | None = None, language: str | None = None) -> list[str]:
        """Most-viewed video ids for a keyword published after a time (100 units)."""
        data = self._get("search", {
            "part": "id",
            "q": query,
            "type": "video",
            "order": "viewCount",
            "publishedAfter": published_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": max_results,
            "regionCode": region_code,
            "relevanceLanguage": language,
        })
        return [i["id"]["videoId"] for i in data.get("items") or [] if i.get("id", {}).get("videoId")]

    def top_comments(self, video_id: str, max_results: int) -> list[str]:
        """Most relevant top-level comments (1 unit); [] when comments are disabled."""
        try:
            data = self._get("commentThreads", {"part": "snippet", "videoId": video_id,
                                                "maxResults": max_results, "order": "relevance",
                                                "textFormat": "plainText"})
        except QuotaExceeded:
            raise
        except YouTubeError as exc:
            log.debug("comments unavailable for %s: %s", video_id, exc)
            return []
        return [i["snippet"]["topLevelComment"]["snippet"]["textDisplay"][:300]
                for i in data.get("items") or []]
