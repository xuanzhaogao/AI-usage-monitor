# AI Usage Monitor

Tracks how much of your Claude and Codex subscription quotas you've used
(5-hour and 7-day rate-limit windows, or the monthly spend budget on Codex
business plans), sampled every 10 minutes, with a local web dashboard showing
the history. macOS only; Python 3.9+ stdlib only.

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

Run everything from this repo's root directory. `install-agent` records the
current `python3` path and this repo's location in the launchd job — if you
move the repo or your Python is upgraded/removed, re-run
`python3 -m ai_usage_monitor install-agent`.

## Menu-bar widget (optional)

Show the quota percentages in the macOS menu bar via
[SwiftBar](https://github.com/swiftbar/SwiftBar):

```bash
# 1. install SwiftBar and launch it once, then pick a plugin folder when asked
brew install --cask swiftbar

# 2. drop the plugin into that folder (baked with this repo's python + path)
python3 -m ai_usage_monitor install-menubar
```

The menu bar shows each provider's most-used window (`C 14% · X 25%`, colored
green/orange/red by severity, ⚠ when stale or erroring); the dropdown lists
every window with reset countdowns and a link to the dashboard. SwiftBar
refreshes it every minute by reading the local database — no network calls.
`python3 -m ai_usage_monitor menubar` prints the raw output; remove the plugin
with `uninstall-menubar`. Like `install-agent`, the plugin bakes in the current
python path and repo location, so re-run `install-menubar` if either changes.

## How it works

- `sample` fetches quota status from `api.anthropic.com/api/oauth/usage`
  (Claude Code's OAuth token) and `chatgpt.com/backend-api/wham/usage`
  (Codex CLI's token) and appends one row per provider/window to SQLite.
- Data lives in `~/.ai-usage-monitor/` (`usage.db`, `sampler.log`); override
  with the `AI_USAGE_MONITOR_DIR` env var.
- `serve` hosts the dashboard on `http://127.0.0.1:8377` (localhost only).
- Tokens are read at sample time and never stored or logged. A failed fetch
  is retried once after 20 s (launchd often ticks the moment the Mac wakes,
  before Wi-Fi is back); persistent failures are recorded as gaps, never
  fake zeros.
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
