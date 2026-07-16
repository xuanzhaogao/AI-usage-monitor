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
