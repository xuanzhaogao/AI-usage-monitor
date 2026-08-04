"""Fetch and parse quota usage from Claude and Codex."""
import base64
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Claude Code's own public OAuth client; the refresh grant is issued to it.
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CLAUDE_CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
USER_AGENT = "ai-usage-monitor/0.1"
REQUEST_TIMEOUT_SECONDS = 15
WINDOWS = ("5h", "7d")

# Both CLIs hold short-lived OAuth access tokens and refresh them only while
# they run, so a machine left alone overnight leaves us an expired token. A
# request with one earns a 401, and enough of those earn a 429 instead — so
# check the expiry we already have and don't send the request at all.
# For Claude we can do better than reporting it: the keychain also holds a
# refresh token, so we renew it ourselves (see refresh_claude_credentials).
EXPIRY_SKEW_SECONDS = 60
EXPIRED_CREDENTIALS_PREFIX = "credentials expired"
REFRESHED_BY = {"claude": "start Claude Code", "codex": "run Codex"}
AUTH_REJECTION_CODES = (401, 403, 429)


def expired_credentials_error(provider):
    return "%s — %s to refresh the token" % (
        EXPIRED_CREDENTIALS_PREFIX, REFRESHED_BY.get(provider, "sign in again"))


def is_expired_credentials(message):
    """True for our own pre-flight expiry error (no request was made)."""
    return bool(message) and message.startswith(EXPIRED_CREDENTIALS_PREFIX)


def is_auth_rejection(message):
    """True when the API refused our credentials, or throttled us for asking
    with bad ones too often. Neither improves by retrying soon."""
    return bool(message) and any("HTTP Error %d" % code in message
                                 for code in AUTH_REJECTION_CODES)


def _is_expired(expires_at, now=None):
    if expires_at is None:  # unknown expiry: let the request decide
        return False
    return expires_at <= (now if now is not None else time.time()) + EXPIRY_SKEW_SECONDS


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
    if isinstance(limits, dict):
        return _parse_windows("codex", limits,
                              (("5h", "primary_window"), ("7d", "secondary_window")),
                              "used_percent", "reset_at")
    spend = data.get("spend_control") if isinstance(data, dict) else None
    limit = spend.get("individual_limit") if isinstance(spend, dict) else None
    if isinstance(limit, dict) and _number(limit.get("used_percent")):
        return [{
            "provider": "codex", "window": "month",
            "used_percent": float(limit["used_percent"]),
            "resets_at": _reset_to_iso(limit.get("reset_at")),
            "error": None,
        }]
    return error_rows("codex", "no rate_limit windows or spend_control in response")


def _security(subcommand, *args):
    """Run `security` against the Claude credentials item; None on any failure."""
    try:
        out = subprocess.run(
            ["security", subcommand, "-s", KEYCHAIN_SERVICE] + list(args),
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def _keychain_oauth():
    """Return the Claude OAuth blob from the macOS Keychain, or None."""
    out = _security("find-generic-password", "-w")
    try:
        return json.loads(out).get("claudeAiOauth") or None
    except Exception:
        return None


def _keychain_attributes():
    """Return the keychain item's attribute dump (no password), or None."""
    return _security("find-generic-password")


def _parse_keychain_account(text):
    """Read the item's account attribute, needed to update it in place."""
    match = re.search(r'"acct"<blob>="([^"]*)"', text or "")
    return match.group(1) if match else None


def _store_keychain_oauth(oauth):
    """Write the blob back to the same keychain item. True when it stuck.

    `-U` updates the existing item in place rather than adding a second one,
    which needs the account it was created under — read it back rather than
    assume, since a wrong account would silently create a rival item.
    """
    account = _parse_keychain_account(_keychain_attributes())
    if not account:
        return False
    secret = json.dumps({"claudeAiOauth": oauth})
    return _security("add-generic-password", "-U", "-a", account,
                     "-w", secret) is not None


def _parse_keychain_mdat(text):
    """Read the item's modification time, which moves on every token write."""
    match = re.search(r'"mdat"<timedate>=[^\n]*?"(\d{14})Z', text or "")
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _file_changed_at(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
    except OSError:
        return None


def _claude_changed_at():
    return (_parse_keychain_mdat(_keychain_attributes())
            or _file_changed_at(CLAUDE_CREDENTIALS_PATH))


def _codex_changed_at():
    try:
        with open(CODEX_AUTH_PATH, encoding="utf-8") as f:
            stamp = json.load(f).get("last_refresh")
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except Exception:
        return _file_changed_at(CODEX_AUTH_PATH)


CREDENTIAL_CLOCKS = {"claude": _claude_changed_at, "codex": _codex_changed_at}


def credentials_changed_at(provider):
    """When this provider's stored credentials last changed, or None.

    Lets a caller tell "the same rejected token" from "signed in again", so a
    fresh login doesn't have to wait out a backoff window.
    """
    clock = CREDENTIAL_CLOCKS.get(provider)
    return clock() if clock else None


def _millis_to_epoch(value):
    return value / 1000.0 if _number(value) else None


def read_claude_credentials():
    """Return Claude's stored credentials; expires_at may be None.

    Carries the whole stored blob so a refresh can put back the fields it
    doesn't own (subscription type, scopes) untouched.
    """
    oauth = _keychain_oauth()
    stored_in_keychain = bool(oauth and oauth.get("accessToken"))
    if not stored_in_keychain:
        if not os.path.exists(CLAUDE_CREDENTIALS_PATH):
            raise RuntimeError(
                "no Claude credentials in keychain or ~/.claude/.credentials.json"
                " — is Claude Code logged in?")
        with open(CLAUDE_CREDENTIALS_PATH, encoding="utf-8") as f:
            oauth = json.load(f).get("claudeAiOauth") or {}
        if not oauth.get("accessToken"):
            raise RuntimeError("no accessToken in ~/.claude/.credentials.json")
    return {"token": oauth["accessToken"],
            "expires_at": _millis_to_epoch(oauth.get("expiresAt")),
            "refresh_token": oauth.get("refreshToken"),
            "refresh_expires_at": _millis_to_epoch(oauth.get("refreshTokenExpiresAt")),
            "in_keychain": stored_in_keychain,
            "oauth": oauth}


def refresh_claude_credentials(creds, now=None):
    """Trade Claude's refresh token for a fresh access token, or None.

    Claude Code only refreshes while it is running, so an idle machine leaves
    us holding a dead token; doing the refresh grant ourselves keeps sampling
    going overnight. The grant rotates the refresh token, so the new blob goes
    back to the keychain straight away — a rotation we stored nowhere would
    log Claude Code out.

    Returns None (caller reports the expiry as before) when there is nothing
    to refresh with or the endpoint refuses.
    """
    now = time.time() if now is None else now
    if not creds.get("refresh_token") or not creds.get("in_keychain"):
        return None
    if _is_expired(creds["refresh_expires_at"], now):
        return None  # a doomed grant is just another 401 against our quota
    if not _store_keychain_oauth(creds["oauth"]):
        # The grant rotates the refresh token, so a store we can't write to is
        # a logged-out Claude Code. Prove the write works while it's still a
        # no-op, and leave the credentials alone if it doesn't.
        return None
    try:
        granted = _post_json(CLAUDE_TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": CLAUDE_CLIENT_ID,
        })
        token = granted["access_token"]
    except Exception:
        return None
    expires_at = now + granted.get("expires_in", 0)
    oauth = dict(creds["oauth"])
    oauth["accessToken"] = token
    oauth["expiresAt"] = int(expires_at * 1000)
    if granted.get("refresh_token"):
        oauth["refreshToken"] = granted["refresh_token"]
    _store_keychain_oauth(oauth)  # rotation already happened; use it regardless
    return dict(creds, token=token, expires_at=expires_at, oauth=oauth,
                refresh_token=oauth.get("refreshToken"))


def _jwt_expiry(token):
    """Return the 'exp' claim of a JWT access token, or None if it isn't one."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None
    return exp if _number(exp) else None


def read_codex_auth():
    """Return {'token', 'account_id', 'expires_at'} for Codex."""
    if not os.path.exists(CODEX_AUTH_PATH):
        raise RuntimeError("Codex auth.json not found — is Codex CLI logged in?")
    with open(CODEX_AUTH_PATH, encoding="utf-8") as f:
        tokens = json.load(f).get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        raise RuntimeError("no access_token in ~/.codex/auth.json")
    return {"token": token, "account_id": tokens.get("account_id"),
            "expires_at": _jwt_expiry(token)}


def _get_json(url, headers):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Accept": "application/json",
                 "Content-Type": "application/json",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_claude():
    try:
        creds = read_claude_credentials()
        if _is_expired(creds["expires_at"]):
            creds = refresh_claude_credentials(creds) or creds
        if _is_expired(creds["expires_at"]):
            return error_rows("claude", expired_credentials_error("claude"))
        data = _get_json(CLAUDE_USAGE_URL, {
            "Accept": "application/json",
            "Authorization": "Bearer " + creds["token"],
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
        })
        return parse_claude(data)
    except Exception as exc:
        return error_rows("claude", str(exc) or type(exc).__name__)


def fetch_codex():
    try:
        auth = read_codex_auth()
        if _is_expired(auth["expires_at"]):
            return error_rows("codex", expired_credentials_error("codex"))
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + auth["token"],
            "User-Agent": USER_AGENT,
        }
        if auth["account_id"]:
            headers["ChatGPT-Account-Id"] = auth["account_id"]
        data = _get_json(CODEX_USAGE_URL, headers)
        return parse_codex(data)
    except Exception as exc:
        return error_rows("codex", str(exc) or type(exc).__name__)


FETCHERS = {"claude": fetch_claude, "codex": fetch_codex}
