"""One-shot sampler: fetch all providers, append one cycle of rows to the DB."""
from . import db, providers


def run_sample(fetchers=None, db_path=None, log_path=None, now=None):
    """Fetch every provider and insert one sample cycle.

    Provider failures become error rows and log lines; only true DB
    malfunctions raise. Returns the inserted rows.
    """
    fetchers = fetchers if fetchers is not None else providers.FETCHERS
    ts = db.utc_now_iso(now)
    rows = []
    for provider, fetch in fetchers.items():
        try:
            rows.extend(fetch())
        except Exception as exc:  # fetchers shouldn't raise; belt and braces
            rows.extend(providers.error_rows(provider, str(exc) or type(exc).__name__))
    conn = db.connect(db_path)
    try:
        db.insert_samples(conn, ts, rows)
    finally:
        conn.close()
    _log_errors(ts, rows, log_path)
    return rows


def _log_errors(ts, rows, log_path=None):
    lines = ["%s %s/%s: %s\n" % (ts, r["provider"], r["window"], r["error"])
             for r in rows if r["error"]]
    if not lines:
        return
    with open(log_path or db.log_file(), "a", encoding="utf-8") as f:
        f.writelines(lines)
