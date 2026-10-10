"""Topic clustering/enrichment, LLM plumbing and Slack formatting."""
import json
from pathlib import Path

import pytest

from yt_trends.config import LlmConfig, Secrets
from yt_trends.llm import FallbackLlm, GeminiClient, LlmError, build_llm, parse_json_object
from yt_trends.models import ScoredVideo, Topic
from yt_trends.slack import build_message, esc, render_text
from yt_trends.topics import cluster_by_keywords, cluster_with_llm, enrich_with_llm, load_prompt

from conftest import FakeLlm, FakeResponse, FakeSession, make_video

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def sv(vid, title="t", channel="UCa", score=1.0, tags=()):
    return ScoredVideo(make_video(vid, channel=channel, title=title, tags=tags), 2.0, 100, True, 1.0, score)


# --- clustering -------------------------------------------------------------

def test_cluster_with_llm_drops_unknown_and_duplicate_ids():
    videos = [sv("a"), sv("b"), sv("c")]
    llm = FakeLlm({"topics": [
        {"topic": "One", "keywords": ["k"], "video_ids": ["a", "b", "zzz"]},
        {"topic": "Two", "video_ids": ["b"]},           # b already used -> topic dropped
        {"topic": "Three", "video_ids": ["c"]},
        {"topic": "", "video_ids": ["c"]},              # nameless
    ]})

    topics = cluster_with_llm(llm, videos, "niche", 5, load_prompt(PROMPTS, "cluster"))

    assert [(t.title, [v.video.id for v in t.videos]) for t in topics] == [("One", ["a", "b"]), ("Three", ["c"])]
    assert '"id": "a"' in llm.prompts[0] and "niche" in llm.prompts[0]


def test_cluster_with_llm_raises_when_nothing_usable():
    with pytest.raises(LlmError):
        cluster_with_llm(FakeLlm({"topics": []}), [sv("a")], "n", 5, load_prompt(PROMPTS, "cluster"))


def test_keyword_fallback_groups_shared_terms():
    videos = [
        sv("a", "Budget app review 2026", score=3),
        sv("b", "My budget app setup", score=2),
        sv("c", "Crypto crash explained", score=1),
        sv("d", "Crypto crash: what next", score=1),
    ]
    topics = cluster_by_keywords(videos, 5)
    titles = [t.title for t in topics]

    assert titles[0] == "budget app"
    assert "crypto crash" in titles
    assert "budget" not in titles  # contained in "budget app"


def test_enrich_maps_details_by_index():
    topics = [Topic("A", [], [sv("a")]), Topic("B", [], [sv("b")])]
    llm = FakeLlm({"topics": [
        {"index": 1, "why_trending": "because", "video_angles": ["x", "y", "z", "extra"]},
        {"index": 9, "why_trending": "ignored"},
    ]})

    enrich_with_llm(llm, topics, "n", ["s1"], load_prompt(PROMPTS, "details"))

    assert topics[0].details == {}
    assert topics[1].details["why_trending"] == "because"
    assert topics[1].details["video_angles"] == ["x", "y", "z"]


# --- llm --------------------------------------------------------------------

def test_parse_json_object_handles_fences():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LlmError):
        parse_json_object("[1, 2]")
    with pytest.raises(LlmError):
        parse_json_object("not json")


def test_fallback_uses_next_provider():
    llm = FallbackLlm([FakeLlm(LlmError("rate limited")), FakeLlm({"ok": True})])
    assert llm.complete_json("p") == {"ok": True}
    with pytest.raises(LlmError, match="all LLM providers failed"):
        FallbackLlm([FakeLlm(LlmError("x"))]).complete_json("p")


def test_build_llm_respects_keys_and_order():
    assert build_llm(LlmConfig(), Secrets("yt"), FakeSession()) is None
    chain = build_llm(LlmConfig(providers=("groq", "gemini")), Secrets("yt", "gk", "qk"), FakeSession())
    assert [c.name for c in chain.clients] == ["groq", "gemini"]


def test_gemini_sends_key_in_header_not_url():
    body = {"candidates": [{"content": {"parts": [{"text": '{"topics": []}'}]}}]}
    session = FakeSession(FakeResponse(payload=body))
    client = GeminiClient("SECRET", "m", 0.4, session)

    assert client.complete_json("hi") == {"topics": []}
    method, url, kwargs = session.requests[0]
    assert "SECRET" not in url
    assert kwargs["headers"]["x-goog-api-key"] == "SECRET"


# --- slack ------------------------------------------------------------------

def test_esc():
    assert esc("<a> & b") == "&lt;a&gt; &amp; b"


def test_message_respects_block_kit_limits():
    long = "x" * 5000
    topics = []
    for i in range(15):
        t = Topic(title=long, keywords=[long], videos=[sv(f"v{i}", title=long)], coverage=1, score=0.5)
        t.details = {"why_trending": long, "video_angles": [long] * 3, "title_ideas": [long] * 3}
        topics.append(t)

    payload = build_message("niche", "2026-10-10", topics, {"history": False})

    assert len(payload["blocks"]) <= 50
    for block in payload["blocks"]:
        texts = [block["text"]["text"]] if "text" in block else [e["text"] for e in block.get("elements", [])]
        assert all(len(t) <= 3000 for t in texts)
    json.dumps(payload)  # serialisable


def test_render_text_marks_new_and_ongoing():
    new = Topic("Fresh", ["k"], [sv("a")], is_new=True)
    old = Topic("Old", ["k"], [sv("b")], is_new=False)
    text = render_text("n", "2026-10-10", [new, old], {})
    assert "NEW" in text and "ONGOING" in text and "watch?v=a" in text
