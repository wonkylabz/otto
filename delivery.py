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

_MAX_COMMENT = 50_000   # GitHub rejects comments over ~65KB; stay well under.

# A delivery landing this long after the triggering message gets a "catching up" preface rather
# than reading as if no time passed — the sleep/downtime case (a run computed fine, delivery just
# couldn't reach Slack yet) is otherwise indistinguishable from an instant reply.
STALE_REPLY_S = int(os.environ.get("OTTO_SLACK_STALE_REPLY_S") or 1800)


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


def mint_action_token(wid, ttl_s=None):
    """Mint a single-use grant to approve/deny `wid` from a notification action button.

    The token travels to a third-party broker, so it is scoped as tightly as a grant can be: ONE
    run, single-use, and expiring with the gate. A leaked topic therefore exposes at most the
    approve/deny of the runs currently parked at a gate — not the unauthenticated API behind it,
    which is what a bare `?id=<wid>` link would have handed over."""
    now = time.time()
    token = secrets.token_urlsafe(24)
    exp = now + (ttl_s if ttl_s is not None else max(60, config.NTFY_ACTION_TTL_S))

    def fn(state):
        _prune(state, now)
        state["tokens"][token] = {"wid": wid, "exp": exp}
        return state

    try:
        storage.mutate_json(_STATE, fn, {})
    except Exception:  # noqa: BLE001 - no token is a missing button, not a failed run
        return None
    return token


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
# A new kind MUST be added here; `audience_for` defaults it to "report", and
# test_core asserts every kind `deliver` handles has an explicit entry, so the choice can't be
# skipped silently.
AUDIENCE = {
    "slack_thread": "conversation",
    "github_issue": "report",
    # A PR review is a durable record a reviewer reads later, not a conversation.
    "github_pr": "report",
    "webhook": "report",          # a machine reads this one; the report shape is the sane default
}
DEFAULT_AUDIENCE = "report"


def audience_for(reply_to):
    """Who will read a result delivered to this target. PURE — safe to call from workflow code."""
    if not isinstance(reply_to, dict):
        return DEFAULT_AUDIENCE
    return AUDIENCE.get(reply_to.get("kind"), DEFAULT_AUDIENCE)


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
        if kind == "webhook":
            return _webhook(reply_to, result, cap)
        if kind == "github_issue":
            return _github_issue(reply_to, result, cap, run_id)
        if kind == "github_pr":
            return _github_pr(reply_to, result)
        if kind == "slack_thread":
            return _slack(reply_to, result, run_id)
        return f"unsupported reply kind: {kind!r}"
    except Exception as e:  # noqa: BLE001 - report, don't propagate
        return f"delivery failed: {str(e)[:140]}"


def _github_pr(reply_to, result):
    """Submit an auto-review to its pull request the moment the run finishes.

    All of the deciding — verdict, nit trim, header, idempotency marker, state stamp — lives in
    `pr_review.submit`, shared with the button and the poll sweep, so what lands cannot depend
    on which of the three routes got there first."""
    import pr_review
    return pr_review.submit_on_completion(reply_to, result)


def _slack(reply_to, result, run_id=None):
    """Post the run's result as a threaded reply in Slack. Idempotent on `run_id` (Slack messages
    can't carry a hidden marker like the GitHub sink, so we track delivered run ids in state).

    A delivery that took a long time to reach here (worker downtime, an overnight machine sleep —
    the run itself finished fine, it just couldn't post) gets a staleness check before it lands:
    if the owner has personally replied in the conversation since the triggering message, this
    would-be reply is now redundant — piling a stale, superseded answer onto ground already
    covered reads far worse than saying nothing (`slack.owner_replied_since`). Short of that, a
    long-delayed reply still says so rather than landing cold as if no time had passed."""
    import slack
    if run_id and slack.was_posted(run_id):
        return "already delivered to slack"
    # The run decided there was nothing to say back (config.NO_REPLY). Staying silent IS the
    # delivery — not marked posted, because nothing was.
    if config.is_no_reply(result):
        return "nothing to reply — stayed silent"
    channel = reply_to.get("channel")
    if not channel:
        return "slack reply_to missing 'channel'"
    thread_ts = reply_to.get("thread_ts")            # the real thread ROOT, if any
    # The specific triggering message's own ts (not the thread root) — `wid_for` always encodes
    # it, so this is the right "since" boundary even deep in an ongoing thread.
    trigger = slack.reply_target_from_wid(run_id) if run_id else None
    since_ts = (trigger or {}).get("thread_ts") or thread_ts
    if since_ts:
        superseded, delay_s = slack.owner_replied_since(
            channel, since_ts, in_thread=bool(thread_ts), thread_root=thread_ts)
        if superseded:
            if run_id:
                slack.mark_posted(run_id)
            return "skipped — the owner already answered this since it was asked"
        if delay_s > STALE_REPLY_S:
            result = (f"_(sorry — catching up after a gap, this is about your message from "
                      f"earlier)_\n\n{result}")
    # Claude emits Markdown. Render as Block Kit rich_text (native lists/indents/code) when we can
    # parse it; the mrkdwn text is always sent as the notification + fallback (and is what shows if
    # the blocks parse returns None).
    raw = result or "(no result)"
    body = slack.to_mrkdwn(raw)
    blocks = slack.to_blocks(raw)
    ok = slack.post(channel, body, thread_ts=reply_to.get("thread_ts"), blocks=blocks)
    if ok:
        slack.mark_posted(run_id)
        return f"posted to slack thread ({channel})"
    return f"could not post to slack ({channel})"


def _webhook(reply_to, result, cap, timeout=15):
    url = reply_to.get("url")
    if not url:
        return "webhook reply_to missing 'url'"
    body = json.dumps({"result": result, "capability": cap}).encode()
    headers = {"Content-Type": "application/json", **(reply_to.get("headers") or {})}
    req = urllib.request.Request(url, method="POST", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return f"posted to webhook ({r.status})"


def _github_issue(reply_to, result, cap, run_id=None):
    """Comment the run's result on the source issue, then move its board card on. Destination:
    the **blocked** column when the run needs a human (verification/QA didn't pass — reply_to
    `blocked`), the Review column for a repo-edit ticket with a draft PR awaiting, else Done.
    Card-move ids are carried on `reply_to` (resolved by the poll activity).

    Comment-first (before the move) is deliberate: if the process dies between the two, a
    still-In-Progress card is visible to the reaper, whereas a moved-but-uncommented card would
    silently lose the result. Idempotent via a hidden `run_id` marker so a retried delivery
    (e.g. after an activity timeout) doesn't post a duplicate."""
    import board
    repo, number = reply_to.get("repo"), reply_to.get("number")
    body = result if len(result) <= _MAX_COMMENT else (
        result[:_MAX_COMMENT] + "\n\n_…truncated by Otto (result exceeded comment limit)._")
    marker = f"<!-- otto-run:{run_id} -->" if run_id else ""
    # Comments delivered before the Mosaic->Otto rename carry the old marker; check BOTH or a
    # retried delivery of a pre-rename run would post a duplicate.
    legacy = f"<!-- mosaic-run:{run_id} -->" if run_id else ""
    if marker and (board.has_comment_marker(repo, number, marker)
                   or board.has_comment_marker(repo, number, legacy)):
        posted = True   # already delivered on a prior (retried) attempt — don't duplicate
    else:
        posted = board.comment(repo, number, (marker + "\n" + body) if marker else body)

    if reply_to.get("blocked"):
        target = reply_to.get("blocked_col")
    elif reply_to.get("repo_edit"):
        target = reply_to.get("review_col")
    else:
        target = reply_to.get("done_col")
    option_id = (reply_to.get("status_options") or {}).get(target)
    moved = board.set_status_raw(reply_to.get("project_id"), reply_to.get("status_field_id"),
                                 reply_to.get("item_id"), option_id)
    bits = [f"commented on {repo}#{number}" if posted else f"comment FAILED on {repo}#{number}"]
    if target:
        bits.append(f"moved to {target}" if moved else f"could NOT move to {target}")
    return "; ".join(bits)
