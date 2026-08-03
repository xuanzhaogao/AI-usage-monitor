import json
import pathlib
from datetime import datetime, timezone

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
