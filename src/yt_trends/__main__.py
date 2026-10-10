"""CLI entry point: `python -m yt_trends [--dry-run] [--niche NAME]`."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import __version__, slack
from .config import ConfigError, Secrets, load_config
from .discovery import collect_suggestions
from .llm import build_llm
from .pipeline import Deps, run_niche
from .store import SqliteStore, open_store
from .youtube import YouTubeClient

log = logging.getLogger("yt_trends")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(prog="yt_trends", description="Daily YouTube niche trends to Slack")
    parser.add_argument("--config", default=os.environ.get("YT_TRENDS_CONFIG", "config.yaml"))
    parser.add_argument("--prompts", default=os.environ.get("YT_TRENDS_PROMPTS", "prompts"))
    parser.add_argument("--niche", help="run only this niche (by name)")
    parser.add_argument("--dry-run", action="store_true", help="print the report instead of posting to Slack")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument("--import-sqlite", metavar="PATH",
                        help="copy an existing SQLite history file into DATABASE_URL, then exit")
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def _alert(webhook: str | None, niche: str, error: Exception, session: requests.Session) -> None:
    """Best-effort failure message to Slack; never raises."""
    if not webhook:
        return
    try:
        slack.post(webhook, {"text": f":warning: yt-trends failed for *{slack.esc(niche)}*: "
                                     f"{slack.esc(str(error))[:1500]}"}, session)
    except Exception as exc:  # noqa: BLE001 - alerting must not mask the original error
        log.error("could not send Slack alert: %s", exc)


def import_sqlite(path: Path, schema: str) -> int:
    """One-off: copy a local SQLite history file into the Postgres DATABASE_URL."""
    from .postgres_store import PostgresStore, StoreConnectionError

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        log.error("DATABASE_URL must be set to import into Postgres")
        return 2
    if not path.exists():
        log.error("SQLite file not found: %s", path)
        return 2
    source = SqliteStore(path)
    try:
        target = PostgresStore(url, schema=schema)
    except StoreConnectionError as exc:
        source.close()
        log.error("%s", exc)
        return 1
    try:
        counts = target.load(source.dump())
    finally:
        target.close()
        source.close()
    log.info("imported rows %s from %s into %s", counts, path, target.describe())
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run every configured niche. Exit code 0 = all ok, 1 = a niche failed, 2 = bad config."""
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        config = load_config(args.config)
        if args.import_sqlite:
            return import_sqlite(Path(args.import_sqlite), config.db_schema)
        secrets = Secrets.from_env()
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    niches = [n for n in config.niches if not args.niche or n.name == args.niche]
    if not niches:
        log.error("no niche named %r in %s", args.niche, args.config)
        return 2

    session = requests.Session()
    session.headers["User-Agent"] = f"yt-trends/{__version__}"
    youtube = YouTubeClient(secrets.youtube_api_key, config.quota_budget, session)
    llm = build_llm(config.llm, secrets, session)
    if not llm:
        log.warning("no GEMINI_API_KEY/GROQ_API_KEY set: using keyword clustering, no topic details")

    try:
        store = open_store(config.db_path, config.db_schema, secrets.database_url)
    except Exception as exc:  # noqa: BLE001 - report DB outages to Slack, then fail the job
        log.error("history store unavailable: %s", exc)
        if not args.dry_run:
            webhooks = {os.environ.get(n.slack_webhook_env, "").strip() for n in niches} - {""}
            for webhook in webhooks:
                _alert(webhook, "all niches (database)", exc, session)
        return 1
    log.info("history store: %s", store.describe())
    failures = 0
    try:
        for niche in niches:
            webhook = os.environ.get(niche.slack_webhook_env, "").strip() or None
            try:
                if not args.dry_run and not webhook:
                    raise ConfigError(f"{niche.slack_webhook_env} is not set (or use --dry-run)")
                deps = Deps(
                    youtube=youtube,
                    store=store,
                    llm=llm,
                    suggest=lambda queries, lang: collect_suggestions(queries, session, lang),
                    prompts_dir=Path(args.prompts),
                    now=datetime.now(timezone.utc),
                )
                result = run_niche(niche, config, deps)
                if args.dry_run:
                    print(slack.render_text(niche.name, result.run_date, result.topics, result.stats))
                else:
                    slack.post(webhook, slack.build_message(niche.name, result.run_date, result.topics,
                                                            result.stats), session)
                log.info("%s: posted %d topics (%s)", niche.name, len(result.topics), result.stats)
            except Exception as exc:  # noqa: BLE001 - one niche failing must not stop the others
                failures += 1
                log.exception("%s failed", niche.name)
                store.rollback()  # a failed Postgres transaction must be reset before the next niche
                if not args.dry_run:
                    _alert(webhook, niche.name, exc, session)
        store.prune(config.retention_days, datetime.now(timezone.utc))
    finally:
        store.close()
    log.info("done: %d niche(s), %d failed, %d YouTube units", len(niches), failures, youtube.units_used)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
