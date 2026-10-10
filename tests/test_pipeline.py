"""End-to-end pipeline runs against fakes (no network)."""
from datetime import timedelta
from pathlib import Path

import pytest

from yt_trends.config import AppConfig, LlmConfig, NicheConfig
from yt_trends.llm import LlmError
from yt_trends.pipeline import Deps, PipelineError, run_niche

from conftest import FakeLlm, FakeYouTube, channel, make_video

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def build_world(now):
    """Two tracked channels, one discovered via search."""
    uploads = {
        "UCa": [make_video("a1", "UCa", 9000, 20, "Budget app review", now=now),
                make_video("a2", "UCa", 1000, 24 * 10, "Old upload", now=now),
                make_video("a3", "UCa", 1200, 24 * 12, "Old upload 2", now=now),
                make_video("a4", "UCa", 800, 24 * 14, "Old upload 3", now=now)],
        "UCb": [make_video("b1", "UCb", 4000, 30, "Best budget app 2026", now=now),
                make_video("b2", "UCb", 50, 10, "Shorts clip", duration=30, now=now)],
        "UCc": [make_video("c1", "UCc", 20000, 40, "Crypto crash explained", now=now)],
    }
    search = {"crypto": [uploads["UCc"][0]]}
    yt = FakeYouTube({c: channel(c) for c in uploads}, uploads, search,
                     handles={"@alpha": "UCa", "@beta": "UCb"})
    return yt


def niche(**overrides):
    base = dict(name="money", channels=("@alpha", "@beta"), search_keywords=("crypto",),
                topics=2, min_duration_sec=61)
    base.update(overrides)
    return NicheConfig(**base)


def config():
    return AppConfig(niches=(), llm=LlmConfig(), db_path=Path(":memory:"))


def deps(yt, store, now, llm=None, suggestions=()):
    return Deps(youtube=yt, store=store, llm=llm, suggest=lambda q, lang: list(suggestions),
                prompts_dir=PROMPTS, now=now)


CLUSTER = {"topics": [
    {"topic": "Budget apps", "keywords": ["budget app"], "video_ids": ["a1", "b1"]},
    {"topic": "Crypto crash", "keywords": ["crypto"], "video_ids": ["c1"]},
]}
DETAILS = {"topics": [{"index": 0, "why_trending": "W0", "video_angles": ["x"]},
                      {"index": 1, "why_trending": "W1"}]}


def test_full_run_with_llm(store, now):
    yt = build_world(now)
    llm = FakeLlm(CLUSTER, DETAILS)

    result = run_niche(niche(), config(), deps(yt, store, now, llm, ["best budget app"]))

    titles = [t.title for t in result.topics]
    assert titles[0] == "Budget apps"          # 2 channels + search demand
    assert set(titles) == {"Budget apps", "Crypto crash"}
    assert result.topics[0].details["why_trending"] in {"W0", "W1"}
    assert result.stats["clustered_by"] == "fake"
    assert result.stats["history"] is False
    assert result.stats["searched_videos"] == 1
    assert "b2" not in {v.video.id for t in result.topics for v in t.videos}  # Short filtered
    assert all(sv.top_comments for t in result.topics for sv in t.videos)
    assert store.channel_id_for_handle("@alpha") == "UCa"


def test_second_day_uses_snapshots_cache_and_novelty(store, now):
    yt = build_world(now)
    run_niche(niche(), config(), deps(yt, store, now, FakeLlm(CLUSTER, DETAILS)))

    tomorrow = now + timedelta(days=1)
    yt.replace_views("b1", 40000)  # b1 explodes overnight
    yt.calls.clear()
    result = run_niche(niche(), config(), deps(yt, store, tomorrow, FakeLlm(CLUSTER, DETAILS)))

    assert result.stats["history"] is True
    assert not any(c.startswith("handle:") for c in yt.calls)   # handle served from cache
    assert all(not t.is_new for t in result.topics)              # both reported yesterday
    b1 = next(sv for t in result.topics for sv in t.videos if sv.video.id == "b1")
    assert b1.has_history and b1.views_24h == pytest.approx(36000)


def test_llm_failure_falls_back_to_keywords(store, now):
    yt = build_world(now)
    llm = FakeLlm(LlmError("down"), LlmError("down"))

    result = run_niche(niche(), config(), deps(yt, store, now, llm))

    assert result.stats["clustered_by"] in {"keywords", "top-videos"}
    assert result.topics and all(t.details == {} for t in result.topics)


def test_unknown_handle_fails_clearly(store, now):
    yt = build_world(now)
    with pytest.raises(PipelineError, match="@nobody"):
        run_niche(niche(channels=("@nobody",)), config(), deps(yt, store, now))


def test_no_recent_videos_fails_clearly(store, now):
    yt = build_world(now)
    with pytest.raises(PipelineError, match="no long-form videos"):
        run_niche(niche(lookback_hours=1, search_keywords=()), config(), deps(yt, store, now))
