"""Local dashboard server: static page + JSON API over the sample DB."""
import json
import os
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
DEFAULT_PORT = 8377
MAX_HISTORY_HOURS = 720
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._serve_static("index.html")
        elif parsed.path.startswith("/static/"):
            self._serve_static(parsed.path[len("/static/"):])
        elif parsed.path == "/api/history":
            self._serve_history(urllib.parse.parse_qs(parsed.query))
        elif parsed.path == "/api/latest":
            self._serve_latest()
        else:
            self.send_error(404)

    def _serve_static(self, name):
        target = os.path.normpath(os.path.join(WEB_DIR, name))
        if os.path.dirname(target) != WEB_DIR or not os.path.isfile(target):
            self.send_error(404)
            return
        with open(target, "rb") as f:
            body = f.read()
        ext = os.path.splitext(target)[1]
        self._respond(200, CONTENT_TYPES.get(ext, "application/octet-stream"), body)

    def _serve_history(self, query):
        try:
            hours = int(query.get("hours", ["24"])[0])
        except ValueError:
            self.send_error(400, "hours must be an integer")
            return
        hours = max(1, min(hours, MAX_HISTORY_HOURS))
        conn = db.connect(self.server.db_path)
        try:
            history = db.query_history(conn, hours)
        finally:
            conn.close()
        self._respond_json({"hours": hours, "history": history})

    def _serve_latest(self):
        conn = db.connect(self.server.db_path)
        try:
            latest = db.query_latest(conn)
        finally:
            conn.close()
        newest = max((w["ts"] for p in latest.values() for w in p.values()),
                     default=None)
        age_minutes = None
        if newest:
            newest_dt = datetime.strptime(newest, db.TS_FORMAT).replace(tzinfo=timezone.utc)
            age_minutes = round(
                (datetime.now(timezone.utc) - newest_dt).total_seconds() / 60, 1)
        self._respond_json({"latest": latest, "latest_ts": newest,
                            "age_minutes": age_minutes})

    def _respond_json(self, payload):
        self._respond(200, "application/json", json.dumps(payload).encode("utf-8"))

    def _respond(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, port=DEFAULT_PORT, db_path=None):
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.db_path = db_path


def serve(port=DEFAULT_PORT, db_path=None, open_browser=False):
    server = DashboardServer(port=port, db_path=db_path)
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Dashboard: " + url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
