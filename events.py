"""Event / webhook ingress adapter.

A third ingress alongside the web chat (interactive) and Temporal Schedules (time): external
systems POST an event (a New Relic alert, a GitHub webhook, an email hook, …) and a matching
**rule** normalizes the payload into a `OttoWorkflow` request. Like schedules, event-triggered
runs are **unattended** — no human is present, so clarification is skipped and writes only run
when the rule opts into `auto_approve` (otherwise gated/skipped by the workflow).

Rules live in `data/event-rules.json` (hot-editable, like `data/schedules.json`):

    [{ "source": "newrelic",            # matches POST /api/events/newrelic
       "when": {"event_type": "INCIDENT"},   # optional dotted-path equality filters
       "template": "Investigate this alert: {condition_name} on {targets.0.name}",
       "cap": "incident",               # optional: pin a capability (skip routing)
       "auto_approve": false,           # pre-authorize writes for this rule
       "reply_to": {"kind": "webhook", "url": "https://…"} }]

Security: the endpoint is disabled unless `OTTO_EVENT_SECRET` is set, and every request must
carry BOTH an `X-Otto-Timestamp` (unix seconds) and an `X-Otto-Signature` holding

    HMAC-SHA256(OTTO_EVENT_SECRET, f"{timestamp}.".encode() + raw_body)   # hex

— the Slack/Stripe construction. Signing the timestamp is what makes it anti-replay: a captured
request cannot be re-dated, and a request with no timestamp (or a stale one) is refused outright,
so the signature is only good for `REPLAY_WINDOW_S` seconds. Unmatched events are ignored (no-op),
so only operator-configured event types ever trigger work.
"""
import hashlib
import hmac
import json
import os
import re
import time

import config
import storage

SECRET = config.secret("OTTO_EVENT_SECRET")
_RULES = os.path.join(config.DATA_DIR, "event-rules.json")
# Seen-signature ring. On disk, not in memory: a restart used to forget every signature, which
# handed an attacker (or a buggy sender) a free replay of anything captured before it.
_SEEN_FILE = os.path.join(config.DATA_DIR, "event-replay.json")

# Replay window (seconds). The X-Otto-Timestamp header is REQUIRED and signed, so a captured
# request is only valid for this long; within it, a signature seen twice is a replay.
REPLAY_WINDOW_S = int(os.environ.get("OTTO_EVENT_REPLAY_WINDOW_S", "300"))
# Cap on the on-disk ring, so a flood of distinct signatures can't grow the file without bound.
# Pruning by expiry normally keeps it far under this; the cap is the backstop.
_SEEN_MAX = 5000


def enabled():
    return bool(SECRET)


def signing_payload(timestamp, raw):
    """The exact bytes the MAC covers: `<timestamp>.` + the raw body. The timestamp is INSIDE the
    MAC (Slack/Stripe construction) — a MAC over the body alone leaves the header freely editable,
    which is the same as having no freshness check at all."""
    return f"{'' if timestamp is None else timestamp}.".encode() + (raw or b"")


def sign(timestamp, raw, secret=None):
    """Produce the hex signature a sender puts in X-Otto-Signature. Exists so senders, tests and
    the docs all derive it from ONE implementation."""
    key = secret if secret is not None else SECRET
    return hmac.new((key or "").encode(), signing_payload(timestamp, raw), hashlib.sha256).hexdigest()


def verify_sig(raw, signature, timestamp=None):
    """True if `signature` is the HMAC-SHA256 (hex) of `<timestamp>.<raw>` under OTTO_EVENT_SECRET.

    Freshness is NOT checked here — `timestamp_fresh` owns that; this only proves the sender knew
    the secret AND committed to that timestamp."""
    if not SECRET:
        return False
    return hmac.compare_digest(sign(timestamp, raw), (signature or "").strip())


def timestamp_fresh(ts_header, now=None, window=None):
    """Require an X-Otto-Timestamp within `window` seconds of now. A MISSING header is refused:
    when it was optional, a replayer simply dropped it and the captured signature stayed valid
    forever. Unparseable -> False."""
    if not ts_header:
        return False
    try:
        ts = float(ts_header)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    return abs(now - ts) <= (window or REPLAY_WINDOW_S)


def _prune(seen, now):
    """Drop expired signatures, then trim to the cap (oldest expiry first)."""
    live = {k: exp for k, exp in seen.items()
            if isinstance(exp, (int, float)) and exp > now}
    if len(live) > _SEEN_MAX:
        keep = sorted(live.items(), key=lambda kv: kv[1], reverse=True)[:_SEEN_MAX]
        live = dict(keep)
    return live


def claim_signature(signature, now=None):
    """Reserve a signature for this request. True if it is the first sighting in the window (go
    ahead), False if an identical request was already accepted (a replay).

    CLAIM, not record: the caller must `release_signature` on any path that does not commit the
    event, or a sender's legitimate retry after a 400/500 is rejected as a replay and the alert is
    lost for the whole window. Same shape as `delivery._claim`/`_release`.

    A failing store fails OPEN (treated as not-a-replay): the signed, time-bounded MAC is the real
    anti-replay control, and a guard that can't read its own file must not take the ingress down.
    An empty signature returns False — it never reaches here (verify_sig rejects it first), and
    refusing is the safe answer either way.
    """
    if not signature:
        return False
    now = time.time() if now is None else now
    won = []

    def fn(seen):
        seen = _prune(seen, now)
        if signature in seen:
            won.append(False)
            return seen                      # pruned; the claim itself is left standing
        seen[signature] = now + REPLAY_WINDOW_S
        won.append(True)
        return seen

    try:
        storage.mutate_json(_SEEN_FILE, fn, {})
    except Exception:  # noqa: BLE001 - the guard must never block the ingress it guards
        return True
    return won[0] if won else True


def release_signature(signature):
    """Undo a claim whose event was then rejected or failed to start, so the sender's retry with
    the identical body is accepted rather than swallowed as a duplicate."""
    if not signature:
        return

    def fn(seen):
        if signature in (seen or {}):
            seen.pop(signature, None)
            return seen
        return storage.UNCHANGED

    try:
        storage.mutate_json(_SEEN_FILE, fn, {})
    except Exception:  # noqa: BLE001
        pass


def load_rules():
    if os.path.exists(_RULES):
        try:
            with open(_RULES) as f:
                return json.load(f)
        except ValueError:
            return []
    return []


def save_rules(rules):
    """Replace the whole rule set (the Admin tab edits the list client-side). Keeps only
    well-formed rules — each needs a `source` and a `template`."""
    clean = [r for r in (rules or [])
             if isinstance(r, dict) and (r.get("source") or "").strip() and (r.get("template") or "").strip()]
    storage.write_json(_RULES, clean)
    return clean


def _get(payload, path):
    """Dotted-path lookup into a nested payload; supports list indices (a.0.b)."""
    cur = payload
    for key in str(path).split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def rule_enabled(rule):
    """Whether a rule may fire. ABSENT means enabled — rules authored before the Events tab grew a
    toggle carry no `enabled` key, and defaulting those to off would silently stop working ingresses
    on upgrade. Only an explicit `enabled: false` disables one."""
    return (rule or {}).get("enabled", True) is not False


def _matches(rule, payload):
    return all(_get(payload, k) == v for k, v in (rule.get("when") or {}).items())


def render(template, payload):
    """Substitute {dotted.path} tokens in a template with payload values."""
    def sub(m):
        val = _get(payload, m.group(1))
        return "" if val is None else str(val)
    return re.sub(r"\{([\w.]+)\}", sub, template or "")


def _approval(rule):
    """Write-approval mode for a rule: 'auto' | 'ask' | 'skip'. `auto_approve: true` (legacy)
    maps to 'auto'; default is 'skip'."""
    mode = rule.get("approval")
    if mode in ("auto", "ask", "skip"):
        return mode
    return "auto" if rule.get("auto_approve") else "skip"


def to_request(source, payload, rules=None):
    """Normalize an event into a workflow request via the first matching ENABLED rule, or None to
    ignore. Returns {request, cap, approval, reply_to}; `cap` is just a name here — the
    server resolves it against the trusted registry (so a rule can't forge a capability's risk).

    A disabled rule is skipped for matching entirely, so a later enabled rule for the same source
    still gets its chance (disabling the first rule doesn't shadow the rest)."""
    rule = next((r for r in (rules if rules is not None else load_rules())
                 if r.get("source") == source and rule_enabled(r) and _matches(r, payload)), None)
    if not rule:
        return None
    request = render(rule.get("template", ""), payload).strip()
    if not request:
        return None
    return {"request": request, "cap": rule.get("cap"),
            "approval": _approval(rule), "reply_to": rule.get("reply_to")}
