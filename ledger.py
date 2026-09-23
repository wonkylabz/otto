"""The tier-call ledger (issue #130): one row per cheap-tier model call — routing, clarify, the
write-intent classifiers, verify/supervise samples, plan, memory GC.

An audit row exists only for an execution attempt, so the calls that decide what a run DOES left
no record beyond an aggregate cost. `gateway.complete` writes a row per call and the caller
annotates it with the parsed outcome via `gateway.decided`.

Telemetry, not the trail: its own table (never rows in `audit`, whose denominators `scorecard`
depends on), TTL'd like transcripts. It holds the DECISION only, never the prompt or the reply —
transcripts already carry that content, scrubbed and TTL'd, and this must not be a second copy.

Best-effort throughout: a ledger that can't write must never fail the tier call it describes.
"""
import contextlib
import datetime
import sqlite3
import time

import config
import privacy
import storage

DECISION_CHARS = 80
_GC_EVERY_S = 3600
_last_gc = [0.0]


def schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS tier_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        wid TEXT,
        tier TEXT NOT NULL,
        model TEXT,
        duration_ms INTEGER,
        cost_usd REAL,
        tokens INTEGER,
        ok INTEGER NOT NULL,
        fell_back INTEGER,
        error TEXT,
        decision TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tier_calls_at ON tier_calls(at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tier_calls_wid ON tier_calls(wid)")


@contextlib.contextmanager
def _conn():
    conn = storage.sqlite_connect(config.DB_PATH)
    try:
        schema(conn)
        yield conn
    finally:
        conn.close()


def _clean(text, limit):
    if text is None:
        return None
    text = " ".join(str(text).split())
    text = privacy.redact(text) or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def record(*, wid, tier, model, duration_ms, cost_usd=0, tokens=0, ok=True, fell_back=False,
           error=None):
    """Append one call; returns its row id, or None if the ledger couldn't be written."""
    at = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        with _conn() as conn:
            cur = conn.execute(
                "INSERT INTO tier_calls (at, wid, tier, model, duration_ms, cost_usd, tokens, ok,"
                " fell_back, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (at, wid, tier, model, int(duration_ms), float(cost_usd or 0), int(tokens or 0),
                 1 if ok else 0, 1 if fell_back else 0, _clean(error, 200)))
            row = cur.lastrowid
        _maybe_gc()
        return row
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


def decide(row_id, value):
    """Attach the caller's parsed outcome to a row. Redacted and clipped: it is short by design."""
    if row_id is None or value is None:
        return
    try:
        with _conn() as conn:
            conn.execute("UPDATE tier_calls SET decision = ? WHERE id = ?",
                         (_clean(value, DECISION_CHARS), row_id))
    except (sqlite3.Error, OSError):
        pass


def calls(wid=None, tier=None, limit=200):
    """Newest-first rows as dicts, optionally filtered by run and/or tier."""
    where, args = [], []
    if wid:
        where.append("wid = ?")
        args.append(wid)
    if tier:
        where.append("tier = ?")
        args.append(tier)
    sql = "SELECT * FROM tier_calls" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY id DESC LIMIT ?"
    try:
        with _conn() as conn:
            return [dict(r) for r in conn.execute(sql, (*args, int(limit))).fetchall()]
    except sqlite3.Error:
        return []


def gc(ttl_h=None):
    """Delete rows older than the TTL; returns how many went."""
    ttl_h = config.TIER_LEDGER_TTL_H if ttl_h is None else ttl_h
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=ttl_h)).isoformat(timespec="seconds")
    try:
        with _conn() as conn:
            return conn.execute("DELETE FROM tier_calls WHERE at < ?", (cutoff,)).rowcount
    except sqlite3.Error:
        return 0


def _maybe_gc():
    now = time.time()
    if now - _last_gc[0] >= _GC_EVERY_S:
        _last_gc[0] = now
        gc()
