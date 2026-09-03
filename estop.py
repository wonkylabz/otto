"""Global emergency stop — a resumable pause for NEW work only.

`data/ESTOP` is a sentinel file. While it exists, every ingress refuses to START work:
the board poll picks up no cards, the Slack poll doesn't read (so no cursor moves), the
webhook and `/api/submit` reject, and a workflow that Temporal dispatched anyway (a cron
fire — Schedules live in the Temporal server, not in this process) exits before its first
side effect.

**In-flight work is NEVER killed.** The one lever Otto had for "stop doing things" was
stopping the service, and nothing heartbeats (`start_to_close_timeout=20min`), so that
strands a running attempt for up to 20 minutes and Temporal won't retry it until the
window expires. This is the lever that was missing: new work stops immediately, work
already running finishes.

**Fail safe: an unreadable, empty or corrupt sentinel still counts as engaged.** The pause
must hold for `touch data/ESTOP` — the operator reaching for this is usually in a hurry,
and the failure that matters is a pause that silently didn't.

The body is optional JSON (`{"reason": ..., "engaged_at": ...}`) used only for display.

Ported from hermes-agent's `agent/estop.py` (MIT), which is itself from gastown. Otto's
version differs in one way that matters: Hermes dispatches cron in-process and can skip a
due job outright, while Otto's schedules fire from the Temporal server, so the workflow
needs its own check (`activities.estop_check`) as the backstop.
"""
import json
import os
import time

import config
from ui import trace

SENTINEL_NAME = "ESTOP"

# Tests point this at a temp dir. Resolved lazily for the same reason config._settings_path
# is: config.DATA_DIR freezes to the import-time cwd, so binding it at import time here
# would make a relocated install read a phantom sentinel at the old path.
_PATH = None

# Per-component "already logged this engagement" stamps, so a poll loop that ticks every
# few seconds logs once per engagement rather than once per tick. Keyed component ->
# engaged_at, so a release-then-re-engage logs again.
_LOGGED = {}


def path():
    return _PATH or os.path.join(config.DATA_DIR, SENTINEL_NAME)


def engaged():
    """True while the sentinel exists. One `os.stat`, no caching beyond the OS — callers may
    run this every tick, and engaging/releasing takes effect on the very next check."""
    return os.path.exists(path())


def state():
    """`{"reason", "engaged_at"}` while engaged, else None. A sentinel that exists but can't
    be read or parsed returns `{}` — still engaged, just with nothing to display."""
    if not engaged():
        return None
    try:
        with open(path(), encoding="utf-8") as f:
            body = json.load(f)
        return body if isinstance(body, dict) else {}
    except (OSError, ValueError):
        return {}


def engage(reason=""):
    """Write the sentinel. Idempotent: re-engaging refreshes the reason and keeps the ORIGINAL
    `engaged_at`, so "paused since" doesn't reset when someone re-clicks the button."""
    was = state() or {}
    body = {"reason": (reason or "").strip()[:300],
            "engaged_at": was.get("engaged_at") or time.time()}
    os.makedirs(os.path.dirname(path()), exist_ok=True)
    # Written in place, not via storage.mutate_json: the whole point is that `touch` is a
    # valid way to create this, so there is no read-modify-write to protect.
    with open(path(), "w", encoding="utf-8") as f:
        json.dump(body, f)
    trace("ESTOP", f"engaged{(' — ' + body['reason']) if body['reason'] else ''}")
    return body


def release():
    """Remove the sentinel. Returns True if it was engaged. Never raises."""
    if not engaged():
        return False
    try:
        os.remove(path())
    except OSError:
        return False
    _LOGGED.clear()
    trace("ESTOP", "released")
    return True


def blocked(component):
    """The call site's question: "may <component> start new work?" — False means go.

    Logs once per component per engagement so a paused poll loop stays quiet."""
    st = state()
    if st is None:
        _LOGGED.pop(component, None)
        return False
    stamp = st.get("engaged_at")
    if _LOGGED.get(component) != stamp:
        _LOGGED[component] = stamp
        trace("ESTOP", f"{component}: paused, not starting new work")
    return True


def status():
    """Shape for `/api/estop` and the doctor: engaged flag plus the display body."""
    st = state()
    return {"engaged": st is not None, "reason": (st or {}).get("reason", ""),
            "engaged_at": (st or {}).get("engaged_at"), "path": path()}
