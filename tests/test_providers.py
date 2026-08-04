import json
import pathlib
import time
from datetime import datetime, timezone

import pytest

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


def test_parse_codex_business_plan_uses_spend_control_month_window():
    rows = providers.parse_codex(load_fixture("codex_usage_business.json"))
    assert rows == [
        {"provider": "codex", "window": "month", "used_percent": 24.0,
         "resets_at": "2026-08-01T00:00:00Z", "error": None},
    ]


def test_parse_codex_neither_shape_yields_error_rows():
    rows = providers.parse_codex({"plan_type": "business", "rate_limit": None})
    assert [r["window"] for r in rows] == ["5h", "7d"]
    assert all(r["used_percent"] is None and r["error"] for r in rows)


def test_error_rows_shape():
    rows = providers.error_rows("codex", "boom")
    assert rows == [
        {"provider": "codex", "window": "5h", "used_percent": None,
         "resets_at": None, "error": "boom"},
        {"provider": "codex", "window": "7d", "used_percent": None,
         "resets_at": None, "error": "boom"},
    ]


def test_is_auth_rejection_matches_the_codes_the_api_uses_to_refuse_us():
    assert providers.is_auth_rejection("HTTP Error 401: Unauthorized")
    assert providers.is_auth_rejection("HTTP Error 403: Forbidden")
    assert providers.is_auth_rejection("HTTP Error 429: Too Many Requests")


def test_is_auth_rejection_ignores_transient_failures():
    assert not providers.is_auth_rejection("HTTP Error 503: Service Unavailable")
    assert not providers.is_auth_rejection("<urlopen error timed out>")
    assert not providers.is_auth_rejection(None)


def test_is_expired_credentials_matches_the_local_expiry_error():
    assert providers.is_expired_credentials(providers.expired_credentials_error("claude"))
    assert not providers.is_expired_credentials("HTTP Error 401: Unauthorized")
    assert not providers.is_expired_credentials(None)


def test_expired_credentials_error_names_the_tool_that_refreshes_the_token():
    assert "Claude Code" in providers.expired_credentials_error("claude")
    assert "Codex" in providers.expired_credentials_error("codex")


KEYCHAIN_DUMP = '''keychain: "/Users/x/Library/Keychains/login.keychain-db"
class: "genp"
attributes:
    0x00000007 <blob>="Claude Code-credentials"
    "acct"<blob>="xgao"
    "cdat"<timedate>=0x32303236303430373139353733325A00  "20260407195732Z\\000"
    "mdat"<timedate>=0x32303236303830333134343732345A00  "20260803144724Z\\000"
    "svce"<blob>="Claude Code-credentials"
'''


def test_parse_keychain_mdat_reads_the_item_modification_time():
    assert providers._parse_keychain_mdat(KEYCHAIN_DUMP) == datetime(
        2026, 8, 3, 14, 47, 24, tzinfo=timezone.utc)


def test_parse_keychain_mdat_returns_none_without_a_modification_date():
    assert providers._parse_keychain_mdat('class: "genp"\n') is None


def test_parse_codex_nan_reset_becomes_none_without_raising():
    data = {"plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": 1.0, "reset_at": float("nan")},
        "secondary_window": {"used_percent": 2.0, "reset_at": 1e20},
    }}
    rows = providers.parse_codex(data)
    assert rows[0]["resets_at"] is None
    assert rows[1]["resets_at"] is None
    assert all(r["error"] is None for r in rows)


def test_parse_keychain_account_reads_the_item_account():
    assert providers._parse_keychain_account(KEYCHAIN_DUMP) == "xgao"


def test_parse_keychain_account_returns_none_without_an_account():
    assert providers._parse_keychain_account('class: "genp"\n') is None


# Anchored to the real clock: the refresh tests pin `now` explicitly, but
# fetch_claude reads it, and it must see a live refresh token behind the
# expired access token.
NOW = time.time()
HOUR_MS = 3_600_000


def stored_oauth(expires_at=None, refresh_expires_at=None):
    """A keychain blob shaped like Claude Code's, with an expired access token."""
    return {
        "accessToken": "old-access",
        "refreshToken": "old-refresh",
        "expiresAt": expires_at if expires_at is not None else int(NOW * 1000) - HOUR_MS,
        "refreshTokenExpiresAt": (refresh_expires_at if refresh_expires_at is not None
                                  else int(NOW * 1000) + 30 * 24 * HOUR_MS),
        "subscriptionType": "team",
        "scopes": ["user:inference"],
    }


def refreshing(monkeypatch, oauth, response=None, raises=None):
    """Wire up a fake keychain + token endpoint; return the recorded calls."""
    calls = {"posted": [], "stored": []}

    def fake_post(url, payload):
        calls["posted"].append((url, payload))
        if raises is not None:
            raise raises
        return response

    monkeypatch.setattr(providers, "_keychain_oauth", lambda: dict(oauth))
    monkeypatch.setattr(providers, "_post_json", fake_post)
    monkeypatch.setattr(providers, "_store_keychain_oauth",
                        lambda blob: calls["stored"].append(blob) or True)
    return calls


def test_refresh_swaps_in_the_new_token_and_stores_the_rotated_blob(monkeypatch):
    calls = refreshing(monkeypatch, stored_oauth(), response={
        "access_token": "new-access", "refresh_token": "new-refresh",
        "expires_in": 28800,
    })

    creds = providers.refresh_claude_credentials(
        providers.read_claude_credentials(), now=NOW)

    assert creds["token"] == "new-access"
    assert creds["expires_at"] == NOW + 28800
    url, payload = calls["posted"][0]
    assert url == providers.CLAUDE_TOKEN_URL
    assert payload == {"grant_type": "refresh_token",
                       "refresh_token": "old-refresh",
                       "client_id": providers.CLAUDE_CLIENT_ID}
    stored = calls["stored"][-1]
    assert stored["accessToken"] == "new-access"
    assert stored["refreshToken"] == "new-refresh"
    assert stored["expiresAt"] == int((NOW + 28800) * 1000)


def test_refresh_preserves_fields_it_does_not_own(monkeypatch):
    calls = refreshing(monkeypatch, stored_oauth(), response={
        "access_token": "new-access", "expires_in": 28800})

    providers.refresh_claude_credentials(
        providers.read_claude_credentials(), now=NOW)

    stored = calls["stored"][-1]
    assert stored["subscriptionType"] == "team"
    assert stored["scopes"] == ["user:inference"]
    # a response without refresh_token means the old one is still valid
    assert stored["refreshToken"] == "old-refresh"


def test_refresh_declines_when_the_refresh_token_itself_has_expired(monkeypatch):
    calls = refreshing(monkeypatch,
                       stored_oauth(refresh_expires_at=int(NOW * 1000) - HOUR_MS),
                       response={"access_token": "new-access", "expires_in": 28800})

    assert providers.refresh_claude_credentials(
        providers.read_claude_credentials(), now=NOW) is None
    assert calls["posted"] == []  # a doomed request is still a 401 we don't need


def test_refresh_declines_when_the_endpoint_fails(monkeypatch):
    refreshing(monkeypatch, stored_oauth(), raises=RuntimeError("HTTP Error 400"))

    assert providers.refresh_claude_credentials(
        providers.read_claude_credentials(), now=NOW) is None


def test_refresh_declines_when_the_keychain_will_not_take_a_write(monkeypatch):
    """Rotating a refresh token we then can't store logs Claude Code out."""
    calls = refreshing(monkeypatch, stored_oauth(), response={
        "access_token": "new-access", "expires_in": 28800})
    monkeypatch.setattr(providers, "_store_keychain_oauth", lambda blob: False)

    assert providers.refresh_claude_credentials(
        providers.read_claude_credentials(), now=NOW) is None
    assert calls["posted"] == []


def test_refresh_probes_the_keychain_before_rotating_anything(monkeypatch):
    calls = refreshing(monkeypatch, stored_oauth(), response={
        "access_token": "new-access", "expires_in": 28800})

    providers.refresh_claude_credentials(
        providers.read_claude_credentials(), now=NOW)

    # the probe writes the blob back unchanged, then the rotation overwrites it
    assert calls["stored"][0]["accessToken"] == "old-access"
    assert calls["stored"][-1]["accessToken"] == "new-access"


def test_fetch_claude_refreshes_an_expired_token_instead_of_erroring(monkeypatch):
    refreshing(monkeypatch, stored_oauth(), response={
        "access_token": "new-access", "expires_in": 28800})
    seen = {}

    def fake_get(url, headers):
        seen["auth"] = headers["Authorization"]
        return load_fixture("claude_usage.json")

    monkeypatch.setattr(providers, "_get_json", fake_get)

    rows = providers.fetch_claude()

    assert seen["auth"] == "Bearer new-access"
    assert all(r["error"] is None for r in rows)


def test_fetch_claude_still_reports_expiry_when_the_refresh_fails(monkeypatch):
    refreshing(monkeypatch, stored_oauth(), raises=RuntimeError("HTTP Error 400"))
    monkeypatch.setattr(providers, "_get_json", lambda url, headers: pytest.fail(
        "must not ask the usage API with a token we know is expired"))

    rows = providers.fetch_claude()

    assert all(providers.is_expired_credentials(r["error"]) for r in rows)
