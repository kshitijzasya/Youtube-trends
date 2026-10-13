"""Configuration loading: YAML for behaviour, environment variables for secrets."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping

import yaml

MAX_TOPICS = 15  # keeps the Slack message under Block Kit's 50-block limit
KNOWN_PROVIDERS = ("gemini", "groq")


class ConfigError(ValueError):
    """Raised when config.yaml or the environment is invalid."""


@dataclass(frozen=True)
class Weights:
    """Relative weights of the topic ranking signals."""

    performance: float = 0.4
    coverage: float = 0.25
    novelty: float = 0.2
    search_demand: float = 0.15


@dataclass(frozen=True)
class NicheConfig:
    """Everything that defines one tracked niche."""

    name: str
    channels: tuple[str, ...]
    search_keywords: tuple[str, ...] = ()
    lookback_hours: int = 72
    topics: int = 5
    candidate_videos: int = 40
    min_duration_sec: int = 61
    search_results_per_keyword: int = 15
    max_discovered_channels: int = 30
    comments_per_video: int = 10
    region_code: str | None = None
    language: str | None = None
    slack_webhook_env: str = "SLACK_WEBHOOK_URL"
    weights: Weights = field(default_factory=Weights)


@dataclass(frozen=True)
class LlmConfig:
    """LLM provider order and model names."""

    providers: tuple[str, ...] = KNOWN_PROVIDERS
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_models: tuple[str, ...] = ()  # tried in order when gemini_model is overloaded
    groq_model: str = "llama-3.3-70b-versatile"
    temperature: float = 0.4


@dataclass(frozen=True)
class AppConfig:
    """Top-level configuration."""

    niches: tuple[NicheConfig, ...]
    llm: LlmConfig
    db_path: Path
    db_schema: str = "yt_trends"
    quota_budget: int = 8000
    novelty_days: int = 7
    retention_days: int = 120


@dataclass(frozen=True)
class Secrets:
    """API keys read from the environment. Empty values are treated as missing."""

    youtube_api_key: str
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    database_url: str | None = None

    @staticmethod
    def from_env(env: Mapping[str, str] = os.environ) -> "Secrets":
        """Read secrets; only the YouTube key is mandatory."""
        youtube = env.get("YT_API_KEY", "").strip()
        if not youtube:
            raise ConfigError("YT_API_KEY is not set")
        return Secrets(
            youtube_api_key=youtube,
            gemini_api_key=env.get("GEMINI_API_KEY", "").strip() or None,
            groq_api_key=env.get("GROQ_API_KEY", "").strip() or None,
            database_url=env.get("DATABASE_URL", "").strip() or None,
        )


def _build(cls, raw: Mapping, context: str):
    """Instantiate a dataclass from a mapping, rejecting unknown keys (catches typos)."""
    allowed = {f.name for f in fields(cls)}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{context}: unknown key(s) {sorted(unknown)}")
    return cls(**raw)


def _parse_niche(raw: Mapping, index: int) -> NicheConfig:
    """Validate and build one niche block."""
    context = f"niches[{index}]"
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{context} must be a mapping")
    data = dict(raw)
    data["channels"] = tuple(str(c).strip() for c in data.get("channels") or () if str(c).strip())
    data["search_keywords"] = tuple(
        str(k).strip() for k in data.get("search_keywords") or () if str(k).strip()
    )
    data["weights"] = _build(Weights, data.get("weights") or {}, f"{context}.weights")
    niche = _build(NicheConfig, data, context)

    if not niche.name:
        raise ConfigError(f"{context}: name is required")
    if not niche.channels and not niche.search_keywords:
        raise ConfigError(f"{context}: needs at least one channel or search keyword")
    if not 1 <= niche.topics <= MAX_TOPICS:
        raise ConfigError(f"{context}: topics must be between 1 and {MAX_TOPICS}")
    if niche.lookback_hours < 1 or niche.candidate_videos < 1:
        raise ConfigError(f"{context}: lookback_hours and candidate_videos must be positive")
    if not 1 <= niche.search_results_per_keyword <= 50:
        raise ConfigError(f"{context}: search_results_per_keyword must be between 1 and 50")
    if any(getattr(niche.weights, f.name) < 0 for f in fields(Weights)):
        raise ConfigError(f"{context}: weights must not be negative")
    return niche


def load_config(path: str | Path) -> AppConfig:
    """Load and validate config.yaml."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc

    settings = dict(raw.get("settings") or {})
    llm_raw = dict(raw.get("llm") or {})
    for key in ("providers", "gemini_fallback_models"):
        if key in llm_raw:
            llm_raw[key] = tuple(str(v) for v in llm_raw[key] or ())
    llm = _build(LlmConfig, llm_raw, "llm")
    bad = [p for p in llm.providers if p not in KNOWN_PROVIDERS]
    if bad:
        raise ConfigError(f"llm.providers: unknown provider(s) {bad}; use {list(KNOWN_PROVIDERS)}")

    niches = tuple(_parse_niche(n, i) for i, n in enumerate(raw.get("niches") or ()))
    if not niches:
        raise ConfigError("at least one niche is required")
    names = [n.name for n in niches]
    if len(names) != len(set(names)):
        raise ConfigError("niche names must be unique")

    settings["db_path"] = Path(settings.get("db_path", "data/trends.db"))
    config = _build(AppConfig, {**settings, "niches": niches, "llm": llm}, "settings")
    if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", config.db_schema):
        raise ConfigError("settings.db_schema: use lowercase letters, digits and _ only")
    return config
