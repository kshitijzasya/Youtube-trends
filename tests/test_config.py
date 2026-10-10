"""Config loading and validation."""
from pathlib import Path

import pytest

from yt_trends.config import ConfigError, Secrets, load_config

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_shipped_config_is_valid():
    """config.yaml is user-edited, so only check that it loads and validates, not specific values."""
    config = load_config(ROOT / "config.yaml")
    assert config.niches


def test_unknown_key_is_rejected(tmp_path):
    path = write(tmp_path, "niches:\n  - name: x\n    channels: ['@a']\n    topcis: 5\n")
    with pytest.raises(ConfigError, match="topcis"):
        load_config(path)


def test_niche_needs_channels_or_keywords(tmp_path):
    with pytest.raises(ConfigError, match="at least one channel"):
        load_config(write(tmp_path, "niches:\n  - name: x\n"))


def test_unknown_provider_rejected(tmp_path):
    path = write(tmp_path, "llm:\n  providers: [openai]\nniches:\n  - name: x\n    channels: ['@a']\n")
    with pytest.raises(ConfigError, match="openai"):
        load_config(path)


def test_defaults_applied(tmp_path):
    config = load_config(write(tmp_path, "niches:\n  - name: x\n    search_keywords: [budget]\n"))
    niche = config.niches[0]
    assert niche.channels == ()
    assert niche.weights.performance == 0.4
    assert str(config.db_path) == "data/trends.db"


def test_bad_schema_name_rejected(tmp_path):
    path = write(tmp_path, "settings:\n  db_schema: Bad-Name\nniches:\n  - name: x\n    channels: ['@a']\n")
    with pytest.raises(ConfigError, match="db_schema"):
        load_config(path)


def test_secrets_from_env():
    secrets = Secrets.from_env({"YT_API_KEY": " k ", "GEMINI_API_KEY": "", "GROQ_API_KEY": "g"})
    assert (secrets.youtube_api_key, secrets.gemini_api_key, secrets.groq_api_key) == ("k", None, "g")
    assert secrets.database_url is None
    assert Secrets.from_env({"YT_API_KEY": "k", "DATABASE_URL": " postgresql://x "}).database_url == "postgresql://x"
    with pytest.raises(ConfigError):
        Secrets.from_env({})
