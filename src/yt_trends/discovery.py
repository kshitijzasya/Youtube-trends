"""Free search-demand signal: YouTube search autocomplete suggestions."""
from __future__ import annotations

import logging
from typing import Iterable

import requests

log = logging.getLogger(__name__)

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"


def youtube_suggestions(query: str, session: requests.Session, language: str | None = None,
                        timeout: int = 10) -> list[str]:
    """Return YouTube autocomplete suggestions for a query.

    This endpoint is unofficial, so any failure returns [] instead of failing the run.
    """
    params = {"client": "firefox", "ds": "yt", "q": query}
    if language:
        params["hl"] = language
    try:
        resp = session.get(SUGGEST_URL, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return [s for s in data[1] if isinstance(s, str)] if len(data) > 1 else []
    except (requests.RequestException, ValueError, IndexError, TypeError) as exc:
        log.warning("autocomplete failed for %r: %s", query, type(exc).__name__)
        return []


def collect_suggestions(queries: Iterable[str], session: requests.Session,
                        language: str | None = None) -> list[str]:
    """Autocomplete suggestions for every query, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for query in queries:
        for suggestion in youtube_suggestions(query, session, language):
            seen.setdefault(suggestion.lower(), None)
    return list(seen)
