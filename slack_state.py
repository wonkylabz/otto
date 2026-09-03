"""Pure decision core for the Slack listener — the state machine behind slack.py.

Every listener decision that has ever gone wrong lives here, in one place, as a deterministic
function: no I/O, no clock reads, no config reads. The clock (`now`) and every tunable (grace
windows, TTLs, bounds) arrive as ARGUMENTS, resolved by the shell (slack.py) from its module
globals at call time — which is also what lets tests keep monkeypatching `slack.MAX_THREADS` etc.

The state operated on is the plain dict persisted at data/slack-state.json:

    {"cursors":   {channel: ts},          # per-channel read cursor (top-level messages)
     "last_poll": epoch,                  # last COMPLETED poll (the downtime detector)
     "posted":    [run_id, ...],          # delivery idempotency (bounded)
     "posted_ts": [ts, ...],              # message ts WE posted (allow_self loop guard, bounded)
     "threads":   {conversation_key: {channel, thread_ts, cursor, wid, session, cap,
                                      last_reply, pending_at, at}}}

Mutators follow storage.mutate_json's contract: mutate `st` in place and return it, or return
storage.UNCHANGED when nothing moved (so the lock-holding write is skipped).
"""
import storage

UNCHANGED = storage.UNCHANGED

def empty():
    """A FRESH empty-state dict for every storage read/mutate default — mutators write into it
    in place when the file doesn't exist yet, so a shared constant here would leak state between
    calls (and between tests)."""
    return {"cursors": {}, "posted": []}


# --- timestamps & cursors ----------------------------------------------------

def normalize_ts(ts):
    """Slack's message-ts format: 10 digits + EXACTLY 6 decimals. Load-bearing, not cosmetic —
    `conversations.history(oldest=…)` with a 7-decimal value returns 0 messages and `ok: True`,
    so a cursor seeded from a raw `time.time()` (`str()` of it gives 7+ dp) makes that channel
    PERMANENTLY deaf: nothing is ever picked, so nothing ever overwrites the bad cursor.

    TRUNCATES rather than rounds (`%.6f` would round .3794477 UP to .379448) — a cursor must never
    advance past a message that hasn't been seen. Accepts a float or an already-formatted ts."""
    whole, _, frac = str(ts).partition(".")
    return f"{whole}.{(frac or '0')[:6].ljust(6, '0')}"


def past_cursor(ts, cur):
    """Whether a message is NEW relative to a cursor. The cursor bound is inclusive on Slack's
    side (`conversations.replies` even returns the thread parent whatever `oldest` says), so
    every poller compares explicitly rather than trusting the API's range."""
    return float(ts) > float(cur)


def advance_cursor(st, channel, ts):
    """Advance a channel's read cursor to `ts` — only ever forward, normalized to `normalize_ts`
    whatever the caller passes (a real message ts or a `time.time()` first-sight seed)."""
    st.setdefault("cursors", {})
    cur = st["cursors"].get(channel)
    if cur is None or float(ts) > float(cur):
        st["cursors"][channel] = normalize_ts(ts)
        return st
    return UNCHANGED


def first_sight_seed(now, grace_s):
    """Where a never-polled channel's cursor starts. First sight is not the same event as first
    message: a DM has usually existed for months and becomes eligible the moment its user is
    allowlisted, so seeding at `now` stamps whatever is already sitting there as READ — including
    a message sent seconds earlier, by the very person just added in order to answer them
    (observed 2026-08-05: both of Victor's messages became unanswerable — no run, no audit row,
    no error). The seed is `now - grace_s`: exactly the window `partition_backlog` keeps on a
    resuming poll, so "burn the backlog, keep what's live" means one thing on both paths."""
    return now - grace_s


def governs(msg):
    """Which cursor a picked message advances: a thread reply its own conversation's, everything
    else the channel's. A reply's ts is later than any still-unhandled top-level message, so
    advancing the channel on one would make Otto deaf to those. The ONE place this mapping lives."""
    return "conversation" if msg.get("in_thread") else "channel"


# --- downtime guard ----------------------------------------------------------

def record_poll(st, now):
    st["last_poll"] = float(now)
    return st


def is_resuming(last_poll, now, downtime_s):
    """Whether this poll follows a gap in listening. A cursor means "read up to here", which is
    only true while polling runs — a gap longer than `downtime_s` (or no completed poll ever)
    means Otto was not listening, and what piled up in the meantime was never its to answer."""
    return last_poll is None or (now - last_poll) > downtime_s


def partition_backlog(msgs, now, grace_s):
    """Split a resuming poll's pickings into (live, backlog). Backlog — anything older than
    `grace_s` — arrived while Otto was NOT listening and is burned (marked seen, never answered)
    rather than replayed hours late at whoever wrote in."""
    live, backlog = [], []
    for m in msgs:
        (backlog if now - float(m["ts"]) > grace_s else live).append(m)
    return live, backlog


# --- delivery idempotency ----------------------------------------------------

def record_posted(st, run_id, bound=500):
    """Record a run's result as delivered, so an activity retry doesn't double-post (Slack
    messages can't carry a hidden idempotency marker like the GitHub sink does)."""
    posted = st.setdefault("posted", [])
    if run_id in posted:
        return UNCHANGED
    posted.append(run_id)
    del posted[:-bound]
    return st


def record_posted_ts(st, ts, bound=500):
    """Remember a message ts WE posted (ack/answer), so allow_self test mode never answers our
    own posts."""
    arr = st.setdefault("posted_ts", [])
    if ts in arr:
        return UNCHANGED
    arr.append(ts)
    del arr[:-bound]
    return st


# --- conversations (continuity) ----------------------------------------------
# THE UNIT OF CONTINUITY IS A CONVERSATION, NOT A MESSAGE. A conversation Otto has answered in
# carries the session id of the run that last answered, so the next message RESUMES it instead of
# starting a cold, contextless run.
#
# What counts as one conversation depends on where it happens, and getting this wrong was the
# 2026-07-31 failure: continuity was keyed on the THREAD, but **a DM *is* the conversation** — Otto
# replied in-thread while the other person kept typing at channel level, as everyone does in a DM,
# so each message opened its own thread, resumed nothing, and arrived as a cold task. Ten runs in
# eight minutes, none of which knew what the conversation was about. In a multi-party CHANNEL the
# thread is the conversation (channel-level replies there would be spam), so:
#   DM               -> key "<channel>",             replies posted at channel level
#   channel thread   -> key "<channel>|<thread_ts>", replies posted in-thread
#
# (The store keeps its old name — "threads" — so pre-existing thread records stay valid: for a
# thread the key is byte-identical to what `thread_key` produced. DM records key on the channel.)

def conversation_key(channel, thread_ts=None):
    """The state key for one CONVERSATION. No thread means the channel itself is the conversation
    (a DM). Keep this the ONLY place that decides — a second opinion about what a conversation is,
    is how the DM case got lost."""
    return f"{channel}|{thread_ts}" if thread_ts else str(channel or "")


def prune_threads(threads, now, ttl_s, max_threads):
    """Drop timed-out conversations, then the oldest-active ones over `max_threads`."""
    live = {k: v for k, v in threads.items() if now - float(v.get("at") or 0) <= ttl_s}
    if len(live) > max_threads:
        keep = sorted(live.items(), key=lambda kv: float(kv[1].get("at") or 0))[-max_threads:]
        live = dict(keep)
    return live


def is_pending(rec, now, stale_s):
    """True while this conversation's previous run is still in flight (so the next message waits
    rather than racing its session). Bounded by `stale_s`: a run that dies without delivering
    must not deafen the conversation forever."""
    at = float((rec or {}).get("pending_at") or 0)
    if not at:
        return False
    return now - at < stale_s


def watch(st, channel, thread_ts, now, ttl_s, max_threads, wid=None, seen=None, pending=False):
    """Start (or refresh) tracking a conversation Otto is answering in. `seen` advances the
    conversation's own read cursor — only ever forward, and only used by thread polling (a DM
    reads through the channel cursor) — so the triggering message isn't re-picked as its own
    follow-up. `pending` marks a run as in flight (cleared by `record_session`)."""
    key = conversation_key(channel, thread_ts)
    threads = prune_threads(st.setdefault("threads", {}), now, ttl_s, max_threads)
    rec = dict(threads.get(key) or {})
    rec.update({"channel": channel, "thread_ts": thread_ts, "at": now})
    if wid:
        rec["wid"] = wid
    if seen is not None:
        cur = rec.get("cursor")
        if cur is None or float(seen) > float(cur):
            rec["cursor"] = normalize_ts(seen)
    if pending:
        rec["pending_at"] = now
    threads[key] = rec
    # Re-prune AFTER inserting so the store never rests over max_threads (the just-inserted
    # record is the newest, so it is never the one evicted). The prune before the read above is
    # separately load-bearing: it stops a TTL-expired record being resurrected with a fresh `at`.
    st["threads"] = prune_threads(threads, now, ttl_s, max_threads)
    return st


def record_session(st, channel, thread_ts, now, ttl_s, max_threads,
                   session=None, cap=None, last_reply=None):
    """Record what the NEXT message in this conversation needs in order to continue it: the Claude
    session id of the run that just answered, its capability, and that reply's text (which the
    handoff classifier reads to tell "answering you" from "here's a new task" — see
    engine.followup_handoff). Also clears the in-flight marker, which is what un-blocks the next
    message.

    A run that produced no session id (a failure, a skipped write) leaves the previous one in
    place rather than wiping it — the conversation stays continuable. Deliberately does NOT touch
    `wid`: that stays the run that OPENED the conversation, because it's the Chat-thread key every
    later turn appends to."""
    key = conversation_key(channel, thread_ts)
    threads = prune_threads(st.setdefault("threads", {}), now, ttl_s, max_threads)
    rec = dict(threads.get(key) or {})
    rec.update({"channel": channel, "thread_ts": thread_ts, "at": now})
    if session:
        rec["session"] = session
    if cap:
        rec["cap"] = cap
    if last_reply:
        rec["last_reply"] = str(last_reply)[:2000]
    rec.pop("pending_at", None)
    threads[key] = rec
    st["threads"] = prune_threads(threads, now, ttl_s, max_threads)   # same bound rule as watch()
    return st


# --- final poll shaping ------------------------------------------------------

def finalize(msgs, own_posts, max_per_poll):
    """De-dupe (a mention can also appear as a DM), order oldest-first for stable delivery, drop
    anything WE posted (loop guard for allow_self test mode — belt-and-braces on top of our
    replies being thread replies, which conversations.history doesn't return anyway), and cap."""
    seen, uniq = set(), []
    for m in sorted(msgs, key=lambda x: float(x["ts"])):
        k = (m["channel"], m["ts"])
        if k not in seen and m["ts"] not in own_posts:
            seen.add(k)
            uniq.append(m)
    return uniq[:max_per_poll]
