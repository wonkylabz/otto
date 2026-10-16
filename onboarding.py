"""First-run onboarding in the UI — asks how the operator plans to use Otto, then shows what that
means for THIS install: a settings diff for the cost profile, and which integrations the chosen
uses need versus what is already connected.

Three calls, the same shape as `snapshot`'s import: `state` (prefill + whether to auto-open),
`preview` (every change `apply` would make, plus a fingerprint) and `apply` (refuses unless the
fingerprint still matches, so it writes exactly what the operator read).

  * **Idempotent.** A profile is a full assignment of its keys, never a delta: applying the same
    answers twice converges on the same settings, and a second apply has an empty diff. Re-running
    from the header is always offered.
  * **Detect + guide, never configure secrets.** Integrations are reported as ready/missing with a
    pointer to the screen that sets them; tokens and connectors stay in their own forms.
  * **UI/config only.** The answers prefill a re-run and pick the recommendations; nothing a run
    reads looks at them.
"""
import hashlib
import json
import os
import re
import shutil
import time

import config
import storage

_STATE = os.path.join(config.DATA_DIR, "onboarding.json")

# Bumped when the questions change meaning, so an install onboarded on an older set is offered the
# new one once instead of keeping answers that no longer map onto anything.
VERSION = 1

USAGES = ("personal", "work", "both")

# Every profile assigns the SAME keys, so switching profile never strands a value the previous one
# set. "balanced" is the code defaults — `save_settings` then removes those keys from the store.
_PROFILE_KEYS = ("max_attempts", "route_confirmations", "judge_confirmations",
                 "max_review_rounds", "max_qa_rounds", "effort", "supervise",
                 "budget_soft_usd", "budget_hard_usd")
PROFILES = {
    "economic": {
        "label": "Economic",
        "blurb": "Fewer retries and second opinions, lower reasoning effort, a per-run spend "
                 "ceiling. Cheapest runs; expect more answers that need a nudge.",
        "settings": {"max_attempts": 2, "route_confirmations": 1, "judge_confirmations": 1,
                     "max_review_rounds": 1, "max_qa_rounds": 1, "effort": "low",
                     "supervise": False, "budget_soft_usd": 1.0, "budget_hard_usd": 3.0},
    },
    "balanced": {
        "label": "Balanced",
        "blurb": "Otto's defaults: three attempts, re-sampled routing and judging, no spend cap.",
        "settings": None,      # resolved to the code defaults at plan time
    },
    "quality": {
        "label": "Quality",
        "blurb": "Defaults plus high reasoning effort. Slowest and most expensive per run.",
        "settings": {"effort": "high"},
    },
}

# What each use needs connected. `match` is tested against every MCP server/connector name Otto
# can see; `where` is the screen that sets it ({tab, sect} → the UI's data-sect).
INTEGRATIONS = {
    "slack":    {"label": "Slack", "default_for": ("work", "both"),
                 "match": r"slack", "where": {"tab": "events", "sect": "ev-slack"},
                 "hint": "Add a Slack user or bot token (setup_wizard.py or .env), then switch the "
                         "listener on in Events → Slack."},
    "email":    {"label": "Email", "default_for": ("personal", "work", "both"),
                 "match": r"gmail|outlook|e-?mail|\bmail", "where": {"tab": "admin", "sect": "mcp"},
                 "hint": "Connect Gmail (or another mail connector) in claude.ai → Settings → "
                         "Connectors; Otto inherits it. Runs on a local model can't use it."},
    "calendar": {"label": "Calendar", "default_for": ("personal", "both"),
                 "match": r"calendar", "where": {"tab": "admin", "sect": "mcp"},
                 "hint": "Connect Google Calendar in claude.ai → Settings → Connectors."},
    "docs":     {"label": "Docs & notes", "default_for": (),
                 "match": r"notion|drive|docs|confluence|obsidian",
                 "where": {"tab": "admin", "sect": "mcp"},
                 "hint": "Connect Notion/Drive in claude.ai, or add a stdio server in Admin → "
                         "MCP servers."},
    "github":   {"label": "GitHub & code repos", "default_for": ("work", "both"),
                 "where": {"tab": "admin", "sect": "projects"},
                 "hint": "Install `gh` and run `gh auth login`, then register a repo in Admin → "
                         "Project repos so Otto can open PRs from an isolated clone."},
    "push":     {"label": "Phone notifications", "default_for": (),
                 "where": {"tab": "admin", "sect": "settings"},
                 "hint": "Set OTTO_NTFY_TOPIC (setup_wizard.py) so approvals and blocked runs "
                         "reach your phone."},
    "local":    {"label": "Local models", "default_for": (),
                 "where": {"tab": "admin", "sect": "models"},
                 "hint": "Add an OpenAI-compatible endpoint in Admin → Models to run cheap tasks "
                         "on your own hardware."},
}


class StalePreview(Exception):
    """The plan changed between preview and apply — nothing was written."""


def _load():
    d = storage.read_json(_STATE, {})
    return d if isinstance(d, dict) else {}


def state():
    """What the UI needs on boot: prior answers to prefill, and whether to open by itself — only
    when the operator has neither finished nor skipped the current VERSION."""
    d = _load()
    done = d.get("version") == VERSION and (d.get("completed_at") or d.get("skipped_at"))
    return {"version": VERSION, "auto_open": not done, "answers": d.get("answers") or {},
            "completed_at": d.get("completed_at"), "skipped_at": d.get("skipped_at"),
            "usages": list(USAGES),
            "profiles": [{"key": k, "label": p["label"], "blurb": p["blurb"]}
                         for k, p in PROFILES.items()],
            "integrations": [{"key": k, "label": i["label"], "default_for": list(i["default_for"])}
                             for k, i in INTEGRATIONS.items()]}


def normalize(answers):
    """Validate the client's answers; unknown values fall back rather than raise, because a stale
    browser tab must still be able to finish onboarding."""
    a = answers if isinstance(answers, dict) else {}
    usage = a.get("usage") if a.get("usage") in USAGES else "personal"
    profile = a.get("profile") if a.get("profile") in PROFILES else "balanced"
    picked = a.get("integrations")
    picked = {x for x in picked if isinstance(x, str)} if isinstance(picked, list) else set()
    wants = [k for k in INTEGRATIONS if k in picked]
    return {"usage": usage, "profile": profile, "integrations": wants}


def profile_settings(profile, cur=None):
    """The full assignment a profile makes, every _PROFILE_KEYS key included."""
    cur = cur or config.settings_all()
    base = {k: cur[k]["default"] for k in _PROFILE_KEYS}
    return {**base, **(PROFILES[profile]["settings"] or {})}


def _settings_plan(profile):
    cur = config.settings_all()
    out = []
    for name, target in profile_settings(profile, cur).items():
        s = cur[name]
        out.append({"name": name, "before": s["value"], "after": target,
                    "pinned": s["env_pinned"], "env": s["env"],
                    "action": "keep" if s["value"] == target
                    else ("pinned" if s["env_pinned"] else "set")})
    return out


def _mcp_names(mcps):
    return [f"{m.get('name', '')} {m.get('display', '')}".lower()
            for m in mcps if m.get("enabled", True)]


def _detect(key, mcps, connectors_checked=True):
    """(ready, detail) for one integration; ready is None when it cannot be told yet. Never runs
    `claude mcp list` — the preview sits behind a spinner — so a connector cache that has never
    been filled is "not checked yet", not "missing": on a first run that is the usual state."""
    spec = INTEGRATIONS[key]
    if key == "slack":
        # The INGRESS, not a tool: a Slack MCP server or connector lets a run post, but only a
        # token plus a switched-on identity makes Otto answer anyone.
        # Slack's own rule for "listening", plus a non-empty allowlist: both lists empty means
        # Otto answers nobody, which is "on" only on paper.
        import slack            # noqa: PLC0415 — slack pulls in Temporal-free but heavy imports
        cfg = slack.load()
        names = {slack.USER: "as you", slack.BOT: "as a bot"}
        live = [i for i in slack.enabled_identities(cfg)
                if any(slack.allow_ids(cfg, k) for k in slack.allow_keys(i))
                or (i == slack.USER and cfg.get("allow_self"))]
        if live:
            return True, "listening " + " and ".join(names[i] for i in live)
        if slack.enabled_identities(cfg):
            return False, "switched on, but its allowlist is empty — it answers nobody"
        toks = [n for n, i in (("user", slack.USER), ("bot", slack.BOT)) if slack.token_set(i)]
        return False, (f"{' and '.join(toks)} token set, listener off" if toks
                       else "no Slack token")
    if key == "github":
        import pr_review        # noqa: PLC0415
        import workspace        # noqa: PLC0415
        if not shutil.which("gh"):
            return False, "`gh` not on PATH"
        # The CACHED login only: resolving it is a `gh api` call of up to 30s.
        who, repos = pr_review.viewer_cached(), len(workspace.git_repos())
        if who is None:
            return None, "sign-in not checked yet — the Events tab's GitHub card checks it"
        if not who:
            return False, "`gh` is installed but not signed in"
        if not repos:
            return False, f"signed in as {who}, no project repo registered"
        return True, f"signed in as {who}, {repos} repo(s) registered"
    if key == "push":
        ok = bool(config.secret("OTTO_NTFY_TOPIC"))
        return ok, ("OTTO_NTFY_TOPIC set" if ok else "OTTO_NTFY_TOPIC not set")
    if key == "local":
        import gateway          # noqa: PLC0415
        # CLASS, not transport: a hosted model on an OpenAI-compatible URL is not "your hardware".
        local = [m["name"] for m in gateway.load().get("pool", []) if gateway.model_kind(m) == "local"]
        return bool(local), (", ".join(local) if local else "no local model configured")
    hits = [n.split()[0] for n in _mcp_names(mcps) if re.search(spec["match"], n)]
    if hits:
        return True, ", ".join(hits)
    if not connectors_checked:
        return None, "not checked yet — Admin → MCP servers lists your claude.ai connectors"
    return False, "nothing connected"


def _integrations_plan(wants, mcps, connectors_checked=True):
    out = []
    for key in wants:
        ready, detail = _detect(key, mcps, connectors_checked)
        spec = INTEGRATIONS[key]
        out.append({"key": key, "label": spec["label"], "ready": ready, "detail": detail,
                    "hint": "" if ready else spec["hint"], "where": spec["where"]})
    return out


def _fingerprint(a, settings):
    changes = [{"name": s["name"], "after": s["after"]} for s in settings if s["action"] == "set"]
    return hashlib.sha256(json.dumps({"answers": a, "changes": changes}, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def _plan(answers, mcps, connectors_checked=True):
    a = normalize(answers)
    settings = _settings_plan(a["profile"])
    fp = _fingerprint(a, settings)
    notes = []
    if a["profile"] == "economic":
        notes.append("Biggest further saving: route cheap phases (routing, clarify, memory) to a "
                     "smaller or local model in Admin → Models.")
    if any(s["action"] == "pinned" for s in settings):
        notes.append("Some settings are pinned by an environment variable and keep that value. "
                     "To use this profile's instead, remove them from .env, restart, and run "
                     "Setup again.")
    return {"answers": a, "fingerprint": fp, "settings": settings,
            "integrations": _integrations_plan(a["integrations"], mcps, connectors_checked),
            "notes": notes}


def preview(answers, mcps=(), connectors_checked=True):
    """Every change `apply` would make, plus the fingerprint it must be given back."""
    return _plan(answers, list(mcps), connectors_checked)


def apply(answers, expect):
    """Write the settings `preview` showed and record the answers. Raises StalePreview when the
    install changed in between. Only the SETTINGS are re-derived — the fingerprint covers nothing
    else, so integration detection is the preview's alone."""
    a = normalize(answers)
    settings = _settings_plan(a["profile"])
    if _fingerprint(a, settings) != expect:
        raise StalePreview("settings changed since the preview — review again")
    changes = {s["name"]: s["after"] for s in settings if s["action"] == "set"}
    if changes:
        config.save_settings(changes)
    now = time.time()
    storage.mutate_json(_STATE, lambda d: {"version": VERSION, "answers": a,
                                           "completed_at": now}, default={})
    return {"applied": sorted(changes)}


def skip(answers=None):
    """Remember the operator dismissed it, so it stops opening by itself. Draft `answers` (from a
    wizard closed part-way) are kept to prefill the next run; without them the stored ones stay."""
    now = time.time()

    def _mut(d):
        d = d if isinstance(d, dict) else {}
        out = {**d, "version": VERSION, "skipped_at": now}
        # Keep only what was actually answered: a draft closed on the welcome step must not
        # store "personal" as a choice, nor [] as a deliberate "no integrations".
        if isinstance(answers, dict) and answers.get("usage") in USAGES:
            draft = normalize(answers)
            if not isinstance(answers.get("integrations"), list):
                draft.pop("integrations")
            out["answers"] = draft
        return out
    storage.mutate_json(_STATE, _mut, default={})
    return state()
