"""Video and topic scoring."""
from datetime import timedelta

import pytest

from yt_trends.config import Weights
from yt_trends.models import Snapshot, Topic
from yt_trends.scoring import channel_baselines, rank_topics, score_video, score_videos, tokens

from conftest import make_video


def test_baseline_uses_settled_uploads(now):
    uploads = {"UCa": [
        make_video("new", views=99999, age_hours=24),        # too new, excluded
        make_video("a", views=1000, age_hours=24 * 10),
        make_video("b", views=2000, age_hours=24 * 20),
        make_video("c", views=3000, age_hours=24 * 30),
        make_video("old", views=99999, age_hours=24 * 200),  # too old, excluded
    ]}
    assert channel_baselines(uploads, now) == {"UCa": 2000.0}


def test_baseline_falls_back_to_all_uploads(now):
    uploads = {"UCa": [make_video("a", views=100, age_hours=5), make_video("b", views=300, age_hours=5)]}
    assert channel_baselines(uploads, now) == {"UCa": 200.0}


def test_growth_uses_snapshot_delta_when_available(now):
    video = make_video("v", views=3000, age_hours=72)
    prev = Snapshot(views=1000, captured_at=now - timedelta(hours=12))

    scored = score_video(video, baseline=7000, previous=prev, now=now)

    assert scored.has_history
    assert scored.views_24h == pytest.approx(4000)       # 2000 in 12h -> 4000/24h
    assert scored.outlier == pytest.approx(3000 / 7000)
    assert scored.pace == pytest.approx(4000 / 1000)


def test_first_sighting_uses_lifetime_average_without_extrapolating_new_uploads(now):
    fresh = score_video(make_video("v", views=500, age_hours=2), 1000, None, now)
    older = score_video(make_video("w", views=900, age_hours=72), 1000, None, now)

    assert not fresh.has_history
    assert fresh.views_24h == pytest.approx(500)   # age clamped to 24h
    assert older.views_24h == pytest.approx(300)


def test_score_videos_skips_shorts_and_sorts(now):
    videos = [make_video("short", views=10**6, duration=30), make_video("ok", views=500),
              make_video("best", views=5000)]
    result = score_videos(videos, {"UCa": 1000}, {}, now, min_duration_sec=61)
    assert [s.video.id for s in result] == ["best", "ok"]


def _topic(title, channel_ids, score):
    from yt_trends.models import ScoredVideo
    videos = [ScoredVideo(make_video(f"{title}{i}", channel=c), 1, 1, False, 1, score)
              for i, c in enumerate(channel_ids)]
    return Topic(title=title, keywords=[], videos=videos)


def test_rank_topics_rewards_coverage_novelty_and_demand():
    covered = _topic("budget apps", ["UCa", "UCb", "UCc"], 1.0)
    stale = _topic("side hustle ideas", ["UCa"], 1.0)
    weights = Weights(performance=0, coverage=0.5, novelty=0.5, search_demand=0)

    ranked = rank_topics([stale, covered], weights, recent_topics=[["side hustle ideas"]], suggestions=[])

    assert [t.title for t in ranked] == ["budget apps", "side hustle ideas"]
    assert covered.is_new and not stale.is_new
    assert stale.novelty == pytest.approx(0.0)


def test_search_demand_counts_matching_suggestions():
    topic = _topic("credit score hacks", ["UCa"], 1.0)
    rank_topics([topic], Weights(), [], ["credit score 800", "best budget app", "how to fix credit"])
    assert topic.search_demand == pytest.approx(2 / 3)


def test_tokens_drop_stopwords():
    assert tokens("How to BUILD the best Budget in 2026!") == {"build", "budget", "2026"}
