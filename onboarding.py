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
# NO budget cap, hard or soft, is here: each is a number the operator chose, and a full
# assignment would make any profile — "Economic" included — silently switch one off.
_PROFILE_KEYS = ("max_attempts", "max_review_rounds", "max_qa_rounds", "effort", "supervise")
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
        "blurb": "Fewer retries and review rounds, lower reasoning effort, no mid-run supervisor. "
                 "Expect more answers that need a nudge. Spend caps you set are kept.",
        "settings": {"max_attempts": 2, "max_review_rounds": 1, "max_qa_rounds": 1,
                     "effort": "low", "supervise": False},
    },
    "balanced": {
        "label": "Balanced",
        "blurb": "Reset to Otto's defaults: three attempts, full review rounds. Spend caps you "
                 "set are kept.",
        "settings": None,      # resolved to the code defaults at plan time
    },
    "quality": {
        "label": "Quality",
        "blurb": "Defaults plus high reasoning effort. Slowest and most expensive per run.",
        "settings": {"effort": "high"},
    },
}

# What each phase of a run does, for the wizard's model step. Keyed 1:1 to `gateway.TASKS`
# (`OnboardingTests`), so a new tier cannot ship unexplained.
PHASES = {
    "routing": "Picks which capability handles your request. Cheap and frequent — a small model is fine.",
    "plan": "Splits a big request into steps (swarm / plan-then-execute).",
    "preview": "Writes the plan you approve before any write. It runs once, with no retry above it — don't pick a weak model.",
    "clarify": "Decides whether to ask you a question first. Cheap.",
    "memory": "Extracts durable facts after a run. Cheap.",
    "verify": "The judge: passes or fails each attempt. A weak judge passes bad work or fails good work.",
    "supervise": "Watches a run mid-flight and can steer or kill it.",
    "memory_gc": "The on-demand memory cleaner — the one pass that deletes memory.",
    "execution": "Does the actual work, with tools. This is the phase where a model's turn budget matters.",
}


def models_plan():
    """The non-Claude models in the pool, each with its turn budget. PURE read: no secret leaves
    (name/model/kind/turns only). `max_turns` unset means the global default — the silent
    misconfiguration this exists to surface: a capable model gets the same ceiling as a 4B one."""
    import gateway          # noqa: PLC0415
    cfg = gateway.load()
    execs = {cfg.get("assign", {}).get("execution")}
    out = []
    for m in cfg.get("pool", []):
        if gateway.backend_of(m) != "local":
            continue                    # Claude has no Otto-side cap; Codex ignores max_turns
        out.append({"name": m.get("name", ""), "model": m.get("model", ""),
                    "kind": gateway.model_kind(m), "max_turns": m.get("max_turns"),
                    "effective": m.get("max_turns") or config.LOCAL_RUNTIME_MAX_TURNS,
                    "executes": m.get("name") in execs})
    return {"default_turns": config.LOCAL_RUNTIME_MAX_TURNS, "models": out}


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
    at = d.get("draft_at")
    at = at if isinstance(at, (int, float)) else 0     # a hand-edited file must not 500 every load
    draft = d.get("draft") if time.time() - at < DRAFT_TTL_S else None
    return {"version": VERSION, "auto_open": not done, "answers": d.get("answers") or {},
            "draft": draft or None,
            "completed_at": d.get("completed_at"), "skipped_at": d.get("skipped_at"),
            "usages": list(USAGES),
            "phases": [{"key": k, "blurb": b} for k, b in PHASES.items()],
            "models": models_plan(),
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
    # A code "default" is the config constant, which read env at import: an UNUSABLE env value
    # (OTTO_EFFORT=bogus) leaks in as the default. Never target it — leave that key alone.
    base = {k: cur[k]["default"] for k in _PROFILE_KEYS
            if config._coerce(cur[k]["kind"], cur[k]["default"]) is not None}
    return {**base, **(PROFILES[profile]["settings"] or {})}


def _settings_plan(profile, cur=None):
    cur = cur or config.settings_all()
    out = []
    targets = profile_settings(profile, cur)
    if profile != "keep":
        # A key the profile WOULD assign but cannot (its default is an unusable env value): a
        # row saying so, never a silent gap that reads as "already reset".
        for name in _PROFILE_KEYS:
            if name not in targets:
                s = cur[name]
                out.append({"name": name, "before": s["value"], "after": None, "pinned": False,
                            "env": s["env"], "action": "skip"})
    for name, target in targets.items():
        s = cur[name]
        # Pinned wins even when the values match: a pinned key's "default" IS the env value (the
        # config constant reads env at import), so "already set" would hide that nothing applies.
        out.append({"name": name, "before": s["value"], "after": target,
                    "pinned": s["env_pinned"], "env": s["env"],
                    "action": "pinned" if s["env_pinned"]
                    else ("keep" if s["value"] == target else "set")})
    return out


def _partition_mcps(mcps, health=None):
    """(usable, unusable) as [(haystack, label)] / [(haystack, label, why)], one pass. Usable =
    enabled, activated (an Otto def stays unspawned until confirmed) and not last seen unhealthy.
    Unusable keeps the REASON, so a row says "switch it on" or "re-authenticate it", never the
    generic "connect one". `health` adds the connectors `claude mcp list` reported but that never
    become rows: the listing keeps only Connected ones, so one needing auth exists only there."""
    import policy               # noqa: PLC0415
    usable, unusable, seen = [], [], set()
    for m in mcps:
        name = m.get("name", "")
        seen.add(name)
        hay, label = f"{name} {m.get('display', '')}".lower(), m.get("display") or name
        if not m.get("enabled", True):
            unusable.append((hay, label, "is switched off in Admin → MCP servers — switch it on"))
        elif m.get("confirmed") is False:
            unusable.append((hay, label, "is registered but not activated — activate it in Admin → MCP servers"))
        elif m.get("health") in policy.UNHEALTHY:
            unusable.append((hay, label, "can't be used right now — reconnect or re-authenticate it"))
        else:
            usable.append((hay, label))
    # Keyed off policy.UNHEALTHY, with a default, so a category added there is never dropped.
    state = {"needs_auth": "needs re-authentication", "failed": "failed to connect",
             "pending": "is waiting for approval"}
    for name, cat in (health or {}).items():
        # A server the listing reports but that has no row: a claude.ai connector not Connected
        # (the parse keeps only those), or a project/local-scope server outside ~/.claude.json.
        if name in seen or cat not in policy.UNHEALTHY:
            continue
        if name.startswith("claude_ai_"):
            label = "claude.ai " + name[len("claude_ai_"):].replace("_", " ")
            where = "reconnect it in claude.ai → Settings → Connectors"
        else:
            label, where = name, "check it with `claude mcp list` (it isn't one Otto manages)"
        unusable.append((name.lower(), label, f"{state.get(cat, 'is unhealthy')} — {where}"))
    return usable, unusable


def _row(ready, detail, hint=None):
    """One integration's verdict. `hint=None` = the integration's generic setup hint; a detector
    that knows the exact fix passes it (or "" when the detail already says it)."""
    return {"ready": ready, "detail": detail, "hint": hint}


def _detect(key, usable, connectors_checked=True, unusable=()):
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
        dead = [i for i in live if gaps[i].get("rejected")]
        unknown = [i for i in live if not gaps[i]["known"] and i not in dead]
        if good:
            # Config, not liveness: whether the poll is running is the Events card's to say.
            extra = f"; {names[short[0]]}, the token is missing scopes — {fix}" if short else ""
            extra += f"; {names[unknown[0]]}, scopes not checked yet" if unknown else ""
            extra += f"; {names[dead[0]]}, Slack refused the token — replace it" if dead else ""
            return _row(True, "set up " + " and ".join(names[i] for i in good) + extra)
        if unknown:
            # The unread identity may be the working one: unknown, not a verdict from the other.
            return _row(None, "token scopes not checked yet — the Events tab's Slack card checks them", "")
        if dead:
            return _row(False, f"set up {names[dead[0]]}, but Slack refused the token "
                        f"({gaps[dead[0]]['rejected']}) — copy a fresh one from the app's OAuth page "
                        "and restart Otto", "")
        if short:
            return _row(False, f"set up {names[short[0]]}, but the token is missing scopes — {fix}", "")
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
        # Any registered repo — GitHub, GHE or another forge, URL-only or a checkout — read
        # offline (no `git` spawn behind the spinner); the guide reads the same count.
        import registry         # noqa: PLC0415
        repos = registry.registered_count()
        if not repos:
            return _row(False, f"signed in as {who}, no project repo registered")
        return _row(True, f"signed in as {who}, {repos} repo(s) registered")
    if key == "push":
        ok = bool(config.NTFY_TOPIC)        # what delivery.notify will actually use
        return _row(ok, ("OTTO_NTFY_TOPIC set" if ok else "OTTO_NTFY_TOPIC not set"))
    if key == "local":
        import gateway          # noqa: PLC0415
        # CLASS, not transport: a hosted model on an OpenAI-compatible URL is not "your hardware".
        local = [m["name"] for m in gateway.load().get("pool", []) if gateway.model_kind(m) == "local"]
        return _row(bool(local), (", ".join(local) if local else "no local model configured"))
    hits = [label for hay, label in usable if re.search(spec["match"], hay)]
    if hits:
        return _row(True, ", ".join(hits))
    sick = [(label, why) for hay, label, why in unusable if re.search(spec["match"], hay)]
    if sick:
        return _row(False, f"{sick[0][0]} {sick[0][1]}", "")
    if not connectors_checked:
        return _row(None, "not checked yet — Admin → MCP servers lists your claude.ai connectors")
    return _row(False, "nothing connected")


def _integrations_plan(wants, mcps, connectors_checked=True, health=None):
    out = []
    usable, unusable = _partition_mcps(mcps, health)   # built once, shared by every MCP row
    for key in wants:
        # An UNKNOWN row gets no hint: its detail says how to find out, and the generic "how to
        # set this up" would contradict a setup that may be complete.
        r = _detect(key, usable, connectors_checked, unusable)
        spec = INTEGRATIONS[key]
        hint = "" if r["ready"] or r["ready"] is None else (spec["hint"] if r["hint"] is None else r["hint"])
        out.append({"key": key, "label": spec["label"], "ready": r["ready"], "detail": r["detail"],
                    "hint": hint, "where": spec["where"]})
    return out


def _fingerprint(a, settings):
    # `before` too: an Admin edit between preview and apply must refuse, not be overwritten unseen.
    changes = [{"name": s["name"], "before": s["before"], "after": s["after"]}
               for s in settings if s["action"] == "set"]
    import snapshot             # noqa: PLC0415 — the shared preview/apply token
    return snapshot.fingerprint({"answers": a, "changes": changes})


def preview(answers, mcps=(), connectors_checked=True, health=None):
    """Every change `apply` would make, plus the fingerprint it must be given back."""
    a = normalize(answers)
    mcps = list(mcps)
    settings = _settings_plan(a["profile"])
    fp = _fingerprint(a, settings)
    notes = []
    if a["profile"] == "economic":
        notes.append("Biggest further savings: route cheap phases (routing, clarify, memory) to a "
                     "smaller or local model in Admin → Models, and set a per-run budget in "
                     "Admin → Runtime settings.")
    if any(s["action"] == "skip" for s in settings):
        notes.append("Some settings have an unreadable value in .env (a typo?), so their default "
                     "can't be known and they were left alone — fix or remove them in .env.")
    if any(s["action"] == "pinned" for s in settings):
        notes.append("Some settings are pinned by an environment variable and keep that value. "
                     "To use this profile's instead, remove them from .env, restart, and run "
                     "Setup again.")
    return {"answers": a, "fingerprint": fp, "settings": settings,
            "integrations": _integrations_plan(a["integrations"], mcps, connectors_checked, health),
            "models": models_plan(), "notes": notes}



def apply(answers, expect):
    """Write the settings `preview` showed and record the answers. Raises StalePreview when the
    install changed in between. Only the SETTINGS are re-derived — the fingerprint covers nothing
    else, so integration detection is the preview's alone."""
    a = normalize(answers)
    planned = {}

    def check(store):
        # Runs INSIDE save_settings' file lock, against the store as it is NOW: a write from any
        # process landing after the preview refuses here, instead of being overwritten.
        settings = _settings_plan(a["profile"], config.settings_all(store=store))
        if _fingerprint(a, settings) != expect:
            raise StalePreview("settings changed since the preview — review again")
        planned.update({s["name"]: s["after"] for s in settings if s["action"] == "set"})
        return planned
    config.save_settings(check)
    # Report what the store now HOLDS: save_settings silently drops a value its spec refuses.
    changes = {k: v for k, v in planned.items() if config.setting(k) == v}
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
