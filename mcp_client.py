"""MCP client for the LOCAL execution backend (phase 1 of "give local models real tools").

`claude -p` speaks MCP for free — Otto just hands it `--mcp-config` and Claude Code does the
rest. The local runtime had no such client, so `_offered_tools` dropped every `mcp__*` entry
and a cap whose whole job is MCP (a daily briefing over Calendar/Gmail/Slack/New Relic) ran with
Bash and nothing else. It then hallucinated the tool names out of its own inlined
instructions, got "tool is not available in this run" three times, and the supervisor killed
all three attempts (run sched-mosaic-9e5e5681, 2026-08-04, ~1.1M input tokens for a wrong
answer). This module is the missing half: a minimal stdio JSON-RPC client, so a local model
gets the same servers Claude gets.

WHAT IT CAN AND CANNOT SERVE — the distinction is the whole design:
  * **stdio servers** (`command` + `args`, from `data/mcp-servers.json` and the user's
    `~/.claude.json`) are subprocesses with credentials in their own `env`. We can spawn
    those ourselves, so they work on both backends: newrelic, kubernetes, aws-mcp, grafana,
    vanta, …
  * **claude.ai connectors** (Gmail, Calendar, Slack, Notion, Atlassian) are REMOTE servers
    whose OAuth lives inside Claude Code's own session. They appear in no config Otto owns,
    so there is nothing to spawn and no token to present. They stay Claude-only, and
    `unservable()` is what makes that a loud refusal at the model dropdown instead of a
    silent 3-attempt failure.
  * remote `url`/`type: http|sse` entries are treated as unservable for the same reason
    (auth we don't hold), rather than half-implemented.

BOUNDING THE TOOL SET IS LOAD-BEARING, not tidiness, and ONE bound is not enough. Measured
on this machine (2026-08-04): all 6 servers = **140 tools / ~31k tokens of schema**, and the
`tools` param is re-sent on EVERY turn of the loop, so an unbounded offer costs more per
attempt than the failure this module exists to fix. Even a single server can be too much
(grafana alone: 73 tools / ~20k). So there are two bounds, and they do different jobs:

  1. **WHICH SERVERS** — a cap's own frontmatter `tools:` line when it has one (the
     declaration is the request); otherwise the request-relevant few out of everything
     servable, so a general cap with no frontmatter isn't locked out (`servers_for`).
  2. **HOW MANY TOOLS** — `Pool` ranks what those servers expose against the request and
     keeps at most `config.LOCAL_MCP_MAX_TOOLS`. This applies to DECLARED caps too, which is
     the half that's easy to miss: `sre-incident-inspector` declares 4 servers = 126 tools /
     ~26k, so "it declared them" is not by itself a survivable budget.

The asymmetry between the two paths is deliberate. A DECLARED cap keeps score-0 tools as
filler up to the budget — the declaration is an explicit grant, and a briefing request
("catch me up") shares no vocabulary with `query_nrql`. An UNDECLARED cap gets only tools
that actually score, so "fix the flaky test" offers no MCP at all instead of 25 arbitrary
tools; Bash is still there, which is what a general cap mostly wants anyway.
"""
import json
import os
import re
import selectors
import subprocess
import threading
import time

import config
import policy
import storage

# Claude Code's naming, so an inlined SKILL.md that says `mcp__newrelic__query_nrql` still
# resolves. OpenAI function names allow [A-Za-z0-9_-]{1,64}; anything else is squashed.
_NAME_OK = re.compile(r"[^A-Za-z0-9_-]")
_MAX_NAME = 64

_PROTOCOL_VERSION = "2025-06-18"


def _expand(v):
    """`${HOME}`-style expansion in a server def (the infra repo's `.mcp.json` uses it).
    Unknown vars are left verbatim — the server can report its own missing-credential error
    better than we can guess at one."""
    return os.path.expandvars(v) if isinstance(v, str) else v


def _is_stdio(d):
    """A def we can actually launch: a command to run, and not declared as a remote
    transport. Remote servers need auth we don't hold — see the module docstring."""
    if not isinstance(d, dict) or not d.get("command"):
        return False
    return (d.get("type") or "stdio") == "stdio" and not d.get("url")


def _user_servers():
    """Stdio server defs from the user's `~/.claude.json`, USER scope only.

    `policy.discover_mcps` walks the whole file for NAMES; we need the full defs, and we
    deliberately take only the top-level `mcpServers` map. A project-scoped server is bound
    to the repo it was installed for, exactly like a project-scoped plugin skill — offering
    one from Otto's own cwd is the trap documented in CLAUDE.md."""
    try:
        with open(os.path.expanduser("~/.claude.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {n: d for n, d in (data.get("mcpServers") or {}).items() if _is_stdio(d)}


def servable(pol=None):
    """Every server the LOCAL backend can launch, `{name: def}`.

    Otto's own registry (`data/mcp-servers.json`, the same defs that become `--mcp-config`
    for Claude) wins over a same-named user entry, so one registry drives both backends.
    Honours the Admin enable/disable overrides `policy.all_mcps` already exposes — a server
    switched off in the UI must not come back through a different door."""
    pol = policy.load() if pol is None else pol
    ov = (pol or {}).get("mcps", {})
    out = dict(_user_servers())
    out.update(policy.mcp_defs() or {})
    # `_is_stdio` is applied HERE, to everything, rather than trusted from each source: this
    # is the one function callers gate on, so a remote entry reaching it through any future
    # source must still be refused.
    return {n: d for n, d in out.items()
            if _is_stdio(d) and ov.get(n, {}).get("enabled", True)}


# --- what a capability asked for -------------------------------------------

def declared_servers(cap):
    """The MCP server names a capability's own `tools:` frontmatter references.

    An agent's `tools:` line is its COMPLETE tool grant on the Claude path (CLAUDE.md), so
    it is also the most honest statement of which servers it needs — and it's already
    written, for every cap that cares. Entries look like `mcp__newrelic__*`,
    `mcp__claude_ai_Gmail__search_threads`, or a bare `mcp__grafana`."""
    declared = getattr(cap, "declared_tools", None) or []
    names = []
    for t in declared:
        m = re.match(r"^mcp__([A-Za-z0-9_-]+?)(?:__.*)?$", str(t).strip())
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def _allowed(name, allowed_tools):
    """Does the run's risk allowlist admit this `mcp__<server>__<tool>`?

    The allowlist carries server-level prefixes (`mcp__newrelic`, built by
    `activities._mcp` from the enabled set) and may carry exact names or `…__*`. Matching
    all three keeps the local backend's gate identical to the one `claude -p` enforces."""
    allowed = set(allowed_tools or [])
    if name in allowed:
        return True
    server = name.split("__")[1] if name.count("__") >= 2 else ""
    return bool(server) and (f"mcp__{server}" in allowed or f"mcp__{server}__*" in allowed)


def _server_allowed(server, allowed_tools):
    allowed = set(allowed_tools or [])
    return f"mcp__{server}" in allowed or f"mcp__{server}__*" in allowed or any(
        t.startswith(f"mcp__{server}__") for t in allowed)


def servers_for(cap, allowed_tools, request=None, pol=None):
    """The servers to actually spawn for this run.

    A cap that DECLARED servers gets exactly those (∩ servable ∩ the risk allowlist). A cap
    with NO declaration — the general worker/assistant, stock and custom caps, none of which
    have frontmatter — used to get nothing at all, which locked the generalists out of MCP
    entirely. It now gets the request-relevant few, chosen from the CACHED catalogue so the
    choice costs no subprocess; with a cold cache it falls back to every candidate (one
    expensive run, which warms the cache for the next). Bounded by `LOCAL_MCP_MAX_SERVERS`
    either way — `Pool` then applies the tool budget on top."""
    have = servable(pol)
    declared = [n for n in declared_servers(cap) if n in have]
    allow = [n for n in (declared or have) if _server_allowed(n, allowed_tools)]
    if declared or not allow:
        return allow
    # Rank the cached TOOLS, then return only the servers that own the survivors. Ranking
    # SERVERS instead would spawn three of them to discover that none of their tools match
    # ("rename the retry helper in workspace.py" did exactly that: 3 cold starts, 0 tools).
    cat = catalogue(pol)
    flat = [(n, t) for n in allow if n in cat for t in cat[n]]
    keep = _rank(flat, lambda nt: f"{nt[0]} {nt[1].get('name')} {nt[1].get('description')}",
                 request or "", config.LOCAL_MCP_MAX_TOOLS, True)
    owners = []
    for n, _t in keep:
        if n not in owners:
            owners.append(n)
    if owners:
        return owners[:config.LOCAL_MCP_MAX_SERVERS]
    # Nothing KNOWN matched. Probe the servers we've never listed (bounded) — they may hold
    # the answer, and listing them warms the cache for every later run. Deliberately the
    # last resort rather than the cold-cache short-circuit it replaced: with one unlistable
    # server in the fleet, that short-circuit never stopped firing.
    return [n for n in allow if n not in cat][:config.LOCAL_MCP_MAX_SERVERS]


def unservable(cap, pol=None):
    """The servers a capability declares that the LOCAL backend cannot provide — connectors,
    remote transports, unknown or disabled entries.

    Non-empty means "this cap cannot do its job on a local model". Callers turn that into a
    refusal (`engine.run_attempt`, the Admin dropdown) so the failure lands where it can be
    fixed, instead of 20 minutes and 1.1M tokens later."""
    want = declared_servers(cap)
    if not want:
        return []          # the common case — don't read two registries to learn nothing
    have = servable(pol)
    return [n for n in want if n not in have]


# --- the tool catalogue (so selection doesn't have to spawn first) ---------
# Scoring a server's tools against the request requires knowing them, and learning them
# means starting the server (~1-3s of `npx`/`uvx` each). So every successful `tools/list` is
# cached here, keyed on a hash of the server DEF — a changed command/args/env invalidates it,
# the same shape `conventions.py` uses for CLAUDE.md digests. A cold cache costs one run that
# spawns every candidate; after that, selection is free.
_CATALOGUE = os.path.join(config.DATA_DIR, "mcp-tools.json")


def _def_key(d):
    return str(hash(json.dumps(d or {}, sort_keys=True)))


def catalogue(pol=None):
    """`{server: [{name, description}]}` for servers whose cached entry still matches their
    current def. Plain read, no lock (storage.read_json contract).

    A server that FAILED to start is cached with an empty tool list, so it scores 0 and is
    never selected — one permanently-broken server must not disable selection for the whole
    fleet. That's not hypothetical: `aws-mcp` proxies a remote AWS endpoint and dies on an
    expired SSO token, and while it was merely absent from the cache every request fell into
    the cold-cache branch and got the same arbitrary three servers. The failure entry expires
    after `LOCAL_MCP_PROBE_TTL_S` so a transiently-down server isn't written off for good."""
    have = servable(pol)
    cached = storage.read_json(_CATALOGUE, {})
    out = {}
    for n, d in have.items():
        e = cached.get(n)
        if not e or e.get("key") != _def_key(d):
            continue
        if e.get("failed") and time.time() - e["failed"] > config.LOCAL_MCP_PROBE_TTL_S:
            continue       # stale failure — treat as unknown so it gets re-probed
        out[n] = e.get("tools") or []
    return out


def _record_catalogue(server, spec, tools, failed=False):
    """Remember one server's tool list (or that it failed to start). Goes through
    storage.mutate_json because server.py and worker.py both write this file (issue #88)."""
    entry = {"key": _def_key(spec),
             "tools": [{"name": t.get("name"),
                        "description": (t.get("description") or "")[:400]} for t in tools]}
    if failed:
        entry["failed"] = time.time()

    def fn(cur):
        if cur.get(server) == entry:
            return storage.UNCHANGED
        cur[server] = entry
        return cur
    try:
        storage.mutate_json(_CATALOGUE, fn, {})
    except Exception:  # noqa: BLE001 - a lost cache costs a spawn, never a run
        pass


_WORD = re.compile(r"[a-z]+")


def _words(text):
    """Significant words, mirroring `engine._keywords` / `registry.Capability.score` so tool
    selection ranks a request the same way routing shortlists a capability."""
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 3}


def _score(text, want):
    """Overlap count between a request's words and a tool's name+description. Deliberately
    the same crude scorer routing uses — IDF weighting was measured to make ranking WORSE on
    a technical corpus (see the `recent_facts` note in CLAUDE.md), and a tool name is an even
    smaller sample than a stored fact."""
    return len(_words(text) & want)


def _rank(items, key, request, budget, require_score):
    """Top-`budget` items by request relevance. `require_score` drops non-matching items
    entirely (undeclared caps) instead of filling the budget with arbitrary ones."""
    want = _words(request)
    scored = [(_score(key(it), want), i, it) for i, it in enumerate(items)]
    if require_score:
        scored = [t for t in scored if t[0] > 0]
    scored.sort(key=lambda t: (-t[0], t[1]))       # ties keep discovery order
    return [it for _s, _i, it in scored[:budget]]


# --- the stdio JSON-RPC session --------------------------------------------

class McpError(RuntimeError):
    """A server-side or transport failure. Surfaced to the model as tool-result TEXT (same
    contract as every other local tool error), never raised into the loop."""


class Session:
    """One MCP server subprocess, spoken to over newline-delimited JSON-RPC on its stdio.

    Deliberately minimal: `initialize` → `tools/list` → `tools/call`. No resources, no
    prompts, no sampling, no notifications beyond the required `initialized` — a local model
    can only use tools, and every extra surface is one more thing to get wrong."""

    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.proc = None
        self._id = 0
        self._sel = None
        self._buf = b""
        self._lock = threading.Lock()

    # -- transport --
    def start(self):
        env = dict(os.environ)
        env.update({k: str(_expand(v)) for k, v in (self.spec.get("env") or {}).items()})
        cmd = [str(_expand(self.spec["command"]))] + [str(_expand(a))
                                                      for a in (self.spec.get("args") or [])]
        self.proc = subprocess.Popen(
            cmd, env=env, cwd=self.spec.get("cwd") or None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self._sel = selectors.DefaultSelector()
        self._sel.register(self.proc.stdout, selectors.EVENT_READ)
        self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "otto-local-runtime", "version": "1"}},
            timeout=config.LOCAL_MCP_STARTUP_S)
        self._notify("notifications/initialized")
        return self

    def _send(self, msg):
        if not self.proc or self.proc.poll() is not None:
            raise McpError(f"server '{self.name}' is not running")
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise McpError(f"server '{self.name}' closed its input ({e})") from None

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _read_message(self, deadline):
        """One JSON object off stdout, honouring a wall-clock deadline.

        `selectors` rather than a reader thread: a hung server must time out the CALL without
        leaving a thread blocked on a pipe for the life of the worker."""
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                if line.strip():
                    try:
                        return json.loads(line)
                    except ValueError:
                        continue        # servers sometimes emit non-JSON banners on stdout
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(f"server '{self.name}' timed out")
            if not self._sel.select(timeout=remaining):
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise McpError(f"server '{self.name}' exited "
                               f"(code {self.proc.poll()}) — check its command and credentials")
            self._buf += chunk

    def _request(self, method, params=None, timeout=None):
        """A request/response round-trip. Notifications and unrelated ids arriving in between
        are skipped, so a chatty server's log notifications can't be mistaken for our reply."""
        timeout = config.LOCAL_MCP_CALL_S if timeout is None else timeout
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            deadline = time.monotonic() + timeout
            while True:
                msg = self._read_message(deadline)
                if msg.get("id") != rid:
                    continue
                if msg.get("error"):
                    err = msg["error"]
                    raise McpError(f"{err.get('message') or err} "
                                   f"(server '{self.name}', {method})")
                return msg.get("result") or {}

    def close(self):
        try:
            if self._sel:
                self._sel.close()
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:  # noqa: BLE001 - teardown must never break a run
            pass

    # -- protocol --
    def list_tools(self):
        """Every tool the server exposes, following `nextCursor` pagination."""
        tools, cursor = [], None
        for _ in range(20):        # bound: a server that never stops paginating
            params = {"cursor": cursor} if cursor else {}
            res = self._request("tools/list", params, timeout=config.LOCAL_MCP_STARTUP_S)
            tools += [t for t in (res.get("tools") or []) if t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call(self, tool, args):
        res = self._request("tools/call", {"name": tool, "arguments": args or {}})
        text = _content_text(res.get("content"))
        if res.get("isError"):
            return f"Error: {text or 'the MCP tool reported a failure'}"
        return text or "(no output)"


def _content_text(content):
    """MCP content blocks flattened to the TEXT a chat model can consume. Non-text blocks
    (image/audio/resource) are described rather than dropped, so a model isn't left thinking
    the call returned nothing."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(str(b.get("text") or ""))
        elif b.get("type") == "resource":
            r = b.get("resource") or {}
            parts.append(str(r.get("text") or f"({r.get('uri') or 'resource'})"))
        else:
            parts.append(f"({b.get('type')} content omitted — this runtime is text-only)")
    return "\n".join(p for p in parts if p).strip()


# --- the pool the runtime holds -------------------------------------------

def _spec_name(server, tool):
    name = _NAME_OK.sub("_", f"mcp__{server}__{tool}")
    return name[:_MAX_NAME]


class Pool:
    """The MCP tools offered to one local run: lazily started servers, a flat name→(server,
    tool) map, and one `close()` the runtime calls in its `finally`.

    A server that fails to start is DROPPED with its reason recorded in `errors` rather than
    failing the run: the other servers (and Bash) still work, and the model is told what it
    doesn't have. A dead New Relic must not cost us the whole briefing.

    THE TOOL BUDGET IS APPLIED HERE, to every run: what the servers expose is ranked against
    the request and trimmed to `config.LOCAL_MCP_MAX_TOOLS`. `trimmed` records how many were
    dropped, so a bounded offer is never silent (CLAUDE.md: "no silent caps")."""

    def __init__(self, servers, allowed_tools=None, pol=None, request=None,
                 max_tools=None, require_score=False):
        self.specs = []          # OpenAI function specs, ready for the `tools` param
        self.errors = {}         # server -> why it isn't available
        self.trimmed = 0         # tools dropped by the budget (never silently)
        self._route = {}         # offered name -> (Session, real tool name)
        self._sessions = []
        have = servable(pol)
        found = []               # (offered_name, session, real_name, spec_dict)
        for name in servers or []:
            spec = have.get(name)
            if not spec:
                self.errors[name] = "not a launchable stdio server for the local backend"
                continue
            try:
                sess = Session(name, spec).start()
                tools = sess.list_tools()
            except Exception as e:  # noqa: BLE001 - a broken server is data, not a crash
                self.errors[name] = str(e)
                _record_catalogue(name, spec, [], failed=True)
                continue
            self._sessions.append(sess)
            _record_catalogue(name, spec, tools)
            for t in tools:
                offered = _spec_name(name, t["name"])
                if any(f[0] == offered for f in found) or not _allowed(offered, allowed_tools):
                    continue
                found.append((offered, sess, t["name"], {
                    "type": "function", "function": {
                        "name": offered,
                        "description": (t.get("description") or t["name"])[:1024],
                        "parameters": t.get("inputSchema")
                        or {"type": "object", "properties": {}}}}))
        budget = config.LOCAL_MCP_MAX_TOOLS if max_tools is None else max_tools
        kept = _rank(found, lambda f: f"{f[0]} {f[3]['function']['description']}",
                     request or "", budget, require_score) if budget else found
        self.trimmed = len(found) - len(kept)
        for offered, sess, real, spec_dict in kept:
            self._route[offered] = (sess, real)
            self.specs.append(spec_dict)

    def __contains__(self, name):
        return name in self._route

    @property
    def names(self):
        return set(self._route)

    def call(self, name, args):
        sess, tool = self._route[name]
        try:
            return sess.call(tool, args)
        except McpError as e:
            return f"Error: {e}"

    def close(self):
        for s in self._sessions:
            s.close()
        self._sessions, self._route = [], {}
