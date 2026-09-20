"""Terminal trace helpers, and the durable trace log behind them.

The [TAGS] map to the Otto layers, so each request prints its journey down the stack. Every
trace line ALSO lands in `data/logs/<stream>-<date>.log` — see `trace` for why that file has to
exist, and `_write` for the three invariants it is written under.
"""
import atexit
import contextvars
import datetime
import os
import re
import sys
import threading
import time

import config
import privacy

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_C = {
    "INGRESS": "\033[96m", "ORCH": "\033[95m", "ROUTER": "\033[94m",
    "GATEWAY": "\033[93m", "RUN": "\033[92m", "GATE": "\033[1;91m",
    "COST": "\033[90m", "MEMORY": "\033[90m", "AUDIT": "\033[90m",
    "ESTOP": "\033[1;91m",
}
_R = "\033[0m"

LOG_DIR = os.path.join(config.DATA_DIR, "logs")
# Which process wrote the line. The worker and the server trace into the SAME directory from
# separate processes, so they must not share a file — `storage.mutate_json`'s lock is for state
# these two both own, and a log is append-only evidence, not state. One file each, no locking
# between them. Derived from argv so `worker.py` and `server.py` name themselves.
def _stream_name():
    """The filename stem for this process's log: `worker`, `server`, else `otto`.

    argv[0] is only a script path when a script was actually run — `python -c` leaves "-c" there
    and `python -m unittest` leaves that whole phrase, space included. Both would become a
    filename, so the name is taken from argv[0] ONLY when it names a real file, and sanitized
    even then."""
    name = os.environ.get("OTTO_LOG_STREAM")
    if not name:
        argv0 = sys.argv[0] if sys.argv else ""
        name = os.path.splitext(os.path.basename(argv0))[0] if os.path.isfile(argv0) else ""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "otto"


_STREAM = _stream_name()
# The run a trace line belongs to. The worker runs workflows concurrently, so the stream is
# interleaved lines from several runs; without this a trace cannot be joined to the audit row
# or the transcript that explains it. A ContextVar rather than a global: activities run on the
# worker's event loop, where a thread-local would leak one run's id into another's lines.
_WID = contextvars.ContextVar("otto_wid", default=None)

_LOCK = threading.Lock()
_SINK = {"fh": None, "path": None, "bytes": 0}


def set_run(wid):
    """Bind the run every later trace line on this context belongs to. Returns the ContextVar
    token so a caller that needs to can reset it; most don't — an activity's context ends with
    the activity."""
    return _WID.set(wid or None)


def current_run():
    """The run id a trace line should carry, or None.

    Explicit binding first, then Temporal's own activity context — which covers every activity
    without one `set_run` call per activity function, and is the only source that is right by
    construction. `temporalio` is an optional import here (the CLI paths trace too and the test
    suite self-skips without it), so a missing or out-of-context SDK just means no id."""
    wid = _WID.get()
    if wid:
        return wid
    try:
        from temporalio import activity   # noqa: PLC0415 - optional dep, resolved per call
        if activity.in_activity():
            return activity.info().workflow_id
    except Exception:  # noqa: BLE001 - an id is a nicety; never break a trace for it
        pass
    return None


def _log_path(now):
    return os.path.join(LOG_DIR, f"{_STREAM}-{now:%Y-%m-%d}.log")


def _rolled_name(now):
    """Where an over-sized log is moved aside to. Stamped with the time it was CLOSED, so the
    directory still sorts chronologically and the TTL sweep can reap it by mtime.

    Second resolution is not unique: `os.replace` onto an existing name DELETES it, and two
    rolls inside one second are exactly what a small cap or a burst produces — measured, 20
    lines at a 300-byte cap left 2 files instead of 5, silently. So the first free suffix wins,
    and a name that cannot be found free is refused rather than overwriting evidence."""
    base = os.path.join(LOG_DIR, f"{_STREAM}-{now:%Y-%m-%dT%H%M%S}")
    for n in range(100):
        cand = f"{base}.log" if not n else f"{base}.{n}.log"
        if not os.path.exists(cand):
            return cand
    return None


def gc_logs(ttl_h=None):
    """Best-effort sweep of trace logs older than the TTL (mirrors `claude_cli.gc_transcripts`).

    Called only when the sink opens a file, not per line: a `listdir` per trace would put the
    filesystem on the path of every routing decision."""
    ttl_h = config.TRACE_LOG_TTL_H if ttl_h is None else ttl_h
    if not ttl_h or not os.path.isdir(LOG_DIR):
        return
    cutoff = time.time() - ttl_h * 3600
    for name in os.listdir(LOG_DIR):
        p = os.path.join(LOG_DIR, name)
        try:
            if name.endswith(".log") and os.path.getmtime(p) < cutoff:
                os.unlink(p)
        except OSError:
            pass


def _close_locked():
    """Drop the cached handle so the next write reopens. Caller MUST hold `_LOCK`."""
    if _SINK["fh"] is not None:
        try:
            _SINK["fh"].close()
        except OSError:
            pass
    _SINK["fh"], _SINK["path"], _SINK["bytes"] = None, None, 0


def _close():
    """`_close_locked` for a caller that does NOT hold the lock — the atexit hook and the tests.

    Two of the three callers were unlocked while the docstring claimed otherwise, and at exit a
    daemon thread (`slack_socket`) can be mid-`_write`: the broad except made that harmless and
    invisible, which is the worse of the two. Split so the claim is true at both spellings."""
    with _LOCK:
        _close_locked()


def _sink(now):
    """The open handle for today's log. Caller holds `_LOCK`; None when it can't be opened.

    `bytes` is counted in-process rather than stat()ed per line — and it is SEEDED from the
    file already on disk, because this process is usually not the first to write today's file
    and a counter starting at 0 would let a restart-heavy day grow without bound."""
    path = _log_path(now)
    if _SINK["path"] == path and _SINK["fh"] is not None:
        return _SINK["fh"]
    _close_locked()
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        rolled = _rolled_name(now) if (config.TRACE_LOG_MAX_BYTES
                                       and size >= config.TRACE_LOG_MAX_BYTES) else None
        if rolled:
            os.replace(path, rolled)
            size = 0
        # Line-buffered and opened for APPEND — the bug this closes is a `>` in run.sh that
        # truncated the whole history on every worker restart, and reopening with "w" here
        # would simply move that truncation into the process.
        _SINK["fh"] = open(path, "a", encoding="utf-8", buffering=1)
        _SINK["path"], _SINK["bytes"] = path, size
        gc_logs()
    except OSError:
        _close_locked()
    return _SINK["fh"]


# The handle is long-lived by design (reopening per line would put an open() on the path of
# every routing decision), so something has to close it — without this the interpreter reports
# an unclosed file on exit, which in a suite run is noise that hides a real one.
atexit.register(lambda: _close())


def _write(line, wid):
    """Append one scrubbed, dated, run-attributed line to the durable log.

    Three invariants, all of them the reason this is a function and not an f-string at the call
    site. It is SCRUBBED (`privacy.redact`, the same one writer argument transcripts make: this
    file now survives restarts and holds whatever a trace interpolated for the whole TTL, and a
    live token has reached a durable sink here before). It is APPENDED, never truncated. And it
    carries the WID, or concurrent runs are one unreadable interleaving.

    Swallows every error: a trace is evidence about a run, never part of it."""
    try:
        now = datetime.datetime.now()
        stamp = f"{now:%Y-%m-%dT%H:%M:%S}"
        out = f"{stamp} [{wid}] {line}\n" if wid else f"{stamp} {line}\n"
        with _LOCK:
            fh = _sink(now)
            if fh is None:
                return
            fh.write(out)
            _SINK["bytes"] += len(out)
            # Rolling on the COUNTER, not on the path changing. The path only changes at the day
            # boundary, so a size check there alone bounds nothing on the service this log is
            # for: a worker that is never restarted wrote one unbounded file all day, and the
            # TTL sweep cannot reap the file still being appended to.
            if config.TRACE_LOG_MAX_BYTES and _SINK["bytes"] >= config.TRACE_LOG_MAX_BYTES:
                path = _SINK["path"]
                _close_locked()
                try:
                    rolled = _rolled_name(now)
                    if rolled:
                        os.replace(path, rolled)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 - logging must never fail the thing it is logging
        pass


def trace(tag, msg, wid=None):
    """One trace line, to the console AND to `data/logs/<stream>-<date>.log`.

    BOTH, deliberately. The console is what a terminal-attached run and `journalctl` show, and
    silencing it to avoid a duplicate would take the traces out of exactly the place an operator
    looks first. The file is what survives a restart — and `ui.trace` is the richest debug stream
    Otto has (routing picks, wall reasons, non-reproduced adverse verdicts, supervisor verdicts,
    local latches), which until issue #128 lived only in a `/tmp` file `run.sh` truncated on
    every start, while the repo's own discipline is to restart the worker after every edit.

    flush=True because stdout is BLOCK-buffered when it is a logfile rather than a terminal:
    a long-lived background thread whose only output is traces (slack_socket) would otherwise
    have them sit unflushed for the life of the process — invisible exactly when being read."""
    msg = privacy.redact(str(msg))
    wid = wid or current_run()
    _write(f"[{tag}] {msg}", wid)
    stamp = f" {wid}" if wid else ""
    if _COLOR:
        print(f"   {_C.get(tag,'')}[{tag:<7}]{_R}{stamp} {msg}", flush=True)
    else:
        print(f"   [{tag:<7}]{stamp} {msg}", flush=True)


def say(msg=""):
    print(msg)


def banner(n_agents, n_skills, n_read):
    say("=" * 70)
    say(" OTTO  -  local orchestrator for your REAL Claude Code agents & skills")
    say("=" * 70)
    say(f" Discovered: {n_agents} subagents + {n_skills} skills  "
        f"({n_read} read-only, {n_agents + n_skills - n_read} writers)")
    say("")
    say(" A request flows:")
    say("   [INGRESS] you  ->  [ROUTER] Claude picks the agent/skill")
    say("   ->  [GATE] reads auto-run / writes need approval")
    say("   ->  [RUN] claude -p executes it  ->  [AUDIT]")
    say("")
    say(" Try:")
    say("   what's on the board                     (read -> auto-runs)")
    say("   refine and implement GitHub issue 1529  (write -> asks approval)")
    say("")
    say(" Commands:  /list   /memory   /help   /quit")
    say(" Env: OTTO_DRY_RUN=1 routes + classifies but does NOT execute.")
    say("=" * 70)
