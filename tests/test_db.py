import os
from datetime import datetime, timedelta, timezone

import pytest

from ai_usage_monitor import db


def make_row(provider="claude", window="5h", pct=17.0,
             resets="2026-07-16T20:19:59Z", error=None):
    return {"provider": provider, "window": window, "used_percent": pct,
            "resets_at": resets, "error": error}


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "nested" / "usage.db"))
    yield connection
    connection.close()


def test_connect_creates_parent_dir_schema_and_wal(tmp_path):
    path = tmp_path / "nested" / "usage.db"
    connection = db.connect(str(path))
    try:
        assert path.exists()
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        cols = [r[1] for r in connection.execute("PRAGMA table_info(samples)")]
        assert cols == ["ts", "provider", "window", "used_percent", "resets_at", "error"]
    finally:
        connection.close()


def test_insert_and_history_round_trip(conn):
    ts = db.utc_now_iso()
    db.insert_samples(conn, ts, [make_row(), make_row(window="7d", pct=3.0)])
    history = db.query_history(conn, hours=24)
    assert history == {"claude": {"5h": [[ts, 17.0]], "7d": [[ts, 3.0]]}}


def test_history_excludes_rows_older_than_cutoff(conn):
    now = datetime.now(timezone.utc)
    old_ts = db.utc_now_iso(now - timedelta(hours=48))
    new_ts = db.utc_now_iso(now)
    db.insert_samples(conn, old_ts, [make_row(pct=50.0)])
    db.insert_samples(conn, new_ts, [make_row(pct=10.0)])
    history = db.query_history(conn, hours=24, now=now)
    assert history == {"claude": {"5h": [[new_ts, 10.0]]}}


def test_history_orders_ascending_and_keeps_null_percent(conn):
    now = datetime.now(timezone.utc)
    t1 = db.utc_now_iso(now - timedelta(hours=2))
    t2 = db.utc_now_iso(now - timedelta(hours=1))
    db.insert_samples(conn, t2, [make_row(pct=None, resets=None, error="boom")])
    db.insert_samples(conn, t1, [make_row(pct=5.0)])
    history = db.query_history(conn, hours=24, now=now)
    assert history["claude"]["5h"] == [[t1, 5.0], [t2, None]]


def test_latest_returns_most_recent_row_per_provider_window(conn):
    now = datetime.now(timezone.utc)
    t1 = db.utc_now_iso(now - timedelta(hours=1))
    t2 = db.utc_now_iso(now)
    db.insert_samples(conn, t1, [make_row(pct=5.0), make_row(provider="codex", pct=1.0)])
    db.insert_samples(conn, t2, [make_row(pct=9.0)])
    latest = db.query_latest(conn)
    assert latest["claude"]["5h"] == {"ts": t2, "used_percent": 9.0,
                                      "resets_at": "2026-07-16T20:19:59Z", "error": None}
    assert latest["codex"]["5h"]["ts"] == t1


def test_latest_empty_db_returns_empty_dict(conn):
    assert db.query_latest(conn) == {}


def test_data_dir_honors_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_USAGE_MONITOR_DIR", str(tmp_path / "custom"))
    assert db.data_dir() == str(tmp_path / "custom")
    assert db.db_file() == str(tmp_path / "custom" / "usage.db")
    assert db.log_file() == str(tmp_path / "custom" / "sampler.log")
