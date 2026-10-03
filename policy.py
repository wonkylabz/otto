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


# --- activation: an added server is stored INACTIVE ------------------------------------------
# Storing an MCP def is the largest single primitive this API exposes: `command`+`args` are
# handed to `--mcp-config` (Claude) and to mcp_client.servable (local), so whatever is written
# here is SPAWNED on the next run, as the operator. Registering it and running it are therefore
# two separate acts — a def is inert until a human has seen the exact command line and pressed
# Activate. The flag lives on the DEF, not on the `enabled` toggle, because it is a property of
# the command, and because that is the one place BOTH backends already read.
#
# An absent `confirmed` means "written before activation existed" and is honoured, not silently
# disabled: those defs were added by the operator under the old contract, and killing a working
# server on upgrade is a worse failure than the one this prevents. Every writer stamps it now,
# so the absent case only ever describes the past.


def mcp_confirmed(d):
    return bool((d or {}).get("confirmed", True))


# A server NAME travels into every tool id as `mcp__<name>__<tool>`, and both the risk
# allowlist and `mcp_client.declared_servers` read that shape back by splitting on `__`. So a
# name is not free text: `a__b` parses back as the server `a`, which exists nowhere — the def
# spawns and not one of its tools is ever admitted, with no error anywhere to say why. Spaces
# and dots break the same parse less visibly.
_MCP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def valid_mcp_name(name):
    """Is `name` usable as the `<name>` in `mcp__<name>__<tool>`? Rejects `__` explicitly: the
    charset allows a single underscore (`newrelic_eu` is a real server here), a doubled one is
    the separator itself."""
    return bool(_MCP_NAME.match(str(name or ""))) and "__" not in str(name)


def mcp_env_keys(d):
    """The environment variable NAMES a def will hand its subprocess — never the values.

    The activation gate asks a human to approve what Otto is about to spawn, and `env` is as
    much a part of that as the argv: a def can carry a credential (or override PATH) with
    nothing on screen to say so."""
    return sorted((d or {}).get("env") or {})


# What the Admin form shows in place of an env value it must not reveal, and what it sends
# back to mean "leave that one alone". A round trip through the edit form must not be able to
# turn a credential into the six dots that stood in for it.
ENV_KEPT = "\u2022\u2022\u2022\u2022\u2022\u2022"

# A value that NAMES a credential rather than being one: `${VAR}`, `$VAR`, or a bare
# SCREAMING_CASE identifier (which `mcp_client.env_for` resolves through the secret helper).
# These are safe to show and are the whole point of the field, so masking them would make the
# edit form unusable for the case it exists to serve.
_ENV_REFERENCE = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$|^[A-Z][A-Z0-9_]{2,}$")


def mcp_env_display(d):
    """A def's `env` as the edit form may render it: references verbatim, literals masked.

    A literal is either a secret or indistinguishable from one, and this dict lands in the
    Admin DOM. A reference is a NAME — showing it is what lets an operator see and fix the
    wiring, which is the whole reason this form exists."""
    import privacy
    def show(k, v):
        # A reference is a NAME, never a secret — always safe, and seeing it is the point.
        # Otherwise the KEY decides, on the same vocabulary privacy scrubs k/v pairs with:
        # `CONFLUENCE_URL` and `CONFLUENCE_USERNAME` are wiring an operator must be able to
        # read and fix, `CONFLUENCE_API_TOKEN` is not.
        return (isinstance(v, str)
                and (bool(_ENV_REFERENCE.match(v)) or not privacy.secret_named(k)))
    return {k: (v if show(k, v) else ENV_KEPT)
            for k, v in ((d or {}).get("env") or {}).items()}


def mcp_editable(name):
    """One def in the shape the edit form needs, or None. Deliberately NOT part of `all_mcps`:
    that payload is fetched on every Admin load and its rows feed the activation gate, which
    must show variable NAMES and never values. Env values — masked by key, but values all the
    same — reach the browser only when the operator asks to edit this one server.

    That masking is a heuristic on the key name, so a literal secret stored under a key that
    does not read as one (`TOK`) is shown. Naming a secret is the supported way, which is why
    the form says a literal is stored in plaintext."""
    d = mcp_defs().get(name)
    if not d:
        return None
    return {"name": name, "command": d.get("command", ""),
            "args": list(d.get("args") or []), "env": mcp_env_display(d)}


def merge_mcp_env(name, submitted):
    """The env to store, given what the form sent back: every value that came back as the mask
    keeps whatever is on the existing def.

    Keyed on the stored def rather than on the submitted dict, so a key the operator DELETED in
    the form is really deleted — a merge that only ever adds would make a mistyped variable
    impossible to remove."""
    stored = (mcp_defs().get(name) or {}).get("env") or {}
    return {k: (stored.get(k, "") if v == ENV_KEPT else v) for k, v in (submitted or {}).items()}


def add_mcp_def(name, entry):
    """Register an MCP server INACTIVE. The ONE writer for a newly added def — every path that
    accepts a command from outside (the Admin form, a profile import) goes through it, or the
    activation step is just a UI convention one endpoint happens to follow.

    Raises ValueError on a name that cannot round-trip through a tool id: the writer enforces
    it rather than the endpoint, so a second caller cannot store a def that silently never
    resolves."""
    if not valid_mcp_name(name):
        raise ValueError(
            "an MCP server name must be letters, digits, '-' or '_' (no spaces, dots or '__') "
            "— it becomes the middle of every tool id, mcp__<name>__<tool>")
    entry = dict(entry)
    entry["confirmed"] = False
    entry["added_at"] = time.time()
    defs = mcp_defs()
    defs[name] = entry
    save_mcp_defs(defs)
    return entry


def confirm_mcp_def(name):
    """Activate a stored-inactive server. Returns the def (so the caller can audit the exact
    command line that just became runnable), or None if there is no such server."""
    defs = mcp_defs()
    if name not in defs:
        return None
    defs[name]["confirmed"] = True
    save_mcp_defs(defs)
    return defs[name]


def mcp_command_line(d):
    """The exact command line a def will spawn — what the human is actually approving."""
    d = d or {}
    return " ".join([str(d.get("command", ""))] + [str(a) for a in d.get("args", [])]).strip()


def load():
    return storage.read_json(_PATH, {"capabilities": {}, "mcps": {}})


def stamp():
    """A cheap change token for the policy store: `(mtime_ns, size)`, or `None` when it does not
    exist yet. Any long-lived cache of `registry.apply_policy(...)` output must key on this —
    `runbooks._CAPS` and `activities._caps` were loaded ONCE per process and never invalidated,
    so an operator reclassifying a cap read->write in Admin kept firing scheduled runs ungated
    under the stale risk until the process restarted (issue #29)."""
    try:
        st = os.stat(_PATH)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


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


# --- per-tool risk tags (the fast lane, issue #193) -----------------------------------------
# A tool is `safe` only when an operator ticked it; everything else — a new tool, an unknown
# server, a tool on a server nobody has opened yet — is GATED. Stored per server as a list of
# bare tool names (`policy.json` → mcps.<server>.safe_tools), beside `notes` and for the same
# reason: one mechanism for every source, and `set_safe_tools` is the ONLY writer.
#
# What `safe` buys: a request from the owner or a Slack approver that needs ONLY safe tools runs
# with no plan preview and no gate — granted exactly those tools and nothing else (no Bash, no
# Edit, no other MCP tool). The tag is the guard; the classifier only picks the lane.


def safe_tool_names(pol, server):
    """The bare tool names ticked safe on one server, or [] (unset = gated)."""
    entry = ((pol or {}).get("mcps") or {}).get(server) or {}
    return list(entry.get("safe_tools") or []) if isinstance(entry, dict) else []


def safe_tools(pol=None):
    """Every safe tool on an ENABLED server, as the `mcp__<server>__<tool>` id both backends
    grant by. A disabled server's tags are kept but grant nothing — the switch must win."""
    import mcp_client           # noqa: PLC0415 — mcp_client imports this module
    pol = load() if pol is None else pol
    out = []
    for server, entry in ((pol or {}).get("mcps") or {}).items():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        out += [mcp_client.tool_id(server, t) for t in entry.get("safe_tools") or []]
    return sorted(set(out))


def set_safe_tools(server, tools):
    """Replace one server's safe set IN PLACE (empty clears it), returning the whole policy.
    `mutate_json` for the same reason as `set_mcp_note`: the run path reads this file."""
    tools = sorted({str(t).strip() for t in (tools or []) if str(t).strip()})

    def _apply(pol):
        pol.setdefault("capabilities", {})
        entry = pol.setdefault("mcps", {}).setdefault(server, {})
        if tools:
            entry["safe_tools"] = tools
        else:
            entry.pop("safe_tools", None)
        return pol
    return storage.mutate_json(_PATH, _apply, {"capabilities": {}, "mcps": {}})


# Keys on an `mcps` entry that ONE dedicated endpoint writes — a whole-policy save carries
# neither, so both are re-attached from the store and never trusted from the client.
_SERVER_OWNED = ("notes", "safe_tools")


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
            entry = {k: v for k, v in entry.items() if k not in _SERVER_OWNED}
        out[name] = entry
    for name, entry in (saved or {}).items():
        if not isinstance(entry, dict):
            continue
        kept = {}
        note = (entry.get("notes") or "").strip()
        if note:
            kept["notes"] = note
        if entry.get("safe_tools"):
            # The safe tags ride the same rule: a toggle must not un-tag every tool.
            kept["safe_tools"] = list(entry["safe_tools"])
        if not kept:
            continue
        if isinstance(out.get(name), dict):
            out[name].update(kept)
        elif name not in out:
            out[name] = kept
    return out


def keep_cap_mcp(saved, incoming):
    """Re-attach each capability's stored `mcp` declaration to a client-supplied
    `capabilities` map — the exact counterpart of `keep_notes`, for the exact same reason.

    `/api/cap-mcp` is the only writer, and the Admin panel re-POSTs the whole capability map
    on any risk flip or on/off toggle while tracking neither field. Without this, flipping one
    switch (or a stale tab doing it) silently un-declares every server an operator set, and
    the next local run of that capability quietly goes back to guessing from the request.
    An `mcp` arriving from the client is DROPPED rather than trusted, so a whole-policy save
    can neither write nor blank a declaration."""
    out = {}
    for name, entry in (incoming or {}).items():
        out[name] = ({k: v for k, v in entry.items() if k != "mcp"}
                     if isinstance(entry, dict) else entry)
    for name, entry in (saved or {}).items():
        want = (entry or {}).get("mcp") if isinstance(entry, dict) else None
        if not want:
            continue
        if isinstance(out.get(name), dict):
            out[name]["mcp"] = want
        elif name not in out:
            out[name] = {"mcp": want}
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
        # Re-strip on import (never trust incoming values) and stamp UNCONFIRMED: a bundle is
        # a command line from another machine, which is exactly the thing activation exists for.
        defs[final] = dict(_safe_mcp(d), confirmed=False, added_at=time.time())
        mcp_names.add(final)
        mcp_added.append(final)
    save_mcp_defs(defs)

    return {"capabilities_added": added, "capabilities_renamed": renamed,
            "mcps_added": mcp_added, "mcps_renamed": mcp_renamed,
            "needs_env": [n for n in mcp_added if defs.get(n, {}).get("env")]}


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
    keep this OFF the per-run hot path; only the Admin view triggers it.

    The health check STARTS each stdio server, so this is one of the doors issue #72 is
    about: same narrowed environment as a run (`claude_cli.child_env`), or a panel load hands
    third-party code the credentials a run no longer does. Deferred import — `claude_cli`
    reaches back here through `mcp_client`."""
    import claude_cli           # noqa: PLC0415 — see above
    res = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=60,
                         env=claude_cli.child_env())
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

    def safe(n):
        return len(safe_tool_names(pol, n))
    out = [{"name": n, "enabled": ov.get(n, {}).get("enabled", True), "source": "claude",
            "health": health.get(n), "notes": note(n), "safe": safe(n)} for n in discover_mcps()]
    # `confirmed` rides on the otto-source rows only: a server discovered from ~/.claude.json or
    # a claude.ai connector was registered outside Otto and is not ours to gate.
    out += [{"name": n, "enabled": ov.get(n, {}).get("enabled", True), "source": "otto",
             "confirmed": mcp_confirmed(d), "command": mcp_command_line(d),
             # Keys only — the activation gate must SAY that a def carries environment, and
             # must never render what is in it.
             "env_keys": mcp_env_keys(d),
             "health": health.get(n), "notes": note(n), "safe": safe(n)}
            for n, d in mcp_defs().items()]
    out += [{"name": c["name"], "display": c.get("display", c["name"]),
             "enabled": ov.get(c["name"], {}).get("enabled", True), "source": "connector",
             "health": health.get(c["name"]), "notes": note(c["name"]), "safe": safe(c["name"])}
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
        import claude_cli       # noqa: PLC0415 — deferred, see `_run_mcp_list`
        subprocess.Popen(["claude", "mcp", "login", cli_name],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         env=claude_cli.child_env())
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "cli_name": cli_name}


def enabled_mcps(pol):
    return [m["name"] for m in all_mcps(pol) if m["enabled"]]


def active_mcp_config(pol):
    """The --mcp-config payload for enabled, ACTIVATED Otto-added servers (None if none).

    One of the two doors a stored def reaches a subprocess through (mcp_client.servable is the
    other, for the local backend). Both gate on `mcp_confirmed`, or registering a command is
    still the same thing as running it — just on one backend."""
    ov = (pol or {}).get("mcps", {})
    active = {n: runnable_mcp(d) for n, d in mcp_defs().items()
              if mcp_confirmed(d) and ov.get(n, {}).get("enabled", True)}
    return {"mcpServers": active} if active else None


def runnable_mcp(d):
    """A def as the MCP client protocol expects it — Otto's own bookkeeping keys stripped."""
    return {k: v for k, v in (d or {}).items() if k not in ("confirmed", "added_at")}
