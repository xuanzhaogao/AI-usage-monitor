# AI Usage Monitor — Design

**Date:** 2026-07-16
**Status:** Approved

## Purpose

A simple local app that tracks how much of the Claude and Codex subscription
rate limits (the 5-hour and 7-day quota windows) have been used, sampled over
time, and displays the history as time-series charts in a local web dashboard.

The goal is to answer questions like "how close did I get to the cap this
week?" and "when does my window reset?" — not to compute token counts or
dollar costs.

## Chosen approach

Cron-style sampler + on-demand viewer (Approach A):

- A one-shot **sampler** run by a macOS launchd agent every 10 minutes.
  It fetches quota status from both providers, appends rows to SQLite, and
  exits. Always-on footprint is a ~1-second process per cycle.
- An on-demand **web server** the user starts when they want to look at the
  dashboard. It reads the same SQLite file.

Rejected alternatives:

- **Single always-running daemon** — permanently resident process for
  something glanced at occasionally; one bug stops both sampling and viewing.
- **Reusing the claude-dashboard plugin's check-usage.js as data source** —
  depends on third-party plugin internals; its Codex checker is currently
  broken (the underlying API works fine when queried directly).

## Architecture

Single Python package, **stdlib only** (urllib, sqlite3, http.server,
argparse, json, subprocess). No pip dependencies.

```
AI-usage-monitor/
├── ai_usage_monitor/
│   ├── __main__.py     # CLI: sample | serve | install-agent | uninstall-agent | status
│   ├── providers.py    # fetch Claude + Codex quota snapshots
│   ├── db.py           # SQLite storage + queries
│   ├── server.py       # local web server + JSON API
│   └── web/            # index.html + vendored chart JS (no CDN, works offline)
├── tests/
└── README.md
```

### CLI subcommands

| Command | Behavior |
|---|---|
| `python3 -m ai_usage_monitor sample` | One-shot: fetch both providers, append rows to DB, exit. This is what launchd runs. |
| `python3 -m ai_usage_monitor serve [--port 8377] [--open]` | Serve the dashboard on `127.0.0.1` (default port 8377). `--open` opens the browser. |
| `python3 -m ai_usage_monitor install-agent` | Write `~/Library/LaunchAgents/com.ai-usage-monitor.plist` (StartInterval 600, RunAtLoad true) pointing at this checkout's sampler, then `launchctl` load it. |
| `python3 -m ai_usage_monitor uninstall-agent` | Unload and remove the plist. |
| `python3 -m ai_usage_monitor status` | Print latest reading per provider/window, sample age, and whether the launchd agent is loaded. |

## Data model

SQLite at `~/.ai-usage-monitor/usage.db` (directory created on first use).
One long-format table:

```sql
CREATE TABLE IF NOT EXISTS samples (
  ts           TEXT NOT NULL,   -- ISO8601 UTC, sample cycle time (same for all rows of a cycle)
  provider     TEXT NOT NULL,   -- 'claude' | 'codex'
  window       TEXT NOT NULL,   -- '5h' | '7d'
  used_percent REAL,            -- NULL if fetch failed
  resets_at    TEXT,            -- ISO8601 UTC when this window resets; NULL if unknown
  error        TEXT             -- NULL on success, short message on failure
);
CREATE INDEX IF NOT EXISTS idx_samples ON samples (provider, window, ts);
```

Each sample cycle appends four rows (claude/codex × 5h/7d). Failed fetches
store `used_percent = NULL` plus an error string so charts show honest gaps,
never fake zeros. At 10-minute sampling this is ~52k rows/year; no retention
policy is needed.

## Data sources & credentials

| Provider | Credential source | Endpoint |
|---|---|---|
| Claude | macOS Keychain item `Claude Code-credentials` via `security find-generic-password -s "Claude Code-credentials" -w` (JSON, `claudeAiOauth.accessToken`); fallback `~/.claude/.credentials.json` | `GET https://api.anthropic.com/api/oauth/usage` with `Authorization: Bearer <token>` |
| Codex | `~/.codex/auth.json` → `tokens.access_token` | `GET https://chatgpt.com/backend-api/wham/usage` with `Authorization: Bearer <token>` |

Both endpoints verified working on this machine on 2026-07-16. Exact response
schemas are captured as fixtures during implementation; parsers normalize both
into the same snapshot shape (`used_percent`, `resets_at` per window).

**Token policy:** tokens are read fresh at each sample, used for one request,
and never written to the DB or logs. The app never refreshes tokens itself —
on a 401 it records an error row; tokens refresh naturally when the user uses
each CLI. If a provider's credentials file/keychain item is missing, the error
message says so specifically (e.g., "Codex auth.json not found — is Codex CLI
logged in?").

## Web server & API

Stdlib `http.server`, bound to `127.0.0.1` only.

| Route | Response |
|---|---|
| `/` | `index.html` (static, from `ai_usage_monitor/web/`) |
| `/api/history?hours=N` | JSON: per provider/window, arrays of `[ts, used_percent]` covering the last N hours (default 24, allowed up to 720) |
| `/api/latest` | JSON: most recent row per provider/window, including `resets_at`, `error`, and sample age |

The server does not sample; it only reads the DB. The DB uses SQLite WAL
mode so the server's reads never block the sampler's (single, small) inserts.

## Dashboard UI

Single static page, vendored chart library (no CDN), designed per the dataviz
skill at implementation time:

- **Status tiles** at top: current 5h and 7d used-percent per provider, each
  with time-until-reset derived from `resets_at`.
- **One time-series chart per provider** below, both windows as lines, with a
  range picker (24h / 7d / 30d). Step-style interpolation so quota resets
  render as clean vertical drops. NULL samples render as gaps.
- **Staleness warning** if the newest sample is older than 30 minutes
  ("sampler not running? run install-agent").
- Light and dark theme both supported.

## Error handling

- The sampler never lets one provider's failure affect the other: each fetch
  is independent, with a 15-second timeout, wrapped so any exception becomes
  an error row plus a line in `~/.ai-usage-monitor/sampler.log`.
- The sampler's exit code is 0 even when fetches fail (launchd shouldn't
  treat quota-API blips as job failures); it exits nonzero only on true
  programming/DB errors.
- The dashboard surfaces the latest error string per provider in its status
  tile so a persistently broken provider is visible, not silent.

## Testing

`pytest`, no network access in tests:

- **Parsers:** provider response parsing against fixture JSON captured from
  the real APIs, including malformed/missing-field cases.
- **DB:** insert + range-query round-trips against a temp-file DB.
- **API:** start the server on a random port with a seeded temp DB; assert
  `/api/history` and `/api/latest` payloads via `http.client`.
- **Fetch isolation:** a provider fetcher that raises produces an error row
  and does not prevent the other provider's row.

Manual verification checklist (not unit-testable): keychain/auth.json reads
with real credentials, launchd agent install/load/unload, browser rendering
of the dashboard.

## Out of scope (YAGNI)

- Token/cost accounting from session logs
- Gemini, z.ai, or other providers
- Notifications/alerts when nearing the cap
- Multi-machine sync or remote access (server is localhost-only)
- Packaging/distribution (runs from this checkout)
