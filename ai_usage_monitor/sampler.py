"""One-shot sampler: fetch all providers, append one cycle of rows to the DB."""
import time

from . import db, providers

RETRY_DELAY_SECONDS = 20


def run_sample(fetchers=None, db_path=None, log_path=None, now=None):
    """Fetch every provider and insert one sample cycle.

    Provider failures become error rows and log lines; only true DB
    malfunctions raise. Returns the inserted rows.
    """
    fetchers = fetchers if fetchers is not None else providers.FETCHERS
    ts = db.utc_now_iso(now)
    conn = db.connect(db_path)
    rows = []
    try:
        for provider, fetch in fetchers.items():
            provider_rows = _fetch_once(provider, fetch)
            if all(r["error"] for r in provider_rows):
                # launchd often ticks the instant the Mac wakes, before the
                # network is back; one delayed retry absorbs those blips.
                time.sleep(RETRY_DELAY_SECONDS)
                provider_rows = _fetch_once(provider, fetch)
            rows.extend(_attribute_error_windows(conn, provider, provider_rows))
        db.insert_samples(conn, ts, rows)
    finally:
        conn.close()
    _log_errors(ts, rows, log_path)
    return rows


def _fetch_once(provider, fetch):
    try:
        return fetch()
    except Exception as exc:  # fetchers shouldn't raise; belt and braces
        return providers.error_rows(provider, str(exc) or type(exc).__name__)


def _attribute_error_windows(conn, provider, provider_rows):
    """Remap an all-error cycle onto the provider's last-known windows.

    A full fetch failure can't know which windows this account actually has
    (e.g. business Codex accounts have only 'month'), so error rows default
    to WINDOWS; recording them under stale window names would pollute
    query_latest forever. Partial results keep their window names.
    """
    if not provider_rows or not all(r["error"] for r in provider_rows):
        return provider_rows
    known = _last_known_windows(conn, provider)
    if not known:
        return provider_rows
    message = provider_rows[0]["error"]
    return [{"provider": provider, "window": w, "used_percent": None,
             "resets_at": None, "error": message} for w in known]


def _last_known_windows(conn, provider):
    newest = conn.execute(
        "SELECT MAX(ts) FROM samples WHERE provider = ? AND error IS NULL",
        (provider,)).fetchone()[0]
    if not newest:
        return None
    cur = conn.execute(
        "SELECT DISTINCT window FROM samples"
        " WHERE provider = ? AND ts = ? AND error IS NULL",
        (provider, newest))
    return [r[0] for r in cur] or None


def _log_errors(ts, rows, log_path=None):
    lines = ["%s %s/%s: %s\n" % (ts, r["provider"], r["window"], r["error"])
             for r in rows if r["error"]]
    if not lines:
        return
    with open(log_path or db.log_file(), "a", encoding="utf-8") as f:
        f.writelines(lines)
