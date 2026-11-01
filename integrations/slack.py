"""Slack: a thread someone asked in (`slack_thread`, a conversation) and a runbook's one-way
channel post (`slack_post`, a notice). The vendor-facing API is the top-level `slack` module;
this is what the core calls through the registry. (`import slack` below is that module.)"""
import os

import config
from integrations import CONVERSATION, Integration, NOTICE

# A delivery landing this long after the triggering message gets a "catching up" preface rather
# than reading as if no time passed — the sleep/downtime case (a run computed fine, delivery just
# couldn't reach Slack yet) is otherwise indistinguishable from an instant reply.
STALE_REPLY_S = 1800


def _stale_reply_s():
    """Read per call: `all()` imports this module from workflow-side code, so nothing here may
    read the env at import (`…pure_at_import`)."""
    return int(os.environ.get("OTTO_SLACK_STALE_REPLY_S") or STALE_REPLY_S)

# Slack's refusals that mean "this bot is not in that channel" — the post certainly did not land.
_BOT_CANNOT_POST = ("not_in_channel", "channel_not_found")


def deliver(reply_to, result, cap=None, run_id=None):
    """Post the run's result as a threaded reply in Slack. Idempotent on `run_id` (Slack messages
    can't carry a hidden marker like the GitHub sink, so we track delivered run ids in state).

    A delivery that took a long time to reach here (worker downtime, an overnight machine sleep —
    the run itself finished fine, it just couldn't post) gets a staleness check before it lands:
    if the owner has personally replied in the conversation since the triggering message, this
    would-be reply is now redundant — piling a stale, superseded answer onto ground already
    covered reads far worse than saying nothing (`slack.owner_replied_since`). Short of that, a
    long-delayed reply still says so rather than landing cold as if no time had passed."""
    import slack
    # WHICH Slack identity answers rides on the reply target (`slack.reply_target`) — the owner's
    # own account or the bot user. Anything without one is the owner's, which is what an in-flight
    # run submitted before the bot existed carries.
    identity = slack.identity_of(reply_to)
    if reply_to.get("kind") == "slack_post" and not reply_to.get("identity"):
        identity = slack.post_identity()
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
            channel, since_ts, in_thread=bool(thread_ts), thread_root=thread_ts,
            identity=identity)
        if superseded:
            if run_id:
                slack.mark_posted(run_id)
            return "skipped — the owner already answered this since it was asked"
        if delay_s > _stale_reply_s():
            result = (f"_(sorry — catching up after a gap, this is about your message from "
                      f"earlier)_\n\n{result}")
    # Claude emits Markdown. Render as Block Kit rich_text (native lists/indents/code) when we can
    # parse it; the mrkdwn text is always sent as the notification + fallback (and is what shows if
    # the blocks parse returns None).
    raw = result or "(no result)"
    body = slack.to_mrkdwn(raw)
    blocks = slack.to_blocks(raw)
    errors = []
    # `errors=` only where the fallback below can read it — a thread reply has no fallback.
    extra = {"errors": errors} if reply_to.get("kind") == "slack_post" else {}
    ok = slack.post(channel, body, thread_ts=reply_to.get("thread_ts"), blocks=blocks,
                    identity=identity, **extra)
    if (not ok and reply_to.get("kind") == "slack_post" and identity == slack.BOT
            and errors and errors[0] in _BOT_CANNOT_POST and slack.enabled()):
        # A bot posts only where it has joined, so a runbook pointed at a channel only the
        # owner is in fails `not_in_channel` — a judged-PASS digest must not die on authorship.
        # ONLY that refusal: a timeout may already have posted, and re-posting it as the owner
        # puts it in the channel twice. And only with the owner's identity switched on.
        identity = slack.USER
        ok = slack.post(channel, body, blocks=blocks, identity=identity)
    if ok:
        slack.mark_posted(run_id)
        return f"posted to slack thread ({channel}, as {identity})"
    return f"could not post to slack ({channel}, as {identity})"


def interim(reply_to, text):
    """A mid-run note in the asker's thread. Returns `(delivered, status, ts)`."""
    import slack
    ok = slack.post(reply_to.get("channel"), slack.to_mrkdwn(text),
                    thread_ts=reply_to.get("thread_ts"), identity=slack.identity_of(reply_to))
    return (bool(ok), ("interim posted to slack" if ok else "interim post failed"),
            ok if isinstance(ok, str) else None)


def source_line(reply_to):
    """Deliberately NOT the channel or user id: the id is a direct pointer into a private DM,
    and "Slack" plus the run id already gets the owner to the right place in the UI."""
    if reply_to.get("kind") == "slack_post":
        return "source: runbook"            # a destination, not where the run came from
    return "source: Slack message"


def on_gate_notice(reply_to, wid, notice_ts):
    """Arm the conversation for the gate its notice just announced (`activities.interim_notice`).

    A notice at a DM's top level grows its own thread, which the DM poll never reads — so its ts
    is recorded and that thread is read too. In a channel thread the notice is already a reply
    inside the watched thread, and Slack does not nest threads. BOT only: nobody can approve under
    the user identity (slack.may_approve), so reading its thread would spend the user token's
    rate limit for 25h on a pick that can never happen. A `slack_post` is one-way: nobody there
    can answer a gate, so nothing is armed."""
    if reply_to.get("kind") != "slack_thread":
        return
    import slack
    identity = slack.identity_of(reply_to)
    slack.mark_awaiting_gate(reply_to.get("channel"), reply_to.get("thread_ts"),
                             wid=wid, identity=identity,
                             notice_ts=(notice_ts if identity == slack.BOT
                                        and not reply_to.get("thread_ts") else None))


def on_delivered(reply_to, payload):
    """A conversation Otto just answered in becomes CONTINUABLE (`activities.deliver_result`):
    record the session id, the capability, and the reply text (which the handoff classifier
    reads next turn) so the next message resumes this conversation. Also clears the in-flight
    marker, whatever the delivery outcome — a jammed conversation would swallow every later
    message. A `slack_post` is a one-way destination: nobody asked, so no conversation is
    bound (`RunbookSlackDeliveryTests`)."""
    if reply_to.get("kind") != "slack_thread":
        return
    import slack
    # A silent turn still binds the session (the next message must resume it), but it must not
    # become `last_reply` — that's what the handoff classifier reads next turn, and "NO_REPLY"
    # is not something Otto said. Falsy leaves the previous reply in place.
    result = payload.get("result", "")
    slack.record_conversation_session(
        reply_to.get("channel"), reply_to.get("thread_ts"),
        session=payload.get("session_id"), cap=payload.get("cap"),
        last_reply="" if config.is_no_reply(result) else result,
        # The session belongs to the identity that ran it: resuming the bot's session under the
        # owner's token would answer as them, in a thread they were never part of.
        identity=slack.identity_of(reply_to),
        # Which run delivered: only its own gate (or its child's) is resolved by this.
        run_id=payload.get("run_id"))


INTEGRATION = Integration(
    name="slack",
    kinds=("slack_thread", "slack_post"),
    audiences={"slack_thread": CONVERSATION, "slack_post": NOTICE},
    deliver=deliver,
    source_line=source_line,
    interim=interim,
    on_gate_notice=on_gate_notice,
    on_delivered=on_delivered,
)
