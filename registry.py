"""Capability registry — discovers your REAL Claude Code subagents and skills.

Subagents live in ~/.claude/agents/*.md ; skills in ~/.claude/skills/*/SKILL.md.
Both carry YAML frontmatter with `name` + `description`. We parse just those two
(no PyYAML dependency) to build the catalogue the router chooses from.
"""
import glob
import json
import math
import os
import re

import config
import repos
import storage

AGENTS_DIR = os.path.expanduser("~/.claude/agents")
SKILLS_DIR = os.path.expanduser("~/.claude/skills")
STOCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capabilities")  # bundled with Otto
PLUGINS_FILE = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
CUSTOM_FILE = os.path.join(config.DATA_DIR, "capabilities.json")
PROJECTS_FILE = os.path.join(config.DATA_DIR, "projects.json")   # external repos to import .claude/ from


# --- per-capability risk (read vs write) ----------------------------------
# This is the real guardrails-layer data: reporters auto-run; mutators need the
# human gate. Explicit overrides keep it accurate; unknowns default to "write"
# (safe: an extra approval prompt is harmless, an un-gated mutation is not).
#   This map covers only what Otto SHIPS. Your own agents and skills are classified by the
#   keyword heuristic below on first sight, and whatever you then set in Admin is persisted to
#   `data/policy.json`, which `apply_policy` lets win over this table — so correcting a
#   misclassified capability of your own is a one-time click, not an edit here.
_RISK = {
    # read-only: report / investigate / review
    "board-status": "read", "deploy-status": "read", "find-claude-session": "read",
    "github-pr-review": "read", "incident": "read", "tech-investigation": "read",
    "empirical-investigation": "read",
    # Pinned, not inferred: brainstorm skips the write gate by being read, and the keyword
    # heuristic reads its description ("weighs options", "pushes back") on vocabulary alone —
    # an edit to that prose must not be able to flip the mode into needing an approval card.
    "brainstorm": "read",
    # writers / mutators: open PRs, post, create, apply, renew, etc.
    "commit": "write", "github-issue": "write", "design-doc": "write", "worker": "write",
    # stock caps bundled with Otto (capabilities/) — writers gated, reviewers/researchers not.
    "product-manager": "write", "qa-tester": "write", "technical-writer": "write",
    "code-reviewer": "read", "researcher": "read",
}
_WRITE_HINTS = ("create", "open ", "grant", "apply", "commit", "ingest", "renew",
                "rotate", "post ", "draft", "implement", "merge", "refresh")
_READ_HINTS = ("status", "overview", "report", "list", "review", "investigate",
               "summary", "summarize", "check", "audit", "find", "read-only", "inspect")


def classify(name, description):
    if name in _RISK:
        return _RISK[name]
    text = (name + " " + description).lower()
    if any(h in text for h in _WRITE_HINTS):
        return "write"
    if any(h in text for h in _READ_HINTS):
        return "read"
    return "write"   # safe default: gate the unknown (an extra prompt beats an un-gated mutation)


class Capability:
    def __init__(self, kind, name, description):
        self.kind = kind                      # "agent" | "skill"
        self.name = name
        self.description = " ".join(description.split())
        self.risk = classify(name, self.description)   # "read" | "write"
        self.enabled = True                            # toggled by admin policy
        self.source = "builtin"                        # default for discovered ~/.claude caps; else "otto" | "project" | "stock"
        self.prompt = None                             # set for custom (kind="custom") caps
        self.plugin = None                             # set for plugin-bundled skills
        self.invoke_name = name                        # bare name used in the actual invocation
        self.cwd = None                                # run `claude -p` from here (project caps)
        self.mcp_config = None                         # repo `.mcp.json` to merge in (project caps)
        self.general = False                           # built-in general fallbacks (assistant/worker; never pruned from routing)
        self.route_hidden = False                      # discoverable + pinnable, but never a Router #1 candidate (brainstorm)
        self.tier = None                               # stock caps only: "bundled" (default-on) | "optional" (default-off)
        self.stock_kind = None                         # stock caps only: "agent" | "skill" — UI grouping, distinct from .kind (always "custom": how it's run)
        self.tool_free = False                         # pure-LLM cap: eligible for LOCAL execution (issue #42; read-risk only)
        self.path = None                               # source .md of an agent/skill — inlined when the LOCAL runtime executes it
        self.declared_tools = []                       # frontmatter `tools:` grant, verbatim (mcp_client reads it to pick servers)

    def score(self, request):
        """This cap's lexical relevance to `request`, scored on its own. Prefer `rank()` for a
        real routing decision — scored against the whole catalogue it can tell a discriminating
        word from a ubiquitous one, which this cannot."""
        return rank(request, [self])[self.name]


# --- lexical ranking (feeds the router's shortlist) -----------------------
# Words too common to discriminate between capabilities. Kept small on purpose: IDF below
# already demotes anything frequent *in this catalogue* ("terraform", "cluster"); this list
# only removes the conversational filler that IDF can't see, because it appears in requests
# but rarely in descriptions.
_RANK_STOP = set(
    "the a an and or of to for in on at is are was were be been it its this that with from by "
    "as if not you your i we our my me they them there here what when where which who how why "
    "do does did can could should would will please just now get got need want make made run "
    "into out about over under again some any all".split())


def _rank_tokens(text):
    """Tokens for lexical matching. Pasted URLs are stripped first: their path segments (e.g. a
    CI build URL's "buildConfiguration/.../Infrastructure") inject topic nouns that skew the
    shortlist toward topic-matching read caps and can prune the cap that does the actual action."""
    text = re.sub(r"https?://\S+", " ", (text or "").lower())
    return [w for w in re.findall(r"[a-z0-9]+", text) if len(w) > 2 and w not in _RANK_STOP]


def rank(request, caps):
    """Lexical relevance of every cap in `caps` to `request`, as {name: float}.

    Scored against the CATALOGUE, not per-cap, for two reasons the old per-cap word count got
    wrong. (1) IDF: a word matching 40 of 157 descriptions ("aws", "cluster") says almost nothing;
    one matching 2 says almost everything. A flat count can't tell them apart, so the discriminating
    word drowns in ubiquitous ones. (2) Length normalization: a raw count rewards a long description
    for being long. Both produced huge ties — and since the shortlist is a top-N cut, a tie AT the
    cutoff means the correct cap's survival was decided by sort order, not relevance.

    A cap's own name is high signal (skill names are hyphenated intent: `aws-vpn-renew`), so it's
    indexed alongside the description and boosted when the request names it outright."""
    caps = list(caps)
    docs = {c.name: set(_rank_tokens(c.description)) | set(_rank_tokens(c.name)) for c in caps}
    df = {}
    for toks in docs.values():
        for w in toks:
            df[w] = df.get(w, 0) + 1
    n = len(caps) or 1
    q = set(_rank_tokens(request))
    r = (request or "").lower()
    out = {}
    for c in caps:
        d = docs[c.name]
        s = sum(math.log(1 + n / df.get(w, 1)) for w in q & d) / math.sqrt(len(d) or 1)
        if c.name.lower() in r:                       # request names the cap outright
            s += 3.0
        bare = set(_rank_tokens(c.name.split(":")[-1]))
        if bare and bare <= q:                        # every word of the bare name is in the request
            s += 2.0
        out[c.name] = s
    return out


def _parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    data, key, buf = {}, None, []
    for line in block.splitlines():
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            if key:
                data[key] = " ".join(buf).strip()
            key, val = m.group(1), m.group(2).strip()
            buf = [val] if val and val not in (">", "|", ">-", "|-") else []
        elif key:
            buf.append(line.strip())
    if key:
        data[key] = " ".join(buf).strip()
    return data


def apply_policy(caps, pol):
    """Overlay admin overrides onto the discovered capabilities (risk + enabled + tool_free).
    tool_free (issue #42: pure-LLM, eligible for local execution) is conservative by default
    and only ever holds for a read-risk cap — a write is never locally executed, so a risk
    flip to write clears it here rather than trusting every downstream check."""
    ov = (pol or {}).get("capabilities", {})
    for c in caps:
        o = ov.get(c.name, {})
        c.risk = o.get("risk", classify(c.name, c.description))
        # Optional-tier stock caps are an opt-in catalog: default OFF until enabled in Admin.
        c.enabled = o.get("enabled", getattr(c, "tier", None) != "optional")
        c.tool_free = bool(o.get("tool_free", False)) and c.risk == "read"
    return caps


def _frontmatter(path):
    try:
        with open(path) as f:
            return _parse_frontmatter(f.read())
    except OSError:
        return {}


def _declared_tools(fm):
    """The frontmatter `tools:` grant as a list. On the Claude path this line IS an agent's
    complete tool grant (CLAUDE.md), so it's also the authoritative statement of which MCP
    servers the cap needs — `mcp_client` reads it to decide what to spawn for a LOCAL run,
    and whether the cap can run locally at all. Absent line -> [] (no MCP offered locally)."""
    raw = (fm or {}).get("tools") or ""
    return [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]


def plugin_skills():
    """Skills bundled in installed Claude Code plugins, USER-scoped installs only. Reads the
    authoritative install manifest (`installed_plugins.json` → a versioned `installPath` per
    plugin), globs each plugin's `skills/` recursively (some plugins nest skills in sub-dirs),
    and namespaces each as `<plugin>:<skill>` — the form Claude Code uses to invoke them.
    Yields (name, description, plugin, path)."""
    try:
        with open(PLUGINS_FILE) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return
    for key, installs in (manifest.get("plugins") or {}).items():
        plugin = key.split("@", 1)[0]
        seen = set()
        # PROJECT-scoped installs are skipped: a plugin installed for one repo is only loaded by
        # `claude -p` when it runs from THAT repo, and Otto's execution cwd is its own directory (or
        # a provisioned clone), so `/<plugin>:<skill>` there is an unknown command. Offering one to
        # the router is strictly a trap — measured: a Slack run routed to a project-scoped plugin
        # skill and burned all three attempts plus a final Opus escalation on `Unknown command`.
        # A plugin installed at BOTH scopes still surfaces via its user-scope entry.
        for inst in installs or []:
            if (inst.get("scope") or "user") != "user":
                continue
            base = inst.get("installPath")
            if not base or base in seen:
                continue
            seen.add(base)
            for path in sorted(glob.glob(os.path.join(base, "skills", "**", "SKILL.md"), recursive=True)):
                fm = _frontmatter(path)
                if fm.get("name"):
                    yield f"{plugin}:{fm['name']}", fm.get("description", ""), plugin, path


def _project_entries():
    """Registered project repos as structured entries `{url, path, instructions}`. Migrates on
    read from BOTH older formats so an existing `data/projects.json` keeps working: a bare path
    string becomes `{path, instructions: ""}` (issue #69), and a `{path}` entry with no `url`
    stays valid with an empty one (its URL is backfilled from the checkout's `origin` by
    `backfill_project_urls`, which costs a subprocess and must not run on this read path).

    A repo is identified by its REMOTE URL; `path` is the operator's OPTIONAL local checkout.
    An entry needs one or the other — an entry with neither is dropped, since nothing
    downstream could resolve it to a directory."""
    try:
        with open(PROJECTS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for p in data:
        if isinstance(p, str):
            out.append({"url": "", "path": p, "instructions": ""})
        elif isinstance(p, dict) and (p.get("path") or p.get("url")):
            out.append({"url": p.get("url", "") or "", "path": p.get("path", "") or "",
                        "instructions": p.get("instructions", "")})
    return out


def project_path(entry):
    """The effective on-disk root for a registered entry — what every consumer downstream of
    registration actually reads (`project_skills`, `conventions.digest`, the READ source note,
    `workspace` cloning). The operator's own checkout WINS when it is present on disk: it is
    a real working tree with full history, and re-cloning a repo already sitting on the machine
    buys nothing. Otherwise the managed clone (`data/repos/<slug>`) stands in.

    Deterministic and offline for the managed case — the path is derived from the URL, so this
    resolves the same whether or not the clone has landed yet. A registration whose clone failed
    therefore reads as a repo with no capabilities and no conventions, never as a missing key."""
    path = (entry or {}).get("path") or ""
    if path and os.path.isdir(path):
        return path
    managed = repos.managed_path((entry or {}).get("url") or "")
    return managed or path


def projects():
    """External repo roots whose project-scoped `.claude/` skills+agents are imported. Returns
    the EFFECTIVE paths (back-compat for the allowlist / discovery callers, which have always
    taken a directory and still do)."""
    return [project_path(e) for e in _project_entries()]


def _entry_for(path):
    """The entry a caller means by `path`. Matched on the effective path first (that is what the
    UI renders and posts back) and on the stored checkout second, so a client holding either
    spelling — or a stale one from before a managed clone landed — still addresses one row."""
    path = (path or "").rstrip("/")
    for e in _project_entries():
        if path in (project_path(e).rstrip("/"), (e.get("path") or "").rstrip("/")):
            return e
    return None


def project_url(path):
    """The remote URL registered for a project root, or "" if it was added as a bare path and
    has not been backfilled."""
    e = _entry_for(path)
    return (e or {}).get("url", "") or ""


def backfill_project_urls():
    """Derive the missing `url` of every legacy path-only entry from that checkout's `origin`,
    once. Costs one `git remote get-url` per un-backfilled entry, so it belongs to startup
    (`rebuild`) and never to `_project_entries`. An entry whose checkout has no usable origin
    keeps an empty URL and goes on working off its path."""
    entries = _project_entries()
    changed = False
    for e in entries:
        if e.get("url") or not (e.get("path") and os.path.isdir(e["path"])):
            continue
        origin = repos.origin_of(e["path"])
        if origin and repos.parse(origin):
            e["url"] = repos.parse(origin)["url"]
            changed = True
    if changed:
        save_projects(entries)
    return changed


def project_namespace(path):
    """Stable per-project key (for the memory namespace + UI), slugged from the repo basename."""
    base = os.path.basename((path or "").rstrip("/")) or (path or "")
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "project"


def project_meta(path):
    """`{path, url, checkout, managed, namespace, instructions}` for a registered project
    (defaults if not found). `path` is the effective root, `checkout` the operator's own
    registered one (empty when Otto's managed clone is all there is)."""
    e = _entry_for(path) or {}
    return {"path": path, "url": e.get("url", "") or "", "checkout": e.get("path", "") or "",
            "managed": repos.is_managed(path), "namespace": project_namespace(path),
            "instructions": e.get("instructions", "")}


def set_project_instructions(path, instructions):
    """Set a project's standing instructions (injected into runs in that project, issue #69)."""
    entries = _project_entries()
    target = _entry_for(path)
    if target is None:
        entries.append({"url": "", "path": path, "instructions": (instructions or "").strip()})
    else:
        for e in entries:
            if (e.get("url"), e.get("path")) == (target.get("url"), target.get("path")):
                e["instructions"] = (instructions or "").strip()
    save_projects(entries)


def save_projects(lst):
    """Persist project entries. Normalises to the structured `{url, path, instructions}` form so
    old bare-string and path-only lists are migrated forward, and metadata is never clobbered."""
    norm = []
    for e in lst:
        if isinstance(e, str):
            norm.append({"url": "", "path": e, "instructions": ""})
        elif isinstance(e, dict) and (e.get("path") or e.get("url")):
            norm.append({"url": e.get("url", "") or "", "path": e.get("path", "") or "",
                         "instructions": e.get("instructions", "")})
    storage.write_json(PROJECTS_FILE, norm)


def add_project(path=None, url=""):
    """Register a project repo and return its EFFECTIVE root. Either half may be omitted: a URL
    alone registers a repo Otto clones for itself, a path alone is the legacy form (its URL is
    backfilled from `origin` at the next `rebuild`), and both together mean "this repo, and I
    already have a checkout of it — use mine".

    Identity is the URL when there is one, so re-registering the same repo under a different
    local path updates the existing row instead of forking a duplicate."""
    path = os.path.abspath(os.path.expanduser(path.strip())) if (path or "").strip() else ""
    info = repos.parse(url) if url else None
    url = info["url"] if info else ""
    entries = _project_entries()
    for e in entries:
        same = (url and e.get("url") == url) or (path and (e.get("path") or "") == path)
        if same:
            e["url"] = url or e.get("url", "")
            e["path"] = path or e.get("path", "")
            save_projects(entries)
            return project_path(e)
    entry = {"url": url, "path": path, "instructions": ""}
    entries.append(entry)
    save_projects(entries)
    return project_path(entry)


def remove_project(path):
    """Deregister by effective path or by registered checkout. The managed clone (if any) is
    discarded with the row — leaving it behind would have the next registration of the same URL
    silently adopt a stale tree."""
    raw = (path or "").strip()
    path = os.path.abspath(os.path.expanduser(raw)) if raw else ""
    target = _entry_for(path) or _entry_for(raw)
    if target is None:
        return
    if target.get("url"):
        repos.discard(target["url"])
    save_projects([e for e in _project_entries()
                   if (e.get("url"), e.get("path")) != (target.get("url"), target.get("path"))])


def project_skills():
    """Capabilities from project-scoped `.claude/{agents,skills}` in configured repos.

    Unlike global caps, these only resolve when `claude -p` runs *from the repo root* — so
    each carries the repo as `cwd` and its `.mcp.json` (if present) to merge into the run.
    The catalogue name is namespaced `<project>:<name>` to avoid clashing with global caps,
    but the bare `invoke_name` is what the actual `/skill` or subagent call uses (project
    skills are invoked by their plain name once cwd is set, not namespaced like plugins).
    Yields (kind, namespaced_name, invoke_name, description, cwd, mcp_config, path)."""
    for root in projects():
        proj = os.path.basename(root.rstrip("/")) or root
        cdir = os.path.join(root, ".claude")
        mcp = os.path.join(root, ".mcp.json")
        mcp = mcp if os.path.exists(mcp) else None
        for path in sorted(glob.glob(os.path.join(cdir, "agents", "*.md"))):
            fm = _frontmatter(path)
            if fm.get("name"):
                yield "agent", f"{proj}:{fm['name']}", fm["name"], fm.get("description", ""), root, mcp, path
        for path in sorted(glob.glob(os.path.join(cdir, "skills", "*", "SKILL.md"))):
            fm = _frontmatter(path)
            if fm.get("name"):
                yield "skill", f"{proj}:{fm['name']}", fm["name"], fm.get("description", ""), root, mcp, path


def stock_caps():
    """Capabilities BUNDLED with Otto (shipped in the repo's `capabilities/` dir), so an
    install has them regardless of the user's ~/.claude. Each is a markdown file with
    name/description frontmatter; the body is the cap's instructions.

    They load as `custom` caps whose prompt IS that body — Otto runs the instructions via
    `claude -p` directly, NOT by spawning a Claude Code subagent by name: a bundled file isn't
    on any path Claude Code scans, so `Use the <name> subagent` (the agent-kind invocation)
    couldn't resolve. Consequence: the file's `model:`/`skills:` frontmatter is NOT honoured
    (Otto picks the execution model via the gateway) — set a Claude `cap_exec` for a heavy
    stock cap or it runs on the local exec tier.

    Two tiers (a lean default set + an opt-in catalog, so one Otto serves installs with very
    different workloads): `capabilities/bundled/` (and legacy flat `capabilities/*.md`) load
    enabled; `capabilities/optional/` load DISABLED by default — enable per-install in the
    Admin tab (a plain `enabled` policy override). Yields (name, description, body, path, tier, kind).

    `kind` (an optional frontmatter `kind: agent|skill` line, default "agent" since every
    stock cap so far is agent-shaped) is UI grouping only — it lets the Admin panel fold a
    stock cap into the Agents/Skills section it conceptually belongs to instead of a separate
    Stock section. It is NOT the execution `Capability.kind`, which stays "custom" for every
    stock cap regardless (see the caller below)."""
    groups = (("bundled", os.path.join(STOCK_DIR, "*.md")),
              ("bundled", os.path.join(STOCK_DIR, "bundled", "*.md")),
              ("optional", os.path.join(STOCK_DIR, "optional", "*.md")))
    for tier, pattern in groups:
        for path in sorted(glob.glob(pattern)):
            fm = _frontmatter(path)
            if not fm.get("name"):
                continue
            try:
                with open(path, errors="replace") as f:
                    body = f.read()
            except OSError:
                continue
            if body.startswith("---"):                 # strip YAML frontmatter
                end = body.find("\n---", 3)
                body = body[end + 4:] if end != -1 else body
            kind = fm.get("kind", "agent").strip().lower()
            if kind not in ("agent", "skill"):
                kind = "agent"
            yield fm["name"], fm.get("description", ""), body.strip(), path, tier, kind


def load():
    caps = []
    for path in sorted(glob.glob(os.path.join(AGENTS_DIR, "*.md"))):
        fm = _frontmatter(path)
        if fm.get("name"):
            cap = Capability("agent", fm["name"], fm.get("description", ""))
            cap.path = path
            cap.declared_tools = _declared_tools(fm)
            caps.append(cap)
    for path in sorted(glob.glob(os.path.join(SKILLS_DIR, "*", "SKILL.md"))):
        fm = _frontmatter(path)
        if fm.get("name"):
            cap = Capability("skill", fm["name"], fm.get("description", ""))
            cap.path = path
            cap.declared_tools = _declared_tools(fm)
            caps.append(cap)
    # Skills bundled in installed plugins (namespaced plugin:skill).
    for name, desc, plugin, path in plugin_skills():
        cap = Capability("skill", name, desc)
        cap.plugin = plugin
        cap.path = path
        caps.append(cap)
    # Project-scoped caps from external repos' `.claude/` (run with the repo as cwd).
    for kind, name, invoke, desc, cwd, mcp, path in project_skills():
        cap = Capability(kind, name, desc)
        cap.invoke_name = invoke
        cap.cwd = cwd
        cap.mcp_config = mcp
        cap.source = "project"
        cap.path = path
        caps.append(cap)
    # Otto-added custom capabilities (a named prompt run via claude -p)
    if os.path.exists(CUSTOM_FILE):
        with open(CUSTOM_FILE) as f:
            for c in json.load(f):
                cap = Capability("custom", c["name"], c.get("description", ""))
                cap.risk = c.get("risk", cap.risk)
                cap.prompt = c.get("prompt", "")
                cap.source = "otto"
                caps.append(cap)
    # Stock capabilities bundled with Otto (repo `capabilities/`) — self-contained defaults so
    # an install works without relying on the user's ~/.claude. Appended AFTER the real caps, so
    # the first-wins de-dupe below lets a user ~/.claude cap of the same name win (stock is a
    # fallback tier). Loaded as custom caps whose prompt is the file body (see stock_caps).
    for name, desc, body, path, tier, kind in stock_caps():
        cap = Capability("custom", name, desc)
        cap.prompt = f"{body}\n\nRequest: {{request}}"
        cap.source = "stock"
        cap.path = path
        cap.tier = tier
        cap.stock_kind = kind
        caps.append(cap)
    # Built-in general assistant: a read-only "just answer the question" capability so a bare,
    # informational request (a question with no action/deliverable) has somewhere to land instead
    # of being force-routed to a specialized WRITE agent that matches the topic. It answers FROM
    # the knowledge + facts already injected into every fresh run's system prompt (issue #67), which
    # is what makes "chat with my knowledge base" work. Always present; a user custom cap with the
    # same name takes precedence (first-wins de-dupe below places these builtins last).
    caps.append(_general_assistant())
    # Built-in general worker (issue #152): the write-side sibling of the assistant. A task-shaped
    # request with a concrete deliverable but NO matching specialized capability lands here instead
    # of being force-routed to a topic-matching specialist (or an interactive, git-self-managing
    # agent like sre-minion). Deliberately THIN: it implements in cwd and reports — the platform
    # (repo-mode finalize, verify ladder, QA loop) owns branching/PRs/review.
    caps.append(_general_worker())
    # Built-in brainstorm partner: the read-only conversational MODE. Same shape as the two
    # general fallbacks, with one difference that matters — `route_hidden`, so Router #1 can
    # never land here on its own. It is reached only by an explicit opt-in (`/brainstorm`, or
    # the composer toggle that pins it), because the mode skips the verify ladder and a route
    # into it by mistake would silently drop a real task's only quality check.
    caps.append(_brainstorm())
    # De-dupe by name (a plugin installed at user + project scope can surface twice).
    seen, unique = set(), []
    for c in caps:
        if c.name in seen:
            continue
        seen.add(c.name)
        unique.append(c)
    return unique


ASSISTANT_NAME = "assistant"


def _general_assistant():
    cap = Capability("custom", ASSISTANT_NAME,
        "General assistant. Answers a direct question, explains a concept, or summarizes "
        "information using the knowledge and context already loaded into Otto — with NO action, "
        "file change, deployment, ticket, or side effect. Route here for an informational or "
        "question-shaped request (\"what is…\", \"are we…\", \"why does…\", \"does X…\", \"explain…\", "
        "\"how does X work\") when the user wants an ANSWER rather than a task performed, and no "
        "specialized capability produces a concrete deliverable. The read-only fallback for "
        "questions the loaded knowledge base can answer.")
    cap.risk = "read"
    cap.source = "stock"                                # ships WITH Otto → grouped under "Stock"
    cap.general = True
    # The context block is a STARTING POINT, not an authority. It used to be described here as
    # "the authoritative source", which flatly contradicted the same context's own header (facts
    # are dated recollections, verify current state with tools and let the tool win) — and the
    # prompt is what the model reads first. That is how one wrong stored fact ("vLLM is not
    # deployed in production") kept being served back as a current-state answer.
    cap.prompt = (
        "You are a general assistant answering the user's request directly and concisely.\n"
        "Reference knowledge, learned facts, and directives the user has loaded are provided in "
        "your system prompt. Start from them, and answer FROM them for anything STABLE — how "
        "something works, a past decision, the user's preferences, background — saying that's "
        "where it came from.\n"
        "They are NOT authoritative about the CURRENT state of any system: they are dated, and a "
        "later change may have superseded them. If the request turns on what exists, is deployed, "
        "enabled, running, or reachable right now, verify it with your read-only tools and let the "
        "tool result override the provided context — even when that context appears to answer the "
        "question outright. Never repeat a remembered current-state claim you did not re-check.\n"
        "Do NOT create, modify, deploy, or otherwise change anything — this is a read-only "
        "answer.\n\n"
        "Request: {request}")
    return cap


WORKER_NAME = config.WORKER_CAP


def _general_worker():
    cap = Capability("custom", WORKER_NAME,
        "General worker. Implements a concrete, task-shaped request end to end — code or "
        "config changes, file edits, small scripts, docs — in the target repo or working "
        "directory, following that repo's own conventions and running its tests. Route here "
        "for an ACTION request with a real deliverable (\"fix…\", \"add…\", \"implement…\", "
        "\"change…\", \"update…\", \"write…\") when NO specialized capability performs that "
        "action. The write-capable fallback for tasks that don't match a purpose-built "
        "agent or skill.")
    cap.risk = "write"
    cap.source = "stock"                                # ships WITH Otto → grouped under "Stock"
    cap.general = True
    cap.prompt = (
        "You are an end-to-end development worker. You take a task from understanding to a "
        "working, tested change — the platform turns your change into a reviewed draft PR.\n\n"
        "Work in phases:\n"
        "1. UNDERSTAND — if the request references a GitHub issue (a #number or URL), read it "
        "with `gh issue view` before touching code. If the request asks YOU to pick which "
        "ticket to work on, list the open candidates (`gh issue list --state open`, or the "
        "project board's Ready column), choose the best by impact vs effort, and state your "
        "pick with a one-line rationale — then implement it in this same run; never stop at "
        "the recommendation. Explore the repo to find the files that "
        "must change and the existing patterns to follow.\n"
        "2. IMPLEMENT — make the change in the current working directory, following the repo's "
        "own conventions (CLAUDE.md, existing code style, existing patterns). Don't introduce "
        "new conventions where the repo already has one.\n"
        "3. VERIFY — run whatever tests, linters, or type checks the repo provides, and fix "
        "what you broke. Report exactly what you changed and what you verified.\n\n"
        "Do NOT manage git yourself: no branches, no commits, no pushes, no `gh pr create` — "
        "the platform owns version control, PR creation, code review, and delivery. Your change "
        "will be code-reviewed automatically after the PR opens, and you may be re-invoked on "
        "the same branch to address review findings — so leave the tree in a clean, committable "
        "state. Do not ask for confirmation mid-task; if the request is ambiguous, state your "
        "assumption and proceed with the most reasonable interpretation.\n\n"
        # THE ESCAPE HATCH THE CONTRACT ABOVE WAS MISSING. "The platform owns git" is right, but
        # it leaves no legal move when the branch you were handed does not contain the code the
        # request is about — and then a capable model deadlocks rather than fails. Measured
        # (`web-d2438694`): given a default-branch tree while the request described a file that
        # only exists on an open PR's branch, the run correctly diagnosed the mismatch and then
        # spent twenty minutes and 784k input tokens reading Otto's own database, transcripts
        # and session files trying to work out how the platform intended it to deliver a change
        # it could not legally reach. The supervisor killed it for wandering off-task. The two
        # attempts after it never noticed the mismatch and edited the wrong file instead.
        "If the code the request describes is NOT in your working directory — the file is "
        "missing, or its contents don't match what the request says is there (different length, "
        "no such function, no such line) — you are probably on a different branch or revision "
        "than the request is about. That is a legitimate outcome, not a failure: say so plainly "
        "as your result, naming what you expected and what you actually found, and stop. Do NOT "
        "switch branches, fetch other refs, or hunt for the code outside your working directory, "
        "and do NOT substitute the nearest similarly-named thing you can find and present it as "
        "the requested change. Never inspect the platform's own internals (its database, "
        "transcripts, sessions or config) to work out how your change will be delivered — that "
        "is not your concern and there is no answer there.\n\n"
        "Request: {request}")
    return cap


BRAINSTORM_NAME = config.BRAINSTORM_CAP


def _brainstorm():
    """The thinking-partner capability. Read-only like the assistant, but a different JOB: the
    assistant answers a question and closes it, this one keeps a question open. Its output shape
    lives in `contracts._THINKING_PARTNER_FORMAT`, applied via the `brainstorm` audience."""
    cap = Capability("custom", BRAINSTORM_NAME,
        "Brainstorm partner. Thinks a half-formed idea through WITH the user in a short "
        "back-and-forth — weighs options, argues a position, pushes back, asks the one question "
        "that decides it — instead of answering and closing. Read-only: no action, file change, "
        "deployment, ticket, or side effect. Reached only when the user explicitly asks for it "
        "(/brainstorm, or the composer's Brainstorm toggle); never an automatic route.")
    cap.risk = "read"
    cap.source = "stock"                                # ships WITH Otto → grouped under "Stock"
    cap.route_hidden = True
    # Deliberately NOT `general = True`: that flag force-keeps a cap in the router shortlist,
    # which is the exact opposite of what this one needs.
    cap.prompt = (
        "You are the user's thinking partner on a half-formed idea. Your job is to make their "
        "thinking better, not to produce a deliverable.\n"
        "Engage with the actual substance: the strongest version of their idea, the thing it "
        "breaks, the option they haven't considered, the assumption it rests on. Say which way "
        "you'd go and why. If you think they're wrong, or solving the wrong problem, say so "
        "directly — agreement they didn't earn is worth nothing to them.\n"
        "Reference knowledge, learned facts, and directives the user has loaded are provided in "
        "your system prompt. Ground yourself in them for anything STABLE — how something works, "
        "a past decision, their preferences. They are NOT authoritative about the CURRENT state "
        "of any system: they are dated. Where a fact about what is deployed, enabled, running or "
        "reachable right now would actually settle the question, check it with your read-only "
        "tools and let the tool result win. Where it wouldn't, don't go looking — a long "
        "investigation is not what was asked for.\n"
        "Do NOT create, modify, deploy, or otherwise change anything — this turn is read-only. "
        "Do NOT write an implementation plan, a phased breakdown, or acceptance criteria unless "
        "the user asks for one.\n\n"
        "Request: {request}")
    return cap
