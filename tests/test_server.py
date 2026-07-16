import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from ai_usage_monitor import db
from ai_usage_monitor.server import DashboardServer


def make_row(provider, window, pct, resets=None, error=None):
    return {"provider": provider, "window": window, "used_percent": pct,
            "resets_at": resets, "error": error}


@pytest.fixture
def seeded_db(tmp_path):
    path = str(tmp_path / "usage.db")
    conn = db.connect(path)
    ts = db.utc_now_iso()
    db.insert_samples(conn, ts, [
        make_row("claude", "5h", 17.0, resets="2026-07-16T20:19:59Z"),
        make_row("claude", "7d", 3.0),
        make_row("codex", "5h", None, error="boom"),
        make_row("codex", "7d", 8.1),
    ])
    conn.close()
    return path


@pytest.fixture
def base_url(seeded_db):
    server = DashboardServer(port=0, db_path=seeded_db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % server.server_address[1]
    server.shutdown()
    server.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, "", err.read()


def test_root_serves_index_html(base_url):
    status, ctype, body = get(base_url + "/")
    assert status == 200
    assert ctype.startswith("text/html")
    assert b"AI Usage Monitor" in body


def test_api_latest_reports_rows_and_fresh_age(base_url):
    status, ctype, body = get(base_url + "/api/latest")
    assert status == 200
    assert ctype.startswith("application/json")
    payload = json.loads(body)
    assert payload["latest"]["claude"]["5h"]["used_percent"] == 17.0
    assert payload["latest"]["codex"]["5h"]["error"] == "boom"
    assert payload["age_minutes"] is not None and payload["age_minutes"] < 5


def test_api_latest_empty_db(tmp_path):
    path = str(tmp_path / "empty.db")
    db.connect(path).close()
    server = DashboardServer(port=0, db_path=path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, _, body = get("http://127.0.0.1:%d/api/latest" % server.server_address[1])
        payload = json.loads(body)
        assert payload == {"latest": {}, "latest_ts": None, "age_minutes": None}
    finally:
        server.shutdown()
        server.server_close()


def test_api_history_returns_series(base_url):
    _, _, body = get(base_url + "/api/history?hours=24")
    payload = json.loads(body)
    assert payload["hours"] == 24
    series = payload["history"]["claude"]["5h"]
    assert len(series) == 1 and series[0][1] == 17.0
    assert payload["history"]["codex"]["5h"][0][1] is None


def test_api_history_clamps_hours(base_url):
    _, _, body = get(base_url + "/api/history?hours=99999")
    assert json.loads(body)["hours"] == 720
    _, _, body = get(base_url + "/api/history?hours=0")
    assert json.loads(body)["hours"] == 1


def test_api_history_rejects_non_integer_hours(base_url):
    status, _, _ = get(base_url + "/api/history?hours=abc")
    assert status == 400


def test_unknown_path_404(base_url):
    status, _, _ = get(base_url + "/nope")
    assert status == 404


def test_static_traversal_is_blocked(base_url):
    host, port = base_url.replace("http://", "").split(":")
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("GET", "/static/../__init__.py")
    assert conn.getresponse().status == 404
    conn.close()
