"""SQLite storage for quota samples."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts           TEXT NOT NULL,
  provider     TEXT NOT NULL,
  window       TEXT NOT NULL,
  used_percent REAL,
  resets_at    TEXT,
  error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples ON samples (provider, window, ts);
-- One row per provider recording the last cycle that actually called the API.
-- Cycles skipped by a backoff still write samples, so sample timestamps can't
-- tell us when we last asked.
CREATE TABLE IF NOT EXISTS attempts (
  provider TEXT PRIMARY KEY,
  ts       TEXT NOT NULL,
  error    TEXT
);
"""


def data_dir():
    return os.environ.get("AI_USAGE_MONITOR_DIR") or os.path.expanduser("~/.ai-usage-monitor")


def db_file():
    return os.path.join(data_dir(), "usage.db")


def log_file():
    return os.path.join(data_dir(), "sampler.log")


def utc_now_iso(now=None):
    return (now or datetime.now(timezone.utc)).strftime(TS_FORMAT)


def connect(path=None):
    path = path or db_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def insert_samples(conn, ts, rows):
    conn.executemany(
        "INSERT INTO samples (ts, provider, window, used_percent, resets_at, error)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(ts, r["provider"], r["window"], r["used_percent"], r["resets_at"], r["error"])
         for r in rows],
    )
    conn.commit()


def record_attempt(conn, provider, ts, error):
    """Remember when we last called this provider's API, and how it went."""
    conn.execute(
        "INSERT INTO attempts (provider, ts, error) VALUES (?, ?, ?)"
        " ON CONFLICT(provider) DO UPDATE SET ts = excluded.ts, error = excluded.error",
        (provider, ts, error))
    conn.commit()


def last_attempt(conn, provider):
    """Return (ts, error) of the last real fetch, or None if never attempted."""
    row = conn.execute("SELECT ts, error FROM attempts WHERE provider = ?",
                       (provider,)).fetchone()
    return (row[0], row[1]) if row else None


def query_history(conn, hours, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).strftime(TS_FORMAT)
    cur = conn.execute(
        "SELECT ts, provider, window, used_percent FROM samples"
        " WHERE ts >= ? ORDER BY ts",
        (cutoff,),
    )
    history = {}
    for ts, provider, window, pct in cur:
        history.setdefault(provider, {}).setdefault(window, []).append([ts, pct])
    return history


def query_latest(conn):
    cur = conn.execute(
        "SELECT s.ts, s.provider, s.window, s.used_percent, s.resets_at, s.error"
        " FROM samples s"
        " JOIN (SELECT provider, window, MAX(ts) AS mts FROM samples"
        "       GROUP BY provider, window) m"
        " ON s.provider = m.provider AND s.window = m.window AND s.ts = m.mts"
    )
    latest = {}
    for ts, provider, window, pct, resets_at, error in cur:
        latest.setdefault(provider, {})[window] = {
            "ts": ts, "used_percent": pct, "resets_at": resets_at, "error": error,
        }
    return latest
