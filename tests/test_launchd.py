import plistlib

from ai_usage_monitor import launchd
from ai_usage_monitor.__main__ import format_status


def test_render_plist_is_valid_and_complete():
    content = launchd.render_plist("/usr/bin/python3", "/repo", "/data")
    data = plistlib.loads(content.encode("utf-8"))
    assert data["Label"] == "com.ai-usage-monitor"
    assert data["ProgramArguments"] == [
        "/usr/bin/python3", "-m", "ai_usage_monitor", "sample"]
    assert data["WorkingDirectory"] == "/repo"
    assert data["StartInterval"] == 600
    assert data["RunAtLoad"] is True
    assert data["StandardOutPath"] == "/data/launchd.out.log"
    assert data["StandardErrorPath"] == "/data/launchd.err.log"


def test_format_status_reports_agent_and_readings():
    latest = {
        "claude": {
            "5h": {"ts": "2026-07-16T10:00:00Z", "used_percent": 17.0,
                   "resets_at": "2026-07-16T20:19:59Z", "error": None},
            "7d": {"ts": "2026-07-16T10:00:00Z", "used_percent": None,
                   "resets_at": None, "error": "boom"},
        },
    }
    text = format_status(latest, loaded=True)
    assert "launchd agent: loaded" in text
    assert "claude 5h : 17.0% used" in text
    assert "resets 2026-07-16T20:19:59Z" in text
    assert "claude 7d : error: boom" in text


def test_format_status_empty_db():
    text = format_status({}, loaded=False)
    assert "NOT loaded" in text
    assert "no samples recorded yet" in text


def test_render_plist_escapes_xml_special_characters():
    content = launchd.render_plist("/usr/bin/python3", "/repo/a&b", "/data/<x>")
    data = plistlib.loads(content.encode("utf-8"))
    assert data["WorkingDirectory"] == "/repo/a&b"
    assert data["StandardOutPath"] == "/data/<x>/launchd.out.log"
