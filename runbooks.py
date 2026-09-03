"""Runbooks — a named, parameterized unit of work you run on demand (and optionally on a cron).

A runbook is deliberately a SUPERSET of a schedule, not a sibling of one, because the three
things people mean by "runbook" are the same object with different fields filled in:

  * `steps: []`                  -> a saved request + pinned cap. Exactly today's schedule, minus
                                    the cron. Runs through the normal single-turn verify ladder.
  * `steps: []` + `doc`          -> the same, plus durable prose (preconditions, rollback,
                                    escalation) handed to the executor as an APPROVED PLAN.
  * `steps: [...]`               -> an ordered dependency graph, each node optionally naming its
                                    own capability, executed by engine.run_plan.

The graph is not new machinery: `engine.run_plan` has always been a DAG executor (toposort +
dependency waves + per-step verify ladder). The only thing that ever authored its node list was
`engine.plan_steps`, an LLM call. A runbook is a human authoring that same node list — which is
why `run_plan(replan=False)` exists: an LLM may rewrite a plan an LLM wrote, never one a person
wrote (see engine.run_plan's docstring).

`doc` maps onto `approved_plan`, the existing channel that carries an approved plan into BOTH
execution and the verify judge (engine._approved_plan_note / verify(approved_plan=)). So a
human-written runbook is judged against its own prose, and skips the plan-preview gate — the
human already wrote and approved the plan.

Params are `{{name}}` placeholders substituted into the request, every step, and the doc.
**A cron and a required param with no default are mutually exclusive** (`validate`): a cron fire
has nobody present to answer the prompt, and silently substituting an empty string into
"decommission {{env}}" is exactly the class of accident this whole repo gates against.

The store is `data/runbooks.json` (display + definition). Firing on a cron is still Temporal's
job — see scheduler.py.
"""
import os
import re
import uuid

import config
import engine
import storage

ID_PREFIX = "rb-"
_STORE = None            # resolved lazily so tests can repoint config.DATA_DIR

MAX_STEPS = 25           # a runbook longer than this is a program, not a runbook
MAX_PARAMS = 10

_PARAM_RE = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*\}\}")
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


def store_path():
    """Resolved on first use, not at import: tests repoint config.DATA_DIR, and the whole
    stale-DATA_DIR failure mode in CLAUDE.md comes from freezing this too early."""
    global _STORE
    if _STORE is None:
        _STORE = os.path.join(config.DATA_DIR, "runbooks.json")
    return _STORE


# --- params ----------------------------------------------------------------

def placeholders(*texts):
    """Every distinct `{{name}}` referenced across the given texts, in first-seen order."""
    seen, out = set(), []
    for t in texts:
        for m in _PARAM_RE.finditer(str(t or "")):
            if m.group(1) not in seen:
                seen.add(m.group(1))
                out.append(m.group(1))
    return out


def interpolate(text, values):
    """Substitute `{{name}}` from `values`. An UNKNOWN placeholder is left verbatim rather than
    blanked: a runbook that renders "decommission {{env}}" reads as obviously broken and fails
    loudly, where "decommission " reads as a valid instruction to decommission everything."""
    if not text:
        return text
    return _PARAM_RE.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), str(text))


def resolve_values(rb, supplied=None):
    """Fill each declared param from `supplied`, falling back to its default. Returns the value
    map. Raises ValueError naming every missing required param (all of them, not just the first —
    a form should highlight every empty field in one pass) or any value outside `choices`."""
    supplied = supplied or {}
    values, missing, bad = {}, [], []
    for p in rb.get("params") or []:
        name = p["name"]
        raw = supplied.get(name)
        raw = p.get("default") if raw is None or str(raw).strip() == "" else raw
        if raw is None or str(raw).strip() == "":
            if p.get("required"):
                missing.append(name)
            continue
        val = str(raw).strip()
        choices = p.get("choices") or []
        if choices and val not in choices:
            bad.append(f"{name}={val!r} (allowed: {', '.join(choices)})")
        values[name] = val
    if missing:
        raise ValueError("missing required parameter(s): " + ", ".join(missing))
    if bad:
        raise ValueError("parameter(s) out of range: " + "; ".join(bad))
    return values


def render(rb, supplied=None):
    """Apply parameters, returning {request, doc, steps, cap, values} ready to hand to a run.
    Steps come back in dependency order (engine._toposort, via normalize at save time)."""
    values = resolve_values(rb, supplied)
    steps = [dict(s, goal=interpolate(s["goal"], values),
                  context=interpolate(s.get("context", ""), values),
                  done_when=interpolate(s.get("done_when", ""), values))
             for s in rb.get("steps") or []]
    return {"request": interpolate(rb.get("request") or rb.get("name") or "", values),
            "doc": interpolate(rb.get("doc") or "", values),
            "steps": steps, "cap": rb.get("cap"), "values": values}


# --- validation ------------------------------------------------------------

def _norm_params(raw):
    out, seen = [], set()
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        if not _NAME_RE.match(name):
            raise ValueError(f"parameter name {name!r} must start with a letter and contain only "
                             f"letters, digits, '-' or '_' (it becomes a {{{{{name}}}}} placeholder)")
        if name in seen:
            raise ValueError(f"duplicate parameter {name!r}")
        seen.add(name)
        choices = [str(c).strip() for c in (p.get("choices") or []) if str(c).strip()]
        default = p.get("default")
        default = str(default).strip() if default is not None else ""
        if choices and default and default not in choices:
            raise ValueError(f"parameter {name!r} default {default!r} is not one of its choices")
        out.append({"name": name, "label": str(p.get("label") or "").strip() or name,
                    "default": default, "choices": choices,
                    "required": bool(p.get("required", True))})
    if len(out) > MAX_PARAMS:
        raise ValueError(f"at most {MAX_PARAMS} parameters")
    return out


def _norm_steps(raw):
    """Normalize authored steps into engine's exact step schema, then toposort. Raises on a
    cycle, a duplicate id, or a `needs` pointing at a step that doesn't exist — all three are
    author mistakes worth refusing at save time, unlike engine._parse_steps' LLM input where a
    bad step is dropped leniently."""
    steps, ids = [], set()
    for i, s in enumerate(raw or []):
        if not isinstance(s, dict):
            continue
        goal = str(s.get("goal") or "").strip()
        if not goal:
            raise ValueError(f"step {i + 1} has no goal")
        sid = str(s.get("id") or "").strip() or f"s{i + 1}"
        if sid in ids:
            raise ValueError(f"duplicate step id {sid!r}")
        ids.add(sid)
        steps.append({
            "id": sid,
            "cap": str(s.get("cap") or "").strip(),      # "" = run on the runbook's own cap
            "goal": goal,
            "context": str(s.get("context") or "").strip(),
            "needs": [str(n).strip() for n in (s.get("needs") or []) if str(n).strip()],
            "produces": str(s.get("produces") or "").strip() or sid,
            "done_when": str(s.get("done_when") or "").strip(),
        })
    if len(steps) > MAX_STEPS:
        raise ValueError(f"at most {MAX_STEPS} steps")
    for s in steps:
        for n in s["needs"]:
            if n not in ids:
                raise ValueError(f"step {s['id']!r} needs {n!r}, which is not a step in this runbook")
            if n == s["id"]:
                raise ValueError(f"step {s['id']!r} needs itself")
    ordered = engine._toposort(steps)
    if steps and not ordered:
        raise ValueError("these steps have a circular dependency — one cannot run before another "
                         "that is itself waiting on it")
    return ordered


def normalize(rb):
    """Validate + normalize a runbook definition. Raises ValueError with a message meant to be
    shown to the author verbatim."""
    name = str(rb.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    request = str(rb.get("request") or "").strip()
    steps = _norm_steps(rb.get("steps"))
    if not request and not steps:
        raise ValueError("a runbook needs either a request or at least one step")
    params = _norm_params(rb.get("params"))
    doc = str(rb.get("doc") or "").strip()
    cron = str(rb.get("cron") or "").strip()

    declared = {p["name"] for p in params}
    used = placeholders(request, doc, *[s["goal"] for s in steps],
                        *[s.get("context", "") for s in steps],
                        *[s.get("done_when", "") for s in steps])
    undeclared = [p for p in used if p not in declared]
    if undeclared:
        raise ValueError("used but not declared as parameter(s): " + ", ".join(undeclared))

    if cron:
        if not cron_valid(cron):
            raise ValueError("cron must have 5 fields, e.g. 0 9 * * *")
        # A cron fire is unattended — there is nobody to answer a prompt. Requiring a default is
        # the difference between a scheduled runbook and a silent empty substitution.
        blocking = [p["name"] for p in params if p["required"] and not p["default"]]
        if blocking:
            raise ValueError(
                "a scheduled runbook cannot have a required parameter with no default — nobody is "
                "present when a cron fires to supply " + ", ".join(blocking) +
                ". Give it a default, make it optional, or remove the cron.")
    return {"name": name, "request": request, "cap": str(rb.get("cap") or "").strip(),
            "cron": cron, "auto_approve": bool(rb.get("auto_approve", False)),
            "params": params, "doc": doc, "steps": steps}


def cron_valid(expr):
    return len((expr or "").split()) == 5


# --- capability resolution -------------------------------------------------

_CAPS = None


def _caps():
    global _CAPS
    if _CAPS is None:
        import policy
        import registry
        _CAPS = registry.load()
        registry.apply_policy(_CAPS, policy.load())
    return _CAPS


def refresh_caps():
    """Drop the cached catalogue (after a policy/registry change)."""
    global _CAPS
    _CAPS = None


def resolve_cap(name):
    """A stored capability NAME -> the trusted `{name, kind, risk}` dict the workflow pins on, or
    None if unknown. A runbook stores only the name on purpose: risk must come from the registry
    at fire time, never from whoever saved the runbook, and a cap whose risk is later reclassified
    write must gate the next run rather than keep firing under a frozen `read` from months ago."""
    name = (name or "").strip()
    if not name:
        return None
    c = next((c for c in _caps() if c.name == name), None)
    if c is None and ":" in name:                 # tolerate a "kind:name" form
        bare = name.split(":", 1)[1]
        c = next((c for c in _caps() if c.name == bare), None)
    return {"name": c.name, "kind": c.kind, "risk": c.risk} if c else None


# --- store -----------------------------------------------------------------

def load():
    return storage.read_json(store_path(), {})


def get(rid):
    return load().get(rid)


def add(rb):
    rid = ID_PREFIX + uuid.uuid4().hex[:8]
    clean = normalize(rb)
    storage.mutate_json(store_path(), lambda s: {**s, rid: clean}, default={})
    return rid, clean


def update(rid, rb):
    clean = normalize(rb)

    def _apply(s):
        if rid not in s:
            raise KeyError(rid)
        return {**s, rid: clean}
    storage.mutate_json(store_path(), _apply, default={})
    return clean


def remove(rid):
    storage.mutate_json(store_path(), lambda s: {k: v for k, v in s.items() if k != rid},
                        default={})


def migrate_schedules(schedules):
    """One-time import of legacy `data/schedules.json` entries as one-request runbooks, keyed by
    their ORIGINAL schedule id so the Temporal schedule that already exists keeps pointing at the
    same definition. Idempotent: an id already in the runbook store is left alone, so this is safe
    to call on every startup. Returns the number imported."""
    if not schedules:
        return 0

    def _apply(s):
        added = {}
        for sid, meta in schedules.items():
            if sid in s:
                continue
            try:
                added[sid] = normalize({
                    "name": (meta.get("request") or "")[:80] or sid,
                    "request": meta.get("request") or "",
                    "cap": (meta.get("cap") or {}).get("name") or "",
                    "cron": meta.get("cron") or "",
                    "auto_approve": meta.get("auto_approve", False),
                })
            except ValueError:
                continue          # an unmigratable legacy row is skipped, never fatal at startup
        return {**s, **added} if added else storage.UNCHANGED

    before = len(load())
    return len(storage.mutate_json(store_path(), _apply, default={})) - before
