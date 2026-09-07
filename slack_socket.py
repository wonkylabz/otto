"""Slack **Socket Mode** — instant delivery for the BOT identity.

Otto's Slack inbound is a poll (`slack.poll`, a Temporal Schedule) because a *user* token has no
event stream. A *bot* token does: Socket Mode is an outbound WebSocket the app holds open, so it
needs no public URL and works behind NAT on a laptop — which is where Otto runs.

**This module is a WAKE-UP SIGNAL, NOT A SECOND INGRESS.** An event does not carry a message into
the pipeline; it only tells the poll to run *now* instead of up to `poll_seconds` from now. Two
reasons, and both are load-bearing:

  * The per-message pipeline in `slack_ingest`/`activities.poll_slack` is a stack of decisions that
    each exist because of a specific incident — the allowlist, the cursor, the downtime/backlog
    rule, the pleasantry short-circuit, resume-vs-handoff, one-turn-at-a-time. A second path
    through them is exactly the divergence this repo keeps paying for. There is one path; this
    makes it run sooner.
  * **Events are lossy by design.** Socket Mode delivers only while connected — anything said
    during a disconnect, a laptop sleep or a restart is simply never sent, and Slack does not
    replay it. So the poll has to remain authoritative regardless, and the cursor (not the event
    stream) stays the record of what has been read. That makes a dropped event a latency blip
    rather than a lost message, which is why this can be best-effort throughout.

Consequences worth keeping in mind when editing:
  * Nothing here needs to be reliable. Every failure path falls back to "the poll will get it".
  * Nothing here decides anything about a message — it does not even parse one. It looks at an
    envelope only closely enough to ignore events that could not possibly produce work.
  * `data/slack-state.json` is never touched here. Cursors move in the poll, once.

Transport notes:
  * Auth is an **app-level token** (`xapp-…`, scope `connections:write`) — a THIRD credential,
    distinct from the bot token that reads and posts. `apps.connections.open` mints a short-lived
    `wss://` URL per connection; it is not reusable, so every reconnect calls it again.
  * Slack asks the client to reconnect roughly hourly (a `disconnect` envelope with reason
    `refresh_requested`). Reconnection is the NORMAL case here, not an error path.
  * Every envelope must be acked by envelope_id or Slack redelivers it. We ack immediately and
    unconditionally — an ack means "received", and since the ack cannot make us lose a message
    (the poll is authoritative) there is no reason to withhold one.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

import config
from ui import trace

# Soft import, exactly like temporal_client.OK: an install that predates this feature (or one that
# skipped requirements.txt) must degrade to the poll with a clear status, never fail to boot.
try:
    import websockets                      # noqa: F401  (presence check)
    from websockets.asyncio.client import connect as _ws_connect
    OK = True
except Exception:                          # noqa: BLE001 - any import failure means "not available"
    _ws_connect = None
    OK = False

# The app-level token (xapp-…). Read here, next to the code that uses it, like slack.USER_TOKEN.
APP_TOKEN = config.secret("OTTO_SLACK_APP_TOKEN")

# How long a wake-up may be suppressed after the previous one. A burst of messages (someone
# pasting three lines) is ONE poll's worth of work — the poll reads everything past the cursor —
# so waking once per burst is both cheaper and identical in outcome.
DEBOUNCE_S = float(os.environ.get("OTTO_SLACK_SOCKET_DEBOUNCE_S") or 2)
# Floor between two triggered polls. The scheduled poll keeps running underneath at its own
# interval; this only bounds the EXTRA ones, so a hot channel cannot turn every message into a
# workflow start.
MIN_INTERVAL_S = float(os.environ.get("OTTO_SLACK_SOCKET_MIN_INTERVAL_S") or 5)
# Reconnect backoff, capped. A disconnect is routine (Slack refreshes hourly), so the first retry
# is immediate; the cap matters only when Slack or the network is actually down.
_BACKOFF_S = [0, 1, 2, 5, 10, 30, 60]

# Envelope types that can produce work. `events_api` carries messages and app_mentions;
# `disconnect` is Slack asking us to reconnect. `hello` is a greeting. Anything else (slash
# commands, interactivity) Otto does not use — acked and ignored.
_WAKE_TYPES = {"events_api"}

# How far a poll pickup may trail the socket's last event before it counts as one the socket
# missed. Generous: the wake starts the poll, so a pickup lands SECONDS after its own event, and
# the point is to catch messages with no event at all — not to race the two clocks.
_MISS_MARGIN_S = float(os.environ.get("OTTO_SLACK_SOCKET_MISS_MARGIN_S") or 90)


def token_set():
    return bool(APP_TOKEN)


def available():
    """Whether Socket Mode can run at all: the library is importable AND the app token is set."""
    return bool(OK and APP_TOKEN)


def enabled(cfg=None):
    """Whether it SHOULD run: available, the bot identity is on, and not switched off in config."""
    import slack
    cfg = cfg if cfg is not None else slack.load()
    return bool(available() and slack.bot_enabled(cfg) and cfg.get("bot_socket_mode", True))


def status(cfg=None):
    """One dict for the UI/status line. Never raises."""
    import slack
    cfg = cfg if cfg is not None else slack.load()
    st = {"library": OK, "token_set": bool(APP_TOKEN), "enabled": enabled(cfg),
          "connected": _STATE.get("connected", False), "last_event": _STATE.get("last_event"),
          "wakes": _STATE.get("wakes", 0), "error": _STATE.get("error"),
          "envelopes": _STATE.get("envelopes", 0), "events": _STATE.get("events", 0),
          "hello": _STATE.get("hello")}
    # A connected socket that is sent nothing looks exactly like a working one. But SILENCE is
    # not evidence — a quiet workspace and an app subscribed to no events are indistinguishable,
    # and a warning built on silence alone fires on every restart into a quiet channel (it did:
    # this warning's first version cried wolf within minutes of shipping).
    #
    # The provable version compares the two paths. The poll stamps when it last found real work;
    # if that happened while the socket was connected and the socket never announced it, events
    # ARE missing — and that is the only shape that says so without guessing.
    if st["enabled"] and st["connected"]:
        pick = slack.last_pick()
        if pick and pick < _STARTED:
            pick = None                       # older than this connection: proves nothing
        missed = pick and (not st["last_event"] or pick > st["last_event"] + _MISS_MARGIN_S)
        if missed:
            st["idle_warning"] = (
                "the poll is finding messages the socket never announced, so events are not "
                "arriving for them. Event Subscriptions → Subscribe to bot events needs all four "
                "of app_mention, message.channels, message.groups and message.im — app_mention "
                "alone covers only the message that STARTS a thread, so every follow-up falls "
                "back to poll speed. Replies still work either way, just slower.")
    if not OK:
        st["why"] = "the websockets package isn't installed — re-run ./install.sh"
    elif not APP_TOKEN:
        st["why"] = "no OTTO_SLACK_APP_TOKEN (an app-level token, xapp-…, scope connections:write)"
    elif not slack.bot_enabled(cfg):
        st["why"] = "the bot identity is off"
    elif not cfg.get("bot_socket_mode", True):
        st["why"] = "switched off in config (bot_socket_mode)"
    return st


# Live connection state, for `status`. Written by the socket thread, read by the web thread —
# plain dict assignment only, which is atomic enough for a status panel and needs no lock.
# `envelopes` counts EVERYTHING Slack sends, `hello` stamps the handshake greeting it sends on
# every connection, and `events` counts only inbound messages. The three separate the failure
# modes that otherwise look identical from outside: no hello = the transport is not really up;
# hello but no events = the app has no event subscriptions (the standing setup mistake, and the
# reason this feature can look dead while `connected` is perfectly true); events but no wakes =
# we are filtering them.
_STATE = {"connected": False, "last_event": None, "wakes": 0, "error": None,
          "envelopes": 0, "events": 0, "hello": None}
# When this process started. `last_event` lives in memory and resets on every restart, while the
# poll's `last_pick` is PERSISTED — so comparing the two across a restart always reports "the
# poll found work the socket never announced", on any install that has ever answered anything.
# That is the same cry-wolf this check was rewritten to remove, one layer down: only a pick this
# process could plausibly have seen an event for is evidence of anything.
_STARTED = time.time()
_THREAD = None
_STOP = threading.Event()


def _open_url():
    """Mint a one-shot wss:// URL. Returns None on any failure (caller backs off and retries)."""
    req = urllib.request.Request(
        "https://slack.com/api/apps.connections.open", method="POST", data=b"",
        headers={"Authorization": f"Bearer {APP_TOKEN}",
                 "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out = json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001 - transient; retried with backoff
        _STATE["error"] = f"connections.open failed: {str(e)[:80]}"
        return None
    if not out.get("ok"):
        # An invalid/short-scoped app token fails EVERY time, so say which it is rather than
        # retrying silently forever — this is the setup mistake, and it has no other symptom.
        err = out.get("error")
        _STATE["error"] = (
            "app token rejected (needs an xapp-… token with connections:write): " + str(err)
            if err in ("invalid_auth", "not_authed", "missing_scope", "token_expired")
            else f"connections.open not ok: {err}")
        trace("SLACK", f"socket: {_STATE['error']}")
        return None
    _STATE["error"] = None
    return out.get("url")


# --- waking the poll ---------------------------------------------------------

_last_wake = 0.0
_wake_lock = threading.Lock()


def _should_wake(now):
    """Rate-limit the extra polls. PURE given `now` and the module's last-wake stamp."""
    global _last_wake
    with _wake_lock:
        if now - _last_wake < MIN_INTERVAL_S:
            return False
        _last_wake = now
        return True


def wake(reason="event"):
    """Ask the Slack poll to run NOW. Best-effort in every direction — a failure here costs
    latency (the scheduled poll still fires), never a message.

    Deliberately starts `SlackPollWorkflow` directly rather than `ScheduleHandle.trigger()`: a
    schedule's action args are frozen at creation, and triggering also fights the schedule's own
    overlap policy. Stacking is prevented here instead — an already-running poll IS the poll this
    event wanted, so a second one would only race it for the same cursor."""
    import estop
    import temporal_client as tc
    if not tc.OK:
        return "no temporal"
    # The pause has to land before anything reads Slack state or starts work (ingress.md). The
    # poll re-checks it too; this just avoids the pointless workflow.
    if estop.blocked("slack"):
        return "paused"
    if not _should_wake(time.time()):
        return "debounced"

    async def _go():
        from workflows import SlackPollWorkflow
        c = await tc.client()
        async for wf in c.list_workflows(
                'WorkflowType = "SlackPollWorkflow" AND ExecutionStatus = "Running"'):
            return f"already polling ({wf.id})"
        wid = f"slack-poll-now-{int(time.time() * 1000):x}"
        await c.start_workflow(SlackPollWorkflow.run, id=wid, task_queue=tc.TASK_QUEUE)
        return wid

    try:
        out = tc.run(_go())
    except Exception as e:  # noqa: BLE001 - never let a transport thread die on this
        trace("SLACK", f"socket wake failed: {str(e)[:100]}")
        return f"failed: {str(e)[:60]}"
    _STATE["wakes"] = _STATE.get("wakes", 0) + 1
    _STATE["last_event"] = time.time()
    return out


# --- the connection ----------------------------------------------------------

async def _pump(url):
    """One connection's lifetime. Returns when Slack asks us to reconnect or the socket drops.

    Owns the `connected` flag in a `finally`: leaving the clear to the caller left the status
    reading "connected" through the whole reconnect gap, which is exactly the window the UI exists
    to show — and Slack refreshes the connection roughly hourly, so it is a window that happens
    all day."""
    async with _ws_connect(url, open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
        # INSIDE the context manager: set before it, this reads "connected" while the handshake
        # is still in flight (or timing out), which is the one moment the flag has to be honest.
        _STATE["connected"] = True
        trace("SLACK", "socket: connected")
        try:
            await _read(ws)
        finally:
            _STATE["connected"] = False


async def _read(ws):
    """Receive, ack, and decide whether to wake, until Slack says to reconnect."""
    import asyncio
    pending = False                       # an event arrived; wake once the burst goes quiet
    while not _STOP.is_set():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=DEBOUNCE_S)
        except asyncio.TimeoutError:
            # Quiet for a debounce window: whatever arrived is one poll's worth of work.
            if pending:
                pending = False
                # Off the socket loop: `wake` blocks on Temporal, and a blocked loop misses the
                # pings that keep this connection alive.
                await asyncio.to_thread(wake)
            continue
        try:
            env = json.loads(raw)
        except ValueError:
            continue
        # Ack FIRST and unconditionally — an un-acked envelope is redelivered, and since the poll
        # (not this stream) is what actually reads messages, withholding an ack could never save
        # one. Ignored envelope types are acked too, or Slack redelivers them forever.
        if env.get("envelope_id"):
            try:
                await ws.send(json.dumps({"envelope_id": env["envelope_id"]}))
            except Exception:  # noqa: BLE001 - the reconnect covers it
                pass
        kind = env.get("type")
        _STATE["envelopes"] = _STATE.get("envelopes", 0) + 1
        if kind == "hello":
            # Slack greets every connection. Its arrival is the proof the transport works, which
            # is exactly what cannot otherwise be told apart from "subscribed to nothing".
            _STATE["hello"] = time.time()
            trace("SLACK", "socket: hello — Slack is talking to us")
            continue
        if kind == "disconnect":
            trace("SLACK", f"socket: disconnect ({env.get('reason')}) — reconnecting")
            return
        if kind in _WAKE_TYPES:
            _STATE["events"] = _STATE.get("events", 0) + 1
            if _is_wakeworthy(env):
                pending = True
    if pending:                           # shutting down with something unpolled: hand it over
        await asyncio.to_thread(wake)


def _is_wakeworthy(env):
    """Whether an envelope could possibly produce work. Deliberately COARSE: this is not the
    allowlist and must not become one (that decision lives in `slack._allowed`, once). It only
    drops the traffic that provably cannot — our own posts, edits/deletes, and non-message events
    — so a busy channel does not trigger a poll per typing indicator. Anything unrecognised wakes:
    a missed wake is a message answered a minute late, which is the failure this feature exists to
    remove. PURE."""
    evt = ((env.get("payload") or {}).get("event") or {})
    kind = evt.get("type")
    if kind not in ("message", "app_mention"):
        return False
    # message_changed / message_deleted / channel_join and friends never carry new work.
    if evt.get("subtype"):
        return False
    # Our own bot's posts — the ack and the answer — must never wake the poll that produced them.
    if evt.get("bot_id"):
        return False
    return True


def _loop():
    import asyncio
    tries = 0
    while not _STOP.is_set():
        url = _open_url()
        if url:
            tries = 0
            try:
                asyncio.run(_pump(url))
            except Exception as e:  # noqa: BLE001 - any socket error is a reconnect, not a crash
                _STATE["error"] = f"socket: {str(e)[:80]}"
                trace("SLACK", f"socket: connection ended ({str(e)[:80]})")
            finally:
                _STATE["connected"] = False
        else:
            tries += 1
        delay = _BACKOFF_S[min(tries, len(_BACKOFF_S) - 1)]
        if _STOP.wait(delay if delay else 0.5):
            break
    _STATE["connected"] = False


def start(cfg=None):
    """Start the listener thread if it should run. Idempotent; returns a short status string."""
    global _THREAD
    if not OK:
        return "skipped (websockets not installed — re-run ./install.sh)"
    if not APP_TOKEN:
        return "skipped (no OTTO_SLACK_APP_TOKEN)"
    if not enabled(cfg):
        return "off (bot identity or socket mode disabled)"
    if _THREAD and _THREAD.is_alive():
        # Includes one still winding down from a `stop()` whose join timed out: clearing `_STOP`
        # and spawning now would leave two connections live.
        return "already running"
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="slack-socket", daemon=True)
    _THREAD.start()
    return "listening (instant delivery for the bot)"


def stop(timeout=3):
    """Stop the listener. Used when the bot identity is switched off from the UI.

    A thread that did NOT stop within the timeout is kept, not dropped. `_loop` can be blocked in
    `apps.connections.open`'s 15s urlopen, so a 3s join times out routinely; nulling `_THREAD`
    there let the next `reconcile` (any Slack config save) clear `_STOP` and spawn a SECOND
    listener while the first was still coming back — two Socket Mode connections, doubled wakes,
    and no way to tell from the outside."""
    global _THREAD
    _STOP.set()
    t = _THREAD
    if t and t.is_alive():
        t.join(timeout)
    if t and t.is_alive():
        _STATE["connected"] = False
        return "stopping (the listener is still winding down)"
    _THREAD = None
    _STATE["connected"] = False
    return "stopped"


def reconcile(cfg=None):
    """Bring the thread in line with config — the socket analogue of `slack.reconcile_schedule`,
    called from the same places (startup, and every save of the Slack config)."""
    if enabled(cfg):
        return start(cfg)
    if _THREAD and _THREAD.is_alive():
        return stop()
    return "off"
