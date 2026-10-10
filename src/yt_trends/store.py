"""History storage: channel cache, daily view snapshots and past reports.

Two backends share one interface:
- SqliteStore: a local file (default; used for local runs and tests)
- PostgresStore (postgres_store.py): Supabase or any Postgres, used when DATABASE_URL is set
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from .models import Snapshot, Topic, Video

EVIDENCE_VIDEOS = 5  # videos stored per reported topic


class HistoryStore(Protocol):
    """What the pipeline needs from a storage backend."""

    def channel_id_for_handle(self, handle: str) -> str | None: ...
    def save_handle(self, handle: str, channel_id: str) -> None: ...
    def previous_snapshots(self, video_ids: Iterable[str], before: datetime) -> dict[str, Snapshot]: ...
    def save_snapshots(self, videos: Iterable[Video], captured_at: datetime) -> None: ...
    def recent_topics(self, niche: str, since: datetime, before_date: str) -> list[list[str]]: ...
    def save_report(self, run_date: str, niche: str, topics: list[Topic]) -> None: ...
    def prune(self, retention_days: int, now: datetime) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


def report_rows(topics: list[Topic]) -> Iterator[tuple]:
    """Backend-neutral report values: (rank, topic, keywords, score, is_new, details, evidence)."""
    for rank, topic in enumerate(topics, start=1):
        evidence = [{
            "title": sv.video.title,
            "url": sv.video.url,
            "channel": sv.video.channel_title,
            "views": sv.video.views,
            "views_24h": int(sv.views_24h),
            "outlier": round(sv.outlier, 2),
        } for sv in topic.videos[:EVIDENCE_VIDEOS]]
        yield (rank, topic.title, [topic.title, *topic.keywords], round(topic.score, 4),
               topic.is_new, topic.details, evidence)


def iso(dt: datetime) -> str:
    """UTC timestamp string that sorts lexicographically."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Inverse of iso()."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS handles (
    handle      TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    channel_id    TEXT NOT NULL,
    channel_title TEXT,
    title         TEXT,
    published_at  TEXT,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    video_id    TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    views       INTEGER NOT NULL,
    likes       INTEGER NOT NULL,
    comments    INTEGER NOT NULL,
    PRIMARY KEY (video_id, captured_at)
);
CREATE TABLE IF NOT EXISTS reports (
    run_date TEXT NOT NULL,
    niche    TEXT NOT NULL,
    rank     INTEGER NOT NULL,
    topic    TEXT NOT NULL,
    keywords TEXT NOT NULL,
    score    REAL NOT NULL,
    PRIMARY KEY (run_date, niche, rank)
);
"""
# Columns added after the first release; created on older files by _migrate().
SQLITE_REPORT_COLUMNS = {"is_new": "INTEGER NOT NULL DEFAULT 1", "details": "TEXT NOT NULL DEFAULT '{}'",
                         "evidence": "TEXT NOT NULL DEFAULT '[]'"}


class SqliteStore:
    """SQLite backend. One instance per run; call close() when done."""

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(str(path))
        self._db.executescript(SQLITE_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add report columns missing from files created by older versions."""
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(reports)")}
        for column, ddl in SQLITE_REPORT_COLUMNS.items():
            if column not in existing:
                self._db.execute(f"ALTER TABLE reports ADD COLUMN {column} {ddl}")

    def describe(self) -> str:
        """Human-readable location for logs."""
        return f"sqlite:{self.path}"

    def close(self) -> None:
        """Commit and close the connection."""
        self._db.commit()
        self._db.close()

    def commit(self) -> None:
        """Flush pending writes."""
        self._db.commit()

    def rollback(self) -> None:
        """Discard pending writes."""
        self._db.rollback()

    def channel_id_for_handle(self, handle: str) -> str | None:
        """Cached handle -> channel id, or None."""
        row = self._db.execute("SELECT channel_id FROM handles WHERE handle = ?",
                               (handle.lower(),)).fetchone()
        return row[0] if row else None

    def save_handle(self, handle: str, channel_id: str) -> None:
        """Cache a resolved handle."""
        self._db.execute("INSERT OR REPLACE INTO handles VALUES (?, ?)", (handle.lower(), channel_id))

    def previous_snapshots(self, video_ids: Iterable[str], before: datetime) -> dict[str, Snapshot]:
        """Latest snapshot per video taken at or before a cut-off time."""
        ids = list(dict.fromkeys(video_ids))
        result: dict[str, Snapshot] = {}
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            marks = ",".join("?" * len(batch))
            rows = self._db.execute(
                f"SELECT video_id, views, captured_at FROM snapshots "
                f"WHERE captured_at <= ? AND video_id IN ({marks}) "
                f"ORDER BY video_id, captured_at DESC",
                (iso(before), *batch),
            )
            for video_id, views, captured_at in rows:
                result.setdefault(video_id, Snapshot(views=views, captured_at=parse_iso(captured_at)))
        return result

    def save_snapshots(self, videos: Iterable[Video], captured_at: datetime) -> None:
        """Record current stats for videos (idempotent per timestamp)."""
        stamp = iso(captured_at)
        for v in videos:
            self._db.execute(
                "INSERT OR REPLACE INTO videos VALUES (?, ?, ?, ?, ?, ?)",
                (v.id, v.channel_id, v.channel_title, v.title, iso(v.published_at), stamp),
            )
            self._db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)",
                (v.id, stamp, v.views, v.likes, v.comments),
            )

    def recent_topics(self, niche: str, since: datetime, before_date: str) -> list[list[str]]:
        """Keyword lists of topics reported for a niche between `since` and `before_date` (exclusive)."""
        rows = self._db.execute(
            "SELECT keywords FROM reports WHERE niche = ? AND run_date >= ? AND run_date < ?",
            (niche, since.strftime("%Y-%m-%d"), before_date),
        )
        return [json.loads(r[0]) for r in rows]

    def save_report(self, run_date: str, niche: str, topics: list[Topic]) -> None:
        """Store today's ranked topics (re-runs on the same day replace them)."""
        self._db.execute("DELETE FROM reports WHERE run_date = ? AND niche = ?", (run_date, niche))
        for rank, title, keywords, score, is_new, details, evidence in report_rows(topics):
            self._db.execute(
                "INSERT INTO reports (run_date, niche, rank, topic, keywords, score, is_new, details, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_date, niche, rank, title, json.dumps(keywords), score, int(is_new),
                 json.dumps(details), json.dumps(evidence)),
            )

    def prune(self, retention_days: int, now: datetime) -> None:
        """Delete history older than the retention window."""
        cutoff = now - timedelta(days=retention_days)
        self._db.execute("DELETE FROM snapshots WHERE captured_at < ?", (iso(cutoff),))
        self._db.execute("DELETE FROM videos WHERE last_seen < ?", (iso(cutoff),))
        self._db.execute("DELETE FROM reports WHERE run_date < ?", (cutoff.strftime("%Y-%m-%d"),))

    def dump(self) -> dict[str, list[tuple]]:
        """All rows converted to Python types (used to import into Postgres)."""
        q = self._db.execute
        return {
            "handles": q("SELECT handle, channel_id FROM handles").fetchall(),
            "videos": [(i, c, ct, t, parse_iso(p) if p else None, parse_iso(ls))
                       for i, c, ct, t, p, ls in q("SELECT * FROM videos")],
            "snapshots": [(v, parse_iso(at), views, likes, comments)
                          for v, at, views, likes, comments in q("SELECT * FROM snapshots")],
            "reports": [(date.fromisoformat(d), n, r, t, json.loads(k), s, bool(new), json.loads(det), json.loads(ev))
                        for d, n, r, t, k, s, new, det, ev in q(
                            "SELECT run_date, niche, rank, topic, keywords, score, is_new, details, evidence "
                            "FROM reports")],
        }


def open_store(db_path: str | Path, schema: str, database_url: str | None) -> HistoryStore:
    """Postgres when DATABASE_URL is set, otherwise the local SQLite file."""
    if database_url:
        from .postgres_store import PostgresStore  # imported lazily: only needed for Postgres
        return PostgresStore(database_url, schema=schema)
    return SqliteStore(db_path)
