"""LAYER 4 - Model gateway (Router #2).

Otto makes small LLM calls of its own - routing and clarification. Those are the
"simpler tasks" that can run on a cheap or self-hosted model, while capability
EXECUTION runs through Claude Code (`claude -p`). This gateway maps each task tier
to a model from a configurable pool: cloud Claude, or a local OpenAI-compatible
endpoint (vLLM / Ollama / LM Studio). Config persists to data/models.json and is
edited from the Admin tab. If a local model fails, we fall back to Claude so Otto
keeps working.

The cloud pool is auto-discovered from the Anthropic API when ANTHROPIC_API_KEY is
set; otherwise it falls back to a known current list. Either way the pool is fully
user-editable, so nothing is hard-locked.
"""
import concurrent.futures
import copy
import json
import os
import re
import time
import urllib.parse
import urllib.request

import claude_cli
import config
import storage
from ui import trace

_PATH = os.path.join(config.DATA_DIR, "models.json")
_STATS_PATH = os.path.join(config.DATA_DIR, "gateway-stats.json")
# "plan" is the swarm planner (engine.decompose) — a simple, local-capable tier like routing,
# but separate so the fan-out decision can use a stronger model than Router #1 if wanted.
# "preview" is the PLAN-FIRST APPROVAL PREVIEW, which is a different thing entirely and used to
# have no tier at all: it took the EXECUTION model, so an operator who set "plan" to Opus got a
# stronger swarm decomposer and a preview that had never read that setting. Worse, an execution
# tier on a LOCAL model cannot run `claude -p --permission-mode plan`, so it fell through to
# `_default_claude` — deliberately the CHEAPEST tier — and the plan a human approves was written
# by Haiku, invisibly. The preview is the one phase with no ladder above it: it runs once and its
# output is what the human reads, so cheap-first is exactly wrong there.
# "supervise" is the shadow-mode run supervisor (issue #143) — mid-attempt checkpoints over
# the live execution stream; local-capable like verify (load() backfills it into old configs).
# "memory_gc" is the on-demand memory garbage collector (`memory.gc_preview`), which is TWO
# model calls, neither of which used to be a tier: the batched KEEP/STALE/VERIFY classifier rode
# "verify" (so downshifting the JUDGE silently downshifted what decides which memories die), and
# the live tool-verification turn was hardcoded to `_default_claude` — the same invisible-default
# mistake the "preview" note above describes, on the one pass that DELETES the operator's memory.
# One tier covers both: the classifier is local-capable, the live check needs Claude for tools and
# degrades to `_default_claude` (see `memory_gc_model_id`).
TASKS = ["routing", "plan", "preview", "clarify", "memory", "verify",
         "supervise", "memory_gc", "execution"]

# Fallback list used only when the API can't be queried (no key).
_KNOWN_CLAUDE = [
    ("claude-opus",   "claude-opus-4-8"),
    ("claude-sonnet", "claude-sonnet-5"),
    ("claude-haiku",  "claude-haiku-4-5-20251001"),
]

_LAST = {}   # task -> {"model": name, "fell_back": bool}  (what actually ran)
# Local models marked down after a failure: name -> epoch until which they're skipped
# (per-process; the persisted copy in gateway-stats.json feeds the UI, issue #90).
_local_down_until = {}


class LocalFallbackDisabled(RuntimeError):
    """Raised INSTEAD of substituting Claude when a local call fails under strict mode
    (config.LOCAL_FALLBACK false — OTTO_LOCAL_FALLBACK=0). Carries the loud, self-explaining
    body in `.message` so whatever surfaces the failure (attempt result, activity error, HTTP
    error) shows the same actionable text. Never raised in the default mode, and never for the
    exempt `verify` tier."""

    def __init__(self, model, what, task=None):
        self.model, self.what, self.task = model, what, task
        self.message = config.strict_stop_message(model, what, task=task)
        # The SHORT str() is what a Temporal ActivityError / HTTP 500 shows, so it has to name the
        # flag too — `.message` (the full body) doesn't survive those layers.
        super().__init__(f"OTTO_LOCAL_FALLBACK=0 stopped this run: {task or 'execution'} on "
                         f"local model '{model}' — {what}")


def _strict_stop(task, model, what):
    """Record + raise a strict-mode stop. Traced under its own STRICT tag and counted separately
    from fallbacks (a stop is the opposite of a fallback — nothing continued on Claude)."""
    trace("STRICT", f"{task or 'execution'}: {model} failed ({what}) and OTTO_LOCAL_FALLBACK=0 "
                    f"— stopping instead of falling back to Claude")
    _LAST[task or "execution"] = {"model": model + " ⛔ (strict stop)", "fell_back": False,
                                  "strict_stop": True}
    _bump(task or "execution", fell_back=False, strict=True)
    raise LocalFallbackDisabled(model, what, task=task)


def _discover_claude():
    """Query the Anthropic models API if a key is present; else the known list."""
    key = config.secret("ANTHROPIC_API_KEY")
    if key:
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            models = [{"name": m["id"], "provider": "claude", "model": m["id"]}
                      for m in data.get("data", [])]
            if models:
                return models
        except Exception:  # noqa: BLE001
            pass
    return [{"name": n, "provider": "claude", "model": mid} for n, mid in _KNOWN_CLAUDE]


def _default_cfg():
    pool = _discover_claude()
    default = next((m["name"] for m in pool if "sonnet" in m["model"]), pool[0]["name"])
    return {"pool": pool, "assign": {t: default for t in TASKS}}


# --- endpoints --------------------------------------------------------------------------
# One OpenAI-compatible server usually serves MANY models, but every pool entry used to carry
# its own base_url + api_key_env — so adding a second model from the same server meant pasting
# the URL and the key again, and rotating the key meant editing every entry. An endpoint holds
# those fields ONCE and a pool entry references it by name (`endpoint`).
#
# Consumers (gateway._chat, embed, test_model, local_runtime, doctor) all read base_url /
# api_key_env off the pool ENTRY, so load() HYDRATES those fields from the referenced endpoint
# and save() strips them back out. That keeps data/models.json normalized — editing the
# endpoint moves every model on it — without touching a single reader.
_EP_FIELDS = ("base_url", "api_key_env", "headers")


def _norm_headers(h):
    """An endpoint's optional headers as a clean {name: value} dict. Names/values carrying a
    newline are DROPPED, not escaped — a header split is a request-smuggling seam, and the
    value can come from a profile import."""
    out = {}
    for k, v in (h or {}).items():
        k, v = str(k).strip(), str("" if v is None else v).strip()
        if k and "\n" not in k + v and "\r" not in k + v:
            out[k] = v
    return out


def _ep_key(m):
    return ((m.get("base_url") or "").rstrip("/"), m.get("api_key_env") or "")


def _ep_name(base_url, taken):
    """A stable, human-readable name for an endpoint adopted from a legacy entry: its host."""
    base = urllib.parse.urlsplit(base_url).netloc or (base_url or "endpoint")
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


def _hydrate(cfg):
    """Fill each local pool entry's connection fields from the endpoint it references, and
    ADOPT legacy per-entry connections into the shared list (identical base_url + key join the
    same endpoint). The endpoint is authoritative for base_url/api_key_env — that is the point
    of configuring it once — including its optional extra headers, which a server behind a
    proxy demands of every model on it. An endpoint holds CONNECTION facts only — `max_turns` stays on the
    model, since a budget of turns measures the model's competence and one server happily serves
    a 4B and a frontier one. An entry whose endpoint no longer exists keeps its own fields if it
    still has them, so a profile import or a hand-edited file self-heals instead of breaking."""
    eps = [dict(e) for e in (cfg.get("endpoints") or []) if e.get("name")]
    by_name = {e["name"]: e for e in eps}
    by_key = {_ep_key(e): e for e in eps}
    for m in cfg.get("pool", []):
        if m.get("provider") == "claude":
            continue
        ep = by_name.get(m.get("endpoint"))
        if ep is None and m.get("base_url"):
            ep = by_key.get(_ep_key(m))
            if ep is None:
                ep = {"name": _ep_name(m["base_url"], by_name), "base_url": m["base_url"],
                      "api_key_env": m.get("api_key_env", ""),
                      "headers": _norm_headers(m.get("headers"))}
                eps.append(ep)
                by_name[ep["name"]] = ep
                by_key[_ep_key(ep)] = ep
            m["endpoint"] = ep["name"]
        if ep is None:
            continue
        m["base_url"] = ep.get("base_url", "")
        m["api_key_env"] = ep.get("api_key_env", "")
        m["headers"] = _norm_headers(ep.get("headers"))
    cfg["endpoints"] = eps
    return cfg


def _dehydrate(cfg):
    """The inverse of _hydrate, applied on the way to disk: a hydrated copy of the endpoint's
    fields must never be persisted on the entry, or the next endpoint edit would leave stale
    copies behind and the models would keep dialling the old server."""
    cfg = copy.deepcopy(cfg)
    # Endpoints persist as their DEFINITION only — the read side decorates them with the models
    # riding on each (gateway.endpoints), and the Admin tab posts that decorated shape straight
    # back, so a derived `models` list would otherwise be written to disk and go stale.
    cfg["endpoints"] = [{k: (_norm_headers(v) if k == "headers" else v)
                         for k, v in e.items() if k in ("name",) + _EP_FIELDS}
                        for e in (cfg.get("endpoints") or []) if e.get("name")]
    by_name = {e["name"]: e for e in cfg["endpoints"]}
    for m in cfg.get("pool", []):
        ep = by_name.get(m.get("endpoint"))
        if not ep:
            continue                       # legacy or dangling: the entry owns its own fields
        m.pop("base_url", None)
        m.pop("api_key_env", None)
        m.pop("headers", None)
    return cfg


def endpoints(cfg=None):
    """The configured endpoints, each with the pool entries riding on it."""
    cfg = cfg or load()
    used = {}
    for m in cfg.get("pool", []):
        if m.get("endpoint"):
            used.setdefault(m["endpoint"], []).append(m["name"])
    return [{**e, "models": used.get(e["name"], [])} for e in cfg.get("endpoints", [])]


def discover_models(base_url, api_key_env="", timeout=8, headers=None):
    """The MODELS an OpenAI-compatible server serves (GET /models), one entry per model:
    `{id, aliases, context}`. The whole reason an endpoint is configured once — adding its
    second model is a pick from this list, not a re-paste of the URL and key.

    Grouped by `root`, NOT one row per id: vLLM lists a served alias and its canonical repo
    path as separate entries sharing a root (`gemma-e4b` + `leon-se/gemma-4-E4B-it-FP8-Dynamic`),
    so a raw id list showed one 4-model server as 8 near-identical models. Both ids answer, and
    the short served name is the one a human recognizes, so it becomes `id` and the rest ride
    along as `aliases` (the caller needs them to tell that an already-added model IS this one).
    Raises on an unreachable/refusing server — the caller turns that into operator-facing text."""
    hdrs = request_headers({"api_key_env": api_key_env, "headers": headers}, content_json=False)
    req = urllib.request.Request((base_url or "").rstrip("/") + "/models", headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    groups = {}
    for x in (data.get("data") or []):
        mid = str(x.get("id") or "")
        if not mid:
            continue
        g = groups.setdefault(str(x.get("root") or mid), {"ids": set(), "context": None})
        g["ids"].add(mid)
        g["context"] = g["context"] or x.get("max_model_len")
    out = []
    for root, g in groups.items():
        # The served name is any id that ISN'T the canonical path; shortest wins if a server
        # registers several. With no alias at all the canonical path is the only thing to call.
        served = sorted(g["ids"] - {root}, key=lambda s: (len(s), s))
        primary = served[0] if served else root
        out.append({"id": primary, "aliases": sorted(g["ids"] - {primary}),
                    "context": g["context"]})
    return sorted(out, key=lambda e: e["id"])


def _normalize(cfg):
    """Migrations + backfills over a RAW stored config. Split out of `load()` so a
    read-modify-write can apply the same view inside storage's lock (see `_mutate`)."""
    if cfg is None:
        cfg = _default_cfg()
    # Migration: drop the discontinued 'fable' model from older saved pools. It isn't a
    # real available model, and Claude-tier entries can't be removed from the Admin UI, so
    # users would otherwise be stuck with it.
    pool = cfg.get("pool") or _default_cfg()["pool"]
    removed = {m["name"] for m in pool if "fable" in (m.get("model", "") + m.get("name", ""))}
    pool = [m for m in pool if m["name"] not in removed]
    # Migration: refresh Claude-tier model ids from _KNOWN_CLAUDE (the source of truth). A
    # saved pool pins the id it was created with (e.g. "claude-sonnet" -> "claude-sonnet-4-6"),
    # so a model bump in the fallback list wouldn't otherwise reach an existing install — the
    # entry keeps invoking the stale id. Assignments reference the entry by name, so repointing
    # the id in place is safe and propagates every future bump automatically.
    _known = {n: mid for n, mid in _KNOWN_CLAUDE}
    for m in pool:
        if m.get("name") in _known:
            m["model"] = _known[m["name"]]
    cfg["pool"] = pool
    # Backfill any task tier missing from an older saved config (e.g. "memory"), and repoint
    # any assignment/override that referenced a removed model at the default.
    default = next((m["name"] for m in pool if "sonnet" in m["model"]), pool[0]["name"])
    assign = cfg.setdefault("assign", {})
    for t in TASKS:
        if assign.get(t) in removed:
            assign[t] = default
        assign.setdefault(t, default)
    # A LOCAL model on "preview" is not a setting, it is a silent substitution: `claude -p
    # --permission-mode plan` cannot run on one, so `preview_model_id` degrades to
    # `_default_claude` and the store, the API and the Admin radio all keep naming a model that
    # never wrote a single plan (user-observed: preview pinned to qwen, every plan written by
    # sonnet). The Admin UI refuses the pick, but the store is the authority and an older config
    # already holds one — so repoint it here rather than leave the assignment lying. No other
    # tier gets this: every one of them either runs local for real or falls back per call.
    if _claude_model(assign.get("preview"), cfg) is None:
        assign["preview"] = next((m["name"] for m in pool
                                  if m.get("provider") == "claude" and "sonnet" in m["model"]),
                                 next((m["name"] for m in pool if m.get("provider") == "claude"),
                                      assign["preview"]))
    # Per-capability execution overrides (capability name -> pool model name). Empty by
    # default; falls back to the phase-level execution model below.
    cap_exec = cfg.setdefault("cap_exec", {})
    for cap in [c for c, mdl in cap_exec.items() if mdl in removed]:
        cap_exec.pop(cap)
    # Per-capability LOCAL execution overrides (issue #42): a deliberately separate map from
    # cap_exec (Claude-only) so the narrow tool-free carve-out never weakens that seam.
    cap_local = cfg.setdefault("cap_local_exec", {})
    pool_names = {m["name"] for m in pool}
    for cap in [c for c, mdl in cap_local.items() if mdl in removed or mdl not in pool_names]:
        cap_local.pop(cap)
    return _hydrate(cfg)


def load():
    return _normalize(storage.read_json(_PATH, None))


def save(cfg):
    storage.write_json(_PATH, _dehydrate(cfg))


def _mutate(fn):
    """Serialized read-modify-write of models.json: `fn(cfg)` sees the same hydrated,
    migrated view `load()` returns, INSIDE storage's lock. An unlocked load/modify/save
    pair loses concurrent writes (server.py is threaded) and can persist a torn read as
    the DEFAULT config, silently dropping every endpoint and assignment."""
    out = {}
    def _apply(raw):
        cfg = _normalize(raw)
        fn(cfg)
        out["cfg"] = cfg
        return _dehydrate(cfg)
    storage.mutate_json(_PATH, _apply, default=None)
    return out["cfg"]


def _model_for(task, cfg=None):
    cfg = cfg or load()
    pool = cfg.get("pool") or _default_cfg()["pool"]
    name = cfg.get("assign", {}).get(task)
    return next((m for m in pool if m["name"] == name), None) or pool[0]


def _default_claude(cfg=None):
    """The Claude model a FALLBACK lands on: SONNET, then opus, then haiku only if neither is in
    the pool. Volume paths — local-model failures on the cheap tiers, the execution re-dispatch
    when a local server can't do tool calls, and the approval preview.

    Two mistakes bracket this choice. 'First pool entry' meant OPUS by default (user-observed:
    execution set to gemma, every run silently billed as opus). Correcting that to CHEAPEST
    overshot: haiku became the silent answer everywhere a local model couldn't serve, including
    the plan preview — the one phase with no verify ladder above it to escalate a weak result,
    where the output is what a human reads and approves. Sonnet is the tier that is neither
    surprise-expensive nor quietly too weak, and an operator who wants either end can assign it
    explicitly per phase."""
    cfg = cfg or load()
    claude = [m for m in cfg.get("pool", []) if m.get("provider") == "claude"]
    for tier in ("sonnet", "opus", "haiku"):
        m = next((m for m in claude if tier in m["model"]), None)
        if m:
            return m["model"]
    return claude[0]["model"] if claude else config.ROUTER_MODEL


def _claude_model(name, cfg):
    """Pool entry for `name`, but only if it's a Claude model (execution must drive
    `claude -p`). Returns None otherwise."""
    m = next((m for m in cfg.get("pool", []) if m["name"] == name), None)
    return m if m and m.get("provider") == "claude" else None


def exec_model_id(cap_name=None):
    """CLAUDE model id for capability execution on the `claude -p` backend.

    With cap_name, a per-capability override (set in the Admin tab, persisted under
    cap_exec) wins — letting cheap capabilities run on a cheaper Claude tier while
    high-stakes ones stay on Opus. A LOCAL override/assignment is not an error, it just
    doesn't resolve here (this function feeds the Claude dispatch + the escalation/
    downshift fallbacks): it falls through to the phase model, then the default Claude.
    Backend CHOICE is exec_model_entry's job."""
    cfg = load()
    if cap_name:
        ovr = (cfg.get("cap_exec") or {}).get(cap_name)
        m = _claude_model(ovr, cfg) if ovr else None
        if m:
            return m["model"]
    m = _model_for("execution", cfg)
    return m["model"] if m.get("provider") == "claude" else _default_claude(cfg)


def preview_model_id(cfg=None):
    """CLAUDE model id for the plan-first approval preview (`plans.plan_preview`).

    An explicit tier, so "which model writes the plan I approve" is a setting rather than a
    consequence of the execution assignment. A LOCAL model here cannot serve plan mode at all,
    so it degrades to `_default_claude` (sonnet-first); see the TASKS note above."""
    cfg = cfg or load()
    m = _model_for("preview", cfg)
    return m["model"] if m.get("provider") == "claude" else _default_claude(cfg)


def memory_gc_model_id(cfg=None):
    """CLAUDE model id for the memory GC's LIVE verification turn (`memory._gc_verify_live`).

    That turn is a real `claude -p` pass with read-only tools, so a LOCAL assignment on this
    tier cannot serve it and degrades to `_default_claude` — same shape as `preview_model_id`.
    The tier's local-capable half is the batch classifier, which goes through `complete()`."""
    cfg = cfg or load()
    m = _model_for("memory_gc", cfg)
    return m["model"] if m.get("provider") == "claude" else _default_claude(cfg)


def exec_model_entry(cap_name=None, cfg=None):
    """The FULL pool entry resolved for capability execution — Claude or local. This is
    the backend dispatch source: provider "claude" → `claude -p` (claude_cli.run_json),
    anything else → the local agent runtime (local_runtime.run_json). Per-cap cap_exec
    override wins over the phase-level execution assignment."""
    cfg = cfg or load()
    if cap_name:
        ovr = (cfg.get("cap_exec") or {}).get(cap_name)
        m = next((m for m in cfg.get("pool", []) if m["name"] == ovr), None) if ovr else None
        if m:
            return m
    return _model_for("execution", cfg)


def resolve_model(name, cfg=None):
    """Pool entry for an arbitrary model name, or None if unknown. Used for a per-run
    execution override (the chat model picker) that bypasses cap_exec/phase assignment
    entirely for that one run — never persisted, never touches data/models.json."""
    if not name:
        return None
    cfg = cfg or load()
    return next((m for m in cfg.get("pool", []) if m["name"] == name), None)


# Retired pool entries whose rows are still in the audit trail. A pool entry is the operator's
# LABEL for a model; deleting the entry loses the mapping, so the id has to be recorded here or
# every historical row under that label stops joining. Add an entry when you delete a pool one.
# Nothing goes in here on a guess: an unresolvable label passes through unchanged (see model_id),
# which leaves the row honest instead of attributing it to a model that may not have served it.
_RETIRED_MODEL_IDS = {}


def model_id(name, cfg=None):
    """The CANONICAL model id for a pool-entry label — `claude-sonnet` -> `claude-sonnet-5`.

    A pool entry has two names: `name` (the operator's label, freely editable) and `model` (the
    id the server actually serves). The Claude paths recorded the id and every local path
    recorded the LABEL, so `model` and `verdict_model` in the audit trail held two namespaces for
    one model and `scorecard` could not join them: 43 of 58 attributed verdicts said
    `claude-sonnet` while every execution row for the same model said `claude-sonnet-5`, which is
    exactly the split `verdict_model` exists to make possible. This is the ONE resolver; `_audit`
    applies it so no writer can reintroduce the second namespace.

    Two labels can point at one id (the same weights on two endpoints) and they deliberately
    collapse here — for model QUALITY they are the same model, and WHICH endpoint served it is
    per-entry state that lives in `record_health`/`fallback_from`. A label matching one entry's
    `name` and another's `model` resolves by `name`, the namespace the ambiguity came from.

    Fails OPEN: an unknown label (a retired entry, a hand-edited row) returns unchanged rather
    than None, so normalizing can never erase what actually ran."""
    if not name or not isinstance(name, str):
        return name
    cfg = cfg or load()
    m = next((m for m in cfg.get("pool", []) if m.get("name") == name), None)
    if m and m.get("model"):
        return m["model"]
    return _RETIRED_MODEL_IDS.get(name, name)


def set_cap_exec(cap_name, model_name):
    """Set (or clear, when model_name is falsy) a capability's execution-model override.
    Accepts any pool model: a Claude pick runs through `claude -p`, a LOCAL pick runs the
    capability on the local agent runtime (no Claude at all). Returns the updated map."""
    def _apply(cfg):
        overrides = cfg.setdefault("cap_exec", {})
        known = {m["name"] for m in cfg.get("pool", [])}
        if model_name and model_name in known:
            overrides[cap_name] = model_name
        else:
            overrides.pop(cap_name, None)   # clear / "default", or an unknown model
    return _mutate(_apply).get("cap_exec", {})


def _local_model(name, cfg):
    """Pool entry for `name`, but only if it's a usable LOCAL model (OpenAI-compatible with a
    base_url). The mirror of _claude_model, for the tool-free execution carve-out (issue #42)."""
    m = next((m for m in cfg.get("pool", []) if m["name"] == name), None)
    return m if m and m.get("provider") != "claude" and m.get("base_url") else None


def set_cap_local_exec(cap_name, model_name):
    """Set (or clear) a capability's LOCAL execution model (issue #42). The inverse constraint
    of set_cap_exec: only local models are accepted — a Claude pick belongs in cap_exec. The
    override only ever takes effect for a tool-free read capability (engine.run_attempt guards
    eligibility); storing it for any cap is harmless. Returns the updated cap_local_exec map."""
    def _apply(cfg):
        overrides = cfg.setdefault("cap_local_exec", {})
        if model_name and _local_model(model_name, cfg):
            overrides[cap_name] = model_name
        else:
            overrides.pop(cap_name, None)   # clear / "default", or an invalid (non-local) pick
    return _mutate(_apply).get("cap_local_exec", {})


def local_exec_model(cap_name, cfg=None):
    """The LOCAL pool entry assigned as `cap_name`'s execution model, or None (no override,
    invalid entry, or the model is currently marked down after a failure — one timeout per
    LOCAL_SKIP_S, same degraded-mode memo as complete())."""
    cfg = cfg or load()
    name = (cfg.get("cap_local_exec") or {}).get(cap_name)
    m = _local_model(name, cfg) if name else None
    if m and _local_down_until.get(m["name"], 0) > time.time():
        if not config.local_fallback_allowed():
            _strict_stop(None, m["name"], "the model is marked down after an earlier failure "
                                          f"(skipped for {config.LOCAL_SKIP_S:.0f}s)")
        trace("GATEWAY", f"local exec: {m['name']} marked down; using Claude this attempt")
        return None
    return m


def local_execute(cap_name, prompt, system_context=None):
    """One EXECUTION-grade turn on `cap_name`'s assigned local model (issue #42): a real
    completion (not the 300-token _openai_complete cap), for tool-free capabilities only —
    there are no tools to drive, so plain text in/out is the whole job.

    Returns {result, tokens, model} on success, or None whenever the Claude path should run
    instead: no local override configured, the model is marked down, or the call failed
    (unavailability falls back to Claude, consistent with complete()). Raises only
    LocalFallbackDisabled — strict mode (OTTO_LOCAL_FALLBACK=0) turns the marked-down/failed
    cases into a hard stop instead of a silent Claude substitution; "no override configured" is
    never a strict stop (that cap is simply assigned to Claude)."""
    m = local_exec_model(cap_name)
    if not m:
        return None
    trace("GATEWAY", f"execution [{cap_name}] -> {m['name']} (local:{m.get('model', '')})")
    try:
        messages = ([{"role": "system", "content": system_context}] if system_context else [])
        messages.append({"role": "user", "content": prompt})
        data = _chat(m, messages, config.LOCAL_EXEC_MAX_TOKENS, config.LOCAL_EXEC_TIMEOUT_S)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = message_text(msg)
        thinking = text if looks_like_thinking(text) else ("" if text.strip() else reasoning_text(msg))
        if thinking.strip() and (not text.strip() or thinking is text):
            # Reasoning-only turn: nudge once for the bare answer (see _openai_complete) —
            # cheaper than burning a whole verify attempt on "(no output)".
            messages += [{"role": "assistant", "content": thinking[-2000:]},
                         {"role": "user", "content": _NUDGE}]
            data = _chat(m, messages, config.LOCAL_EXEC_MAX_TOKENS, config.LOCAL_EXEC_TIMEOUT_S)
            text = message_text((data.get("choices") or [{}])[0].get("message"))
            if looks_like_thinking(text):
                text = ""
        usage = data.get("usage") or {}
        _LAST["execution"] = {"model": m["name"], "fell_back": False}
        _bump("execution", fell_back=False, health=(m["name"], True))
        return {"result": text, "model": m["name"],
                "tokens": {"input": usage.get("prompt_tokens", 0) or 0,
                           "output": usage.get("completion_tokens", 0) or 0,
                           "cache_read": 0, "cache_write": 0}}
    except Exception as e:  # noqa: BLE001 - any failure -> the caller runs the Claude path
        down_until = time.time() + config.LOCAL_SKIP_S
        _local_down_until[m["name"]] = down_until
        bad = (m["name"], False, f"the tool-free local completion failed: {e}")
        if not config.local_fallback_allowed():
            _bump("execution", fell_back=False, down_model=m["name"], down_until=down_until,
                  health=bad)
            _strict_stop(None, m["name"], f"the tool-free local completion failed: {str(e)[:200]}")
        trace("GATEWAY", f"local exec {m['name']} failed ({e}); this attempt runs on Claude")
        _LAST["execution"] = {"model": m["name"] + " → claude (fallback)", "fell_back": True}
        _bump("execution", fell_back=True, down_model=m["name"], down_until=down_until, health=bad)
        return None


# Capability tiers, strongest first — the escalation ladder for the verify->retry loop.
_TIER_ORDER = ["opus", "sonnet", "haiku"]


def escalation_model_id(cfg=None):
    """The model to fall back to on the FINAL verify->retry attempt.

    The loop's first attempts use the configured execution model (which may be a
    cheap Claude tier — or, once per-capability local execution lands, a local one
    that already routes through Claude here). The last attempt escalates to the most
    capable Claude model in the pool so a struggling executor gets one strong shot
    before we give up. Falls back to the normal execution model if nothing stronger
    is available."""
    cfg = cfg or load()
    claude = [m for m in cfg.get("pool", []) if m.get("provider") == "claude"]
    for tier in _TIER_ORDER:
        m = next((m for m in claude if tier in m["model"]), None)
        if m:
            return m["model"]
    return exec_model_id()


def downshift_model_id(cfg=None):
    """The CHEAPEST Claude model in the pool — used when a run crosses its SOFT cost budget, so
    the remaining attempts finish on a cheaper tier. The mirror of escalation_model_id (strongest).
    Falls back to the normal execution model if no known tier is present."""
    cfg = cfg or load()
    claude = [m for m in cfg.get("pool", []) if m.get("provider") == "claude"]
    for tier in reversed(_TIER_ORDER):   # cheapest first
        m = next((m for m in claude if tier in m["model"]), None)
        if m:
            return m["model"]
    return exec_model_id()


def last(task):
    return _LAST.get(task)


def _bump(task, fell_back, down_model=None, down_until=None, strict=False, health=None):
    """Persist one gateway call outcome to the cross-process stats file (issue #90). The
    counters must survive process boundaries: most complete() calls run inside worker.py
    activities, while /api/health is served by server.py — an in-memory counter would
    under-report to the UI. Best-effort: stats must never fail a call.

    `health` is a (name, ok, detail) triple folded into the SAME mutation as the counters, so
    recording model health costs no extra lock/write per call."""
    def _mut(d):
        t = d.setdefault("tasks", {}).setdefault(task, {"calls": 0, "fallbacks": 0})
        t["calls"] += 1
        if fell_back:
            t["fallbacks"] += 1
        if strict:
            # Strict-mode stops are NOT fallbacks (nothing continued on Claude) — counted apart so
            # /api/health can show "local is failing and being allowed to fail" as its own signal.
            t["strict_stops"] = t.get("strict_stops", 0) + 1
        if down_model:
            d.setdefault("down", {})[down_model] = down_until
        if health:
            _set_health(d, *health)
        return d
    try:
        storage.mutate_json(_STATS_PATH, _mut, default={})
    except OSError:
        pass


def _bump_cost(task, cost):
    """Add one Claude-backed tier call's USD to the cross-process ledger, per task.

    Separate from `_bump` because the two do not fire together: `_bump` books the call once per
    `complete()`, while the cost is only known after the Claude turn returns and a fallback path
    pays it on top of a local call that already counted. Same store, same lock. LOCAL models book
    nothing — self-hosted inference has no per-call price, and inventing one would make the
    ledger disagree with the bill."""
    def _mut(d):
        t = d.setdefault("tasks", {}).setdefault(task, {"calls": 0, "fallbacks": 0})
        t["cost_usd"] = round((t.get("cost_usd") or 0) + cost, 6)
        return d
    try:
        storage.mutate_json(_STATS_PATH, _mut, default={})
    except OSError:
        pass


# --- model health -----------------------------------------------------------------------
# A model that is unreachable, mis-configured or rejecting calls used to be invisible: with
# Claude fallback on, every run still succeeds and the only trace is a fallback percentage
# nobody reads. Health is recorded from REAL outcomes wherever a call runs, and refreshed by
# an on-demand probe when the Admin view asks — the same errors-visible shape as MCP health
# (policy.mcp_health), so it can feed the same Admin-tab warning badge.
_HEALTH_TTL = 900       # a probe result older than this is stale; the Admin view re-probes it
_PROBE_TIMEOUT_S = 4    # socket budget for the page-load refresh (a /models list takes ms)


def _set_health(d, name, ok, detail="", via="run"):
    """Write one model's last-known outcome into the stats dict (caller owns the lock).

    Deliberately LAST-WRITE-WINS rather than sticky-with-decay: an unhealthy mark stays until
    something actually succeeds — a later call, a probe, the Recheck button — exactly like the
    MCP pills, and a success clears it with no separate reset path to forget. That also means a
    model nothing has called since it broke keeps warning, which is the point."""
    if not name:
        return d
    d.setdefault("health", {})[name] = {"ok": bool(ok), "detail": str(detail or "")[:300],
                                        "at": time.time(), "via": via}
    return d


def record_health(name, ok, detail="", via="run"):
    """Standalone health write for the callers that have no counter to bump — the local
    execution runtime (an unreachable/tool-rejecting server) and test_model. Best-effort."""
    if not name:
        return
    try:
        storage.mutate_json(_STATS_PATH, lambda d: _set_health(d, name, ok, detail, via),
                            default={})
    except OSError:
        pass


# --- the per-capability local latch ----------------------------------------------------
# `record_health` answers "can this MODEL serve anything"; this answers the different question
# "can this CAPABILITY do its job on this model". They are not the same and must not share a
# store: a local model that answers every call perfectly is healthy, and still cannot run a cap
# whose work needs tools it was never given.
#
# Why it has to persist ACROSS runs. Within one run the ladder already self-corrects — a write
# cap that fails verification on local re-dispatches the rest of the ladder to Claude (issue
# #172). But nothing remembered that, so the NEXT run paid the identical doomed first attempt.
# Measured over the trail 2026-07-06..2026-08-25: `github-pr-review` failed on qwen3.6 fifteen
# times across separate runs (7/22 local vs 13/13 on Claude), `sre-secretary` went 0-for-9 on
# DeepSeek, and `daily-summary` 0-for-3 — every one of them re-litigated from scratch.
#
# Keyed on (capability, model) because they are different questions: `sre-pm` passes 8/12 on
# qwen3.6 and 5/15 on Qwen 3.6 35b, so latching the cap outright would throw away a pairing
# that works. Threshold and TTL were calibrated against that trail rather than picked: at three
# CONSECUTIVE judged failures it latches every genuine loser while sparing `board-status` and
# `sre-pm`/qwen3.6, whose failures are interleaved with real passes.
#
# A latch is a CIRCUIT BREAKER, not a ban. It expires, and the next run gets one probationary
# local attempt: a pass clears it, a fail re-arms it immediately. Without that half-open state
# the same trail shows what it would cost — `github-pr-review` ends PPFPP, so a permanent latch
# would have refused four attempts that went on to pass.


def _latch_key(cap_name, model_name):
    return f"{cap_name}\u0000{model_name}"


def record_cap_local(cap_name, model_name, passed):
    """One JUDGED local attempt's outcome for this (capability, model). Best-effort.

    Judged verdicts ONLY — the same denominator `scorecard` uses. A harness death or a
    supervisor kill is not evidence about the pairing (nobody read the output), and counting one
    would latch a cap off local because the worker restarted."""
    if not cap_name or not model_name:
        return
    def _mut(d):
        latches = d.setdefault("cap_local", {})
        key = _latch_key(cap_name, model_name)
        e = latches.get(key) or {}
        if passed:
            # A pass clears everything, including a live latch — this is the probation path.
            latches.pop(key, None)
            return d
        fails = int(e.get("fails") or 0) + 1
        now = time.time()
        e.update({"fails": fails, "at": now})
        # Re-arm on the PROBATION failure too, not only the first time. Keying the TTL to the
        # original latch instead would make the breaker one-shot: it expires, the probationary
        # attempt fails, and because `latched_at` was already set the latch stays expired
        # forever — every later run paying the doomed attempt again, which is the exact bug
        # this store exists to end. A live latch is never extended, so a burst of failures
        # inside one window cannot push the re-test out.
        if (fails >= max(1, config.setting("cap_local_latch_fails"))
                and not _latched_at(e, now)):
            e["latched_at"] = now
        latches[key] = e
        return d
    try:
        storage.mutate_json(_STATS_PATH, _mut, default={})
    except OSError:
        pass


def _latched_at(entry, now):
    """Is this entry's latch still live? Shared by the reader and the writer so "latched" means
    one thing — the writer's re-arm test and the run path's refusal cannot drift apart."""
    at = entry.get("latched_at")
    if not at:
        return False
    ttl = config.setting("cap_local_latch_ttl_s")
    return True if ttl <= 0 else (now - at) < ttl


def cap_local_latched(cap_name, model_name, now=None):
    """Is this (capability, model) currently latched off the local backend?

    False once the TTL has expired — that expiry IS the probation window, and the run it lets
    through re-arms the latch on a fail because `fails` was never reset. `now` is injectable so
    the TTL can be tested without sleeping."""
    if not cap_name or not model_name:
        return False
    latches = storage.read_json(_STATS_PATH, {}).get("cap_local")
    e = (latches or {}).get(_latch_key(cap_name, model_name))
    if not isinstance(e, dict):
        return False
    return _latched_at(e, now if now is not None else time.time())


def cap_local_latches(now=None):
    """{(cap, model): {fails, latched_at, expired}} for the Admin surface. Read-only."""
    latches = storage.read_json(_STATS_PATH, {}).get("cap_local") or {}
    now = now if now is not None else time.time()
    out = {}
    for key, e in latches.items():
        if not isinstance(e, dict) or "\u0000" not in key:
            continue
        cap_name, model_name = key.split("\u0000", 1)
        if e.get("latched_at"):
            out[(cap_name, model_name)] = {
                "fails": e.get("fails"), "latched_at": e.get("latched_at"),
                "expired": not cap_local_latched(cap_name, model_name, now)}
    return out


def clear_cap_local(cap_name=None, model_name=None):
    """Forget a latch (Admin) — the escape hatch for "I fixed the cap / swapped the server, give
    local another chance NOW" rather than waiting out the TTL.

    Three scopes, because the caller rarely knows the model: no cap = forget everything; a cap
    with no model = forget that capability's latches on EVERY model (what the Admin button
    sends, since the latch is displayed per-cap); both = forget exactly that pairing."""
    def _mut(d):
        if cap_name is None:
            d.pop("cap_local", None)
            return d
        latches = d.get("cap_local") or {}
        if model_name:
            latches.pop(_latch_key(cap_name, model_name), None)
            return d
        prefix = cap_name + "\u0000"
        for key in [k for k in latches if k.startswith(prefix)]:
            latches.pop(key, None)
        return d
    try:
        storage.mutate_json(_STATS_PATH, _mut, default={})
    except OSError:
        pass


def model_health():
    """{name: {ok, detail, at, via}} — the last known outcome per pool entry, or {} if nothing
    has run or been probed yet. Read-only: never probes, so the run path and the 15s badge poll
    both stay free."""
    data = storage.read_json(_STATS_PATH, {})
    h = data.get("health")
    return h if isinstance(h, dict) else {}


def unhealthy_models(cfg=None):
    """Pool entries whose LAST outcome was a failure — the actionable signal behind the
    Admin-tab warning badge. Cache only (no probing). Scoped to the current pool so a model
    that was removed after it broke stops warning about a config that no longer exists."""
    cfg = cfg or load()
    h = model_health()
    out = []
    for m in cfg.get("pool", []):
        e = h.get(m["name"])
        if isinstance(e, dict) and not e.get("ok", True):
            # WHAT IT IS USED FOR is the actionable half. A phase assignment is the obvious one;
            # a per-capability pin is the one that bites hardest and is easiest to forget — a cap
            # with `cap_exec` set to a dead local endpoint runs every attempt against it (observed
            # 2026-08-04: sre-pm pinned to an unreachable local model).
            out.append({"name": m["name"], "provider": m.get("provider"),
                        "model": m.get("model", ""), "detail": e.get("detail") or "",
                        "at": e.get("at"), "via": e.get("via") or "run",
                        "phases": [t for t, n in (cfg.get("assign") or {}).items() if n == m["name"]],
                        "caps": sorted(c for c, n in {**(cfg.get("cap_exec") or {}),
                                                      **(cfg.get("cap_local_exec") or {})}.items()
                                       if n == m["name"])})
    return out


def probe_models(force=False, cfg=None):
    """Refresh model health for the Admin view; returns the full {name: entry} map.

    LOCAL entries are probed whenever their last result is stale — a GET /models is
    milliseconds and needs no inference. CLAUDE entries are probed ONLY on `force` (the
    Recheck button): each one is a real `claude -p` turn that costs tokens and seconds, so it
    must never ride on a page load. An un-probed Claude entry still reports health from its
    real calls, which is the signal that matters anyway.

    The un-forced refresh runs CONCURRENTLY on a shorter timeout because it blocks a page load
    and the cost is paid by the DEAD endpoints: measured on the real pool, one unreachable local
    model spent the full 8s socket timeout and served /api/models in 8.8s — serial probes would
    make that N×8s. A forced Recheck keeps the generous timeout (the user asked, and sees the
    button say so)."""
    cfg = cfg or load()
    now = time.time()
    health = model_health()
    due = []
    for m in cfg.get("pool", []):
        entry = health.get(m["name"]) or {}
        stale = (now - (entry.get("at") or 0)) > _HEALTH_TTL
        if m.get("provider") == "claude":
            if not force:
                continue
        elif not (force or stale):
            continue
        due.append(m["name"])
    if not due:
        return health
    timeout = None if force else _PROBE_TIMEOUT_S
    if force or len(due) == 1:
        for name in due:
            test_model(name, cfg=cfg, timeout=timeout)   # records health itself
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(due))) as pool:
            # test_model swallows its own failures into a verdict, so nothing here can raise.
            list(pool.map(lambda n: test_model(n, cfg=cfg, timeout=timeout), due))
    return model_health()


def stats():
    """Per-task call/fallback/cost counters plus any local models currently marked down (seconds
    remaining). Feeds /api/health, /api/stats and the Admin badge.

    `overhead_usd` is the total Otto spent JUDGING rather than executing — the figure the audit
    trail cannot hold, because an audit row exists only for an execution attempt."""
    data = storage.read_json(_STATS_PATH, {})
    now = time.time()
    down = {name: int(until - now)
            for name, until in (data.get("down") or {}).items() if until and until > now}
    tasks = data.get("tasks") or {}
    overhead = round(sum((t.get("cost_usd") or 0) for t in tasks.values()), 4)
    return {"tasks": tasks, "down": down, "overhead_usd": overhead}


def _claude_complete(prompt, model):
    """One cheap-tier `claude -p` turn with a BOUNDED timeout, raising on error. The 900s
    default is an execution-attempt budget; a routing/clarify call that stalls (observed: a
    haiku routing call sat ≥180s — likely a rate-limit wait — until Temporal killed the whole
    activity, run web-d8853d79) must die fast enough for complete()'s in-process fallback to
    get its shot INSIDE the activity's 180s window. Raising (instead of returning the error
    dict) also keeps the literal "(timed out)" string out of the tiers' parsers, where it
    would read as a clarifying question / verify critique."""
    # A cheap tier is a pure text judgement — it never calls a tool — but it was still paying
    # the full preamble (every built-in tool, 26 MCP servers, 75 skills, 16 agents, plus the
    # worker cwd's CLAUDE.md) on every call, and there are ~10 of these per run. Stripping all
    # three takes one call from 45.4k to 9.3k tokens. Safe here precisely BECAUSE the call is
    # tool-free: nothing it could invoke has been taken away.
    out = claude_cli.run_json(prompt, model=model, timeout=config.CLAUDE_TIER_TIMEOUT_S,
                              disallowed_tools=config.ALL_BUILTIN_TOOLS,
                              setting_sources="", strict_mcp=True)
    if out.get("is_error"):
        raise RuntimeError(f"claude tier call failed: {str(out.get('result'))[:120]}")
    # (text, cost). Returning the cost is the whole point: every judge-side call — verify,
    # supervise, routing, clarify, the plan critique, memory extraction — reported nothing, so
    # the audit total and the scorecard's average were EXECUTION-only and a run that spent
    # three judged rungs looked exactly as cheap as one that passed first time.
    return (out.get("result", "") or ""), (out.get("total_cost_usd", 0) or 0)


def _claude_tier(task, prompt, model):
    """`_claude_complete` with its cost booked against `task`. Every Claude-backed tier call
    goes through here — a bare `_claude_complete` is spend that never reaches the ledger."""
    text, cost = _claude_complete(prompt, model)
    if cost:
        _bump_cost(task, cost)
    return text


def complete(task, prompt):
    """Run a SIMPLE task (routing/clarify) on its assigned model; return text."""
    m = _model_for(task)
    # Degraded-mode memo: a local model that just failed is skipped for LOCAL_SKIP_S —
    # straight to the Claude fallback — so a dead endpoint costs one timeout, not one per call.
    if m.get("provider") != "claude" and _local_down_until.get(m["name"], 0) > time.time():
        if not config.local_fallback_allowed(task):
            _strict_stop(task, m["name"], "the model is marked down after an earlier failure "
                                          f"(skipped for {config.LOCAL_SKIP_S:.0f}s)")
        trace("GATEWAY", f"{task}: {m['name']} marked down; going straight to Claude")
        _LAST[task] = {"model": m["name"] + " → claude (down, skipped)", "fell_back": True}
        _bump(task, fell_back=True)
        return _claude_tier(task, prompt, _default_claude())
    trace("GATEWAY", f"{task} -> {m['name']} ({m.get('provider')}:{m.get('model','')})")
    try:
        if m.get("provider") == "claude":
            text = _claude_tier(task, prompt, m["model"])
        else:
            text = _openai_complete(m, prompt)
            if not text.strip():
                # The local model produced nothing usable (reasoning-only even after the
                # nudge). An empty reply is NOT an answer: the write-intent classifier
                # defaults it to WRITE (observed: read caps gating on every run), clarify
                # treats it as "clear", verify as FAIL. Fall back to Claude for a REAL
                # verdict — but don't mark the model down: the endpoint is healthy, the
                # answer just flopped, and a down-mark would exile it for LOCAL_SKIP_S.
                if not config.local_fallback_allowed(task):
                    _strict_stop(task, m["name"], "the model returned no usable answer "
                                                  "(reasoning-only even after the nudge)")
                trace("GATEWAY", f"{m['name']} gave no usable answer; falling back to Claude")
                _LAST[task] = {"model": m["name"] + " → claude (empty reply)", "fell_back": True}
                # Health, like the down-mark, stays CLEAN here: the endpoint answered, so it is
                # reachable and correctly configured — flagging it would point the operator at
                # an infrastructure problem that isn't there.
                _bump(task, fell_back=True, health=(m["name"], True))
                return _claude_tier(task, prompt, _default_claude())
        _LAST[task] = {"model": m["name"], "fell_back": False}
        _bump(task, fell_back=False, health=(m["name"], True))
        return text or ""
    except LocalFallbackDisabled:
        # The empty-reply strict stop above is raised INSIDE this try — never let the generic
        # handler below turn it back into the Claude fallback it exists to prevent.
        raise
    except Exception as e:  # noqa: BLE001 - any failure -> graceful fallback to Claude
        down_model = down_until = None
        if m.get("provider") != "claude":
            down_until = time.time() + config.LOCAL_SKIP_S
            _local_down_until[m["name"]] = down_until
            down_model = m["name"]
        # Health is recorded for a failing CLAUDE tier too, unlike the down-mark: the mark is
        # about not re-hitting a dead endpoint (pointless for Claude, which is the fallback),
        # while health is about TELLING somebody. A tier that silently retries Claude-to-Claude
        # on every call is exactly the failure this badge exists to make visible.
        bad = (m["name"], False, f"the {task} call failed: {e}")
        if down_model and not config.local_fallback_allowed(task):
            # `down_model` is set only for a LOCAL entry — a failing CLAUDE tier still retries on
            # the default Claude below (strict mode is about not masking LOCAL failures, and has
            # no opinion on Claude-to-Claude recovery). Mark-down still applies (it's about not
            # re-hitting a dead endpoint), but nothing substitutes for the call.
            _bump(task, fell_back=False, down_model=down_model, down_until=down_until, health=bad)
            _strict_stop(task, m["name"], f"the call failed: {str(e)[:200]}")
        trace("GATEWAY", f"{m['name']} failed ({e}); falling back to Claude")
        _LAST[task] = {"model": m["name"] + " → claude (fallback)", "fell_back": True}
        _bump(task, fell_back=True, down_model=down_model, down_until=down_until, health=bad)
        return _claude_tier(task, prompt, _default_claude())


def plan_complete(prompt):
    """The STRONG planner call for plan-then-execute mode (engine.plan_steps). Pinned to the
    strongest Claude in the pool regardless of the 'plan' tier's configured model: that tier is
    local-eligible and drives the cheap swarm-decompose decision, but the whole premise of
    plan-then-execute is that a CAPABLE model writes the atomic-step plan a weak local executor
    then follows. Distinct from complete('plan', …) on purpose. Raises on Claude failure (the
    caller degrades to no-plan / single-turn execution)."""
    text = _claude_tier("plan_strong", prompt, escalation_model_id())
    _LAST["plan_strong"] = {"model": escalation_model_id(), "fell_back": False}
    return text or ""


def api_key(m):
    """The bearer key for a local model entry. `api_key_env` names an ENV VAR, but users
    paste the literal key into that field often enough (observed: a vllm_… key stored
    verbatim, silently 401-ing every call) that we accept both: env var value when the
    var exists, else the field's own value as the key.
      config.secret puts the OTTO_SECRET_COMMAND helper between those two, so an endpoint key
    can stay in the password manager instead of being pasted into data/models.json. It only
    consults the helper for something SHAPED like a var name, so a pasted literal is never
    handed to the helper's argv."""
    ref = m.get("api_key_env") or ""
    return config.secret(ref) or ref


def request_headers(m, content_json=True):
    """EVERY header a call to this model's endpoint must carry: JSON content type, the bearer
    key, and the endpoint's own optional headers. The ONE implementation — a call site that
    builds its own dict silently drops the extra headers, and a vLLM behind a proxy that
    requires them answers 401/404 on that path only.

    Values resolve exactly like `api_key` (env var > OTTO_SECRET_COMMAND > the literal), so a
    header carrying a credential stays out of data/models.json. The endpoint's headers are
    applied LAST and win: an operator who typed `Authorization: …` meant that one."""
    headers = {"Content-Type": "application/json"} if content_json else {}
    if m.get("api_key_env"):
        headers["Authorization"] = "Bearer " + api_key(m)
    for k, v in _norm_headers(m.get("headers")).items():
        headers[k] = config.secret(v) or v
    return headers


def _chat(m, messages, max_tokens, timeout):
    """One raw /chat/completions call against a local model's endpoint; returns the parsed
    response dict. The single HTTP seam shared by the cheap-tier _openai_complete and the
    execution-grade local_execute (and the one tests patch)."""
    body = {"model": m["model"], "temperature": 0, "max_tokens": max_tokens,
            "messages": messages}
    req = urllib.request.Request(
        m["base_url"].rstrip("/") + "/chat/completions",
        method="POST", headers=request_headers(m), data=json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def message_text(msg):
    """The CONTENT of an OpenAI-shaped assistant message, ALWAYS a str (`content: null` is
    legitimate server output and crashed the router when passed through — run web-f73ccc45).
    Deliberately content-ONLY: a reasoning model's think-stream (qwen `reasoning_content`,
    gemma-4/vLLM `reasoning`) is never a usable answer — falling back to it delivered walls
    of meta-commentary as results and fake clarifying questions. reasoning_text() exposes
    the thinking for callers that want to NUDGE the model into answering."""
    if not isinstance(msg, dict):
        return ""
    return str(msg.get("content") or "")


def reasoning_text(msg):
    """A reasoning model's think-stream for this message, or '' — the signal that the model
    thought but never answered (content empty), which callers convert into ONE 'answer now'
    nudge. Field name varies by server: qwen-style `reasoning_content`, gemma-4/vLLM
    `reasoning`."""
    if not isinstance(msg, dict):
        return ""
    return str(msg.get("reasoning_content") or msg.get("reasoning") or "")


_NUDGE = ("Now reply with ONLY your final answer to the original instruction — "
          "no reasoning, no commentary.")


def looks_like_thinking(text):
    """True when assistant CONTENT is actually a leaked think-stream. Some serving stacks
    only split reasoning into its own field sometimes — observed live: gemma-4 on vLLM
    emitting content whose first line is the literal word 'thought' followed by pages of
    deliberation (with the real answer buried at the tail). Deliberately precise — a first
    line of exactly 'thought'/'thinking' or an opening think-tag — so a genuine answer that
    merely starts with 'Thoughtful…' never matches."""
    t = (text or "").lstrip()
    first = t.split("\n", 1)[0].strip().lower()
    return first in ("thought", "thinking", "thought:", "thinking:") or \
        t.lower().startswith(("<think>", "<thought>"))


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text):
    """Remove a think-stream a server left INLINE in content. Handles a complete
    `<think>…</think>` pair AND the common half-leak where the server ate the opening `<think>`
    into its reasoning field but left a stray closing `</think>` behind (qwen3 on vLLM with no
    reasoning parser) — everything up to that first `</think>` is reasoning. Lives here (not
    only in local_runtime) so the cheap-tier completion path and the verdict parsers can reuse
    it; local_runtime re-exports it."""
    if not text:
        return text
    text = _THINK_RE.sub("", text)
    low = text.lower()
    if "</think>" in low and "<think>" not in low:
        text = text[low.index("</think>") + len("</think>"):]
    return text.strip()


# Unfenced chain-of-thought a server left in `content` because no reasoning parser split it
# out (qwen3.6 on vLLM). looks_like_thinking() only catches a literal 'thought'/'thinking'
# opener or a <think> tag; this catches the FAR commoner case of first-person deliberation with
# NO fence — the tell is repeated self-correction. Report: a 27KB "result" (web-ccbb5378) that
# was pure deliberation ("But wait… I need to reconsider…") with the answer only in the tail.
_DELIBERATION_RE = re.compile(
    r"\b(but wait|wait[,.:]? (?:no|actually)|let me reconsider|i (?:need|have) to reconsider|"
    r"i(?:'ve| have) been overthinking|on second thought|let me (?:re)?(?:think|check|verify)|"
    r"actually[,.:]? (?:no|wait)|hold on[,.:]|hmm[,.:])", re.IGNORECASE)


def _is_reasoning_stream(text):
    """True when `content` is an unfenced think-stream, not a deliverable. Conservative (needs
    ≥2 self-correction markers) so a genuine answer that reflects ONCE never trips it — and even
    a false positive only costs one extra nudge turn (the model is re-asked for the bare answer),
    never a truncated deliverable."""
    return bool(text) and len(_DELIBERATION_RE.findall(text)) >= 2


def _answer_and_thinking(msg):
    """Split a local model's message into (answer, thinking). A fenced think-stream left inline
    in content is STRIPPED, so a verdict/answer that lands after a `</think>` (or a complete
    `<think>…</think>` pair) is returned CLEAN — the failure that scored a genuine verify PASS as
    FAIL and delivered raw reasoning to routing/clarify. When the turn is nothing but reasoning
    — an unfenced deliberation stream, a leaked think opener, or empty content with the thinking
    split into a reasoning field — answer is '' and thinking carries the material for the
    caller's one 'answer now' nudge."""
    raw = message_text(msg)
    answer = _strip_reasoning(raw)
    if answer and not looks_like_thinking(answer) and not _is_reasoning_stream(answer):
        return answer, ""
    # Reasoning-only: prefer the split reasoning field, else the stripped/raw think-stream.
    return "", (reasoning_text(msg) or answer or raw)


def _openai_complete(m, prompt, timeout=None):
    timeout = config.LOCAL_TIMEOUT_S if timeout is None else timeout
    messages = [{"role": "user", "content": prompt}]
    data = _chat(m, messages, config.LOCAL_COMPLETE_MAX_TOKENS, timeout)
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text, thinking = _answer_and_thinking(msg)
    if thinking and not text:
        # Reasoning-only turn (the cheap-tier twin of the runtime's nudge): the model thought
        # but never surfaced a bare answer — whether split into a reasoning field, fenced, or an
        # unfenced deliberation leak. Feed its own thinking back and ask for the bare answer.
        messages += [{"role": "assistant", "content": thinking[-2000:]},
                     {"role": "user", "content": _NUDGE}]
        data = _chat(m, messages, config.LOCAL_COMPLETE_MAX_TOKENS, timeout)
        text, _ = _answer_and_thinking((data.get("choices") or [{}])[0].get("message") or {})
    return text


def embed(texts, model_name=None):
    """Embed texts via an OpenAI-compatible /embeddings endpoint (issue #67). `model_name` is a
    pool entry. Returns a list of vectors aligned with `texts`, or None when no usable LOCAL
    embedding model is configured or the call fails — the caller (knowledge.py) then falls back
    to keyword matching. Never uses Claude: Claude has no embeddings API, and grounding stays off
    the Claude-only execution path."""
    texts = list(texts or [])
    if not texts or not model_name:
        return None
    cfg = load()
    m = next((x for x in cfg.get("pool", []) if x["name"] == model_name), None)
    if not m or m.get("provider") == "claude" or not m.get("base_url"):
        return None
    try:
        body = {"model": m["model"], "input": texts}
        req = urllib.request.Request(
            m["base_url"].rstrip("/") + "/embeddings",
            method="POST", headers=request_headers(m), data=json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        vecs = [d["embedding"] for d in data.get("data", [])]
        return vecs if len(vecs) == len(texts) else None
    except Exception as e:  # noqa: BLE001 - any failure -> keyword fallback in knowledge.py
        trace("GATEWAY", f"embed via {model_name} failed ({e}); knowledge falls back to keyword match")
        return None


def test_model(name, cfg=None, timeout=None):
    """Check a model is reachable so the UI can show OK / not. Returns {ok, ms, detail}.

    For local models we hit GET /models (lists available models) instead of running
    inference - that's instant and avoids a cold model load timing out.

    Every outcome is recorded into the model-health store (via="probe"), so this is the single
    probe seam behind both the per-row "test" button and probe_models()'s refresh — a failed
    test lights the Admin-tab badge, and a passing one clears it."""
    cfg = cfg or load()
    m = next((x for x in cfg.get("pool", []) if x["name"] == name), None)
    if not m:
        return {"ok": False, "detail": "unknown model"}
    t0 = time.time()
    ms = lambda: int((time.time() - t0) * 1000)  # noqa: E731

    def done(ok, detail):
        record_health(name, ok, detail, via="probe")
        return {"ok": ok, "ms": ms(), "detail": detail}
    try:
        if m.get("provider") == "claude":
            out = claude_cli.run_json("Reply with exactly: OK", model=m["model"])
            return done(not out.get("is_error"),
                        (out.get("result", "") or "")[:40] or "no reply from claude -p")
        if not m.get("base_url"):
            # A model pointing at an endpoint that no longer exists: name the endpoint, or the
            # operator sees a urlopen error against an empty URL and has nothing to go fix.
            return done(False, f"no endpoint: '{m.get('endpoint') or '(none)'}' is not configured")
        # local: list models (fast, no inference / no model load)
        req = urllib.request.Request(m["base_url"].rstrip("/") + "/models",
                                     headers=request_headers(m, content_json=False))
        with urllib.request.urlopen(req, timeout=timeout or 8) as r:
            data = json.loads(r.read())
        ids = [x.get("id") for x in data.get("data", [])]
        if m["model"] in ids:
            return done(True, f"reachable · {len(ids)} models · '{m['model']}' available")
        return done(False, f"server up but '{m['model']}' not found — pull it or fix the id "
                           f"({len(ids)} available)")
    except Exception as e:  # noqa: BLE001
        # Name the endpoint: a bare "<urlopen error timed out>" in the Admin banner says nothing
        # about WHICH host to go and start.
        where = (f"cannot reach {m.get('base_url')}: "
                 if m.get("provider") != "claude" and m.get("base_url") else "")
        return done(False, (where + str(e))[:180])
