"""Cross-process-safe state writes (issue #88).

server.py (HTTP threads) and worker.py (Temporal activities) are SEPARATE processes that
mutate the same data/*.json files; swarm children make concurrent writes real. Every
read-modify-write must go through mutate_json(): an exclusive fcntl lock on a sidecar
"<path>.lock" held across load -> mutate -> atomic replace, so concurrent writers
serialize and a lost update is impossible. Writes land via write-to-temp + os.replace,
so readers (and a crash mid-write) never observe a torn/truncated file — plain reads
need no lock. POSIX-only (fcntl.flock) — works on Linux and macOS, the two supported
deployment targets; not Windows.

This module is also the deliberate seam a SQLite backend swaps in behind (issue #103,
started with the audit trail — see sqlite_connect() below): keep ALL state access going
through it, no raw open()/json.dump at call sites.
"""
import contextlib
import fcntl
import json
import os
import sqlite3
import tempfile

# A mutator may return this sentinel to skip the write entirely (e.g. "nothing new to
# remember") — the data file is left untouched, not even created.
UNCHANGED = object()


def read_json(path, default):
    """Tolerant load: a missing or invalid file yields `default`. Pass a fresh literal
    (e.g. [] or {}) — the default is returned as-is, not copied."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _atomic_write(path, data):
    """Write JSON to a temp file in the same directory, fsync, then os.replace() over the
    target — readers see either the old or the new file, never a partial one."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mutate_json(path, fn, default):
    """Serialized read-modify-write: lock the sidecar, load (tolerantly, `default` when
    missing/invalid), apply `fn(data) -> new data`, atomically replace. `fn` runs INSIDE
    the critical section — keep it fast and side-effect-free. Returning UNCHANGED skips
    the write. Returns the data as stored (or as loaded, when unchanged)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path + ".lock", "a+") as lock:      # "a+" never truncates an existing lock file
        fcntl.flock(lock, fcntl.LOCK_EX)          # released when the file closes
        data = read_json(path, default)
        out = fn(data)
        if out is UNCHANGED:
            return data
        _atomic_write(path, out)
        return out


def write_json(path, data):
    """Full atomic overwrite under the same lock — for last-writer-wins saves that don't
    depend on the prior contents (board config, clear-store endpoints)."""
    return mutate_json(path, lambda _current: data, default=None)


def sqlite_connect(path):
    """A connection to a WAL-mode SQLite db, for callers storing state as tables instead of a
    single JSON blob (issue #103) — e.g. the audit trail and chat history, where
    read-modify-write-the-whole-file doesn't scale. WAL lets readers and a writer proceed
    concurrently; busy_timeout makes the rare writer/writer race (server.py + worker.py are
    separate processes) block-and-retry instead of raising "database is locked". Callers should
    open one of these per operation and close it (contextlib.closing) rather than holding a
    connection open across threads — sqlite3 connections aren't thread-safe to share.

    `isolation_level=None` puts the connection in AUTOCOMMIT: a lone INSERT/UPDATE commits on its
    own (a bare `conn.commit()` after one is a harmless no-op), and any multi-statement critical
    section must declare itself with tx() below. That's deliberate — it keeps sqlite3 from
    silently opening a DEFERRED transaction, which is what turns a read-then-write into a
    lock-upgrade that can fail instantly with "database is locked" even with busy_timeout set."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def tx(conn):
    """A serialized read-modify-write critical section on a sqlite_connect() connection — the
    SQLite equivalent of mutate_json()'s fcntl lock. BEGIN IMMEDIATE takes the write lock UP
    FRONT (before the reads), so two processes doing read-then-write can't interleave and lose an
    update; the loser waits out busy_timeout instead of failing on a lock upgrade. Commits on
    clean exit, rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
