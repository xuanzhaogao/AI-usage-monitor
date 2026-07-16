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
- Codex business accounts have no 5h/7d windows; their monthly spend-control
  budget is tracked as a single "Monthly" series instead.

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
