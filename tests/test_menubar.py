from datetime import datetime, timezone

from ai_usage_monitor import menubar


NOW = datetime(2026, 7, 17, 20, 0, 0, tzinfo=timezone.utc)


def row(ts="2026-07-17T19:55:00Z", pct=11.0, resets=None, error=None):
    return {"ts": ts, "used_percent": pct, "resets_at": resets, "error": error}


def healthy_latest():
    return {
        "claude": {
            "5h": row(pct=11.0, resets="2026-07-18T01:20:00+00:00"),
            "7d": row(pct=14.0, resets="2026-07-20T05:00:00+00:00"),
        },
        "codex": {
            "month": row(pct=25.0, resets="2026-08-01T00:00:00Z"),
        },
    }


def headline(text):
    return text.splitlines()[0]


def test_headline_shows_worst_window_per_provider():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW)
    line = headline(out)
    # Claude worst of 5h/7d is 14; Codex monthly is 25.
    assert "C 14%" in line
    assert "X 25%" in line


def test_healthy_headline_is_green_and_unmarked():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW)
    line = headline(out)
    assert "⚠" not in line
    assert "color=%s" % menubar.COLOR_OK in line


def test_high_utilization_headline_is_critical():
    latest = healthy_latest()
    latest["claude"]["7d"] = row(pct=92.0, resets="2026-07-20T05:00:00+00:00")
    line = headline(menubar.format_menubar(latest, age_minutes=5, now=NOW))
    assert "C 92%" in line
    assert "color=%s" % menubar.COLOR_CRIT in line


def test_stale_headline_is_marked_and_muted():
    line = headline(menubar.format_menubar(healthy_latest(), age_minutes=120, now=NOW))
    assert "⚠" in line
    assert "color=%s" % menubar.COLOR_MUTED in line


def test_missing_age_is_treated_as_stale():
    line = headline(menubar.format_menubar(healthy_latest(), age_minutes=None, now=NOW))
    assert "⚠" in line


def test_dropdown_lists_windows_with_reset_countdown():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW)
    assert "Claude 5-hour: 11% · resets in 5h 20m" in out
    assert "Claude 7-day: 14% · resets in 2d 9h" in out
    assert "Codex Monthly: 25% · resets in 14d 4h" in out


def test_error_window_shows_warning_and_marks_headline():
    latest = healthy_latest()
    latest["claude"]["5h"] = row(pct=None, resets=None, error="HTTP Error 401: Unauthorized")
    out = menubar.format_menubar(latest, age_minutes=5, now=NOW)
    assert "⚠" in headline(out)
    assert "Claude 5-hour: ⚠ HTTP Error 401: Unauthorized" in out


def test_dropdown_has_dashboard_link():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW)
    assert "Open dashboard | href=%s" % menubar.DASHBOARD_URL in out
    assert "---" in out


def test_empty_latest_reports_no_samples():
    out = menubar.format_menubar({}, age_minutes=None, now=NOW)
    assert "no samples" in out.lower()


def test_newest_age_minutes():
    latest = {"claude": {"5h": {"ts": "2026-07-17T19:30:00Z"}}}
    assert round(menubar.newest_age_minutes(latest, now=NOW)) == 30
    assert menubar.newest_age_minutes({}, now=NOW) is None


def test_render_menubar_plugin_wraps_the_command():
    script = menubar.render_menubar_plugin("/usr/bin/python3", "/repo/dir")
    assert script.startswith("#!/bin/sh")
    assert '"/repo/dir"' in script
    assert '"/usr/bin/python3" -m ai_usage_monitor menubar' in script


def test_swiftbar_plugin_dir_reads_the_correct_domain(monkeypatch):
    calls = {}

    class Result:
        stdout = "/Users/me/swiftbar\n"

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return Result()

    monkeypatch.setattr(menubar.subprocess, "run", fake_run)
    assert menubar.swiftbar_plugin_dir() == "/Users/me/swiftbar"
    # The SwiftBar bundle id is com.ameba.SwiftBar (not com.ambar).
    assert calls["cmd"] == ["defaults", "read", "com.ameba.SwiftBar",
                            "PluginDirectory"]
