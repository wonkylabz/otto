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
carry a matching HMAC-SHA256 signature of the raw body. Unmatched events are ignored (no-op), so
only operator-configured event types ever trigger work.
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

# Replay window (seconds). A valid signature seen twice within this window is treated as a replay;
# an optional X-Otto-Timestamp header, if present, must be within this window of now.
REPLAY_WINDOW_S = int(os.environ.get("OTTO_EVENT_REPLAY_WINDOW_S", "300"))
_SEEN = {}   # signature -> expiry epoch (in-memory replay guard; a restart forgets, acceptable)


def enabled():
    return bool(SECRET)


def verify_sig(raw, signature):
    """True if `signature` is the HMAC-SHA256 (hex) of `raw` under OTTO_EVENT_SECRET."""
    if not SECRET:
        return False
    expected = hmac.new(SECRET.encode(), raw or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


def timestamp_fresh(ts_header, now=None, window=None):
    """When an X-Otto-Timestamp header IS present, require it within `window` seconds of now
    (blocks a captured request re-sent much later). Missing header -> True (backward-compatible;
    the signature-dedup below still blocks exact replays). Unparseable header -> False."""
    if not ts_header:
        return True
    try:
        ts = float(ts_header)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else now
    return abs(now - ts) <= (window or REPLAY_WINDOW_S)


def is_replay(signature, now=None):
    """True if this exact signature was already seen within the replay window (an exact webhook
    replay of an already-processed request); records it and prunes expired entries otherwise.
    Empty signature -> False (the signature check upstream already rejected it)."""
    if not signature:
        return False
    now = time.time() if now is None else now
    for k in [k for k, exp in _SEEN.items() if exp < now]:
        _SEEN.pop(k, None)
    if signature in _SEEN:
        return True
    _SEEN[signature] = now + REPLAY_WINDOW_S
    return False


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
