import os
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


def test_headline_shows_claude_5h_claude_7d_and_codex():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW)
    # Claude 5h 11, Claude 7d 14, Codex monthly 25.
    assert headline(out) == "C·11-14 X·25"


def test_headline_uses_codex_worst_window_and_marks_missing_values():
    latest = healthy_latest()
    del latest["claude"]["7d"]
    latest["codex"] = {"5h": row(pct=30.0), "7d": row(pct=42.0)}
    assert headline(menubar.format_menubar(latest, age_minutes=5, now=NOW)) \
        == "C·11-? X·42"


def test_headline_is_uncolored_even_when_critical():
    latest = healthy_latest()
    latest["claude"]["7d"] = row(pct=92.0, resets="2026-07-20T05:00:00+00:00")
    assert headline(menubar.format_menubar(latest, age_minutes=5, now=NOW)) \
        == "C·11-92 X·25"


def test_stale_headline_is_marked():
    line = headline(menubar.format_menubar(healthy_latest(), age_minutes=120, now=NOW))
    assert line == "⚠ C·11-14 X·25"


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


def test_refresh_button_runs_a_sample_via_the_plugin():
    out = menubar.format_menubar(
        healthy_latest(), age_minutes=5, now=NOW,
        plugin_path="/Users/me/swiftbar/aiusage.1m.sh")
    line = [ln for ln in out.splitlines() if "Refresh now" in ln][0]
    assert 'bash="/Users/me/swiftbar/aiusage.1m.sh"' in line
    assert "param1=sample" in line
    assert "terminal=false" in line
    assert "refresh=true" in line


def test_no_refresh_button_without_plugin_path():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW)
    assert "Refresh now" not in out


PLUGIN = "/Users/me/swiftbar/aiusage.1m.sh"
REPO_8BIT = "lmstudio-community/Qwen3.8-27B-MLX-8bit"
REPO_6BIT = "lmstudio-community/Qwen3.8-27B-MLX-6bit"


def local(state="stopped", model=None, memory_gb=None, proxy=False,
          models=(REPO_6BIT, REPO_8BIT)):
    return {"status": {"state": state, "model": model, "memory_gb": memory_gb,
                       "proxy": proxy},
            "models": list(models)}


def test_no_local_model_section_by_default():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW,
                                 plugin_path=PLUGIN)
    assert "Local model" not in out


def test_running_local_model_shows_model_memory_and_stop():
    out = menubar.format_menubar(
        healthy_latest(), age_minutes=5, now=NOW, plugin_path=PLUGIN,
        local=local("running", REPO_8BIT, memory_gb=38.2, proxy=True))
    assert "Local model: ● running · MLX-8bit · 38 GB | color=#2b5d9b,#8ab4f8 refresh=true" in out.splitlines()
    stop = [ln for ln in out.splitlines() if "Stop local model" in ln][0]
    assert 'bash="%s" param1=local-model param2=stop' % PLUGIN in stop
    assert "refresh=true" in stop
    assert "Start local model" not in out
    # Local-model status never changes the quota headline.
    assert headline(out) == "C·11-14 X·25"


def test_running_without_proxy_is_flagged():
    out = menubar.format_menubar(
        healthy_latest(), age_minutes=5, now=NOW, plugin_path=PLUGIN,
        local=local("running", REPO_8BIT, proxy=False))
    assert "Local model: ● running · MLX-8bit · ⚠ proxy down | color=#2b5d9b,#8ab4f8 refresh=true" \
        in out.splitlines()


def test_starting_local_model_can_be_stopped():
    out = menubar.format_menubar(
        healthy_latest(), age_minutes=5, now=NOW, plugin_path=PLUGIN,
        local=local("starting", REPO_6BIT))
    assert "Local model: ◐ starting · MLX-6bit" in out
    assert "Stop local model" in out


def test_stopped_local_model_offers_each_downloaded_model():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW,
                                 plugin_path=PLUGIN, local=local())
    lines = out.splitlines()
    assert "Local model: ○ stopped | color=#2b5d9b,#8ab4f8 refresh=true" in lines
    start = lines.index("▶ Start local model")
    assert lines[start + 1].startswith("--MLX-6bit | ")
    assert "param2=start param3=%s" % REPO_6BIT in lines[start + 1]
    assert lines[start + 2].startswith("--MLX-8bit | ")
    assert "Stop local model" not in out


def test_stopped_without_downloaded_models():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW,
                                 plugin_path=PLUGIN, local=local(models=()))
    assert "--No downloaded models" in out


def test_local_model_actions_need_plugin_path():
    out = menubar.format_menubar(healthy_latest(), age_minutes=5, now=NOW,
                                 local=local())
    assert "Local model: ○ stopped" in out
    assert "Start local model" not in out


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


def test_render_menubar_plugin_handles_sample_argument():
    script = menubar.render_menubar_plugin("/usr/bin/python3", "/repo/dir")
    # Invoked as `<plugin> sample`, the wrapper runs a fresh sample instead
    # of printing the menu-bar output.
    assert '"$1" = "sample"' in script
    assert '"/usr/bin/python3" -m ai_usage_monitor sample' in script


def test_render_menubar_plugin_bakes_local_model_dir():
    assert "AI_USAGE_LOCAL_MODEL_DIR" not in menubar.render_menubar_plugin(
        "/usr/bin/python3", "/repo/dir")
    script = menubar.render_menubar_plugin("/usr/bin/python3", "/repo/dir",
                                           "/models/qwen")
    assert 'export AI_USAGE_LOCAL_MODEL_DIR="/models/qwen"' in script
    assert '"$1" = "local-model"' in script
    assert '"/usr/bin/python3" -m ai_usage_monitor "$@"' in script


def test_install_rejects_local_model_dir_without_start_script(tmp_path, monkeypatch):
    monkeypatch.setattr(menubar, "swiftbar_plugin_dir", lambda: str(tmp_path))
    assert menubar.install_menubar(str(tmp_path)) == 1
    assert not list(tmp_path.glob("aiusage.*.sh"))


def test_install_replaces_stale_interval_variant(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "swiftbar"
    plugin_dir.mkdir()
    (plugin_dir / "aiusage.5m.sh").write_text("old 5-minute plugin")
    monkeypatch.setattr(menubar, "swiftbar_plugin_dir", lambda: str(plugin_dir))
    monkeypatch.setattr(menubar.subprocess, "run", lambda *a, **k: None)

    assert menubar.install_menubar() == 0

    remaining = sorted(p.name for p in plugin_dir.glob("aiusage.*.sh"))
    assert remaining == [menubar.PLUGIN_NAME]  # only the new 1m plugin
    assert menubar.PLUGIN_NAME == "aiusage.1m.sh"
    assert os.access(str(plugin_dir / menubar.PLUGIN_NAME), os.X_OK)


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


def test_informational_dropdown_lines_are_not_greyed_out():
    # SwiftBar disables (greys out) lines without an action, so informational
    # lines get a harmless refresh action plus their own tint; clickable
    # items keep the system text color.
    out = menubar.format_menubar(
        healthy_latest(), age_minutes=5, now=NOW, plugin_path=PLUGIN,
        local=local("running", REPO_8BIT, memory_gb=38.2, proxy=True))
    lines = out.splitlines()
    for text in ("Claude 5-hour: 11% · resets in 5h 20m",
                 "Codex Monthly: 25% · resets in 14d 4h",
                 "Local model: ● running · MLX-8bit · 38 GB",
                 "Last sample 5 min ago"):
        assert "%s | color=#2b5d9b,#8ab4f8 refresh=true" % text in lines
    for ln in lines:
        if "bash=" in ln or "href=" in ln:
            assert "color=" not in ln
