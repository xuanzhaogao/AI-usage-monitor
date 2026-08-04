import base64
import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from ai_usage_monitor import db, providers, sampler


def live_claude_creds():
    return {"token": "tok", "expires_at": time.time() + 3600}


def jwt_expiring_at(epoch_seconds):
    """A JWT-shaped token whose payload carries only an 'exp' claim."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": epoch_seconds}).encode()).rstrip(b"=").decode()
    return "header.%s.signature" % payload


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
    monkeypatch.setattr(providers, "read_claude_credentials", live_claude_creds)
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
    monkeypatch.setattr(providers, "read_claude_credentials", live_claude_creds)

    def fail(url, headers):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(providers, "_get_json", fail)
    rows = providers.fetch_claude()
    assert all(r["used_percent"] is None for r in rows)
    assert "timed out" in rows[0]["error"]


def test_fetch_codex_sends_account_id_header_when_present(monkeypatch):
    monkeypatch.setattr(providers, "read_codex_auth",
                        lambda: {"token": "tok", "account_id": "acct-1",
                                 "expires_at": time.time() + 3600})
    captured = {}

    def fake_get_json(url, headers):
        captured["headers"] = headers
        return {"rate_limit": {"primary_window": {"used_percent": 1.0, "reset_at": None},
                               "secondary_window": {"used_percent": 2.0, "reset_at": None}}}

    monkeypatch.setattr(providers, "_get_json", fake_get_json)
    providers.fetch_codex()
    assert captured["headers"]["ChatGPT-Account-Id"] == "acct-1"


def test_fetch_codex_omits_account_id_header_when_absent(monkeypatch):
    monkeypatch.setattr(providers, "read_codex_auth",
                        lambda: {"token": "tok", "account_id": None,
                                 "expires_at": None})
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
    assert providers.read_codex_auth() == {"token": "tok", "account_id": "acct",
                                           "expires_at": None}


def test_read_codex_auth_extracts_expiry_from_the_access_token_jwt(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": jwt_expiring_at(1786000000)}}))
    monkeypatch.setattr(providers, "CODEX_AUTH_PATH", str(auth))
    assert providers.read_codex_auth()["expires_at"] == 1786000000


def test_fetch_codex_skips_the_request_when_the_token_has_expired(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(
        {"tokens": {"access_token": jwt_expiring_at(int(time.time()) - 10)}}))
    monkeypatch.setattr(providers, "CODEX_AUTH_PATH", str(auth))
    monkeypatch.setattr(providers, "_get_json", _no_request_expected)
    rows = providers.fetch_codex()
    assert all(r["error"] == providers.expired_credentials_error("codex") for r in rows)


def _no_request_expected(url, headers):
    raise AssertionError("no HTTP request should be made with expired credentials")


def test_fetch_claude_skips_the_request_when_the_token_has_expired(monkeypatch):
    monkeypatch.setattr(providers, "read_claude_credentials",
                        lambda: {"token": "tok", "expires_at": time.time() - 10})
    monkeypatch.setattr(providers, "_get_json", _no_request_expected)
    rows = providers.fetch_claude()
    assert [r["window"] for r in rows] == ["5h", "7d"]
    assert all(r["error"] == providers.expired_credentials_error("claude") for r in rows)


def test_fetch_claude_treats_a_token_expiring_within_the_skew_as_expired(monkeypatch):
    monkeypatch.setattr(
        providers, "read_claude_credentials",
        lambda: {"token": "tok",
                 "expires_at": time.time() + providers.EXPIRY_SKEW_SECONDS - 5})
    monkeypatch.setattr(providers, "_get_json", _no_request_expected)
    assert providers.is_expired_credentials(providers.fetch_claude()[0]["error"])


def test_fetch_claude_requests_when_the_expiry_is_unknown(monkeypatch):
    monkeypatch.setattr(providers, "read_claude_credentials",
                        lambda: {"token": "tok", "expires_at": None})
    monkeypatch.setattr(providers, "_get_json", lambda url, headers: {
        "five_hour": {"utilization": 4, "resets_at": None},
        "seven_day": {"utilization": 2, "resets_at": None}})
    assert providers.fetch_claude()[0]["used_percent"] == 4.0


def month_rows():
    return [{"provider": "codex", "window": "month", "used_percent": 24.0,
             "resets_at": None, "error": None}]


def test_error_rows_remap_to_last_known_windows(tmp_path):
    db_path = str(tmp_path / "usage.db")
    log_path = str(tmp_path / "sampler.log")
    sampler.run_sample(fetchers={"codex": month_rows}, db_path=db_path, log_path=log_path)

    def broken():
        raise RuntimeError("blip")

    sampler.run_sample(fetchers={"codex": broken}, db_path=db_path, log_path=log_path)
    sampler.run_sample(fetchers={"codex": month_rows}, db_path=db_path, log_path=log_path)
    conn = db.connect(db_path)
    try:
        latest = db.query_latest(conn)
        windows = conn.execute(
            "SELECT DISTINCT window FROM samples WHERE provider='codex'").fetchall()
    finally:
        conn.close()
    assert list(latest["codex"].keys()) == ["month"]
    assert latest["codex"]["month"]["used_percent"] == 24.0
    assert windows == [("month",)]  # the blip cycle recorded a month gap, not 5h/7d


def test_error_rows_keep_default_windows_without_history(tmp_path):
    db_path = str(tmp_path / "usage.db")

    def broken():
        raise RuntimeError("first-ever sample fails")

    sampler.run_sample(fetchers={"codex": broken}, db_path=db_path,
                       log_path=str(tmp_path / "sampler.log"))
    conn = db.connect(db_path)
    try:
        latest = db.query_latest(conn)
    finally:
        conn.close()
    assert sorted(latest["codex"].keys()) == ["5h", "7d"]


def test_partial_parse_errors_keep_their_window_names(tmp_path):
    db_path = str(tmp_path / "usage.db")
    log_path = str(tmp_path / "sampler.log")
    sampler.run_sample(fetchers={"codex": month_rows}, db_path=db_path, log_path=log_path)
    partial = [{"provider": "codex", "window": "5h", "used_percent": 1.0,
                "resets_at": None, "error": None},
               {"provider": "codex", "window": "7d", "used_percent": None,
                "resets_at": None, "error": "missing window"}]
    sampler.run_sample(fetchers={"codex": lambda: partial}, db_path=db_path, log_path=log_path)
    conn = db.connect(db_path)
    try:
        latest = db.query_latest(conn)
    finally:
        conn.close()
    assert latest["codex"]["5h"]["used_percent"] == 1.0
    assert latest["codex"]["7d"]["error"] == "missing window"


def test_cli_sample_exit_code_zero_even_on_fetch_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AI_USAGE_MONITOR_DIR", str(tmp_path))
    monkeypatch.setattr(providers, "FETCHERS",
                        {"claude": lambda: providers.error_rows("claude", "down")})
    from ai_usage_monitor.__main__ import main
    assert main(["sample"]) == 0


def test_read_claude_credentials_prefers_keychain(monkeypatch):
    monkeypatch.setattr(providers, "_keychain_oauth",
                        lambda: {"accessToken": "kc-tok", "expiresAt": 1786000000000,
                                 "refreshToken": "kc-refresh"})
    creds = providers.read_claude_credentials()
    assert creds["token"] == "kc-tok"
    assert creds["expires_at"] == 1786000000.0
    assert creds["refresh_token"] == "kc-refresh"
    assert creds["in_keychain"]


def test_read_claude_credentials_falls_back_to_credentials_file(monkeypatch, tmp_path):
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "file-tok"}}))
    monkeypatch.setattr(providers, "_keychain_oauth", lambda: None)
    monkeypatch.setattr(providers, "CLAUDE_CREDENTIALS_PATH", str(creds_file))
    creds = providers.read_claude_credentials()
    assert creds["token"] == "file-tok"
    assert creds["expires_at"] is None
    # not in_keychain: we have nowhere to put a rotated refresh token, so the
    # refresh is skipped rather than risk losing it
    assert not creds["in_keychain"]


def test_refresh_is_skipped_for_file_stored_credentials(monkeypatch):
    monkeypatch.setattr(providers, "_post_json", lambda url, payload: pytest.fail(
        "must not rotate a refresh token we cannot store"))
    creds = {"token": "file-tok", "expires_at": None, "refresh_token": "r",
             "refresh_expires_at": None, "in_keychain": False, "oauth": {}}
    assert providers.refresh_claude_credentials(creds) is None


def test_read_claude_credentials_missing_everywhere_raises_specific_message(
        monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "_keychain_oauth", lambda: None)
    monkeypatch.setattr(providers, "CLAUDE_CREDENTIALS_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(RuntimeError, match="is Claude Code logged in"):
        providers.read_claude_credentials()


def test_read_claude_credentials_file_without_token_raises_specific_message(
        monkeypatch, tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {}}))
    monkeypatch.setattr(providers, "_keychain_oauth", lambda: None)
    monkeypatch.setattr(providers, "CLAUDE_CREDENTIALS_PATH", str(creds))
    with pytest.raises(RuntimeError, match="no accessToken"):
        providers.read_claude_credentials()


def test_all_error_fetch_is_retried_once_after_delay(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(sampler.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("dns not ready")
        return ok_rows("claude")

    rows = sampler.run_sample(fetchers={"claude": flaky},
                              db_path=str(tmp_path / "usage.db"),
                              log_path=str(tmp_path / "sampler.log"))
    assert calls["n"] == 2
    assert sleeps == [sampler.RETRY_DELAY_SECONDS]
    assert all(r["error"] is None for r in rows)


def test_successful_fetch_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler.time, "sleep",
                        lambda s: (_ for _ in ()).throw(AssertionError("no sleep expected")))
    calls = {"n": 0}

    def ok():
        calls["n"] += 1
        return ok_rows("claude")

    sampler.run_sample(fetchers={"claude": ok},
                       db_path=str(tmp_path / "usage.db"),
                       log_path=str(tmp_path / "sampler.log"))
    assert calls["n"] == 1


def counting_fetcher(rows):
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return rows
    return fetch, calls


def test_auth_rejection_is_not_retried_after_the_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler.time, "sleep",
                        lambda s: (_ for _ in ()).throw(AssertionError("no sleep expected")))
    fetch, calls = counting_fetcher(
        providers.error_rows("claude", "HTTP Error 401: Unauthorized"))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=str(tmp_path / "usage.db"),
                       log_path=str(tmp_path / "sampler.log"))
    assert calls["n"] == 1


def test_expired_credentials_are_not_retried_after_the_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler.time, "sleep",
                        lambda s: (_ for _ in ()).throw(AssertionError("no sleep expected")))
    fetch, calls = counting_fetcher(
        providers.error_rows("claude", providers.expired_credentials_error("claude")))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=str(tmp_path / "usage.db"),
                       log_path=str(tmp_path / "sampler.log"))
    assert calls["n"] == 1


def test_auth_rejection_skips_the_fetch_on_the_next_cycle(tmp_path, monkeypatch):
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(providers, "credentials_changed_at", lambda provider: None)
    fetch, calls = counting_fetcher(
        providers.error_rows("claude", "HTTP Error 429: Too Many Requests"))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=db_path, log_path=log_path,
                       now=start)
    rows = sampler.run_sample(fetchers={"claude": fetch}, db_path=db_path,
                              log_path=log_path, now=start + timedelta(minutes=10))
    assert calls["n"] == 1  # second cycle never hit the API
    assert all(r["error"] == "HTTP Error 429: Too Many Requests" for r in rows)


def test_fetching_resumes_once_the_backoff_window_passes(tmp_path):
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    fetch, calls = counting_fetcher(
        providers.error_rows("claude", "HTTP Error 429: Too Many Requests"))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=db_path, log_path=log_path,
                       now=start)
    later = start + timedelta(minutes=sampler.AUTH_BACKOFF_MINUTES + 1)
    sampler.run_sample(fetchers={"claude": lambda: ok_rows("claude")}, db_path=db_path,
                       log_path=log_path, now=later)
    conn = db.connect(db_path)
    try:
        assert db.query_latest(conn)["claude"]["5h"]["used_percent"] == 10.0
    finally:
        conn.close()


def test_backoff_expires_even_though_skipped_cycles_keep_recording(tmp_path, monkeypatch):
    """The window is measured from the last real attempt, not the last row."""
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    monkeypatch.setattr(providers, "credentials_changed_at", lambda provider: None)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    rejected, rejections = counting_fetcher(
        providers.error_rows("claude", "HTTP Error 429: Too Many Requests"))
    for minutes in (0, 10, 20, 31):
        sampler.run_sample(fetchers={"claude": rejected}, db_path=db_path,
                           log_path=log_path, now=start + timedelta(minutes=minutes))
    assert rejections["n"] == 2  # asked at 12:00 and again at 12:31, not in between


def test_a_fresh_rejection_re_arms_the_backoff_window(tmp_path, monkeypatch):
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    monkeypatch.setattr(providers, "credentials_changed_at", lambda provider: None)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    rejected, rejections = counting_fetcher(
        providers.error_rows("claude", "HTTP Error 429: Too Many Requests"))
    for minutes in (0, 31, 41):
        sampler.run_sample(fetchers={"claude": rejected}, db_path=db_path,
                           log_path=log_path, now=start + timedelta(minutes=minutes))
    assert rejections["n"] == 2  # 12:41 is inside the window re-armed at 12:31


def test_backoff_is_dropped_when_credentials_change_after_a_skipped_cycle(
        tmp_path, monkeypatch):
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(providers, "credentials_changed_at", lambda provider: None)
    rejected, _ = counting_fetcher(
        providers.error_rows("claude", "HTTP Error 401: Unauthorized"))
    sampler.run_sample(fetchers={"claude": rejected}, db_path=db_path,
                       log_path=log_path, now=start)
    sampler.run_sample(fetchers={"claude": rejected}, db_path=db_path,
                       log_path=log_path, now=start + timedelta(minutes=10))
    monkeypatch.setattr(providers, "credentials_changed_at",
                        lambda provider: start + timedelta(minutes=15))
    fetch, calls = counting_fetcher(ok_rows("claude"))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=db_path, log_path=log_path,
                       now=start + timedelta(minutes=20))
    assert calls["n"] == 1


def test_backoff_is_dropped_once_the_credentials_change(tmp_path, monkeypatch):
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    rejected, _ = counting_fetcher(
        providers.error_rows("claude", "HTTP Error 401: Unauthorized"))
    sampler.run_sample(fetchers={"claude": rejected}, db_path=db_path,
                       log_path=log_path, now=start)
    monkeypatch.setattr(providers, "credentials_changed_at",
                        lambda provider: start + timedelta(minutes=5))
    fetch, calls = counting_fetcher(ok_rows("claude"))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=db_path, log_path=log_path,
                       now=start + timedelta(minutes=10))
    assert calls["n"] == 1  # signed in again, so don't wait out the window
    conn = db.connect(db_path)
    try:
        assert db.query_latest(conn)["claude"]["5h"]["used_percent"] == 10.0
    finally:
        conn.close()


def test_backoff_holds_when_the_credentials_predate_the_failure(tmp_path, monkeypatch):
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    rejected, _ = counting_fetcher(
        providers.error_rows("claude", "HTTP Error 401: Unauthorized"))
    sampler.run_sample(fetchers={"claude": rejected}, db_path=db_path,
                       log_path=log_path, now=start)
    monkeypatch.setattr(providers, "credentials_changed_at",
                        lambda provider: start - timedelta(hours=2))
    fetch, calls = counting_fetcher(ok_rows("claude"))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=db_path, log_path=log_path,
                       now=start + timedelta(minutes=10))
    assert calls["n"] == 0


def test_claude_credentials_changed_at_prefers_the_keychain_item_time(monkeypatch):
    monkeypatch.setattr(providers, "_keychain_attributes",
                        lambda: '"mdat"<timedate>=0x00  "20260803144724Z\\000"')
    assert providers.credentials_changed_at("claude") == datetime(
        2026, 8, 3, 14, 47, 24, tzinfo=timezone.utc)


def test_claude_credentials_changed_at_falls_back_to_the_file_mtime(monkeypatch, tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    monkeypatch.setattr(providers, "_keychain_attributes", lambda: None)
    monkeypatch.setattr(providers, "CLAUDE_CREDENTIALS_PATH", str(creds))
    changed = providers.credentials_changed_at("claude")
    assert abs((changed - datetime.now(timezone.utc)).total_seconds()) < 60


def test_claude_credentials_changed_at_is_none_when_nothing_is_readable(
        monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "_keychain_attributes", lambda: None)
    monkeypatch.setattr(providers, "CLAUDE_CREDENTIALS_PATH", str(tmp_path / "absent.json"))
    assert providers.credentials_changed_at("claude") is None


def test_codex_credentials_changed_at_uses_last_refresh(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"last_refresh": "2026-08-03T15:08:12.719643Z",
                                "tokens": {"access_token": "tok"}}))
    monkeypatch.setattr(providers, "CODEX_AUTH_PATH", str(auth))
    assert providers.credentials_changed_at("codex") == datetime(
        2026, 8, 3, 15, 8, 12, 719643, tzinfo=timezone.utc)


def test_codex_credentials_changed_at_falls_back_to_the_file_mtime(monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "tok"}}))
    monkeypatch.setattr(providers, "CODEX_AUTH_PATH", str(auth))
    changed = providers.credentials_changed_at("codex")
    assert abs((changed - datetime.now(timezone.utc)).total_seconds()) < 60


def test_expired_credentials_skip_the_next_cycle(tmp_path, monkeypatch):
    """An expiry now costs a refresh grant, so a failing one must not repeat
    every 10 minutes — that is the 401-into-429 escalation we already fixed."""
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(providers, "credentials_changed_at", lambda provider: None)
    expired = providers.error_rows("claude", providers.expired_credentials_error("claude"))
    sampler.run_sample(fetchers={"claude": lambda: expired}, db_path=db_path,
                       log_path=log_path, now=start)
    fetch, calls = counting_fetcher(ok_rows("claude"))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=db_path, log_path=log_path,
                       now=start + timedelta(minutes=10))
    assert calls["n"] == 0


def test_expired_credentials_retry_as_soon_as_the_token_is_rewritten(
        tmp_path, monkeypatch):
    """A successful refresh — ours or Claude Code's — moves the keychain's
    modification time, which releases the backoff immediately."""
    db_path, log_path = str(tmp_path / "usage.db"), str(tmp_path / "sampler.log")
    start = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(providers, "credentials_changed_at", lambda provider: None)
    expired = providers.error_rows("claude", providers.expired_credentials_error("claude"))
    sampler.run_sample(fetchers={"claude": lambda: expired}, db_path=db_path,
                       log_path=log_path, now=start)
    monkeypatch.setattr(providers, "credentials_changed_at",
                        lambda provider: start + timedelta(minutes=5))
    fetch, calls = counting_fetcher(ok_rows("claude"))
    sampler.run_sample(fetchers={"claude": fetch}, db_path=db_path, log_path=log_path,
                       now=start + timedelta(minutes=10))
    assert calls["n"] == 1


def test_retry_that_also_fails_records_error_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler.time, "sleep", lambda s: None)

    def broken():
        raise RuntimeError("still down")

    rows = sampler.run_sample(fetchers={"claude": broken},
                              db_path=str(tmp_path / "usage.db"),
                              log_path=str(tmp_path / "sampler.log"))
    assert all("still down" in r["error"] for r in rows)
