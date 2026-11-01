"""Result delivery for unattended runs.

Interactive runs show their result in the chat; unattended ones (events, schedules) finish with
no audience — the answer only reaches the audit log. A `reply_to` target lets a triggering event
(or schedule) say where the result should go. Delivered from a Temporal activity so it's durable
and retried.

`reply_to` is `{kind, ...}`. Supported now:
  * {"kind": "webhook", "url": "...", "headers": {...}}  -> POST {result, capability} as JSON.
  * {"kind": "github_issue", "repo": "owner/repo", "number": N, ...}  -> comment the result on
    the issue and move its board card to the Review (repo-edit) or Done column (board.py).
  * {"kind": "slack_thread", "channel": "C…", "thread_ts": "…"}  -> post the result as a threaded
    reply in Slack (slack.py). Idempotent on the run id so an activity retry doesn't double-post.
Future sinks (email, …) slot in alongside these.
"""
import hashlib
import json
import os
import secrets
import time
import urllib.request

import config
import integrations
import privacy
import storage
from ui import trace

# Push bookkeeping, shared between the worker (which sends) and the server (which redeems an
# action token and reports push health). Separate processes mutate it, so every write goes
# through storage.mutate_json. Holds three short-lived things and nothing durable:
#   "sent"   {dedupe key: unix ts} — the retry-duplicate guard (config.NTFY_DEDUPE_S)
#   "last"   {ok, at, error, title} — the outcome of the last BLOCKING push, for /api/health
#   "tokens" {token: {wid, exp}} — single-use approve/deny grants for ntfy action buttons
_STATE = os.path.join(config.DATA_DIR, "notify-state.json")


_NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}

# A push nobody is blocked on ("complete") is not worth a health signal or a token — only the
# ones a human is waiting behind get the extra machinery.
_BLOCKING_KINDS = (None, "approval", "clarify", "terminal", "reaper")


def _prune(state, now):
    """Drop expired dedupe keys and action tokens. Called inside every mutation so the store
    stays bounded without a sweeper — nothing here outlives a day."""
    window = max(0, config.NTFY_DEDUPE_S)
    state["sent"] = {k: t for k, t in (state.get("sent") or {}).items()
                     if isinstance(t, (int, float)) and now - t < window}
    state["tokens"] = {k: v for k, v in (state.get("tokens") or {}).items()
                       if isinstance(v, dict) and (v.get("exp") or 0) > now}
    return state


def _dedupe_key(title, wid, kind):
    return hashlib.sha1(f"{kind}\x00{wid}\x00{title}".encode()).hexdigest()[:16]


def _claim(key, now):
    """Reserve a dedupe key. True if this push is the first of its kind in the window (send it),
    False if an identical one already went out (drop it). Claimed BEFORE the HTTP call and
    released if that call fails, so a failed push doesn't suppress its own durable retry — which
    is the one case the retry exists for."""
    if not key or config.NTFY_DEDUPE_S <= 0:
        return True
    won = []

    def fn(state):
        _prune(state, now)
        if key in state["sent"]:
            won.append(False)
            return storage.UNCHANGED
        state["sent"][key] = now
        won.append(True)
        return state

    try:
        storage.mutate_json(_STATE, fn, {})
    except Exception:  # noqa: BLE001 - the guard must never block the push it guards
        return True
    return won[0] if won else True


def _release(key):
    """Undo a claim whose push then failed to leave the machine."""
    if not key:
        return

    def fn(state):
        if key in (state.get("sent") or {}):
            state["sent"].pop(key, None)
            return state
        return storage.UNCHANGED

    try:
        storage.mutate_json(_STATE, fn, {})
    except Exception:  # noqa: BLE001
        pass


def _record_health(title, ok, error=""):
    """Remember whether the last blocking push actually left the machine. A push that silently
    fails is worse than no push at all: the run parks at its gate, the owner's phone stays quiet,
    and 24h later it auto-declines with nothing anywhere saying the alert never arrived. Surfaced
    on /api/health so the UI can say so."""
    def fn(state):
        state["last"] = {"ok": bool(ok), "at": time.time(),
                         "title": str(title)[:80], "error": str(error)[:200]}
        return state

    try:
        storage.mutate_json(_STATE, fn, {})
    except Exception:  # noqa: BLE001
        pass


def health():
    """Outcome of the last blocking push, for /api/health. `{}` when none has been sent (or the
    feature is off) — an absent record is not a failure."""
    if not config.NTFY_TOPIC:
        return {}
    last = (storage.read_json(_STATE, {}) or {}).get("last")
    return last if isinstance(last, dict) else {}


def mint_action_token(wid, ttl_s=None, revision=None):
    """Mint a single-use grant to approve/deny `wid` from a notification action button.

    The token travels to a third-party broker, so it is scoped as tightly as a grant can be: ONE
    run, single-use, and expiring with the gate. A leaked topic therefore exposes at most the
    approve/deny of the runs currently parked at a gate — not the unauthenticated API behind it,
    which is what a bare `?id=<wid>` link would have handed over.

    `revision` is the plan round the push announced: the button decides THAT plan, so a tap
    after a "request changes" round is refused by the workflow instead of approving a plan the
    phone never showed (`OttoWorkflow.decide`)."""
    now = time.time()
    token = secrets.token_urlsafe(24)
    exp = now + (ttl_s if ttl_s is not None else max(60, config.NTFY_ACTION_TTL_S))

    def fn(state):
        _prune(state, now)
        state["tokens"][token] = {"wid": wid, "exp": exp, "revision": revision}
        return state

    try:
        storage.mutate_json(_STATE, fn, {})
    except Exception:  # noqa: BLE001 - no token is a missing button, not a failed run
        return None
    return token


def peek_action_token(token):
    """`(wid, revision)` a live token grants, WITHOUT spending it — or `(None, None)`. The token
    is spent only once the decision has landed or been refused (`redeem_action_token`): a tap
    while the worker restarts must not burn the only button the phone has."""
    if not token:
        return None, None
    try:
        state = storage.read_json(_STATE, {}) or {}
    except Exception:  # noqa: BLE001
        return None, None
    entry = (state.get("tokens") or {}).get(token)
    if not isinstance(entry, dict) or entry.get("exp", 0) <= time.time():
        return None, None
    return entry.get("wid"), entry.get("revision")


def redeem_action_token(token):
    """Consume a token, returning the run id it grants — or None if unknown, expired or already
    used. Deleted on redemption: a button tapped twice (or a URL that leaked afterwards) must not
    approve a SECOND gate the same run reaches after a plan revision."""
    if not token:
        return None
    now = time.time()
    got = []

    def fn(state):
        _prune(state, now)
        entry = state["tokens"].pop(token, None)
        got.append(entry)
        return state

    try:
        storage.mutate_json(_STATE, fn, {})
    except Exception:  # noqa: BLE001
        return None
    entry = got[0] if got else None
    return (entry or {}).get("wid") if isinstance(entry, dict) else None


def gate_actions(token):
    """ntfy action buttons for an approval push: approve/deny without opening anything.

    `clear: true` dismisses the notification on tap, so a stale card can't be tapped twice from
    the shade. Both are POSTs to the same token endpoint — the server, not the button, decides
    what a decision means, so the phone never carries the signal name for the workflow API."""
    base = f"{config.CLICK_URL.rstrip('/')}/api/gate/{token}"
    return [{"action": "http", "label": "Approve", "url": base, "method": "POST",
             "body": json.dumps({"approve": True}), "headers": {"Content-Type": "application/json"},
             "clear": True},
            {"action": "http", "label": "Deny", "url": base, "method": "POST",
             "body": json.dumps({"approve": False}), "headers": {"Content-Type": "application/json"},
             "clear": True}]


def notify(title, *, lines=None, detail=None, click=None, tags=None, priority="high",
           kind=None, wid=None, actions=None):
    """Push a notification to the owner's ntfy topic (issue #92) — fired when a run blocks on
    a human (approval, clarification, needs-human terminal). No-op when OTTO_NTFY_TOPIC is
    unset; NEVER raises — a notification failure must not fail the run it announces. Uses
    ntfy's JSON publish endpoint (unicode-safe, unlike raw headers). Returns True if sent.
    ntfy's JSON endpoint requires `priority` as an INTEGER 1-5 (a string like "high" 400s —
    only the raw-header API accepts names), so map names to ints and clamp anything else.
    `kind="complete"` marks the opt-in clean-finish push — dropped unless
    OTTO_NTFY_ON_COMPLETE is set (human-blocking pushes are always on when the topic is).
    When `wid` is provided, appends a final line `run: <wid>` to the message AND deep-links the
    tap to that run (`#run=<wid>`) — a push that lands on the home tab makes the reader hunt for
    the run it just told them about, which is the one thing it was for.

    An identical push inside config.NTFY_DEDUPE_S is DROPPED (returns False). That guard exists
    for the durable activity retry a blocking push now gets: a retry after a lost result would
    otherwise ring the phone twice for one gate.

    THERE IS NO FREE-TEXT BODY PARAMETER, deliberately. The push goes to a third-party broker
    whose topic name is its only credential, so the two kinds of text are separated at the
    signature and cannot be confused at a call site:

      * `lines` — Otto's own metadata (capability, risk, repo, source, reason), built by
        `privacy.context_lines`. Always sent.
      * `detail` — request / ticket / message CONTENT. Dropped entirely unless
        OTTO_NTFY_DETAIL is set, and redacted + clipped when it is.

    The old signature took the request text as a positional `body`, which is how
    `request[:250]` of every finished run — a colleague's DM, a ticket body, whatever was
    pasted into the composer — ended up on ntfy.sh. Everything after `title` is KEYWORD-ONLY so
    that shape now raises TypeError instead of leaking quietly: a caller who has not thought
    about which half their text belongs in cannot accidentally pick the always-sent one.
    Everything sent is redacted regardless: that is the floor under the content rule, not the
    control itself — a request body is private whether or not it holds a token."""
    if kind == "complete" and not config.NTFY_ON_COMPLETE:
        return False
    if not config.NTFY_TOPIC:
        return False
    prio = _NTFY_PRIORITY.get(priority, priority) if isinstance(priority, str) else priority
    if not isinstance(prio, int) or not (1 <= prio <= 5):
        prio = 4
    parts = [str(x) for x in (lines or []) if x]
    if detail and config.NTFY_DETAIL:
        preview = " ".join(str(detail).split())[:max(0, config.NTFY_DETAIL_CHARS)]
        if preview:
            parts.append("")
            parts.append(preview)
    if wid:
        parts.append("")
        parts.append(f"run: {wid}")
    message = privacy.redact("\n".join(parts).strip()) or "(open Otto for details)"
    landing = config.CLICK_URL.rstrip("/")
    payload = {"topic": config.NTFY_TOPIC,
               "title": privacy.redact(str(title))[:150],
               "message": message[:800],
               "click": click or (f"{landing}#run={wid}" if wid else landing),
               "priority": prio}
    if tags:
        payload["tags"] = list(tags)
    if actions and config.NTFY_ACTIONS:
        payload["actions"] = list(actions)
    blocking = kind in _BLOCKING_KINDS
    key = _dedupe_key(payload["title"], wid, kind)
    if not _claim(key, time.time()):
        trace("NTFY", f"duplicate push within {config.NTFY_DEDUPE_S}s — dropped")
        return False
    try:
        req = urllib.request.Request(
            config.NTFY_URL, method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode())
        with urllib.request.urlopen(req, timeout=10):
            pass
        if blocking:
            _record_health(payload["title"], True)
        return True
    except Exception as e:  # noqa: BLE001 - strictly best-effort
        _release(key)
        if blocking:
            _record_health(payload["title"], False, str(e))
        trace("NTFY", f"push failed ({e}) — run continues")
        return False


# WHO READS a delivered result, per reply-target kind. The run's output contract is chosen from
# this (engine._output_contract), because "how should this be written" is a property of where it
# lands — not something the workflow should hardcode per ingress.
#   "conversation" — a specific person, in a live exchange they can reply to. Needs a reply, not a
#                    report: an operator-facing report delivered here reaches them as Otto talking
#                    ABOUT them (the 2026-07-31 "Here's the reply to send back on the operator's
#                    behalf: …" that a colleague received verbatim).
#   "report"       — the operator, or a durable record teammates read later (a ticket comment).
# A new kind declares its audience in `integrations/`, beside its `deliver`; `audience_for`
# defaults an undeclared one to "report" (`IntegrationRegistryTests`).
AUDIENCE = integrations.audiences()
DEFAULT_AUDIENCE = "report"
# This module OWNS the value (contracts.CONVERSATION_AUDIENCE mirrors it by value, deliberately —
# importing contracts here would be a cycle). Named so `interim` reads as a rule, not a string.
CONVERSATION_AUDIENCE = "conversation"
NOTICE_AUDIENCE = "notice"


def has_asker(reply_to):
    """Whether someone at this target ASKED for the run — and so is owed its decline, skip or
    error message. False for a one-way post: those messages are written to an asker who does not
    exist there, and the operator who can act sees them in the UI. PURE — workflow-safe."""
    return bool(reply_to) and audience_for(reply_to) != NOTICE_AUDIENCE


def audience_for(reply_to):
    """Who will read a result delivered to this target. PURE — safe to call from workflow code."""
    if not isinstance(reply_to, dict):
        return DEFAULT_AUDIENCE
    return AUDIENCE.get(reply_to.get("kind"), DEFAULT_AUDIENCE)


def interim(reply_to, text):
    """Say something to the asker MID-RUN, without ending the run.

    Distinct from `deliver` in the two ways that matter: it does not mark the run delivered (a
    `deliver` here would make `_slack`'s idempotency swallow the real answer when it arrives) and
    it does not record a session or clear the conversation's in-flight flag. It is the same shape
    as the interim ack the poll posts when a run starts.

    Only ever sent to a **conversation** audience — a person in a live exchange who is waiting on
    a reply and has no other window onto the run. A "report" target (a ticket comment, a webhook)
    is a durable record read later by someone who is not sitting there, so a progress note is
    noise in a permanent place. That split is `AUDIENCE`, reused rather than re-decided.

    Never raises. Returns `(delivered, status, ts)` — a BOOLEAN plus a human status for the trace,
    not just the string, plus the posted message's ts (None when unknown): a gate notice records
    it so an approval sent as a reply in ITS thread can be found. Callers act on this (the gate notice arms a conversation only when the
    asker was really told), and deciding that by matching a substring against prose means any
    rewording of a status message silently changes behaviour with no test failing.
    """
    if not reply_to or audience_for(reply_to) != CONVERSATION_AUDIENCE:
        return False, "no interim channel for this target", None
    text = privacy.redact(str(text or ""))
    if not text.strip():
        return False, "nothing to say", None
    owner = integrations.for_kind(reply_to.get("kind"))
    if owner is None or owner.interim is None:
        return False, "no interim channel for this target", None
    try:
        return owner.interim(reply_to, text)
    except Exception as e:  # noqa: BLE001 - never let a progress note break the run
        return False, f"interim failed: {str(e)[:80]}", None


def deliver(reply_to, result, cap=None, run_id=None):
    """Send `result` to its reply target. Returns a short status string for the audit/trace.
    Never raises — delivery failure shouldn't fail the run that produced the result. `run_id`
    marks the GitHub comment so a retried delivery doesn't post a duplicate."""
    if not reply_to:
        return "no reply target"
    kind = (reply_to or {}).get("kind")
    # ONE scrub for every sink, before any of them formats the text. A delivered result is the
    # only run output a non-owner ever reads, and it is written by a capability that ran with
    # real tools against real credentials — an env dump, a `kubectl get secret`, a curl header
    # echoed back into the report. `_DIRECT_REPLY_FORMAT` tells the model not to do that, and
    # this is what holds when the model does it anyway. Idempotent, so `slack.post`'s own
    # defensive scrub (the choke point for callers that bypass `deliver`, e.g. the interim ack)
    # costs nothing here.
    result = privacy.redact(result)
    try:
        owner = integrations.for_kind(kind)
        if owner is None:
            return f"unsupported reply kind: {kind!r}"
        return owner.deliver(reply_to, result, cap, run_id)
    except Exception as e:  # noqa: BLE001 - report, don't propagate
        return f"delivery failed: {str(e)[:140]}"
