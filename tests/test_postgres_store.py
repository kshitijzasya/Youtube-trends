"""Postgres-only behaviour: security defaults, SQLite import, credential hygiene."""
import os
from datetime import timedelta

import pytest

from yt_trends.models import Topic
from yt_trends.store import SqliteStore, open_store

from conftest import make_video

psycopg = pytest.importorskip("psycopg")
from yt_trends.postgres_store import PostgresStore, StoreConnectionError, safe_location  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


def test_safe_location_hides_credentials():
    url = "postgresql://postgres.abcd:S3cret%21@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
    assert safe_location(url) == "aws-0-ap-south-1.pooler.supabase.com:5432/postgres"


def test_raw_at_in_password_never_leaks_and_is_explained():
    url = "postgresql://postgres:[Pass@3518]@db.abcdefghijklmnop.supabase.co:5432/postgres"

    assert safe_location(url) == "db.abcdefghijklmnop.supabase.co:5432/postgres"
    with pytest.raises(StoreConnectionError) as exc:
        PostgresStore(url)
    message = str(exc.value)
    assert "Pass" not in message and "3518" not in message
    assert "%40" in message and "brackets" in message and "Session pooler" in message


def test_scrub_removes_credential_fragments():
    from yt_trends.postgres_store import _scrub
    url = "postgresql://postgres.ref:abc@def@host:5432/postgres"
    assert _scrub('could not translate host name "def@host"; password abc@def', url) == \
        'could not translate host name "***@host"; password ***'


def test_pooler_url_has_no_problems():
    from yt_trends.postgres_store import _url_problems
    good = "postgresql://postgres.abcd:Letters123@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
    encoded = "postgresql://postgres.abcd:Pass%403518@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
    assert _url_problems(good) == [] and _url_problems(encoded) == []


def test_invalid_schema_rejected():
    with pytest.raises(ValueError, match="invalid schema"):
        PostgresStore("postgresql://x@localhost/db", schema="Robert'); DROP TABLE")


def test_connection_error_does_not_leak_password():
    with pytest.raises(StoreConnectionError) as exc:
        PostgresStore("postgresql://user:TOPSECRET@127.0.0.1:1/db?connect_timeout=2")
    assert "TOPSECRET" not in str(exc.value)
    assert "127.0.0.1:1/db" in str(exc.value)


def test_open_store_picks_backend_by_url(tmp_path):
    store = open_store(tmp_path / "x.db", "yt_trends", None)
    assert isinstance(store, SqliteStore)
    store.close()


@needs_pg
def test_tables_have_row_level_security(store):
    if not isinstance(store, PostgresStore):
        pytest.skip("postgres only")
    rows = store._conn.execute(
        "SELECT c.relname, c.relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind = 'r' ORDER BY c.relname", (store.schema,)).fetchall()
    assert rows == [("handles", True), ("reports", True), ("snapshots", True), ("videos", True)]


@needs_pg
def test_schema_creation_is_idempotent(store):
    if not isinstance(store, PostgresStore):
        pytest.skip("postgres only")
    again = PostgresStore(URL, schema=store.schema)  # runs DDL a second time
    again.close()


@needs_pg
def test_import_from_sqlite(store, tmp_path, now):
    if not isinstance(store, PostgresStore):
        pytest.skip("postgres only")
    source = SqliteStore(tmp_path / "old.db")
    source.save_handle("@alpha", "UCa")
    source.save_snapshots([make_video("v1", views=100)], now - timedelta(days=1))
    source.save_report("2026-10-09", "n", [Topic("Budget apps", ["ynab"], [])])
    source.commit()

    counts = store.load(source.dump())
    store.load(source.dump())  # second import is a no-op, not an error

    assert counts == {"handles": 1, "videos": 1, "snapshots": 1, "reports": 1}
    assert store.channel_id_for_handle("@alpha") == "UCa"
    assert store.previous_snapshots(["v1"], now)["v1"].views == 100
    assert store.recent_topics("n", now - timedelta(days=7), "2026-10-10") == [["Budget apps", "ynab"]]
    source.close()
