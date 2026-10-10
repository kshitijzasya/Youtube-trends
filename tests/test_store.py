"""History storage: behaviour shared by SQLite and Postgres (see the `store` fixture)."""
from datetime import timedelta

from yt_trends.models import ScoredVideo, Topic
from yt_trends.store import SqliteStore

from conftest import make_video


def test_previous_snapshot_is_latest_before_cutoff(store, now):
    store.save_snapshots([make_video("v1", views=100)], now - timedelta(hours=48))
    store.save_snapshots([make_video("v1", views=500)], now - timedelta(hours=24))
    store.save_snapshots([make_video("v1", views=900)], now - timedelta(hours=2))  # too recent
    store.commit()

    snap = store.previous_snapshots(["v1", "missing"], before=now - timedelta(hours=12))

    assert set(snap) == {"v1"}
    assert snap["v1"].views == 500
    assert snap["v1"].captured_at == now - timedelta(hours=24)


def test_snapshot_rerun_same_timestamp_updates(store, now):
    store.save_snapshots([make_video("v1", views=100)], now)
    store.save_snapshots([make_video("v1", views=150)], now)
    assert store.previous_snapshots(["v1"], now)["v1"].views == 150


def test_empty_inputs_are_noops(store, now):
    store.save_snapshots([], now)
    assert store.previous_snapshots([], now) == {}
    store.save_report("2026-10-10", "n", [])


def test_reports_replace_same_day_and_history_excludes_today(store, now):
    topic = Topic(title="Budget apps", keywords=["ynab"], videos=[])
    store.save_report("2026-10-09", "n", [topic])
    store.save_report("2026-10-10", "n", [topic])
    store.save_report("2026-10-10", "n", [topic])  # re-run same day

    recent = store.recent_topics("n", now - timedelta(days=7), before_date="2026-10-10")

    assert recent == [["Budget apps", "ynab"]]
    assert store.recent_topics("other", now - timedelta(days=7), "2026-10-11") == []


def test_report_keeps_details_and_evidence(store, now):
    video = ScoredVideo(make_video("v1", title="Why budgets fail"), 3.21, 1500.7, True, 2.0, 1.0)
    topic = Topic(title="Budgets", keywords=["budget"], videos=[video], is_new=False,
                  details={"why_trending": "tax season", "title_ideas": ["a", "b"]})
    store.save_report("2026-10-10", "n", [topic])
    store.commit()
    # read back through the backend's own connection
    if isinstance(store, SqliteStore):
        row = store._db.execute("SELECT is_new, details, evidence FROM reports").fetchone()
        import json
        is_new, details, evidence = row[0], json.loads(row[1]), json.loads(row[2])
    else:
        is_new, details, evidence = store._conn.execute(
            store._q("SELECT is_new, details, evidence FROM {reports}")).fetchone()

    assert not is_new
    assert details["why_trending"] == "tax season"
    assert evidence == [{"title": "Why budgets fail", "url": "https://www.youtube.com/watch?v=v1",
                         "channel": "Channel UCa", "views": 1000, "views_24h": 1500, "outlier": 3.21}]


def test_handle_cache_is_case_insensitive(store):
    store.save_handle("@MrBeast", "UCx")
    store.save_handle("@mrbeast", "UCy")  # upsert
    assert store.channel_id_for_handle("@MRBEAST") == "UCy"


def test_prune_removes_old_rows(store, now):
    store.save_snapshots([make_video("old")], now - timedelta(days=200))
    store.save_snapshots([make_video("new")], now)
    store.save_report("2026-01-01", "n", [Topic("Old", [], [])])
    store.prune(120, now)

    assert set(store.previous_snapshots(["old", "new"], now)) == {"new"}
    assert store.recent_topics("n", now - timedelta(days=400), "2026-12-31") == []


def test_rollback_discards_uncommitted_writes(store):
    store.save_handle("@a", "UCa")
    store.commit()
    store.save_handle("@b", "UCb")
    store.rollback()
    assert store.channel_id_for_handle("@a") == "UCa"
    assert store.channel_id_for_handle("@b") is None


def test_sqlite_creates_parent_directory(tmp_path):
    store = SqliteStore(tmp_path / "nested" / "trends.db")
    store.close()
    assert (tmp_path / "nested" / "trends.db").exists()


def test_sqlite_migrates_files_from_older_versions(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE reports (run_date TEXT NOT NULL, niche TEXT NOT NULL, rank INTEGER NOT NULL, "
                 "topic TEXT NOT NULL, keywords TEXT NOT NULL, score REAL NOT NULL, "
                 "PRIMARY KEY (run_date, niche, rank))")
    conn.execute("INSERT INTO reports VALUES ('2026-10-01', 'n', 1, 'T', '[\"T\"]', 0.5)")
    conn.commit()
    conn.close()

    store = SqliteStore(path)
    [report] = store.dump()["reports"]
    assert report[6:] == (True, {}, [])  # defaults for the new columns
