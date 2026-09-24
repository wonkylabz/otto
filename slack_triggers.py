"""Slack triggers — a bot's post in a watched channel becomes an unattended run.

The event ingress for systems that already talk to Slack (a New Relic alert, a PagerDuty page, a
Grafana notification, AWS DevOps Agent's findings) but cannot reach Otto: Otto polls outward, so
nothing is exposed. Rides the `slack-poll` schedule (`activities.poll_slack`), reads with the
rule's own identity's token, and normalizes a matching post into a request with the SAME template
language as a webhook rule (`events.render`).

Deliberately NOT a webhook rule with `source: "slack"`: those match `POST /api/events/<source>`,
so a signed POST to `/api/events/slack` would fire an alert trigger with a forged body.

Rules live in `data/slack-triggers.json`:

    [{ "id": "a1b2c3",                    # minted on save; keys the dedupe
       "channels": ["C0123ABCD"],          # every channel this rule watches
       "bots": ["B01NEWRELIC"],            # bot_id / app_id / app name allowed to fire it (required)
       "match": "(?i)\\bopened\\b",        # regex over the post's text; empty = every post
       "key": "Issue ID: (\\w+)",          # group 1 dedupes; absent = one run per post
       "template": "Investigate this alert: {text}",
       "cap": "incident-responder",    # optional pin, resolved against the trusted registry
       "approval": "ask",                  # auto | ask | skip
       "identity": "bot",                  # which token reads (and replies)
       "reply_in_thread": true,
       "max_age_s": 900 }]

Decisions are PURE functions below; `poll` is the shell doing I/O.
"""
import hashlib
import os
import re
import time
import uuid

import config
import events
import slack_state
import storage
from ui import trace

_RULES = os.path.join(config.DATA_DIR, "slack-triggers.json")
_STATE = os.path.join(config.DATA_DIR, "slack-triggers-state.json")

MAX_AGE_S = 900             # an alert older than this is history, not work — after downtime too
DEDUPE_TTL_S = 24 * 3600    # one run per incident key for this long
_MAX_FIRED = 2000
_MAX_TEXT = 4000
_APPROVALS = ("auto", "ask", "skip")


# --- rules -----------------------------------------------------------------

def load_rules():
    """Re-normalized on read, so a rule stored before a normalization fix is served fixed."""
    return [r for r in (normalize(x) for x in storage.read_json(_RULES, [])) if r]


def rule_enabled(rule):
    return (rule or {}).get("enabled", True) is not False


def normalize(rule):
    """A storable rule, or None. PURE apart from minting an id. A rule with no `bots` is refused:
    "any author" in a shared channel lets whoever can post there start work."""
    if not isinstance(rule, dict):
        return None
    raw = rule.get("channels") or [rule.get("channel")]
    channels = list(dict.fromkeys(c for c in (str(x or "").strip().strip(".,;#") for x in raw) if c))
    template = str(rule.get("template") or "").strip()
    bots = [b for b in (str(x).strip().strip(".,;") for x in (rule.get("bots") or [])) if b]
    if not (channels and template and bots):
        return None
    for pat in (rule.get("match"), rule.get("key")):
        if pat:
            try:
                re.compile(pat)
            except re.error:
                return None
    out = {"id": str(rule.get("id") or uuid.uuid4().hex[:8]), "channels": channels, "bots": bots,
           "template": template,
           "approval": rule.get("approval") if rule.get("approval") in _APPROVALS else "ask",
           "identity": "user" if rule.get("identity") == "user" else "bot",
           "reply_in_thread": rule.get("reply_in_thread", True) is not False}
    for k in ("match", "key", "cap"):
        if str(rule.get(k) or "").strip():
            out[k] = str(rule[k]).strip()
    try:
        out["max_age_s"] = max(60, int(rule.get("max_age_s") or MAX_AGE_S))
    except (TypeError, ValueError):
        out["max_age_s"] = MAX_AGE_S
    if rule.get("enabled") is False:
        out["enabled"] = False
    return out


def save_rules(rules):
    clean = [r for r in (normalize(x) for x in (rules or [])) if r]
    storage.write_json(_RULES, clean)
    return clean


def any_active(rules=None):
    return any(rule_enabled(r) for r in (rules if rules is not None else load_rules()))


# --- pure decisions ----------------------------------------------------------

def message_text(m):
    """Everything a human would read in the post. Integrations rarely use `text`: New Relic and
    PagerDuty put the alert in attachments or Block Kit, leaving `text` empty or a bare fallback."""
    parts = [m.get("text") or ""]
    for a in m.get("attachments") or []:
        parts += [a.get(k) or "" for k in ("pretext", "title", "text")]
        parts += [f"{f.get('title', '')}: {f.get('value', '')}" for f in a.get("fields") or []]
        if not any(a.get(k) for k in ("pretext", "title", "text")):
            parts.append(a.get("fallback") or "")

    def walk(node):
        if isinstance(node, dict):
            t = node.get("text")
            if isinstance(t, str) and node.get("type") in ("plain_text", "mrkdwn", "text"):
                parts.append(t)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(m.get("blocks") or [])
    seen, out = set(), []
    for p in (p.strip() for p in parts):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "\n".join(out)[:_MAX_TEXT]


def author_ok(rule, m):
    """Only a listed BOT fires a rule — a human's message never does, whatever it says."""
    if not m.get("bot_id"):
        return False
    names = {m.get("bot_id"), m.get("app_id"), (m.get("bot_profile") or {}).get("name"),
             m.get("username")}
    names = {str(n).lower() for n in names if n}
    return any(str(b).lower() in names for b in rule.get("bots") or [])


def match(rule, text):
    """The regex's named groups (possibly empty) on a match, else None."""
    pat = rule.get("match")
    if not pat:
        return {}
    m = re.search(pat, text or "")
    return {k: v for k, v in m.groupdict().items() if v is not None} if m else None


def dedupe_key(rule, text, ts):
    pat = rule.get("key")
    if pat:
        m = re.search(pat, text or "")
        if m:
            return (m.group(1) if m.groups() else m.group(0)).strip()
    return f"ts:{ts}"


def wid_for(rule_id, key):
    """Deterministic, so REJECT_DUPLICATE makes a re-poll a no-op. Hashed: a key is alert text."""
    return "evt-s-" + hashlib.sha256(f"{rule_id}|{key}".encode()).hexdigest()[:12]


def fresh(ts, now, max_age_s):
    try:
        return now - float(ts) <= max_age_s
    except (TypeError, ValueError):
        return False


def pick(rules, m, now, channel):
    """(rule, payload, key) for the first enabled rule a post in `channel` fires, or None. PURE."""
    text = message_text(m)
    for r in rules:
        if not (rule_enabled(r) and author_ok(r, m) and fresh(m.get("ts"), now, r.get("max_age_s", MAX_AGE_S))):
            continue
        groups = match(r, text)
        if groups is None:
            continue
        key = dedupe_key(r, text, f"{channel}:{m.get('ts')}")
        payload = {**groups, "text": text, "channel": channel, "ts": m.get("ts"),
                   "bot": (m.get("bot_profile") or {}).get("name") or m.get("username") or "",
                   "key": key}
        return r, payload, key
    return None


def to_params(rule, payload, wid):
    request = events.render(rule["template"], payload).strip()
    if not request:
        return None
    params = {"request": request, "unattended": True, "cap": rule.get("cap"),
              "approval": rule["approval"], "chat_key": wid,
              "chat_title": (payload.get("text") or "")[:80],
              "chat_labels": ["slack-trigger"]}
    if rule.get("reply_in_thread", True):
        params["reply_to"] = {"kind": "slack_thread", "channel": payload["channel"],
                              "thread_ts": payload.get("ts"), "identity": rule["identity"]}
    return params


# --- state -------------------------------------------------------------------

def _st():
    return storage.read_json(_STATE, {"cursors": {}, "fired": {}})


def _cursor_key(identity, channel):
    return f"{identity}|{channel}"


def _advance(identity, channel, ts):
    def fn(st):
        cur = st.setdefault("cursors", {}).get(_cursor_key(identity, channel))
        if cur is None or slack_state.past_cursor(ts, cur):
            st["cursors"][_cursor_key(identity, channel)] = slack_state.normalize_ts(ts)
            return st
        return storage.UNCHANGED
    storage.mutate_json(_STATE, fn, {"cursors": {}, "fired": {}})


def already_fired(key_id, now=None):
    exp = (_st().get("fired") or {}).get(key_id)
    return isinstance(exp, (int, float)) and exp > (now or time.time())


def _record_fired(key_id, now):
    def fn(st):
        fired = {k: v for k, v in (st.get("fired") or {}).items()
                 if isinstance(v, (int, float)) and v > now}
        fired[key_id] = now + DEDUPE_TTL_S
        if len(fired) > _MAX_FIRED:
            fired = dict(sorted(fired.items(), key=lambda kv: kv[1])[-_MAX_FIRED:])
        st["fired"] = fired
        return st
    storage.mutate_json(_STATE, fn, {"cursors": {}, "fired": {}})


# --- the shell -----------------------------------------------------------------

PAGE = 200
MAX_PAGES = 10


def history(identity, channel, oldest):
    """Every post after `oldest`, paged: (messages, error). Slack pages NEWEST first, so stopping
    at page one silently skipped the oldest posts of a storm. Messages None = nothing is usable
    (the cursor must not move); a list with an error = the page cap hit, oldest beyond it lost."""
    import slack
    out, cursor = [], None
    for _ in range(MAX_PAGES):
        res = slack._api("conversations.history", identity=identity, channel=channel,
                         oldest=oldest, limit=PAGE, cursor=cursor)
        if not res.get("ok"):
            return None, res.get("error") or "history failed"
        out += res.get("messages") or []
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not (res.get("has_more") and cursor):
            return out, None
    return out, f"over {PAGE * MAX_PAGES} posts since the last poll; older ones skipped"

def poll(resolve_cap, now=None):
    """Read each watched channel once, start a run per post a rule fires. Never raises.

    `resolve_cap(name)` -> {name, kind, risk} | None: a pinned cap's RISK comes from the trusted
    registry, never from the stored rule. The pause is checked before any channel is read, so a
    cursor never advances past an alert while stopped. A FAILED start stops that channel's cursor
    where it is, so the post is retried next poll."""
    import estop
    import ingress
    if estop.blocked("events"):
        return {"triggered": [], "paused": True}
    rules = [r for r in load_rules() if rule_enabled(r)]
    now = time.time() if now is None else now
    started, dupes, errors = [], [], []
    by_channel = {}
    for r in rules:
        for ch in r["channels"]:
            by_channel.setdefault((r["identity"], ch), []).append(r)
    for (identity, channel), rs in by_channel.items():
        try:
            max_age = max(r.get("max_age_s", MAX_AGE_S) for r in rs)
            cur = (_st().get("cursors") or {}).get(_cursor_key(identity, channel))
            floor = slack_state.normalize_ts(now - max_age)
            if cur is None or slack_state.past_cursor(floor, cur):
                cur = floor                   # first sight, or a gap: never replay stale alerts
            got, err = history(identity, channel, cur)
            if err:
                errors.append(f"{channel}: {err}")
            if got is None:
                continue
            msgs = sorted((m for m in got
                           if m.get("ts") and slack_state.past_cursor(m["ts"], cur)),
                          key=lambda m: float(m["ts"]))
            for m in msgs:
                hit = pick(rs, m, now, channel)
                if hit:
                    rule, payload, key = hit
                    key_id = f"{rule['id']}|{key}"
                    wid = wid_for(rule["id"], key)
                    if already_fired(key_id, now):
                        dupes.append(wid)
                    else:
                        params = to_params(rule, payload, wid)
                        if params and params.get("cap"):
                            params["cap"] = resolve_cap(params["cap"])
                            if params["cap"] is None:
                                errors.append(f"rule {rule['id']} pins an unknown capability")
                                params = None
                        if params:
                            status = ingress.start_run(wid, params, estop_key="events",
                                                       trace_tag="SLACKTRIG")
                            if status == ingress.FAILED:
                                break                 # cursor stays; retried next poll
                            _record_fired(key_id, now)
                            (started if status == ingress.STARTED else dupes).append(wid)
                _advance(identity, channel, m["ts"])
        except Exception as e:  # noqa: BLE001 - one bad channel must not stop the rest
            errors.append(f"{channel}: {str(e)[:100]}")
    if started:
        trace("SLACKTRIG", f"started {len(started)} run(s) from Slack triggers")
    for e in errors:
        trace("SLACKTRIG", e)
    if by_channel:
        storage.mutate_json(_STATE, lambda st: {**st, "last_poll": now, "last_errors": errors[:10]},
                            {"cursors": {}, "fired": {}})
    return {"triggered": started, "duplicate": dupes, "errors": errors}
