"""Admin policy: per-capability risk/enabled overrides + which MCP servers Otto
may use. Persisted to data/policy.json so changes survive restarts.

This is the editable face of the tools+guardrails layer. Whatever the admin saves
here actually changes behaviour: risk decides gating, enabled decides routing, and
enabled MCP servers are handed to each run as allowed tools.
"""
import json
import os
import re
import subprocess
import time

import config
import storage

_PATH = os.path.join(config.DATA_DIR, "policy.json")
_CUSTOM = os.path.join(config.DATA_DIR, "capabilities.json")   # Otto-added capabilities
_MCPDEF = os.path.join(config.DATA_DIR, "mcp-servers.json")    # Otto-added MCP servers
_CONN_CACHE = os.path.join(config.DATA_DIR, "mcp-connectors-cache.json")  # claude.ai connectors
_CONN_TTL = 3600   # connectors change rarely; the Admin view also force-refreshes on demand


def _read(path, default):
    return storage.read_json(path, default)


def _write(path, data):
    storage.write_json(path, data)


def custom_caps():
    return _read(_CUSTOM, [])


def save_custom_caps(lst):
    _write(_CUSTOM, lst)


def mcp_defs():
    return _read(_MCPDEF, {})


def save_mcp_defs(d):
    _write(_MCPDEF, d)


def load():
    return storage.read_json(_PATH, {"capabilities": {}, "mcps": {}})


# --- per-server usage notes -----------------------------------------------------------------
# A note is the operator's own instructions for driving ONE MCP server: the thing the tool
# schemas can't say ("only ever query the EU account here", "this proxy needs region=us-east-1
# on every call"). It rides in policy.json next to `enabled`, keyed by the all_mcps() name, so
# ONE mechanism covers all three sources — a server discovered from ~/.claude.json and a
# claude.ai connector are both un-editable as defs, and both can still carry a note.
#
# What a note CANNOT do: fix a server that fails to LAUNCH. A stdio server is spawned before
# the model's first turn, so prose about `aws-vault exec …` reaches a model that has no way to
# act on it — that case is a wrapper in the server's own command/args, not a note.

MCP_NOTE_MAX = 600   # per server: guidance the model reads on every run, not documentation


def mcp_notes(pol=None):
    """{name: note} for every ENABLED MCP server carrying an operator note. Disabled servers
    are excluded — their tools aren't in the run, so their guidance is pure context cost."""
    pol = load() if pol is None else pol
    out = {}
    for name, entry in ((pol or {}).get("mcps") or {}).items():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        note = (entry.get("notes") or "").strip()
        if note:
            out[name] = note
    return out


def set_mcp_note(name, note):
    """Save (or clear, with empty text) one server's note IN PLACE, returning the whole policy.

    `mutate_json` rather than save(): this rewrites a file the run path reads, and it must
    not carry along whatever else the caller's in-memory copy happens to hold."""
    note = (note or "").strip()[:MCP_NOTE_MAX]

    def _apply(pol):
        pol.setdefault("capabilities", {})
        entry = pol.setdefault("mcps", {}).setdefault(name, {})
        if note:
            entry["notes"] = note
        else:
            entry.pop("notes", None)
        return pol
    return storage.mutate_json(_PATH, _apply, {"capabilities": {}, "mcps": {}})


def keep_notes(saved, incoming):
    """Re-attach the stored notes to a client-supplied `mcps` map.

    Two things at once, both because `set_mcp_note` is the ONLY writer of a note. The Admin
    panel POSTs the whole policy on any enable/disable and tracks only `enabled`, so without
    this a toggle (or a stale tab) silently erases every note; and a `notes` arriving from the
    client is DROPPED rather than trusted, so a whole-policy save can neither write nor blank
    one. An empty note is not a note — it is dropped from both sides, so the store doesn't
    accumulate a `"notes": ""` key per server."""
    out = {}
    for name, entry in (incoming or {}).items():
        if isinstance(entry, dict):
            entry = {k: v for k, v in entry.items() if k != "notes"}
        out[name] = entry
    for name, entry in (saved or {}).items():
        note = ((entry or {}).get("notes") or "").strip() if isinstance(entry, dict) else ""
        if not note:
            continue
        if isinstance(out.get(name), dict):
            out[name]["notes"] = note
        elif name not in out:
            out[name] = {"notes": note}
    return out


def save(pol):
    storage.write_json(_PATH, pol)


# --- shareable extension bundles (export / import) ------------------------

BUNDLE_VERSION = 1


def _safe_mcp(d):
    """An MCP server def with secret VALUES stripped — keep env var KEYS so the importer
    knows what to set, but never ship the values (api_key_env-style indirection)."""
    entry = {"command": d.get("command", ""), "args": list(d.get("args", []))}
    if d.get("env"):
        entry["env"] = {k: "" for k in d["env"]}
    return entry


def export_bundle():
    """A portable, secret-free bundle of Otto-added capabilities + MCP servers."""
    return {
        "otto_bundle": BUNDLE_VERSION,
        "capabilities": custom_caps(),
        "mcp_servers": {n: _safe_mcp(d) for n, d in mcp_defs().items()},
    }


def _dedupe(name, taken):
    i = 2
    while f"{name}-{i}" in taken:
        i += 1
    return f"{name}-{i}"


def import_bundle(bundle, existing_caps=(), existing_mcps=()):
    """Merge a bundle into the local custom caps + MCP defs WITHOUT overwriting anything
    (built-ins included). Name collisions are renamed with a numeric suffix. Secret values
    are never imported. Returns a summary of what was added / renamed."""
    if not isinstance(bundle, dict) or "otto_bundle" not in bundle:
        raise ValueError("not an Otto bundle (missing 'otto_bundle')")

    caps = custom_caps()
    cap_names = set(existing_caps) | {c["name"] for c in caps}
    added, renamed = [], []
    for inc in bundle.get("capabilities", []) or []:
        name = (inc.get("name") or "").strip()
        if not name:
            continue
        final = name if name not in cap_names else _dedupe(name, cap_names)
        if final != name:
            renamed.append({"from": name, "to": final})
        caps.append({"name": final, "description": inc.get("description", ""),
                     "risk": inc.get("risk", "write"), "prompt": inc.get("prompt", "")})
        cap_names.add(final)
        added.append(final)
    save_custom_caps(caps)

    defs = mcp_defs()
    mcp_names = set(existing_mcps) | set(defs)
    mcp_added, mcp_renamed = [], []
    for name, d in (bundle.get("mcp_servers") or {}).items():
        final = name if name not in mcp_names else _dedupe(name, mcp_names)
        if final != name:
            mcp_renamed.append({"from": name, "to": final})
        defs[final] = _safe_mcp(d)   # re-strip on import too — never trust incoming values
        mcp_names.add(final)
        mcp_added.append(final)
    save_mcp_defs(defs)

    return {"capabilities_added": added, "capabilities_renamed": renamed,
            "mcps_added": mcp_added, "mcps_renamed": mcp_renamed,
            "needs_env": [n for n in mcp_added if defs.get(n, {}).get("env")]}


# --- portable profile (export / import) ------------------------------------------------------
# The full "make another machine behave like this one" bundle (portability): everything the
# extension bundle above carries PLUS policy overrides, the model config, behaviour rules,
# knowledge docs, and project standing-instructions keyed by git ORIGIN (paths are
# machine-local). Secret-free by construction; import is NON-CLOBBERING (a tuned install is
# never overwritten — skipped items are reported instead). Surfaces: `python3 profile.py
# export/import` and GET/POST /api/profile/{export,import}.

PROFILE_VERSION = 1


def _safe_models(cfg):
    """The model config with secrets stripped. `api_key_env` is SUPPOSED to be an env-var NAME,
    but gateway.api_key() also accepts a pasted literal key in that field — never export those:
    keep the value only when it names an env var on THIS machine, else blank it and flag it.
    An endpoint's extra headers resolve the same way (gateway.request_headers), so a header
    value gets the same rule — otherwise an `X-Api-Key: <literal>` rides out in the bundle."""
    pool, needs_key = [], []
    for m in cfg.get("pool", []):
        m = dict(m)
        k = m.get("api_key_env")
        if k and k not in os.environ:
            m["api_key_env"] = ""
            needs_key.append(m.get("name", "?"))
        if m.get("headers"):
            # The NAME is configuration the importer needs; only the value can be a credential.
            m["headers"] = {h: (v if v and v in os.environ else "")
                            for h, v in m["headers"].items()}
            if any(not v for v in m["headers"].values()) and m.get("name", "?") not in needs_key:
                needs_key.append(m.get("name", "?"))
        pool.append(m)
    return {"pool": pool, "assign": dict(cfg.get("assign", {})),
            "cap_exec": dict(cfg.get("cap_exec", {})),
            "cap_local_exec": dict(cfg.get("cap_local_exec", {})),
            "needs_key": needs_key}


def export_profile():
    import engine
    import gateway
    import knowledge
    import registry
    import workspace
    pol = load()
    origins = {r["path"]: r.get("origin") for r in workspace.git_repos()}
    projects = [{"name": os.path.basename(e["path"].rstrip("/")),
                 "origin": origins.get(e["path"]) or "",
                 "instructions": e.get("instructions", "")}
                for e in registry._project_entries()]
    docs = knowledge.export_docs()
    return {
        "otto_profile": PROFILE_VERSION,
        "bundle": export_bundle(),
        # Notes only — an MCP's enable state is a local decision, but the operator's usage
        # guidance for a server is exactly the kind of knowledge worth carrying to a new box.
        "policy": {"capabilities": dict(pol.get("capabilities", {})),
                   "mcps": {n: {"notes": t} for n, t in mcp_notes(pol).items()}},
        "models": _safe_models(gateway.load()),
        "behaviors": [{"rule": b.get("rule", ""), "scope": b.get("scope", "global")}
                      for b in engine.behaviors()],
        "knowledge": {"settings": {"threshold": knowledge.settings().get("threshold"),
                                   "embed_model": knowledge.settings().get("embed_model")},
                      "docs": docs},
        "projects": projects,
    }


def import_profile(profile, existing_caps=(), existing_mcps=()):
    """Merge a profile into this install WITHOUT overwriting anything already configured:
    - custom caps + MCP defs via import_bundle (rename-on-collision, secret-free)
    - policy overrides only for caps with NO local override
    - model config: pool entries / cap overrides added if absent; phase assignments applied
      ONLY when this install has no saved models.json (a fresh machine) — a tuned one keeps its
      assignments and the skip is reported
    - behaviour rules via add_behavior (self-deduping)
    - knowledge docs by title (existing titles skipped; text re-chunks + re-embeds locally)
    - project instructions matched by git ORIGIN against locally registered repos; unmatched
      origins are returned for the human to clone + register
    Returns a summary of added/skipped/unmatched."""
    if not isinstance(profile, dict) or "otto_profile" not in profile:
        raise ValueError("not an Otto profile (missing 'otto_profile')")
    import engine
    import gateway
    import knowledge
    import registry
    import workspace

    summary = {}
    if profile.get("bundle"):
        summary["bundle"] = import_bundle(profile["bundle"], existing_caps, existing_mcps)

    # policy overrides — never touch a cap the local admin already configured
    pol = load()
    local_ov = pol.setdefault("capabilities", {})
    added, skipped = [], []
    for name, ov in (profile.get("policy", {}).get("capabilities") or {}).items():
        if name in local_ov:
            skipped.append(name)
        elif isinstance(ov, dict):
            local_ov[name] = {k: ov[k] for k in ("risk", "enabled", "tool_free") if k in ov}
            added.append(name)
    # MCP notes — same non-clobbering rule: a locally-written note is never overwritten.
    local_mcps = pol.setdefault("mcps", {})
    notes_added, notes_skipped = [], []
    for name, ov in (profile.get("policy", {}).get("mcps") or {}).items():
        note = ((ov or {}).get("notes") or "").strip()[:MCP_NOTE_MAX] if isinstance(ov, dict) else ""
        if not note:
            continue
        if (local_mcps.get(name) or {}).get("notes"):
            notes_skipped.append(name)
            continue
        local_mcps.setdefault(name, {})["notes"] = note
        notes_added.append(name)
    if added or notes_added:
        save(pol)
    summary["policy"] = {"added": added, "skipped": skipped,
                         "mcp_notes_added": notes_added, "mcp_notes_skipped": notes_skipped}

    # models — additive; assignments only on a fresh (no saved models.json) install
    inc = profile.get("models") or {}
    fresh = not os.path.exists(gateway._PATH)
    cfg = gateway.load()
    names = {m["name"] for m in cfg.get("pool", [])}
    pool_added = []
    for m in inc.get("pool", []):
        if m.get("name") and m["name"] not in names and m.get("provider") != "claude":
            entry = dict(m)
            entry.pop("needs_key", None)
            cfg["pool"].append(entry)
            names.add(m["name"])
            pool_added.append(m["name"])
    for key in ("cap_exec", "cap_local_exec"):
        local = cfg.setdefault(key, {})
        for cap_name, model in (inc.get(key) or {}).items():
            if cap_name not in local and model in names:
                local[cap_name] = model
    assigned = False
    if fresh:
        for task, model in (inc.get("assign") or {}).items():
            if model in names:
                cfg.setdefault("assign", {})[task] = model
                assigned = True
    gateway.save(cfg)
    summary["models"] = {"pool_added": pool_added, "assignments_applied": assigned,
                         "needs_key": inc.get("needs_key", [])}

    # behaviour rules — add_behavior de-dupes on (rule, scope)
    rules = 0
    for b in profile.get("behaviors") or []:
        if engine.add_behavior(b.get("rule", ""), b.get("scope", "global")):
            rules += 1
    summary["behaviors"] = {"added": rules}

    # knowledge — settings only when unset locally; docs by title, re-embedded locally
    ks = (profile.get("knowledge") or {}).get("settings") or {}
    if ks.get("embed_model") and not knowledge.settings().get("embed_model"):
        knowledge.set_settings(threshold=ks.get("threshold"), embed_model=ks["embed_model"])
    have = {d["title"] for d in knowledge.documents()}
    docs_added, docs_skipped = [], []
    for d in profile.get("knowledge", {}).get("docs") or []:
        title = (d.get("title") or "").strip()
        if not title or not (d.get("text") or "").strip():
            continue
        if title in have:
            docs_skipped.append(title)
            continue
        knowledge.add_document(title, d["text"], source=d.get("source") or "profile-import")
        docs_added.append(title)
    summary["knowledge"] = {"added": docs_added, "skipped": docs_skipped}

    # project instructions — match by git origin; registering a repo needs a local clone, so
    # unmatched origins go back to the human instead of guessing at paths
    by_origin = {r.get("origin"): r["path"] for r in workspace.git_repos() if r.get("origin")}
    matched, unmatched = [], []
    for p in profile.get("projects") or []:
        path = by_origin.get(p.get("origin"))
        if not path:
            if p.get("origin"):
                unmatched.append({"name": p.get("name", ""), "origin": p["origin"]})
            continue
        if p.get("instructions") and not registry.project_meta(path)["instructions"]:
            registry.set_project_instructions(path, p["instructions"])
        matched.append(p.get("name", ""))
    summary["projects"] = {"matched": matched, "unmatched": unmatched}
    return summary


def discover_mcps():
    """Read MCP server names from the user's Claude config (read-only)."""
    names = {}
    for path in (os.path.expanduser("~/.claude.json"),):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "mcpServers" and isinstance(v, dict):
                        for n in v:
                            names[n] = True
                    else:
                        walk(v)
            elif isinstance(o, list):
                for i in o:
                    walk(i)
        walk(data)
    return sorted(names)


def _run_mcp_list():
    """Raw `claude mcp list` text. Health-checks every server (slow, with network timeouts) —
    keep this OFF the per-run hot path; only the Admin view triggers it."""
    res = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=60)
    return res.stdout or ""


# Health categories parsed from `claude mcp list`. Each server line ends in one of these
# statuses; anything Otto doesn't recognise stays "unknown" (surfaced, never hidden).
UNHEALTHY = ("failed", "needs_auth", "pending")
_HEALTH_MAP = (
    ("Connected", "connected"),
    ("Needs authentication", "needs_auth"),
    ("Failed to connect", "failed"),
    ("Pending approval", "pending"),
)


def _classify_status(text):
    low = text.lower()
    for needle, cat in _HEALTH_MAP:
        if needle.lower() in low:
            return cat
    return "unknown"


def _mcp_name(head):
    """The all_mcps() key for a `claude mcp list` line's leading segment: a claude.ai
    connector is sanitized to the tool-namespace form (`claude.ai Gmail` → `claude_ai_Gmail`);
    every other server keeps its raw config name (`aws-mcp`, `plugin:acme:…`)."""
    if head.startswith("claude.ai "):
        return re.sub(r"[^0-9A-Za-z]+", "_", head).strip("_")
    return head


def _parse_connectors(text):
    """claude.ai account connectors that are Connected, as {name, display, status}.

    Lines look like:  `claude.ai Gmail: https://… - ✔ Connected`. We sanitize the display
    name to the tool-namespace form Claude Code uses (`claude.ai Gmail` → `claude_ai_Gmail`,
    so `mcp__claude_ai_Gmail` is the right --allowedTools prefix). Only Connected ones are
    kept — there's no point allowlisting a connector that still needs auth."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("claude.ai "):
            continue
        name, _, rest = line.partition(": ")
        status = rest.rsplit(" - ", 1)[-1].strip() if " - " in rest else ""
        if "Connected" not in status:
            continue
        out.append({"name": _mcp_name(name), "display": name, "status": status})
    return out


def _parse_health(text):
    """Health of EVERY server `claude mcp list` reports, as {all_mcps-name: category}.
    Category ∈ connected|needs_auth|failed|pending|unknown. Keyed to match all_mcps() rows
    so a status pill can be joined onto each MCP — this is the errors-visible half of the
    connector parse (which keeps only the Connected ones)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        head, sep, rest = line.partition(": ")
        if not sep or " - " not in rest:
            continue
        out[_mcp_name(head)] = _classify_status(rest.rsplit(" - ", 1)[-1].strip())
    return out


def _mcp_status(allow_refresh=False, force=False):
    """Cached parse of `claude mcp list`: {'connectors': [...], 'health': {name: cat}}. The
    slow health-checking `claude mcp list` runs only when the Admin view asks (allow_refresh),
    and only past the TTL unless `force` (the "Recheck" button); the run path reads the cache
    and never blocks. On a transient failure we keep the stale cache rather than dropping it."""
    cached = _read(_CONN_CACHE, {})
    cached = cached if isinstance(cached, dict) else {}
    fresh = (time.time() - cached.get("at", 0)) < _CONN_TTL
    if allow_refresh and (force or not fresh):
        try:
            text = _run_mcp_list()
            cached = {"at": time.time(),
                      "connectors": _parse_connectors(text),
                      "health": _parse_health(text)}
            _write(_CONN_CACHE, cached)
        except Exception:
            pass
    return cached


def discover_connectors(allow_refresh=False, force=False, status=None):
    """Connected claude.ai connectors (Gmail, Slack, Calendar, …). They live in the Claude
    account, NOT in ~/.claude.json, so `discover_mcps()` can't see them — only `claude mcp
    list` can. Pass `status` to reuse a `_mcp_status` result instead of asking for another."""
    status = _mcp_status(allow_refresh=allow_refresh, force=force) if status is None else status
    return status.get("connectors", [])


def mcp_health(allow_refresh=False, force=False, status=None):
    """{mcp-name: health-category} for every server the last `claude mcp list` reported, or {}
    if never polled. Cached alongside connectors so reads never block the run path."""
    status = _mcp_status(allow_refresh=allow_refresh, force=force) if status is None else status
    return status.get("health", {})


def all_mcps(pol, allow_refresh=False, force=False):
    """Discovered local stdio servers (from ~/.claude.json, read-only) + Otto-added servers
    + connected claude.ai connectors, each with its enabled state, source, and last-known
    `health` (None until polled). `allow_refresh` lets the Admin view re-poll `claude mcp
    list`; `force` bypasses the TTL (the Recheck button). The run path leaves both off."""
    ov = (pol or {}).get("mcps", {})
    # ONE status read, shared by both consumers below. They used to fetch it independently, which
    # was free on the cached path (the first call rewrites the cache, so the second sees it fresh)
    # but ran the ~8s `claude mcp list` TWICE under `force` — the Recheck button paid it twice for
    # identical data.
    status = _mcp_status(allow_refresh=allow_refresh, force=force)
    health = mcp_health(status=status)

    def note(n):
        return (ov.get(n, {}).get("notes") or "")
    out = [{"name": n, "enabled": ov.get(n, {}).get("enabled", True), "source": "claude",
            "health": health.get(n), "notes": note(n)} for n in discover_mcps()]
    out += [{"name": n, "enabled": ov.get(n, {}).get("enabled", True), "source": "otto",
             "health": health.get(n), "notes": note(n)} for n in mcp_defs()]
    out += [{"name": c["name"], "display": c.get("display", c["name"]),
             "enabled": ov.get(c["name"], {}).get("enabled", True), "source": "connector",
             "health": health.get(c["name"]), "notes": note(c["name"])}
            for c in discover_connectors(status=status)]
    return out


def unhealthy_count(pol):
    """How many ENABLED MCPs are broken per the cached health check — the actionable signal
    behind the Admin-tab warning badge. Reads the cache only (no slow re-poll), and counts
    only enabled rows so the dozen unused connectors that merely 'need auth' aren't noise."""
    return sum(1 for m in all_mcps(pol)
               if m["enabled"] and m.get("health") in UNHEALTHY)


def reconnect_mcp(name, pol):
    """Kick off `claude mcp login <server>` for a known MCP (OAuth / re-auth). Resolves `name`
    against the trusted all_mcps() set and derives the CLI identifier server-side — a connector
    is addressed by its full display name (`claude.ai New Relic`), everything else by its raw
    config key — so no client string ever reaches the shell. Fire-and-forget: `login` opens a
    browser (Otto runs on the user's own machine) and its own local listener handles the OAuth
    callback, so we detach and return immediately; the user finishes in the browser then Rechecks.
    Returns {ok, cli_name} or {ok: False, error}."""
    row = next((m for m in all_mcps(pol) if m["name"] == name), None)
    if not row:
        return {"ok": False, "error": "unknown MCP server"}
    cli_name = row.get("display", row["name"]) if row.get("source") == "connector" else row["name"]
    try:
        subprocess.Popen(["claude", "mcp", "login", cli_name],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "cli_name": cli_name}


def enabled_mcps(pol):
    return [m["name"] for m in all_mcps(pol) if m["enabled"]]


def active_mcp_config(pol):
    """The --mcp-config payload for enabled Otto-added servers (None if none)."""
    ov = (pol or {}).get("mcps", {})
    defs = mcp_defs()
    active = {n: d for n, d in defs.items() if ov.get(n, {}).get("enabled", True)}
    return {"mcpServers": active} if active else None
