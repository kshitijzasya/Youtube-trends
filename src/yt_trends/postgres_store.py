"""Postgres history backend (Supabase or any Postgres 13+).

Tables live in their own schema (default `yt_trends`) with row level security enabled and no
policies, so Supabase's public REST API cannot read them; this job connects as the database
owner, which bypasses RLS.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from .models import Snapshot, Topic, Video
from .store import report_rows

TABLES = ("handles", "videos", "snapshots", "reports")
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {handles} (
    handle      TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {videos} (
    id            TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    channel_title TEXT,
    title         TEXT,
    published_at  TIMESTAMPTZ,
    last_seen     TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS {snapshots} (
    video_id    TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    views       BIGINT NOT NULL,
    likes       BIGINT NOT NULL,
    comments    BIGINT NOT NULL,
    PRIMARY KEY (video_id, captured_at)
);
CREATE INDEX IF NOT EXISTS snapshots_captured_at_idx ON {snapshots} (captured_at);
CREATE TABLE IF NOT EXISTS {reports} (
    run_date DATE NOT NULL,
    niche    TEXT NOT NULL,
    rank     INTEGER NOT NULL,
    topic    TEXT NOT NULL,
    keywords JSONB NOT NULL,
    score    DOUBLE PRECISION NOT NULL,
    is_new   BOOLEAN NOT NULL DEFAULT TRUE,
    details  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (run_date, niche, rank)
);
ALTER TABLE {handles} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {videos} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {snapshots} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {reports} ENABLE ROW LEVEL SECURITY;
"""


class StoreConnectionError(RuntimeError):
    """Raised when the database cannot be reached (message never contains the password)."""


def _split_url(url: str) -> tuple[str, str, str]:
    """(userinfo, host[:port], dbname) of a postgres:// URL, splitting at the LAST '@'.

    Splitting at the last '@' keeps a password containing a raw '@' out of the host part.
    """
    rest = url.split("://", 1)[1]
    netloc, _, path = rest.partition("/")
    userinfo, _, hostport = netloc.rpartition("@")
    return userinfo, hostport, path.split("?", 1)[0] or "?"


def safe_location(url: str) -> str:
    """host:port/dbname for logs, without credentials."""
    if "://" in url:
        _, hostport, dbname = _split_url(url)
        return f"{hostport}/{dbname}"
    try:
        info = conninfo_to_dict(url)
    except psycopg.ProgrammingError:
        return "<unparseable DATABASE_URL>"
    return f"{info.get('host', '?')}:{info.get('port', '5432')}/{info.get('dbname', '?')}"


def _scrub(text: str, url: str) -> str:
    """Remove the URL's credentials (and any fragment of them) from a message."""
    if "://" in url:
        userinfo = _split_url(url)[0]
        secrets = [userinfo, userinfo.partition(":")[2], *re.split(r"[:@]", userinfo)]
    else:
        try:
            secrets = [conninfo_to_dict(url).get("password") or ""]
        except psycopg.ProgrammingError:
            secrets = []
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 3:
            text = text.replace(secret, "***")
    return text


def _url_problems(url: str) -> list[str]:
    """Common Supabase connection-string mistakes, as human-readable hints."""
    if "://" not in url:
        return []
    problems = []
    userinfo, hostport, _ = _split_url(url)
    if "@" in userinfo:
        problems.append("the password contains a raw '@' - URL-encode it as %40 (or use a password "
                        "of letters and digits only)")
    if "[" in userinfo or "]" in userinfo:
        problems.append("remove the [ ] brackets around the password (they were part of the placeholder)")
    if re.match(r"^db\.[a-z0-9]+\.supabase\.co(:\d+)?$", hostport):
        problems.append("this is Supabase's Direct connection (IPv6-only, fails on GitHub Actions) - "
                        "use the Session pooler string instead")
    return problems


class PostgresStore:
    """Postgres backend. One instance per run; call close() when done."""

    def __init__(self, url: str, schema: str = "yt_trends") -> None:
        if not SCHEMA_NAME.match(schema):
            raise ValueError(f"invalid schema name {schema!r}: use lowercase letters, digits and _")
        self.schema = schema
        self._location = safe_location(url)
        problems = _url_problems(url)
        if problems:
            raise StoreConnectionError(f"DATABASE_URL for {self._location} looks wrong: " + "; ".join(problems))
        try:
            # prepare_threshold=None: no server-side prepared statements, so both Supabase
            # pooler modes (session :5432 and transaction :6543) work.
            self._conn = psycopg.connect(url, prepare_threshold=None, connect_timeout=20,
                                         application_name="yt-trends")
        except psycopg.Error as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            raise StoreConnectionError(_scrub(f"cannot connect to {self._location}: {reason}", url)) from None
        self._names = {"schema": sql.Identifier(schema),
                       **{t: sql.Identifier(schema, t) for t in TABLES}}
        self._conn.execute(self._q(DDL))
        self._conn.commit()

    def _q(self, text: str) -> sql.Composed:
        """Fill {schema}/{table} placeholders with quoted identifiers."""
        return sql.SQL(text).format(**self._names)

    def describe(self) -> str:
        """Human-readable location for logs (no credentials)."""
        return f"postgres:{self._location} schema={self.schema}"

    def close(self) -> None:
        """Commit and close the connection."""
        if not self._conn.closed:
            self._conn.commit()
            self._conn.close()

    def commit(self) -> None:
        """Flush pending writes."""
        self._conn.commit()

    def rollback(self) -> None:
        """Discard pending writes (required after an error inside a transaction)."""
        self._conn.rollback()

    def channel_id_for_handle(self, handle: str) -> str | None:
        """Cached handle -> channel id, or None."""
        row = self._conn.execute(self._q("SELECT channel_id FROM {handles} WHERE handle = %s"),
                                 (handle.lower(),)).fetchone()
        return row[0] if row else None

    def save_handle(self, handle: str, channel_id: str) -> None:
        """Cache a resolved handle."""
        self._conn.execute(
            self._q("INSERT INTO {handles} VALUES (%s, %s) "
                    "ON CONFLICT (handle) DO UPDATE SET channel_id = EXCLUDED.channel_id"),
            (handle.lower(), channel_id),
        )

    def previous_snapshots(self, video_ids: Iterable[str], before: datetime) -> dict[str, Snapshot]:
        """Latest snapshot per video taken at or before a cut-off time."""
        ids = list(dict.fromkeys(video_ids))
        if not ids:
            return {}
        rows = self._conn.execute(
            self._q("SELECT DISTINCT ON (video_id) video_id, views, captured_at FROM {snapshots} "
                    "WHERE captured_at <= %s AND video_id = ANY(%s) ORDER BY video_id, captured_at DESC"),
            (before, ids),
        ).fetchall()
        return {vid: Snapshot(views=views, captured_at=at.astimezone(timezone.utc)) for vid, views, at in rows}

    def save_snapshots(self, videos: Iterable[Video], captured_at: datetime) -> None:
        """Record current stats for videos (idempotent per timestamp)."""
        stamp = captured_at.astimezone(timezone.utc).replace(microsecond=0)
        videos = list(videos)
        if not videos:
            return
        with self._conn.cursor() as cur:
            cur.executemany(
                self._q("INSERT INTO {videos} VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                        "channel_title = EXCLUDED.channel_title, title = EXCLUDED.title, "
                        "last_seen = EXCLUDED.last_seen"),
                [(v.id, v.channel_id, v.channel_title, v.title, v.published_at, stamp) for v in videos],
            )
            cur.executemany(
                self._q("INSERT INTO {snapshots} VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (video_id, captured_at) DO UPDATE SET views = EXCLUDED.views, "
                        "likes = EXCLUDED.likes, comments = EXCLUDED.comments"),
                [(v.id, stamp, v.views, v.likes, v.comments) for v in videos],
            )

    def recent_topics(self, niche: str, since: datetime, before_date: str) -> list[list[str]]:
        """Keyword lists of topics reported for a niche between `since` and `before_date` (exclusive)."""
        rows = self._conn.execute(
            self._q("SELECT keywords FROM {reports} WHERE niche = %s AND run_date >= %s AND run_date < %s"),
            (niche, since.date(), date.fromisoformat(before_date)),
        ).fetchall()
        return [r[0] for r in rows]

    def save_report(self, run_date: str, niche: str, topics: list[Topic]) -> None:
        """Store today's ranked topics (re-runs on the same day replace them)."""
        day = date.fromisoformat(run_date)
        self._conn.execute(self._q("DELETE FROM {reports} WHERE run_date = %s AND niche = %s"), (day, niche))
        rows = [(day, niche, rank, title, Jsonb(keywords), score, is_new, Jsonb(details), Jsonb(evidence))
                for rank, title, keywords, score, is_new, details, evidence in report_rows(topics)]
        if rows:
            with self._conn.cursor() as cur:
                cur.executemany(self._q("INSERT INTO {reports} VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"), rows)

    def prune(self, retention_days: int, now: datetime) -> None:
        """Delete history older than the retention window."""
        cutoff = now - timedelta(days=retention_days)
        self._conn.execute(self._q("DELETE FROM {snapshots} WHERE captured_at < %s"), (cutoff,))
        self._conn.execute(self._q("DELETE FROM {videos} WHERE last_seen < %s"), (cutoff,))
        self._conn.execute(self._q("DELETE FROM {reports} WHERE run_date < %s"), (cutoff.date(),))

    def load(self, dump: dict[str, list[tuple]]) -> dict[str, int]:
        """Insert rows from SqliteStore.dump(); existing rows are kept. Returns rows read per table."""
        placeholders = {"handles": 2, "videos": 6, "snapshots": 5, "reports": 9}
        with self._conn.cursor() as cur:
            for table, count in placeholders.items():
                rows = dump.get(table) or []
                if table == "reports":
                    rows = [(*r[:4], Jsonb(r[4]), r[5], r[6], Jsonb(r[7]), Jsonb(r[8])) for r in rows]
                if rows:
                    marks = ", ".join(["%s"] * count)
                    cur.executemany(self._q(f"INSERT INTO {{{table}}} VALUES ({marks}) ON CONFLICT DO NOTHING"), rows)
        self._conn.commit()
        return {table: len(dump.get(table) or []) for table in placeholders}
