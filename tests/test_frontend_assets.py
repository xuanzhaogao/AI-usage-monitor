import os
import re

from ai_usage_monitor.server import WEB_DIR


def read(name):
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_index_references_only_existing_static_assets():
    html = read("index.html")
    refs = re.findall(r'(?:src|href)="/static/([^"]+)"', html)
    assert set(refs) == {"uplot.css", "style.css", "uplot.js", "app.js"}
    for name in refs:
        assert os.path.isfile(os.path.join(WEB_DIR, name)), name


def test_index_has_expected_dom_hooks():
    html = read("index.html")
    for element_id in ["range-picker", "stale-banner",
                       "tiles-claude", "chart-claude",
                       "tiles-codex", "chart-codex"]:
        assert 'id="%s"' % element_id in html, element_id


def test_app_js_uses_the_documented_api():
    js = read("app.js")
    assert "/api/latest" in js
    assert "/api/history?hours=" in js


def test_style_declares_light_and_dark_series_colors():
    css = read("style.css")
    assert "#2a78d6" in css and "#1baf7a" in css          # light slots
    assert "#3987e5" in css and "#199e70" in css          # dark slots
    assert "#eda100" in css and "#c98500" in css          # month slot
    assert "prefers-color-scheme: dark" in css
