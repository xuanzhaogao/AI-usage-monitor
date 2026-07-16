# AI Usage Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local app that samples Claude and Codex subscription quota usage (5-hour and 7-day windows) every 10 minutes via launchd and shows the history as time-series charts in a local web dashboard.

**Architecture:** Two independent pieces sharing a SQLite file: a one-shot sampler (`python3 -m ai_usage_monitor sample`, run by a launchd agent) and an on-demand web server (`python3 -m ai_usage_monitor serve`) that serves a static chart page plus a JSON API. Spec: `docs/superpowers/specs/2026-07-16-ai-usage-monitor-design.md`.

**Tech Stack:** Python 3.9+ stdlib only at runtime (urllib, sqlite3, http.server, argparse, plistlib). pytest for tests. Vendored uPlot 1.6.31 for charts (no CDN at runtime).

## Global Constraints

- Runtime code uses **only the Python standard library** — no pip dependencies. Tests may use pytest.
- Data directory: `os.environ.get("AI_USAGE_MONITOR_DIR") or os.path.expanduser("~/.ai-usage-monitor")`. DB file `usage.db`, sampler log `sampler.log` inside it. Tests always set `AI_USAGE_MONITOR_DIR` or pass explicit paths — never touch the real home directory.
- All timestamps are UTC strings in format `%Y-%m-%dT%H:%M:%SZ` (constant `db.TS_FORMAT`).
- The **row dict contract** used everywhere (parsers → sampler → DB):
  `{"provider": "claude"|"codex", "window": "5h"|"7d", "used_percent": float|None, "resets_at": str|None, "error": str|None}` — `ts` is added by the DB layer, one shared value per sample cycle.
- Provider fetchers never raise: any failure becomes error rows (`used_percent=None`, `error=<message>`).
- Tokens are never written to the DB, logs, or test fixtures.
- Web server binds `127.0.0.1` only, default port **8377**.
- Tests must not use the network. Run tests from the repo root: `python3 -m pytest tests/ -v`.
- launchd label: `com.ai-usage-monitor`, `StartInterval` 600.
- Never use time-bombed literal timestamps in tests — derive timestamps from `datetime.now(timezone.utc)`.
- Commit after every task; commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Verified API facts (captured 2026-07-16 from working calls / claude-dashboard source)

**Claude:** `GET https://api.anthropic.com/api/oauth/usage` with headers `Authorization: Bearer <token>`, `anthropic-beta: oauth-2025-04-20`, `Accept: application/json`. Token comes from macOS Keychain item `Claude Code-credentials` (`security find-generic-password -s "Claude Code-credentials" -w` → JSON → `claudeAiOauth.accessToken`), fallback file `~/.claude/.credentials.json` (same JSON shape). Response:

```json
{
  "five_hour": {"utilization": 17, "resets_at": "2026-07-16T20:19:59.622Z"},
  "seven_day": {"utilization": 3, "resets_at": "2026-07-20T04:59:59.622Z"},
  "seven_day_sonnet": {"utilization": 0, "resets_at": null}
}
```

`utilization` is a percent number. We use `five_hour` and `seven_day` only.

**Codex:** `GET https://chatgpt.com/backend-api/wham/usage` with headers `Authorization: Bearer <token>` and, when present, `ChatGPT-Account-Id: <account_id>`. Both values come from `~/.codex/auth.json` → `tokens.access_token` and `tokens.account_id` (the account id may be absent — the endpoint returned 200 with bearer-only on this machine). Response:

```json
{
  "plan_type": "plus",
  "rate_limit": {
    "primary_window": {"used_percent": 22.5, "window_minutes": 300, "reset_at": 1784235600},
    "secondary_window": {"used_percent": 8.1, "window_minutes": 10080, "reset_at": 1784584800}
  }
}
```

`primary_window` = 5h, `secondary_window` = 7d. `reset_at` may be epoch seconds (number) or an ISO string — the parser must accept both.

**Contingency:** if manual verification shows `chatgpt.com` returning 403 to urllib (Cloudflare disliking the UA), swap the internals of `providers._get_json` to shell out to `curl -s --max-time 15 -H ...` — the function's signature and both fetchers stay unchanged.

---

### Task 1: Package skeleton + DB module

**Files:**
- Create: `.gitignore`
- Create: `ai_usage_monitor/__init__.py`
- Create: `ai_usage_monitor/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces (used by every later task):
  - `db.TS_FORMAT: str` — `"%Y-%m-%dT%H:%M:%SZ"`
  - `db.data_dir() -> str`, `db.db_file() -> str`, `db.log_file() -> str`
  - `db.utc_now_iso(now: datetime|None = None) -> str`
  - `db.connect(path: str|None = None) -> sqlite3.Connection` — creates parent dir, schema, WAL
  - `db.insert_samples(conn, ts: str, rows: list[dict]) -> None` — rows use the row dict contract
  - `db.query_history(conn, hours: int, now: datetime|None = None) -> dict` — `{provider: {window: [[ts, used_percent|None], ...]}}`, ascending ts
  - `db.query_latest(conn) -> dict` — `{provider: {window: {"ts","used_percent","resets_at","error"}}}`

- [ ] **Step 1: Create skeleton files**

`.gitignore`:

```gitignore
__pycache__/
*.pyc
.pytest_cache/
```

`ai_usage_monitor/__init__.py`:

```python
"""Monitor Claude and Codex quota usage over time."""
```

- [ ] **Step 2: Write the failing tests**

`tests/test_db.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError` (db module missing).

- [ ] **Step 4: Implement `ai_usage_monitor/db.py`**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add .gitignore ai_usage_monitor/__init__.py ai_usage_monitor/db.py tests/test_db.py
git commit -m "feat: add SQLite storage for quota samples

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Provider response parsers

**Files:**
- Create: `ai_usage_monitor/providers.py`
- Create: `tests/fixtures/claude_usage.json`
- Create: `tests/fixtures/codex_usage.json`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: nothing from other modules (pure functions).
- Produces (used by Task 3):
  - `providers.WINDOWS: tuple` — `("5h", "7d")`
  - `providers.error_rows(provider: str, message: str) -> list[dict]` — two error rows per the row dict contract
  - `providers.parse_claude(data) -> list[dict]` — always returns exactly 2 rows (5h, 7d)
  - `providers.parse_codex(data) -> list[dict]` — always returns exactly 2 rows (5h, 7d)

- [ ] **Step 1: Create fixtures**

`tests/fixtures/claude_usage.json`:

```json
{
  "five_hour": {"utilization": 17, "resets_at": "2026-07-16T20:19:59.622Z"},
  "seven_day": {"utilization": 3, "resets_at": "2026-07-20T04:59:59.622Z"},
  "seven_day_sonnet": {"utilization": 0, "resets_at": null}
}
```

`tests/fixtures/codex_usage.json`:

```json
{
  "plan_type": "plus",
  "rate_limit": {
    "primary_window": {"used_percent": 22.5, "window_minutes": 300, "reset_at": 1784235600},
    "secondary_window": {"used_percent": 8.1, "window_minutes": 10080, "reset_at": 1784584800}
  }
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_providers.py`:

```python
import json
import pathlib

from ai_usage_monitor import providers

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_claude_returns_both_windows():
    rows = providers.parse_claude(load_fixture("claude_usage.json"))
    assert rows == [
        {"provider": "claude", "window": "5h", "used_percent": 17.0,
         "resets_at": "2026-07-16T20:19:59.622Z", "error": None},
        {"provider": "claude", "window": "7d", "used_percent": 3.0,
         "resets_at": "2026-07-20T04:59:59.622Z", "error": None},
    ]


def test_parse_claude_missing_window_becomes_error_row():
    rows = providers.parse_claude({"five_hour": {"utilization": 17, "resets_at": None}})
    assert rows[0]["error"] is None
    assert rows[1] == {"provider": "claude", "window": "7d", "used_percent": None,
                       "resets_at": None, "error": "missing or invalid 'seven_day' in response"}


def test_parse_claude_non_dict_input_yields_two_error_rows():
    rows = providers.parse_claude(None)
    assert [r["window"] for r in rows] == ["5h", "7d"]
    assert all(r["used_percent"] is None and r["error"] for r in rows)


def test_parse_codex_converts_epoch_reset_to_iso():
    rows = providers.parse_codex(load_fixture("codex_usage.json"))
    assert rows[0] == {"provider": "codex", "window": "5h", "used_percent": 22.5,
                       "resets_at": "2026-07-16T21:00:00Z", "error": None}
    assert rows[1]["window"] == "7d"
    assert rows[1]["used_percent"] == 8.1
    assert rows[1]["resets_at"] == "2026-07-20T22:00:00Z"


def test_parse_codex_accepts_iso_string_reset():
    data = {"plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": 1.0, "reset_at": "2026-07-16T21:00:00Z"},
        "secondary_window": {"used_percent": 2.0, "reset_at": None},
    }}
    rows = providers.parse_codex(data)
    assert rows[0]["resets_at"] == "2026-07-16T21:00:00Z"
    assert rows[1]["resets_at"] is None
    assert rows[1]["error"] is None


def test_parse_codex_missing_rate_limit_yields_two_error_rows():
    rows = providers.parse_codex({"plan_type": "plus"})
    assert all(r["used_percent"] is None and r["error"] for r in rows)


def test_error_rows_shape():
    rows = providers.error_rows("codex", "boom")
    assert rows == [
        {"provider": "codex", "window": "5h", "used_percent": None,
         "resets_at": None, "error": "boom"},
        {"provider": "codex", "window": "7d", "used_percent": None,
         "resets_at": None, "error": "boom"},
    ]
```

Note: `1784235600` epoch seconds is `2026-07-16T21:00:00Z` and `1784584800` is `2026-07-20T22:00:00Z` — the implementer should re-verify with `python3 -c "from datetime import datetime, timezone; print(datetime.fromtimestamp(1784235600, tz=timezone.utc))"` and correct the fixture/test pair if they disagree (adjust the epoch in the fixture, not the parser).

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: ai_usage_monitor.providers`.

- [ ] **Step 4: Implement the parsers in `ai_usage_monitor/providers.py`**

```python
"""Fetch and parse quota usage from Claude and Codex."""
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
USER_AGENT = "ai-usage-monitor/0.1"
REQUEST_TIMEOUT_SECONDS = 15
WINDOWS = ("5h", "7d")


def error_rows(provider, message):
    return [
        {"provider": provider, "window": w, "used_percent": None,
         "resets_at": None, "error": message}
        for w in WINDOWS
    ]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reset_to_iso(value):
    if _number(value):
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        return value
    return None


def _parse_windows(provider, data, window_keys, percent_key, reset_key):
    rows = []
    for window, key in window_keys:
        raw = data.get(key) if isinstance(data, dict) else None
        if isinstance(raw, dict) and _number(raw.get(percent_key)):
            rows.append({
                "provider": provider, "window": window,
                "used_percent": float(raw[percent_key]),
                "resets_at": _reset_to_iso(raw.get(reset_key)),
                "error": None,
            })
        else:
            rows.append({
                "provider": provider, "window": window, "used_percent": None,
                "resets_at": None,
                "error": "missing or invalid %r in response" % key,
            })
    return rows


def parse_claude(data):
    return _parse_windows("claude", data,
                          (("5h", "five_hour"), ("7d", "seven_day")),
                          "utilization", "resets_at")


def parse_codex(data):
    limits = data.get("rate_limit") if isinstance(data, dict) else None
    return _parse_windows("codex", limits,
                          (("5h", "primary_window"), ("7d", "secondary_window")),
                          "used_percent", "reset_at")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_providers.py -v`
Expected: all PASS. If the two epoch↔ISO assertions fail, fix the fixture epoch values per the note in Step 2.

- [ ] **Step 6: Commit**

```bash
git add ai_usage_monitor/providers.py tests/fixtures tests/test_providers.py
git commit -m "feat: parse Claude and Codex usage API responses

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Fetchers, sampler, and `sample` CLI command

**Files:**
- Modify: `ai_usage_monitor/providers.py` (append fetchers below the parsers)
- Create: `ai_usage_monitor/sampler.py`
- Create: `ai_usage_monitor/__main__.py`
- Test: `tests/test_sampler.py`

**Interfaces:**
- Consumes: `db.connect/insert_samples/utc_now_iso/log_file`, `providers.parse_*`, `providers.error_rows`.
- Produces:
  - `providers.read_claude_token() -> str` (raises RuntimeError with a specific message when credentials are missing)
  - `providers.read_codex_auth() -> tuple[str, str|None]` (token, account_id) — raises RuntimeError `"Codex auth.json not found — is Codex CLI logged in?"` when the file is absent
  - `providers._get_json(url: str, headers: dict) -> dict` (the single network touchpoint)
  - `providers.fetch_claude() -> list[dict]`, `providers.fetch_codex() -> list[dict]` — never raise
  - `providers.FETCHERS: dict[str, callable]` — `{"claude": fetch_claude, "codex": fetch_codex}`
  - `sampler.run_sample(fetchers=None, db_path=None, log_path=None, now=None) -> list[dict]`
  - `python3 -m ai_usage_monitor sample` exits 0 even when provider fetches fail
  - `main(argv=None) -> int` in `__main__.py` (Tasks 4 and 6 add subcommands to it)

- [ ] **Step 1: Write the failing tests**

`tests/test_sampler.py`:

```python
import json
import urllib.error

import pytest

from ai_usage_monitor import db, providers, sampler


def ok_rows(provider):
    return [
        {"provider": provider, "window": "5h", "used_percent": 10.0,
         "resets_at": None, "error": None},
        {"provider": provider, "window": "7d", "used_percent": 5.0,
         "resets_at": None, "error": None},
    ]


def test_run_sample_inserts_one_cycle_for_all_providers(tmp_path):
    db_path = str(tmp_path / "usage.db")
    fetchers = {"claude": lambda: ok_rows("claude"), "codex": lambda: ok_rows("codex")}
    rows = sampler.run_sample(fetchers=fetchers, db_path=db_path,
                              log_path=str(tmp_path / "sampler.log"))
    assert len(rows) == 4
    conn = db.connect(db_path)
    try:
        stored = conn.execute("SELECT DISTINCT ts FROM samples").fetchall()
        assert len(stored) == 1  # one shared ts per cycle
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 4
    finally:
        conn.close()


def test_one_provider_failing_does_not_block_the_other(tmp_path):
    db_path = str(tmp_path / "usage.db")
    log_path = str(tmp_path / "sampler.log")

    def broken():
        raise RuntimeError("kaboom")

    fetchers = {"claude": lambda: ok_rows("claude"), "codex": broken}
    sampler.run_sample(fetchers=fetchers, db_path=db_path, log_path=log_path)
    conn = db.connect(db_path)
    try:
        latest = db.query_latest(conn)
    finally:
        conn.close()
    assert latest["claude"]["5h"]["used_percent"] == 10.0
    assert latest["codex"]["5h"]["used_percent"] is None
    assert "kaboom" in latest["codex"]["5h"]["error"]


def test_errors_are_appended_to_log(tmp_path):
    log_path = tmp_path / "sampler.log"
    fetchers = {"codex": lambda: providers.error_rows("codex", "boom")}
    sampler.run_sample(fetchers=fetchers, db_path=str(tmp_path / "usage.db"),
                       log_path=str(log_path))
    content = log_path.read_text()
    assert "codex/5h: boom" in content
    assert "codex/7d: boom" in content


def test_no_log_file_written_when_all_succeed(tmp_path):
    log_path = tmp_path / "sampler.log"
    fetchers = {"claude": lambda: ok_rows("claude")}
    sampler.run_sample(fetchers=fetchers, db_path=str(tmp_path / "usage.db"),
                       log_path=str(log_path))
    assert not log_path.exists()


def test_fetch_claude_parses_injected_response(monkeypatch):
    monkeypatch.setattr(providers, "read_claude_token", lambda: "tok")
    captured = {}

    def fake_get_json(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return {"five_hour": {"utilization": 17, "resets_at": None},
                "seven_day": {"utilization": 3, "resets_at": None}}

    monkeypatch.setattr(providers, "_get_json", fake_get_json)
    rows = providers.fetch_claude()
    assert captured["url"] == providers.CLAUDE_USAGE_URL
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert rows[0]["used_percent"] == 17.0


def test_fetch_claude_network_error_returns_error_rows(monkeypatch):
    monkeypatch.setattr(providers, "read_claude_token", lambda: "tok")

    def fail(url, headers):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(providers, "_get_json", fail)
    rows = providers.fetch_claude()
    assert all(r["used_percent"] is None for r in rows)
    assert "timed out" in rows[0]["error"]


def test_fetch_codex_sends_account_id_header_when_present(monkeypatch):
    monkeypatch.setattr(providers, "read_codex_auth", lambda: ("tok", "acct-1"))
    captured = {}

    def fake_get_json(url, headers):
        captured["headers"] = headers
        return {"rate_limit": {"primary_window": {"used_percent": 1.0, "reset_at": None},
                               "secondary_window": {"used_percent": 2.0, "reset_at": None}}}

    monkeypatch.setattr(providers, "_get_json", fake_get_json)
    providers.fetch_codex()
    assert captured["headers"]["ChatGPT-Account-Id"] == "acct-1"


def test_fetch_codex_omits_account_id_header_when_absent(monkeypatch):
    monkeypatch.setattr(providers, "read_codex_auth", lambda: ("tok", None))
    captured = {}

    def fake_get_json(url, headers):
        captured["headers"] = headers
        return {"rate_limit": {"primary_window": {"used_percent": 1.0, "reset_at": None},
                               "secondary_window": {"used_percent": 2.0, "reset_at": None}}}

    monkeypatch.setattr(providers, "_get_json", fake_get_json)
    providers.fetch_codex()
    assert "ChatGPT-Account-Id" not in captured["headers"]


def test_read_codex_auth_missing_file_raises_specific_message(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "CODEX_AUTH_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(RuntimeError, match="is Codex CLI logged in"):
        providers.read_codex_auth()


def test_read_codex_auth_reads_token_and_account_id(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok", "account_id": "acct"}}))
    monkeypatch.setattr(providers, "CODEX_AUTH_PATH", str(auth))
    assert providers.read_codex_auth() == ("tok", "acct")


def test_cli_sample_exit_code_zero_even_on_fetch_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AI_USAGE_MONITOR_DIR", str(tmp_path))
    monkeypatch.setattr(providers, "FETCHERS",
                        {"claude": lambda: providers.error_rows("claude", "down")})
    from ai_usage_monitor.__main__ import main
    assert main(["sample"]) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_sampler.py -v`
Expected: FAIL — missing `sampler` module / fetch functions.

- [ ] **Step 3: Append fetchers to `ai_usage_monitor/providers.py`**

```python
def _keychain_token():
    """Return the Claude OAuth token from the macOS Keychain, or None."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def read_claude_token():
    token = _keychain_token()
    if token:
        return token
    if not os.path.exists(CLAUDE_CREDENTIALS_PATH):
        raise RuntimeError(
            "no Claude credentials in keychain or ~/.claude/.credentials.json"
            " — is Claude Code logged in?")
    with open(CLAUDE_CREDENTIALS_PATH, encoding="utf-8") as f:
        token = json.load(f).get("claudeAiOauth", {}).get("accessToken")
    if not token:
        raise RuntimeError("no accessToken in ~/.claude/.credentials.json")
    return token


def read_codex_auth():
    if not os.path.exists(CODEX_AUTH_PATH):
        raise RuntimeError("Codex auth.json not found — is Codex CLI logged in?")
    with open(CODEX_AUTH_PATH, encoding="utf-8") as f:
        tokens = json.load(f).get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise RuntimeError("no access_token in ~/.codex/auth.json")
    return token, tokens.get("account_id")


def _get_json(url, headers):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_claude():
    try:
        token = read_claude_token()
        data = _get_json(CLAUDE_USAGE_URL, {
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
        })
        return parse_claude(data)
    except Exception as exc:
        return error_rows("claude", str(exc) or type(exc).__name__)


def fetch_codex():
    try:
        token, account_id = read_codex_auth()
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": USER_AGENT,
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        data = _get_json(CODEX_USAGE_URL, headers)
        return parse_codex(data)
    except Exception as exc:
        return error_rows("codex", str(exc) or type(exc).__name__)


FETCHERS = {"claude": fetch_claude, "codex": fetch_codex}
```

- [ ] **Step 4: Create `ai_usage_monitor/sampler.py`**

```python
"""One-shot sampler: fetch all providers, append one cycle of rows to the DB."""
from . import db, providers


def run_sample(fetchers=None, db_path=None, log_path=None, now=None):
    """Fetch every provider and insert one sample cycle.

    Provider failures become error rows and log lines; only true DB
    malfunctions raise. Returns the inserted rows.
    """
    fetchers = fetchers if fetchers is not None else providers.FETCHERS
    ts = db.utc_now_iso(now)
    rows = []
    for provider, fetch in fetchers.items():
        try:
            rows.extend(fetch())
        except Exception as exc:  # fetchers shouldn't raise; belt and braces
            rows.extend(providers.error_rows(provider, str(exc) or type(exc).__name__))
    conn = db.connect(db_path)
    try:
        db.insert_samples(conn, ts, rows)
    finally:
        conn.close()
    _log_errors(ts, rows, log_path)
    return rows


def _log_errors(ts, rows, log_path=None):
    lines = ["%s %s/%s: %s\n" % (ts, r["provider"], r["window"], r["error"])
             for r in rows if r["error"]]
    if not lines:
        return
    with open(log_path or db.log_file(), "a", encoding="utf-8") as f:
        f.writelines(lines)
```

- [ ] **Step 5: Create `ai_usage_monitor/__main__.py`**

```python
"""Command-line interface."""
import argparse
import sys

from . import sampler


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m ai_usage_monitor",
        description="Monitor Claude and Codex quota usage over time.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sample", help="fetch quota status once and append it to the local DB")
    args = parser.parse_args(argv)
    if args.command == "sample":
        sampler.run_sample()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS (including Tasks 1–2 suites).

- [ ] **Step 7: Commit**

```bash
git add ai_usage_monitor/providers.py ai_usage_monitor/sampler.py ai_usage_monitor/__main__.py tests/test_sampler.py
git commit -m "feat: add provider fetchers, sampler, and sample CLI command

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Web server and JSON API

**Files:**
- Create: `ai_usage_monitor/server.py`
- Create: `ai_usage_monitor/web/index.html` (placeholder — replaced in Task 5)
- Modify: `ai_usage_monitor/__main__.py` (add `serve` subcommand)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `db.connect/query_history/query_latest/TS_FORMAT`.
- Produces:
  - `server.DEFAULT_PORT = 8377`, `server.WEB_DIR` (the package's `web/` dir)
  - `server.DashboardServer(port=DEFAULT_PORT, db_path=None)` — ThreadingHTTPServer bound to 127.0.0.1; `port=0` picks an ephemeral port (tests); real port at `server_address[1]`
  - `server.serve(port=DEFAULT_PORT, db_path=None, open_browser=False)` — blocking
  - HTTP: `GET /` → `web/index.html`; `GET /static/<name>` → file from `web/` (flat, traversal-safe); `GET /api/history?hours=N` → `{"hours": N_clamped_1_to_720, "history": {provider: {window: [[ts, pct|null], ...]}}}`; `GET /api/latest` → `{"latest": {...query_latest...}, "latest_ts": str|null, "age_minutes": float|null}`
  - CLI: `python3 -m ai_usage_monitor serve [--port N] [--open]`

- [ ] **Step 1: Write the failing tests**

`tests/test_server.py`:

```python
import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from ai_usage_monitor import db
from ai_usage_monitor.server import DashboardServer


def make_row(provider, window, pct, resets=None, error=None):
    return {"provider": provider, "window": window, "used_percent": pct,
            "resets_at": resets, "error": error}


@pytest.fixture
def seeded_db(tmp_path):
    path = str(tmp_path / "usage.db")
    conn = db.connect(path)
    ts = db.utc_now_iso()
    db.insert_samples(conn, ts, [
        make_row("claude", "5h", 17.0, resets="2026-07-16T20:19:59Z"),
        make_row("claude", "7d", 3.0),
        make_row("codex", "5h", None, error="boom"),
        make_row("codex", "7d", 8.1),
    ])
    conn.close()
    return path


@pytest.fixture
def base_url(seeded_db):
    server = DashboardServer(port=0, db_path=seeded_db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, "", err.read()


def test_root_serves_index_html(base_url):
    status, ctype, body = get(base_url + "/")
    assert status == 200
    assert ctype.startswith("text/html")
    assert b"AI Usage Monitor" in body


def test_api_latest_reports_rows_and_fresh_age(base_url):
    status, ctype, body = get(base_url + "/api/latest")
    assert status == 200
    assert ctype.startswith("application/json")
    payload = json.loads(body)
    assert payload["latest"]["claude"]["5h"]["used_percent"] == 17.0
    assert payload["latest"]["codex"]["5h"]["error"] == "boom"
    assert payload["age_minutes"] is not None and payload["age_minutes"] < 5


def test_api_latest_empty_db(tmp_path):
    path = str(tmp_path / "empty.db")
    db.connect(path).close()
    server = DashboardServer(port=0, db_path=path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, _, body = get("http://127.0.0.1:%d/api/latest" % server.server_address[1])
        payload = json.loads(body)
        assert payload == {"latest": {}, "latest_ts": None, "age_minutes": None}
    finally:
        server.shutdown()
        server.server_close()


def test_api_history_returns_series(base_url):
    _, _, body = get(base_url + "/api/history?hours=24")
    payload = json.loads(body)
    assert payload["hours"] == 24
    series = payload["history"]["claude"]["5h"]
    assert len(series) == 1 and series[0][1] == 17.0
    assert payload["history"]["codex"]["5h"][0][1] is None


def test_api_history_clamps_hours(base_url):
    _, _, body = get(base_url + "/api/history?hours=99999")
    assert json.loads(body)["hours"] == 720
    _, _, body = get(base_url + "/api/history?hours=0")
    assert json.loads(body)["hours"] == 1


def test_api_history_rejects_non_integer_hours(base_url):
    status, _, _ = get(base_url + "/api/history?hours=abc")
    assert status == 400


def test_unknown_path_404(base_url):
    status, _, _ = get(base_url + "/nope")
    assert status == 404


def test_static_traversal_is_blocked(base_url):
    host, port = base_url.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("GET", "/static/../__init__.py")
    assert conn.getresponse().status == 404
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: ai_usage_monitor.server`.

- [ ] **Step 3: Create placeholder `ai_usage_monitor/web/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>AI Usage Monitor</title></head>
<body><p>AI Usage Monitor — dashboard arrives in the frontend task.</p></body>
</html>
```

- [ ] **Step 4: Implement `ai_usage_monitor/server.py`**

```python
"""Local dashboard server: static page + JSON API over the sample DB."""
import json
import os
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
DEFAULT_PORT = 8377
MAX_HISTORY_HOURS = 720
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._serve_static("index.html")
        elif parsed.path.startswith("/static/"):
            self._serve_static(parsed.path[len("/static/"):])
        elif parsed.path == "/api/history":
            self._serve_history(urllib.parse.parse_qs(parsed.query))
        elif parsed.path == "/api/latest":
            self._serve_latest()
        else:
            self.send_error(404)

    def _serve_static(self, name):
        target = os.path.normpath(os.path.join(WEB_DIR, name))
        if os.path.dirname(target) != WEB_DIR or not os.path.isfile(target):
            self.send_error(404)
            return
        with open(target, "rb") as f:
            body = f.read()
        ext = os.path.splitext(target)[1]
        self._respond(200, CONTENT_TYPES.get(ext, "application/octet-stream"), body)

    def _serve_history(self, query):
        try:
            hours = int(query.get("hours", ["24"])[0])
        except ValueError:
            self.send_error(400, "hours must be an integer")
            return
        hours = max(1, min(hours, MAX_HISTORY_HOURS))
        conn = db.connect(self.server.db_path)
        try:
            history = db.query_history(conn, hours)
        finally:
            conn.close()
        self._respond_json({"hours": hours, "history": history})

    def _serve_latest(self):
        conn = db.connect(self.server.db_path)
        try:
            latest = db.query_latest(conn)
        finally:
            conn.close()
        newest = max((w["ts"] for p in latest.values() for w in p.values()),
                     default=None)
        age_minutes = None
        if newest:
            newest_dt = datetime.strptime(newest, db.TS_FORMAT).replace(tzinfo=timezone.utc)
            age_minutes = round(
                (datetime.now(timezone.utc) - newest_dt).total_seconds() / 60, 1)
        self._respond_json({"latest": latest, "latest_ts": newest,
                            "age_minutes": age_minutes})

    def _respond_json(self, payload):
        self._respond(200, "application/json", json.dumps(payload).encode("utf-8"))

    def _respond(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, port=DEFAULT_PORT, db_path=None):
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.db_path = db_path


def serve(port=DEFAULT_PORT, db_path=None, open_browser=False):
    server = DashboardServer(port=port, db_path=db_path)
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Dashboard: " + url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
```

- [ ] **Step 5: Add the `serve` subcommand to `ai_usage_monitor/__main__.py`**

Replace the file with:

```python
"""Command-line interface."""
import argparse
import sys

from . import sampler, server


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m ai_usage_monitor",
        description="Monitor Claude and Codex quota usage over time.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sample", help="fetch quota status once and append it to the local DB")
    p_serve = sub.add_parser("serve", help="serve the dashboard on 127.0.0.1")
    p_serve.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    p_serve.add_argument("--open", action="store_true", dest="open_browser",
                         help="open the dashboard in the default browser")
    args = parser.parse_args(argv)
    if args.command == "sample":
        sampler.run_sample()
        return 0
    if args.command == "serve":
        server.serve(port=args.port, open_browser=args.open_browser)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add ai_usage_monitor/server.py ai_usage_monitor/web/index.html ai_usage_monitor/__main__.py tests/test_server.py
git commit -m "feat: add dashboard web server with history and latest APIs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Dashboard frontend

**Files:**
- Create: `ai_usage_monitor/web/uplot.js`, `ai_usage_monitor/web/uplot.css` (vendored uPlot 1.6.31)
- Modify: `ai_usage_monitor/web/index.html` (replace placeholder)
- Create: `ai_usage_monitor/web/style.css`
- Create: `ai_usage_monitor/web/app.js`
- Test: `tests/test_frontend_assets.py`

**Interfaces:**
- Consumes: the Task 4 HTTP API exactly as specified (`/api/latest`, `/api/history?hours=N`, `/static/<name>`).
- Produces: the complete dashboard page. No later task depends on its internals.

**Design notes (dataviz-skill decisions, already validated):** series colors are categorical slots 1–2 of the reference palette — 5h = blue (`#2a78d6` light / `#3987e5` dark), 7d = aqua (`#1baf7a` light / `#199e70` dark) — validated with the dataviz palette validator in both modes (light passes with a sub-3:1 contrast WARN on aqua, which obligates visible labels: satisfied by uPlot's always-on legend with per-series cursor readout). One y-axis fixed 0–100%. Step lines 2px, no point markers, nulls render as gaps (`spanGaps: false`). Grid is hairline in the muted gridline token; all text wears ink tokens, never series colors. Staleness banner uses the reserved warning status color with a ⚠ icon + text, never color alone.

- [ ] **Step 1: Vendor uPlot (pinned 1.6.31)**

```bash
curl -fsSL -o ai_usage_monitor/web/uplot.js "https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js"
curl -fsSL -o ai_usage_monitor/web/uplot.css "https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css"
head -c 200 ai_usage_monitor/web/uplot.js
```

Expected: both files download; the js begins with a `/*! https://github.com/leeoniya/uPlot ... */` banner and is >40 KB.

- [ ] **Step 2: Write the failing test**

`tests/test_frontend_assets.py`:

```python
import os
import re

from ai_usage_monitor.server import WEB_DIR


def read(name):
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_index_references_only_existing_static_assets():
    html = read("index.html")
    refs = re.findall(r'(?:src|href)="/static/([^"]+)"', html)
    assert set(refs) == {"uplot.css", "style.css", "uplot.js", "app.js"}
    for name in refs:
        assert os.path.isfile(os.path.join(WEB_DIR, name)), name


def test_index_has_expected_dom_hooks():
    html = read("index.html")
    for element_id in ["range-picker", "stale-banner",
                       "tile-claude-5h", "tile-claude-7d", "chart-claude",
                       "tile-codex-5h", "tile-codex-7d", "chart-codex"]:
        assert 'id="%s"' % element_id in html, element_id


def test_app_js_uses_the_documented_api():
    js = read("app.js")
    assert "/api/latest" in js
    assert "/api/history?hours=" in js


def test_style_declares_light_and_dark_series_colors():
    css = read("style.css")
    assert "#2a78d6" in css and "#1baf7a" in css          # light slots
    assert "#3987e5" in css and "#199e70" in css          # dark slots
    assert "prefers-color-scheme: dark" in css
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_frontend_assets.py -v`
Expected: FAIL — index.html is still the placeholder; style.css/app.js missing.

- [ ] **Step 4: Replace `ai_usage_monitor/web/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Usage Monitor</title>
<link rel="stylesheet" href="/static/uplot.css">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<header>
  <h1>AI Usage Monitor</h1>
  <div id="range-picker" role="group" aria-label="Time range">
    <button data-hours="24" class="active">24h</button>
    <button data-hours="168">7d</button>
    <button data-hours="720">30d</button>
  </div>
</header>
<div id="stale-banner" hidden></div>
<main>
  <section class="provider">
    <h2>Claude</h2>
    <div class="tiles">
      <div class="tile" id="tile-claude-5h"></div>
      <div class="tile" id="tile-claude-7d"></div>
    </div>
    <div class="chart" id="chart-claude"></div>
  </section>
  <section class="provider">
    <h2>Codex</h2>
    <div class="tiles">
      <div class="tile" id="tile-codex-5h"></div>
      <div class="tile" id="tile-codex-7d"></div>
    </div>
    <div class="chart" id="chart-codex"></div>
  </section>
</main>
<script src="/static/uplot.js"></script>
<script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 5: Create `ai_usage_monitor/web/style.css`**

```css
:root {
  color-scheme: light dark;
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11, 11, 11, 0.10);
  --warning: #fab219;
  --series-5h: #2a78d6;
  --series-7d: #1baf7a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #0d0d0d;
    --surface: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255, 255, 255, 0.10);
    --series-5h: #3987e5;
    --series-7d: #199e70;
  }
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 1.5rem;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page);
  color: var(--text-primary);
}

header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
  margin-bottom: 1rem;
}

h1 { font-size: 1.25rem; margin: 0; }
h2 { font-size: 1rem; margin: 0 0 0.75rem; color: var(--text-secondary); }

#range-picker { display: flex; }
#range-picker button {
  font: inherit;
  padding: 0.35rem 0.9rem;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text-secondary);
  cursor: pointer;
}
#range-picker button:first-child { border-radius: 6px 0 0 6px; }
#range-picker button:last-child { border-radius: 0 6px 6px 0; }
#range-picker button.active {
  color: var(--text-primary);
  font-weight: 600;
  background: var(--grid);
}

#stale-banner {
  border: 1px solid var(--border);
  border-left: 4px solid var(--warning);
  background: var(--surface);
  color: var(--text-primary);
  padding: 0.6rem 0.9rem;
  border-radius: 6px;
  margin-bottom: 1rem;
}

main { display: grid; gap: 1.5rem; }

.provider {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1rem 1.25rem 1.25rem;
}

.tiles { display: flex; gap: 0.75rem; margin-bottom: 1rem; flex-wrap: wrap; }

.tile {
  flex: 1 1 10rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.6rem 0.9rem;
}
.tile-label { font-size: 0.8rem; color: var(--muted); }
.tile-value { font-size: 1.6rem; font-weight: 600; }
.tile-sub { font-size: 0.8rem; color: var(--text-secondary); overflow-wrap: anywhere; }

.chart { min-height: 260px; color: var(--muted); }

.u-legend { color: var(--text-secondary); font-size: 0.8rem; }
```

- [ ] **Step 6: Create `ai_usage_monitor/web/app.js`**

```js
"use strict";

const SERIES = [
  { window: "5h", label: "5-hour", cssVar: "--series-5h" },
  { window: "7d", label: "7-day", cssVar: "--series-7d" },
];
const PROVIDERS = ["claude", "codex"];
const REFRESH_MS = 60000;

const charts = {};
let currentHours = 24;

function cssColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(url + " returned " + resp.status);
  return resp.json();
}

function escapeHTML(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function formatReset(iso) {
  if (!iso) return "reset time unknown";
  const ms = Date.parse(iso) - Date.now();
  if (Number.isNaN(ms)) return "reset time unknown";
  if (ms <= 0) return "resets now";
  const totalMinutes = Math.round(ms / 60000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `resets in ${days}d ${hours}h`;
  if (hours > 0) return `resets in ${hours}h ${minutes}m`;
  return `resets in ${minutes}m`;
}

function renderTile(el, label, info) {
  const value = info && info.used_percent != null
    ? Math.round(info.used_percent) + "%"
    : "–";
  const sub = !info ? "no data"
    : info.error ? escapeHTML(info.error)
    : formatReset(info.resets_at);
  el.innerHTML =
    `<div class="tile-label">${label}</div>` +
    `<div class="tile-value">${value}</div>` +
    `<div class="tile-sub">${sub}</div>`;
}

function buildAligned(providerHistory) {
  const stamps = new Set();
  for (const s of SERIES) {
    for (const [ts] of providerHistory[s.window] || []) stamps.add(ts);
  }
  const xs = Array.from(stamps).sort();
  const index = new Map(xs.map((ts, i) => [ts, i]));
  const data = [xs.map((ts) => Date.parse(ts) / 1000)];
  for (const s of SERIES) {
    const ys = new Array(xs.length).fill(null);
    for (const [ts, pct] of providerHistory[s.window] || []) {
      ys[index.get(ts)] = pct;
    }
    data.push(ys);
  }
  return data;
}

function makeChart(el, data) {
  const axisStyle = {
    stroke: cssColor("--muted"),
    grid: { stroke: cssColor("--grid"), width: 1 },
    ticks: { stroke: cssColor("--axis"), width: 1 },
  };
  const opts = {
    width: el.clientWidth || 600,
    height: 260,
    scales: { y: { range: [0, 100] } },
    axes: [
      { ...axisStyle },
      { ...axisStyle, values: (u, vals) => vals.map((v) => v + "%") },
    ],
    series: [
      {},
      ...SERIES.map((s) => ({
        label: s.label,
        stroke: cssColor(s.cssVar),
        width: 2,
        spanGaps: false,
        points: { show: false },
        paths: uPlot.paths.stepped({ align: 1 }),
      })),
    ],
  };
  return new uPlot(opts, data, el);
}

function renderChart(provider, history) {
  const el = document.getElementById("chart-" + provider);
  const data = buildAligned(history[provider] || {});
  if (charts[provider]) {
    charts[provider].destroy();
    delete charts[provider];
  }
  el.textContent = "";
  if (data[0].length) {
    charts[provider] = makeChart(el, data);
  } else {
    el.textContent = "no samples in this range";
  }
}

function renderBanner(latest) {
  const banner = document.getElementById("stale-banner");
  if (latest.age_minutes == null) {
    banner.hidden = false;
    banner.textContent =
      "⚠ No samples yet — run “python3 -m ai_usage_monitor sample”, " +
      "then “install-agent” for continuous sampling.";
  } else if (latest.age_minutes > 30) {
    banner.hidden = false;
    banner.textContent =
      `⚠ Last sample ${Math.round(latest.age_minutes)} minutes ago — ` +
      "the sampler may not be running (run install-agent).";
  } else {
    banner.hidden = true;
  }
}

async function refresh() {
  const [latest, hist] = await Promise.all([
    fetchJSON("/api/latest"),
    fetchJSON("/api/history?hours=" + currentHours),
  ]);
  renderBanner(latest);
  for (const provider of PROVIDERS) {
    for (const s of SERIES) {
      renderTile(
        document.getElementById(`tile-${provider}-${s.window}`),
        s.label,
        (latest.latest[provider] || {})[s.window],
      );
    }
    renderChart(provider, hist.history);
  }
}

document.getElementById("range-picker").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-hours]");
  if (!button) return;
  currentHours = Number(button.dataset.hours);
  for (const b of document.querySelectorAll("#range-picker button")) {
    b.classList.toggle("active", b === button);
  }
  refresh();
});

window.addEventListener("resize", () => {
  for (const provider of PROVIDERS) {
    const chart = charts[provider];
    if (chart) {
      chart.setSize({
        width: chart.root.parentElement.clientWidth || 600,
        height: 260,
      });
    }
  }
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refresh);

refresh();
setInterval(refresh, REFRESH_MS);
```

- [ ] **Step 7: Run the tests**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 8: Visual check with demo data (dataviz "render and look" step)**

Write this throwaway script to the session scratchpad (do NOT commit it), run it, and open the printed URL:

```python
# scratchpad: seed_demo.py — seed a temp DB with 48h of fake samples and serve it
import math
import random
import tempfile
from datetime import datetime, timedelta, timezone

from ai_usage_monitor import db
from ai_usage_monitor.server import serve

path = tempfile.mktemp(suffix=".db")
conn = db.connect(path)
now = datetime.now(timezone.utc)
for i in range(288):  # 48h of 10-min cycles
    t = now - timedelta(minutes=10 * (287 - i))
    ts = db.utc_now_iso(t)
    five = (i * 1.4) % 100
    seven = min(99, i / 3.2)
    rows = [
        {"provider": "claude", "window": "5h", "used_percent": five,
         "resets_at": None, "error": None},
        {"provider": "claude", "window": "7d", "used_percent": seven,
         "resets_at": None, "error": None},
        {"provider": "codex", "window": "5h",
         "used_percent": None if 100 < i < 120 else (i * 0.9) % 100,
         "resets_at": None,
         "error": "simulated outage" if 100 < i < 120 else None},
        {"provider": "codex", "window": "7d",
         "used_percent": min(99, 40 + 30 * math.sin(i / 40) + random.random()),
         "resets_at": None, "error": None},
    ]
    db.insert_samples(conn, ts, rows)
conn.close()
serve(port=0, db_path=path, open_browser=True)
```

Run: `python3 <scratchpad>/seed_demo.py` (from the repo root).
Check, in both light and dark OS themes: step lines with visible drops, a gap (not a zero line) during the simulated codex outage, legend under each chart, tiles populated, range buttons switch 24h/7d/30d, no label collisions or horizontal page scroll. Fix anything broken before committing. Stop the server with Ctrl-C.

- [ ] **Step 9: Commit**

```bash
git add ai_usage_monitor/web tests/test_frontend_assets.py
git commit -m "feat: add dashboard frontend with uPlot step charts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: launchd agent + status command

**Files:**
- Create: `ai_usage_monitor/launchd.py`
- Modify: `ai_usage_monitor/__main__.py` (add `install-agent`, `uninstall-agent`, `status`; add `format_status`)
- Test: `tests/test_launchd.py`

**Interfaces:**
- Consumes: `db.data_dir/connect/query_latest`.
- Produces:
  - `launchd.PLIST_LABEL = "com.ai-usage-monitor"`
  - `launchd.plist_path() -> str` — `~/Library/LaunchAgents/com.ai-usage-monitor.plist`
  - `launchd.render_plist(python: str, repo_root: str, data_dir: str) -> str`
  - `launchd.install_agent() -> int`, `launchd.uninstall_agent() -> int`, `launchd.agent_loaded() -> bool`
  - `__main__.format_status(latest: dict, loaded: bool) -> str` (pure, testable)
  - CLI: `install-agent`, `uninstall-agent`, `status`

- [ ] **Step 1: Write the failing tests**

`tests/test_launchd.py`:

```python
import plistlib

from ai_usage_monitor import launchd
from ai_usage_monitor.__main__ import format_status


def test_render_plist_is_valid_and_complete():
    content = launchd.render_plist("/usr/bin/python3", "/repo", "/data")
    data = plistlib.loads(content.encode("utf-8"))
    assert data["Label"] == "com.ai-usage-monitor"
    assert data["ProgramArguments"] == [
        "/usr/bin/python3", "-m", "ai_usage_monitor", "sample"]
    assert data["WorkingDirectory"] == "/repo"
    assert data["StartInterval"] == 600
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"] == "/data/launchd.out.log"
    assert data["StandardErrorPath"] == "/data/launchd.err.log"


def test_format_status_reports_agent_and_readings():
    latest = {
        "claude": {
            "5h": {"ts": "2026-07-16T10:00:00Z", "used_percent": 17.0,
                   "resets_at": "2026-07-16T20:19:59Z", "error": None},
            "7d": {"ts": "2026-07-16T10:00:00Z", "used_percent": None,
                   "resets_at": None, "error": "boom"},
        },
    }
    text = format_status(latest, loaded=True)
    assert "launchd agent: loaded" in text
    assert "claude 5h : 17.0% used" in text
    assert "resets 2026-07-16T20:19:59Z" in text
    assert "claude 7d : error: boom" in text


def test_format_status_empty_db():
    text = format_status({}, loaded=False)
    assert "NOT loaded" in text
    assert "no samples recorded yet" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_launchd.py -v`
Expected: FAIL — `ModuleNotFoundError: ai_usage_monitor.launchd`.

- [ ] **Step 3: Implement `ai_usage_monitor/launchd.py`**

```python
"""Install/uninstall the macOS launchd sampling agent."""
import os
import subprocess
import sys

from . import db

PLIST_LABEL = "com.ai-usage-monitor"


def plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % PLIST_LABEL)


def render_plist(python, repo_root, data_dir):
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>ai_usage_monitor</string>
    <string>sample</string>
  </array>
  <key>WorkingDirectory</key><string>{repo_root}</string>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{data_dir}/launchd.out.log</string>
  <key>StandardErrorPath</key><string>{data_dir}/launchd.err.log</string>
</dict>
</plist>
""".format(label=PLIST_LABEL, python=python, repo_root=repo_root, data_dir=data_dir)


def install_agent():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(db.data_dir(), exist_ok=True)
    path = plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_plist(sys.executable, repo_root, db.data_dir()))
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    result = subprocess.run(["launchctl", "load", path],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("launchctl load failed: " + result.stderr.strip(), file=sys.stderr)
        return 1
    print("Installed launchd agent %s (samples every 10 minutes)." % PLIST_LABEL)
    print("Plist: " + path)
    return 0


def uninstall_agent():
    path = plist_path()
    if not os.path.exists(path):
        print("agent not installed (no plist at %s)" % path)
        return 0
    subprocess.run(["launchctl", "unload", path], capture_output=True)
    os.remove(path)
    print("Removed " + path)
    return 0


def agent_loaded():
    result = subprocess.run(["launchctl", "list", PLIST_LABEL], capture_output=True)
    return result.returncode == 0
```

- [ ] **Step 4: Extend `ai_usage_monitor/__main__.py`**

Replace the file with:

```python
"""Command-line interface."""
import argparse
import sys

from . import db, launchd, sampler, server


def format_status(latest, loaded):
    lines = ["launchd agent: " +
             ("loaded" if loaded else "NOT loaded (run install-agent)")]
    if not latest:
        lines.append("no samples recorded yet — run: python3 -m ai_usage_monitor sample")
        return "\n".join(lines)
    for provider in sorted(latest):
        for window in ("5h", "7d"):
            info = latest[provider].get(window)
            if info is None:
                continue
            if info["error"]:
                lines.append("%s %-3s: error: %s (at %s)"
                             % (provider, window, info["error"], info["ts"]))
            else:
                lines.append("%s %-3s: %.1f%% used, resets %s (sampled %s)"
                             % (provider, window, info["used_percent"],
                                info["resets_at"] or "?", info["ts"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m ai_usage_monitor",
        description="Monitor Claude and Codex quota usage over time.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sample", help="fetch quota status once and append it to the local DB")
    p_serve = sub.add_parser("serve", help="serve the dashboard on 127.0.0.1")
    p_serve.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    p_serve.add_argument("--open", action="store_true", dest="open_browser",
                         help="open the dashboard in the default browser")
    sub.add_parser("install-agent", help="install the launchd agent (samples every 10 min)")
    sub.add_parser("uninstall-agent", help="unload and remove the launchd agent")
    sub.add_parser("status", help="print the latest readings and agent state")
    args = parser.parse_args(argv)

    if args.command == "sample":
        sampler.run_sample()
        return 0
    if args.command == "serve":
        server.serve(port=args.port, open_browser=args.open_browser)
        return 0
    if args.command == "install-agent":
        return launchd.install_agent()
    if args.command == "uninstall-agent":
        return launchd.uninstall_agent()
    if args.command == "status":
        conn = db.connect()
        try:
            latest = db.query_latest(conn)
        finally:
            conn.close()
        print(format_status(latest, launchd.agent_loaded()))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add ai_usage_monitor/launchd.py ai_usage_monitor/__main__.py tests/test_launchd.py
git commit -m "feat: add launchd agent install/uninstall and status command

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: README + end-to-end verification with real credentials

**Files:**
- Create: `README.md`

This task runs against the real APIs and the real launchd — it must run on the user's machine session (not a sandboxed/denied-network context). If any live call fails, report the exact error; the 403 contingency in "Verified API facts" applies.

- [ ] **Step 1: Write `README.md`**

```markdown
# AI Usage Monitor

Tracks how much of your Claude and Codex subscription rate limits (5-hour and
7-day windows) you've used, sampled every 10 minutes, with a local web
dashboard showing the history. macOS only; Python 3.9+ stdlib only.

## Quick start

```bash
# take one sample now (reads Claude keychain credentials + ~/.codex/auth.json)
python3 -m ai_usage_monitor sample

# see the latest readings
python3 -m ai_usage_monitor status

# open the dashboard
python3 -m ai_usage_monitor serve --open

# sample automatically every 10 minutes (launchd agent)
python3 -m ai_usage_monitor install-agent
```

Run everything from this repo's root directory.

## How it works

- `sample` fetches quota status from `api.anthropic.com/api/oauth/usage`
  (Claude Code's OAuth token) and `chatgpt.com/backend-api/wham/usage`
  (Codex CLI's token) and appends one row per provider/window to SQLite.
- Data lives in `~/.ai-usage-monitor/` (`usage.db`, `sampler.log`); override
  with the `AI_USAGE_MONITOR_DIR` env var.
- `serve` hosts the dashboard on `http://127.0.0.1:8377` (localhost only).
- Tokens are read at sample time and never stored or logged. Fetch failures
  are recorded as gaps, never fake zeros.

## Uninstall

```bash
python3 -m ai_usage_monitor uninstall-agent
rm -rf ~/.ai-usage-monitor
```

## Development

```bash
python3 -m pytest tests/ -v
```

Design docs: `docs/superpowers/specs/` and `docs/superpowers/plans/`.
```

- [ ] **Step 2: Full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 3: Live sample (real credentials, real network)**

```bash
python3 -m ai_usage_monitor sample && python3 -m ai_usage_monitor status
```

Expected: `status` prints four readings with real percentages and no `error:` lines (agent NOT loaded yet is fine). If codex shows an error, apply the 403/curl contingency from "Verified API facts" and re-run.

- [ ] **Step 4: Live dashboard check**

```bash
python3 -m ai_usage_monitor serve --port 8378 &
sleep 1 && curl -s http://127.0.0.1:8378/api/latest | python3 -m json.tool
```

Expected: JSON with the just-sampled values and small `age_minutes`. Then stop the background server (`kill %1`). Ask the user to open the dashboard and confirm it renders (or open it for them with `serve --open`).

- [ ] **Step 5: Install the agent and verify a scheduled run**

```bash
python3 -m ai_usage_monitor install-agent
launchctl list com.ai-usage-monitor
launchctl start com.ai-usage-monitor && sleep 5
python3 -m ai_usage_monitor status
```

Expected: agent listed; after `start`, `status` shows a fresh sample timestamp (RunAtLoad also fires one on install). Check `~/.ai-usage-monitor/launchd.err.log` is empty or absent.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: add README with quick start and uninstall

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
