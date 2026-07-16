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
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OverflowError, OSError):
            return None
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
