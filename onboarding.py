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
DRAFT_TTL_S = 24 * 3600

# Every profile but "keep" assigns the SAME keys, so switching profile never strands a value the
# previous one set. "balanced" is the code defaults — `save_settings` then removes those keys.
# Route and judge re-sampling are deliberately NOT here: one sample is a coin flip, and the cost
# of a misroute or a false FAIL exceeds the samples saved (routing-capabilities.md, run-pipeline.md).
_PROFILE_KEYS = ("max_attempts", "max_review_rounds", "max_qa_rounds", "effort", "supervise",
                 "budget_soft_usd", "budget_hard_usd", "budget_soft_tokens", "budget_hard_tokens")
PROFILES = {
    # The default pick: an install upgraded mid-life has tuned values, and a wizard that opens by
    # itself must not reset them because someone clicked through it.
    "keep": {
        "label": "Keep my settings",
        "blurb": "Change nothing. Admin → Runtime settings stays exactly as it is.",
        "settings": {},
    },
    "economic": {
        "label": "Economic",
        "blurb": "Fewer retries and review rounds, lower reasoning effort, and a run past $1 "
                 "drops to a cheaper model. Never stops a run. Expect more answers that need a nudge.",
        "settings": {"max_attempts": 2, "max_review_rounds": 1, "max_qa_rounds": 1,
                     "effort": "low", "supervise": False, "budget_soft_usd": 1.0},
    },
    "balanced": {
        "label": "Balanced",
        "blurb": "Reset to Otto's defaults: three attempts, full review rounds, no spend cap.",
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
    # No `match`: a Slack MCP server gives runs a tool, never the ingress (see _detect).
    "slack":    {"label": "Slack", "default_for": ("work", "both"),
                 "where": {"tab": "events", "sect": "ev-slack"},
                 "hint": "Add a Slack user or bot token (setup_wizard.py or .env), then switch the "
                         "listener on in Events → Slack."},
    "email":    {"label": "Email", "default_for": ("personal", "work", "both"),
                 # Any "<x>mail" provider (fastmail, protonmail, apple-mail) or a standalone
                 # e-mail word; not "mailgun"/"mailchimp", which send mail rather than read it.
                 "match": r"gmail|outlook|fastmail|protonmail|(?<![a-z0-9])(apple-?)?e?-?mail(?![a-z0-9])",
                 "where": {"tab": "admin", "sect": "mcp"},
                 "hint": "Connect Gmail (or another mail connector) in claude.ai → Settings → "
                         "Connectors; Otto inherits it. Runs on a local model can't use it."},
    "calendar": {"label": "Calendar", "default_for": ("personal", "both"),
                 "match": r"calendar", "where": {"tab": "admin", "sect": "mcp"},
                 "hint": "Connect Google Calendar in claude.ai → Settings → Connectors."},
    "docs":     {"label": "Docs & notes", "default_for": (),
                 # Not bare "docs": that is every documentation-LOOKUP server (aws-docs, …).
                 # Boundaries are alnum-only, not \b: "_" is a separator in server names.
                 "match": r"notion|google.?(drive|docs)|(?<![a-z0-9])g?drive(?![a-z0-9])|confluence|obsidian",
                 "where": {"tab": "admin", "sect": "mcp"},
                 "hint": "Connect Notion/Drive in claude.ai, or add a stdio server in Admin → "
                         "MCP servers."},
    "github":   {"label": "GitHub & code repos", "default_for": ("work", "both"),
                 "where": {"tab": "admin", "sect": "projects"},
                 "hint": "Install `gh` and run `gh auth login`, then register a repo in Admin → "
                         "Project repos so Otto can open PRs from an isolated clone."},
    # No `where`: OTTO_NTFY_TOPIC is a secret, settable from no screen — only setup_wizard/.env.
    "push":     {"label": "Phone notifications", "default_for": (), "where": None,
                 "hint": "Run ./.venv/bin/python setup_wizard.py (or set OTTO_NTFY_TOPIC in .env) "
                         "and restart, so approvals and blocked runs reach your phone."},
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
    # A detour's draft is for resuming THAT detour; a stale one would reopen Setup on an old
    # profile one click from re-applying over what was tuned since.
    draft = d.get("draft") if time.time() - (d.get("draft_at") or 0) < DRAFT_TTL_S else None
    return {"version": VERSION, "auto_open": not done, "answers": d.get("answers") or {},
            "draft": draft or None,
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
    pick = lambda k, ok, dflt: a.get(k) if isinstance(a.get(k), str) and a.get(k) in ok else dflt  # noqa: E731
    usage = pick("usage", USAGES, "personal")
    profile = pick("profile", PROFILES, "keep")
    picked = a.get("integrations")
    picked = {x for x in picked if isinstance(x, str)} if isinstance(picked, list) else set()
    wants = [k for k in INTEGRATIONS if k in picked]
    return {"usage": usage, "profile": profile, "integrations": wants}


def profile_settings(profile, cur=None):
    """The full assignment a profile makes, every _PROFILE_KEYS key included — or nothing, for
    "keep"."""
    if profile == "keep":
        return {}
    cur = cur or config.settings_all()
    base = {k: cur[k]["default"] for k in _PROFILE_KEYS}
    return {**base, **(PROFILES[profile]["settings"] or {})}


def _settings_plan(profile):
    cur = config.settings_all()
    out = []
    for name, target in profile_settings(profile, cur).items():
        s = cur[name]
        # Pinned wins even when the values match: a pinned key's "default" IS the env value (the
        # config constant reads env at import), so "already set" would hide that nothing applies.
        out.append({"name": name, "before": s["value"], "after": target,
                    "pinned": s["env_pinned"], "env": s["env"],
                    "action": "pinned" if s["env_pinned"]
                    else ("keep" if s["value"] == target else "set")})
    return out


def _usable_mcps(mcps):
    """[(haystack, label)] for servers a run can actually use: enabled, activated (an Otto-added
    def stays unspawned until confirmed) and not last seen unhealthy. The label is what the rest
    of the UI shows — a connector's display name, never its tool-namespace id."""
    import policy               # noqa: PLC0415
    return [(f"{m.get('name', '')} {m.get('display', '')}".lower(), m.get("display") or m.get("name", ""))
            for m in mcps if m.get("enabled", True) and m.get("confirmed") is not False
            and m.get("health") not in policy.UNHEALTHY]


def _row(ready, detail, hint=None):
    """One integration's verdict. `hint=None` = the integration's generic setup hint; a detector
    that knows the exact fix passes it (or "" when the detail already says it)."""
    return {"ready": ready, "detail": detail, "hint": hint}


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
        # The detail names the fix for every state past "no token", so the token hint is dropped.
        states = {i: slack.answering_state(i, cfg) for i in (slack.USER, slack.BOT)}
        live = [i for i, st in states.items() if st == "on"]
        if not live and "test" in states.values():
            return _row(False, "test mode only — it answers just your own self-DM; add an allowlist", "")
        # A token missing a scope polls and answers nobody. Cached grant only — no auth.test here;
        # a grant nobody has read yet is unknown, like the gh login and the connector cache.
        # Judged PER identity: one working identity is a working Slack, whatever the other's state.
        gaps = {i: slack.scope_gaps(i, cfg, cached_only=True) for i in live}
        good = [i for i in live if gaps[i]["known"] and not gaps[i]["missing"]]
        short = [i for i in live if gaps[i]["missing"]]
        fix = ("add the missing scopes, reinstall the Slack app and update the token")
        if good:
            # Config, not liveness: whether the poll is running is the Events card's to say.
            extra = f"; {names[short[0]]}, the token is missing scopes — {fix}" if short else ""
            return _row(True, "set up " + " and ".join(names[i] for i in good) + extra)
        if short:
            return _row(False, f"set up {names[short[0]]}, but the token is missing scopes — {fix}", "")
        if live:
            return _row(None, "token scopes not checked yet — the Events tab's Slack card checks them", "")
        if "nobody" in states.values():
            return _row(False, "switched on, but nobody is both allowlisted and watched — it answers "
                    "nobody; add people or channels in Events → Slack", "")
        toks = [n for n, i in (("user", slack.USER), ("bot", slack.BOT)) if slack.token_set(i)]
        if toks:
            return _row(False, f"{' and '.join(toks)} token set, listener off — switch it on in Events → Slack", "")
        return _row(False, "no Slack token")
    if key == "github":
        import pr_review        # noqa: PLC0415
        if not shutil.which("gh"):
            return _row(False, "`gh` not on PATH")
        # The CACHED login only: resolving it is a `gh api` call of up to 30s.
        who = pr_review.viewer_cached()
        if who is None:
            return _row(None, "sign-in not checked yet — the Events tab's GitHub card checks it")
        if not who:
            # "" is a FAILED lookup: signed out, or GitHub unreachable — `gh api` can't tell them.
            return _row(False, "couldn't confirm the `gh` sign-in — run `gh auth status`")
        # The SAME list the GitHub guide and the PR-review form use (github.com, deduped,
        # URL-first), read offline: no `git` spawn behind the spinner.
        repos = len(pr_review.known_repos(offline=True))
        if not repos:
            return _row(False, f"signed in as {who}, no project repo registered")
        return _row(True, f"signed in as {who}, {repos} repo(s) registered")
    if key == "push":
        ok = bool(config.secret("OTTO_NTFY_TOPIC"))
        return _row(ok, ("OTTO_NTFY_TOPIC set" if ok else "OTTO_NTFY_TOPIC not set"))
    if key == "local":
        import gateway          # noqa: PLC0415
        # CLASS, not transport: a hosted model on an OpenAI-compatible URL is not "your hardware".
        local = [m["name"] for m in gateway.load().get("pool", []) if gateway.model_kind(m) == "local"]
        return _row(bool(local), (", ".join(local) if local else "no local model configured"))
    hits = [label for hay, label in _usable_mcps(mcps) if re.search(spec["match"], hay)]
    if hits:
        return _row(True, ", ".join(hits))
    if not connectors_checked:
        return _row(None, "not checked yet — Admin → MCP servers lists your claude.ai connectors")
    return _row(False, "nothing connected")


def _integrations_plan(wants, mcps, connectors_checked=True):
    out = []
    for key in wants:
        # An UNKNOWN row gets no hint: its detail says how to find out, and the generic "how to
        # set this up" would contradict a setup that may be complete.
        r = _detect(key, mcps, connectors_checked)
        spec = INTEGRATIONS[key]
        hint = "" if r["ready"] or r["ready"] is None else (spec["hint"] if r["hint"] is None else r["hint"])
        out.append({"key": key, "label": spec["label"], "ready": r["ready"], "detail": r["detail"],
                    "hint": hint, "where": spec["where"]})
    return out


def _fingerprint(a, settings):
    # `before` too: an Admin edit between preview and apply must refuse, not be overwritten unseen.
    changes = [{"name": s["name"], "before": s["before"], "after": s["after"]}
               for s in settings if s["action"] == "set"]
    return hashlib.sha256(json.dumps({"answers": a, "changes": changes}, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def preview(answers, mcps=(), connectors_checked=True):
    """Every change `apply` would make, plus the fingerprint it must be given back."""
    a = normalize(answers)
    mcps = list(mcps)
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



def apply(answers, expect):
    """Write the settings `preview` showed and record the answers. Raises StalePreview when the
    install changed in between. Only the SETTINGS are re-derived — the fingerprint covers nothing
    else, so integration detection is the preview's alone."""
    a = normalize(answers)
    # Check and write as ONE step: an Admin save landing between them would be overwritten.
    with config.SETTINGS_LOCK:
        settings = _settings_plan(a["profile"])
        if _fingerprint(a, settings) != expect:
            raise StalePreview("settings changed since the preview — review again")
        changes = {s["name"]: s["after"] for s in settings if s["action"] == "set"}
        if changes:
            config.save_settings(changes)
        # Report what the store now HOLDS: save_settings silently drops a value its spec refuses.
        changes = {k: v for k, v in changes.items() if config.setting(k) == v}
    now = time.time()
    def _mut(d):
        d = d if isinstance(d, dict) else {}
        prev = d.get("answers") if isinstance(d.get("answers"), dict) else {}
        # "keep" applies nothing, so it must not overwrite the record of what IS in force — nor,
        # with nothing applied before, become a "last applied" profile itself.
        stored = dict(a)
        if a["profile"] == "keep":
            stored.pop("profile")
            if prev.get("profile"):
                stored["profile"] = prev["profile"]
        return {"version": VERSION, "answers": stored, "completed_at": now}   # drops any draft
    storage.mutate_json(_STATE, _mut, default={})
    return {"applied": sorted(changes)}


def save_draft(answers):
    """Park the wizard's answers while the operator goes to connect something — the hints there
    say "restart Otto", so in-memory state would not survive. Neither completes nor skips: it
    still opens by itself, resuming from the draft. Apply and skip clear it."""
    draft = normalize(answers)

    def _mut(d):
        d = d if isinstance(d, dict) else {}
        return {**d, "draft": draft, "draft_at": time.time()}
    storage.mutate_json(_STATE, _mut, default={})
    return state()


def skip(answers=None):
    """Remember the operator dismissed it, so it stops opening by itself. Draft `answers` (from a
    wizard closed part-way) are kept to prefill the next run; without them the stored ones stay."""
    now = time.time()

    def _mut(d):
        d = d if isinstance(d, dict) else {}
        out = {**d, "version": VERSION, "skipped_at": now}
        out.pop("draft", None)
        out.pop("draft_at", None)
        # Keep only what was actually answered: a draft closed on the welcome step must not
        # store "personal" as a choice, nor [] as a deliberate "no integrations".
        # The profile is never drafted: the stored one is what was last APPLIED, and the next
        # Setup preselects it, so a draft would read as the active profile.
        if isinstance(answers, dict) and answers.get("usage") in USAGES:
            draft = normalize(answers)
            draft.pop("profile")
            if not isinstance(answers.get("integrations"), list):
                draft.pop("integrations")
            prev = dict(d.get("answers")) if isinstance(d.get("answers"), dict) else {}
            # Picks made for another usage are not this one's: drop them, so the next run offers
            # the new usage's defaults instead of preselecting the old ones.
            if "integrations" not in draft and prev.get("usage") != draft["usage"]:
                prev.pop("integrations", None)
            out["answers"] = {**prev, **draft}      # unreached fields keep their stored value
        return out
    storage.mutate_json(_STATE, _mut, default={})
    return state()
