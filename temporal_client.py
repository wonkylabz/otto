"""Shared Temporal client plumbing.

The sync HTTP server and the scheduler both need to call the async Temporal client.
This keeps one background event loop + one connection, used via `run(coro)`. Importing
this is safe without temporalio installed — `OK` is False and callers fall back.
"""
import asyncio
import os
import threading

try:
    from temporalio.client import Client  # noqa: F401
    OK = True
except Exception:  # noqa: BLE001
    OK = False

ADDR = os.environ.get("TEMPORAL_ADDR", "localhost:7233")
TASK_QUEUE = os.environ.get("OTTO_TASK_QUEUE", "otto")

_loop = None
_client = None


def _bg_loop():
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True).start()
    return _loop


def run(coro):
    """Run a coroutine on the background loop and block for its result."""
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop()).result()


async def client():
    global _client
    if _client is None:
        _client = await Client.connect(ADDR)
    return _client


async def _workflow_input(wid):
    """The params dict a workflow was STARTED with, read back from its own Temporal history, or
    None. Temporal records the start event's input, so this recovers things a run carried but the
    audit trail never stored — notably `reply_to`, the thread/issue a result must go back to.
    Bounded by the namespace's history retention: an old enough run returns None, so every caller
    needs a fallback."""
    c = await client()
    try:
        async for e in c.get_workflow_handle(wid).fetch_history_events():
            attrs = getattr(e, "workflow_execution_started_event_attributes", None)
            if not attrs or not getattr(attrs, "input", None):
                continue
            payloads = list(attrs.input.payloads)
            if not payloads:
                return None
            from temporalio.converter import DataConverter
            vals = await DataConverter.default.decode(payloads, [dict])
            return vals[0] if vals and isinstance(vals[0], dict) else None
    except Exception:  # noqa: BLE001 - history gone/unreachable: caller falls back
        return None
    return None


def workflow_input(wid):
    """Sync wrapper for `_workflow_input`. NEVER call this from a coroutine already running on the
    background loop (see CLAUDE.md) — await `_workflow_input` there instead."""
    if not OK or not wid:
        return None
    try:
        return run(_workflow_input(wid))
    except Exception:  # noqa: BLE001
        return None


def connected():
    if not OK:
        return False
    try:
        run(client())
        return True
    except Exception:  # noqa: BLE001
        return False


def workflow_gone(exc):
    """True only when Temporal has AUTHORITATIVELY said this workflow id does not exist.

    A describe/query can fail two ways that look identical to `except Exception` but mean
    opposite things: NOT_FOUND (the id is gone — the dev server's DB was wiped, or history
    aged out) versus a transport failure (deadline exceeded, server unavailable, a restart
    mid-poll). Collapsing the second into the first reports a perfectly healthy run as a
    terminal failure, which is exactly what the chat pipeline then paints — measured against
    a run that kept executing for another 15 minutes after the UI called it failed.
    """
    if not OK:
        return False
    try:
        from temporalio.service import RPCError, RPCStatusCode
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND
