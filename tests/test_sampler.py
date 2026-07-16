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


def test_read_claude_token_prefers_keychain(monkeypatch):
    monkeypatch.setattr(providers, "_keychain_token", lambda: "kc-tok")
    assert providers.read_claude_token() == "kc-tok"


def test_read_claude_token_falls_back_to_credentials_file(monkeypatch, tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "file-tok"}}))
    monkeypatch.setattr(providers, "_keychain_token", lambda: None)
    monkeypatch.setattr(providers, "CLAUDE_CREDENTIALS_PATH", str(creds))
    assert providers.read_claude_token() == "file-tok"


def test_read_claude_token_missing_everywhere_raises_specific_message(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "_keychain_token", lambda: None)
    monkeypatch.setattr(providers, "CLAUDE_CREDENTIALS_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(RuntimeError, match="is Claude Code logged in"):
        providers.read_claude_token()


def test_read_claude_token_file_without_token_raises_specific_message(monkeypatch, tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {}}))
    monkeypatch.setattr(providers, "_keychain_token", lambda: None)
    monkeypatch.setattr(providers, "CLAUDE_CREDENTIALS_PATH", str(creds))
    with pytest.raises(RuntimeError, match="no accessToken"):
        providers.read_claude_token()
