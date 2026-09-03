"""Otto's learning stores: memory (facts), solutions, behaviors, and their garbage collection.

Extracted from engine.py (which re-exports everything here — callers and tests keep using
engine.X, same facade contract as audit.py). Three tables it owns: `memory` (learned facts,
`namespace` NULL for global else a project slug), `solutions` (verified-pass approaches,
bounded) and `behaviors` (advisory user directives, bounded). Schema + connections live in
audit.py (`_conn`), which resolves the db path through the engine facade so tests patching
engine._DB cover these stores too.
"""
import concurrent.futures
import datetime
import json
import re
import threading
import time
import uuid

import config
import gateway
import registry
import storage
import workspace
from audit import _append_audit, _append_content, _audit, _conn
from ui import trace


def _eng():
    """The engine facade — tests monkeypatch attributes there (engine._DB, engine._claude,
    engine._extract_solution, ...), so patch-sensitive values and cross-calls resolve through
    it at call time, never bind at import. Same contract as audit._eng."""
    import engine
    return engine


_SOLUTIONS_MAX = 200
_BEHAVIORS_MAX = 50
# Conversational filler (>3 chars, so it survives `_keywords`) stripped from a REQUEST before
# ranking stored facts against it. Applied to the query only — `_keywords` itself mirrors routing's
# scorer and must keep doing so.
_FACT_QUERY_STOP = frozenset("""
about also anyone anything been being both cannot could does doing done each else even ever every
from give given have having help here into just know like made make many more most much must need
none only other over please should show some such tell thanks thank that their them then there
these they thing things this those took very want what when where which while whose will with
would your yours reach reachable
""".split())


def record_attempt(wid, request, cap, result, cost, attempt, verdict, remember=False,
                   tokens=None, model=None, repo=None, project=None, duration_s=None,
                   backend=None, fallback_from=None, fallback_reason=None):
    """Audit one attempt (with its verify verdict, token usage, the model that ran it, and
    which backend — plus what it fell back from, if the chosen model couldn't run). On the
    final/passing attempt also distil memory from the result — facts go to the project's
    namespace when the run is in a project (issue #69)."""
    _audit(wid, request, cap, result, cost, attempt=attempt,
           verified=None if verdict is None else verdict.get("passed"),
           critique=None if verdict is None else verdict.get("critique"),
           verdict_source=None if verdict is None else verdict.get("source"),
           verdict_model=None if verdict is None else verdict.get("model"),
           tokens=tokens, model=model, repo=repo, duration_s=duration_s,
           backend=backend, fallback_from=fallback_from, fallback_reason=fallback_reason)
    # Feed the per-capability local latch (gateway.record_cap_local). Deliberately HERE rather
    # than in either ladder: `_ladder_core` and `OttoWorkflow._verify_ladder` are two mirrors of
    # the same loop, and workflow code cannot touch the disk anyway — this activity is the one
    # place both already hand over a verdict, the model and the backend together.
    # Judged verdicts only: a harness death or a supervisor kill says nothing about whether the
    # capability can work on this model.
    if backend == "local" and verdict and verdict.get("source") == "judge":
        gateway.record_cap_local(cap.name, model, bool(verdict.get("passed")))
    if remember:
        known = recent_facts(limit=40, project=project)
        _remember(cap, request, _extract_facts(request, result, known=known), project=project,
                  verified=None if verdict is None else verdict.get("passed"))
        # Only a genuinely verified pass teaches a reusable approach worth recalling (a final
        # FAILED attempt still has remember=True for fact distillation, but verdict.passed=False).
        if verdict and verdict.get("passed"):
            _remember_solution(cap, request, _eng()._extract_solution(request, cap, result))
    trace("AUDIT", f"trail -> {_eng()._DB}")


def _norm(fact):
    """Normalize a fact for duplicate comparison — case-fold, collapse whitespace, drop
    trailing punctuation — so 'X is down.' and 'x is down' count as the same fact."""
    return " ".join(str(fact).lower().split()).rstrip(".!?;,")


# Narration/meta the memory model emits alongside real facts. Storing these is expensive: the
# recall window is a small recency tail (`recent_facts` limit=12), so every junk row CROWDS OUT a
# real fact — which is how a run ends up reasoning from "## Memory facts from this audit". Bias is
# the opposite of `slack.is_pleasantry`: dropping a genuine fact costs almost nothing (memory is
# advisory), so anything that smells like narration goes.
_FACT_REJECT_PREFIXES = (
    "let me", "let's", "lets ", "i ", "i'", "here's", "here is", "based on", "sure", "okay",
    "ok,", "to give you", "to answer", "first,", "next,", "finally,", "in summary", "summary:",
    "note:", "notes:", "reply", "answer:", "#", "*", "```", "---",
)
_FACT_REJECT_SUBSTRINGS = (
    "memory fact", "not part of the delivered", "reply to send", "for context",
    "don't have access", "do not have access", "available context",
)


def _is_durable_fact(line):
    """Whether an extracted line is a FACT rather than the model narrating. PURE (unit-tested).

    Rejects questions (a question is by definition not a durable fact), markdown headings/fences,
    first-person narration ("Let me extract…", "I don't have access to…") and references to the
    extraction process itself — all observed in the live store on 2026-07-30, where they filled
    most of the 12-fact recall window."""
    s = (line or "").strip()
    if len(s) < 15 or len(s.split()) < 4:
        return False
    if "?" in s:                          # questions, and lines that trail into one
        return False
    low = s.lower().lstrip("*#>` ").strip()
    if low.startswith(_FACT_REJECT_PREFIXES):
        return False
    if low.rstrip("*` ").endswith(":"):   # "dev-a CI/Terraform parity gaps:**" — a label, not a fact
        return False
    return not any(m in low for m in _FACT_REJECT_SUBSTRINGS)


def _clip_fact(line, limit):
    """A stored fact bounded to `limit`, cut on a word boundary and marked. A bare slice ends a
    fact mid-word ("...retention decisions must a"), which reads as corrupt data wherever it is
    shown and is indistinguishable from a fact the model truncated itself."""
    line = line.strip()
    if len(line) <= limit:
        return line
    head = line[:limit - 1]
    space = head.rfind(" ")
    if space > limit * 0.6:          # only honour the boundary if it isn't a drastic cut
        head = head[:space]
    return head.rstrip(" ,;:-") + "\u2026"


def _extract_facts(request, result, known=None):
    """Distil 0-3 durable facts worth remembering from a completed run. Runs on the
    'memory' model tier (can be a cheap/local model). The transcript lives in the audit
    log — memory is for *context*, not a replay of what happened.

    Most runs should yield NOTHING: only genuinely durable, reusable facts are worth
    keeping (issue #55). The model is shown what Otto already knows so it neither
    restates nor contradicts existing memory; anything already known is dropped here too
    as a backstop against semantic near-misses the model lets through."""
    if not result or result == "(no output)":
        return []
    known = recent_facts(limit=40) if known is None else known
    known_block = ("\n\nOtto ALREADY remembers these — do NOT repeat, rephrase, or restate any of them:\n"
                   + "\n".join(f"- {k}" for k in known)) if known else ""
    text = gateway.complete(
        "memory",
        "You curate the long-term memory of an automation platform. From this completed task, extract only "
        "NEW, durable FACTS that will genuinely help future, unrelated work — stable entities, decisions, "
        "current state, or outcomes (NOT a summary of the steps, NOT one-off details, NOT anything ephemeral). "
        "Be strict: most tasks teach nothing reusable, so returning NONE is the common, correct answer. "
        "Never restate something already known. List 0-3 facts, one per line, no bullets or numbering. "
        "If there is nothing new and durable worth remembering, reply exactly: NONE."
        f"{known_block}\n\n"
        f"Task: {request}\n\nResult:\n{result[:2000]}",
    ).strip()
    if not text or text.upper().startswith("NONE"):
        return []
    seen = {_norm(k) for k in known}
    facts = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*•0123456789. ").strip()
        if not line or line.upper() == "NONE":
            continue
        if not _is_durable_fact(line):
            continue          # narration/question/heading, not a fact — never store it
        norm = _norm(line)
        if not norm or norm in seen:
            continue          # already known (or surfaced twice in this batch) — don't duplicate
        seen.add(norm)
        facts.append(_clip_fact(line, 200))
    return facts[:3]


def _resolve_project(cap, repo=None):
    """The registered project PATH a run belongs to, or None (issue #69). A project-scoped cap
    carries its repo as `cwd`; a repo-mode run targets an allowlisted project repo by name. A run
    with neither stays global — unchanged behaviour."""
    cwd = getattr(cap, "cwd", None)
    if cwd and cwd in registry.projects():
        return cwd
    if repo:
        r = workspace.resolve(repo)
        if r:
            return r["path"]
    return None


def _memory_ns(project=None):
    """The `memory.namespace` value a run's facts live under: None for the global store, else the
    project's slug (what used to be the data/memory/<ns>.json filename) so one project's learned
    facts don't leak into unrelated work."""
    return registry.project_namespace(project) if project else None


def _events_in(conn, namespace):
    """One namespace's events, oldest first — `namespace=None` means the global store. The
    `IS` comparison matters: `= NULL` never matches in SQL."""
    rows = conn.execute(
        "SELECT data FROM memory WHERE namespace IS ? ORDER BY id", (namespace,)).fetchall()
    return [json.loads(r["data"]) for r in rows]


def _facts_of(events, dated=False):
    if dated:
        return [{"fact": fct, "at": e.get("at") or "", "unverified": bool(e.get("unverified"))}
                for e in events for fct in e.get("facts", [])]
    return [fct for e in events for fct in e.get("facts", [])]


def _recent_facts(conn, limit, project, dated=False):
    # GLOBAL first, then the project's, then tail-limit the CONCATENATION — not one merged
    # `ORDER BY at`. With a small limit, project facts are meant to crowd out global ones, and
    # timestamps between the two namespaces interleave, so sorting by `at` would pick a
    # different set than the JSON files did.
    facts = _facts_of(_events_in(conn, None), dated)
    if project:
        facts = facts + _facts_of(_events_in(conn, _memory_ns(project)), dated)
    return facts[-limit:]


def recent_facts(limit=12, project=None, dated=False, request=None):
    """Facts to carry into a run: the global facts, plus this project's (if any). A run with no
    project sees ONLY the global store — project-scoped facts never leak across projects.

    `dated=True` returns `{"fact", "at", "unverified"}` dicts instead of bare strings, for callers
    that must show a fact's AGE and whether the run that produced it passed verification (see
    `_memory_context` — an undated recollection reads as current truth).

    `request` makes the window RELEVANT rather than merely recent. Without it the window is the
    newest `limit` facts, which is how a question about a production service got a grab-bag of CI
    and Savings-Plan trivia while the vLLM facts sat just outside the tail. With it, facts sharing
    keywords with the request come first (best overlap first), and the remaining slots are filled
    with the newest facts as before — so a fresh run gains relevance without losing recency, and a
    resume run (no request) keeps the exact old behaviour."""
    with _conn() as conn:
        rows = _recent_facts(conn, 10_000, project, dated)
    limit = max(0, int(limit))
    if not request or len(rows) <= limit:
        return rows[-limit:] if limit else []
    # Conversational filler carries no topic signal, and `_keywords` can't filter it: that
    # tokenizer deliberately mirrors routing's scorer. Stripping it on the QUERY side only is what
    # keeps "is THERE a production vLLM environment, how do I REACH it" from matching facts on
    # "there"/"reach". (An IDF weighting was tried first and measurably backfired: "vllm" appears in
    # 13 facts in the live store so it scored LOWEST, while the accidental "reach"/"there" were rare
    # and scored highest — rarity in a technical store is not importance.)
    want = _keywords(request) - _FACT_QUERY_STOP
    text = (lambda r: r["fact"]) if dated else (lambda r: r)
    if not want:
        return rows[-limit:]
    # index keeps the store's own order stable inside each group (oldest first), so the rendered
    # block still reads chronologically and two runs with the same request pick the same facts.
    hit, miss = [], []
    for i, r in enumerate(rows):
        score = len(want & _keywords(text(r)))
        (hit if score > 0 else miss).append((score, i, r))
    hit.sort(key=lambda x: (-x[0], -x[1]))                  # strongest signal, then most recent
    picked = hit[:limit]
    if len(picked) < limit:                                 # fill the rest with the newest, as before
        picked += miss[-(limit - len(picked)):]
    picked.sort(key=lambda x: x[1])                          # back into store order
    return [r for _, _, r in picked]


def memory_events(namespace=None, every=False):
    """Stored memory events, oldest first — one namespace by default, or every namespace when
    `every` is set (each row carries its `namespace` so the Memory tab can show provenance).

    Each row also carries its `id`, which is the handle `delete_fact` needs — a fact is not a row
    of its own (a run stores 0-3 of them together), so forgetting ONE requires naming its row."""
    with _conn() as conn:
        if not every:
            rows = conn.execute(
                "SELECT id, data FROM memory WHERE namespace IS ? ORDER BY id",
                (namespace,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, namespace, data FROM memory "
                "ORDER BY (namespace IS NOT NULL), id").fetchall()
        out = []
        for r in rows:
            event = dict(json.loads(r["data"]), id=r["id"])
            if every:
                event["namespace"] = r["namespace"]
            out.append(event)
        return out


def delete_fact(event_id, fact, namespace=None, every=False):
    """Forget ONE learned fact, leaving the rest of its run's facts (and every other run) alone.

    Facts are stored in groups — one row per run holding the 0-3 facts distilled from it — so the
    handle is the row `id` from `memory_events` plus the fact's exact text. Matching is done on
    `_norm` (the same normalisation `_remember` de-dupes on) so a fact copied out of the UI still
    matches despite whitespace/case drift. A row whose last fact is removed is DELETED rather than
    left with an empty `facts` list: `_remember` never writes an empty row, so keeping one would
    invent a shape no reader expects.

    Scoped to the GLOBAL store by default, matching `clear_memory` — `/api/memory` lists global
    facts, so a delete driven from that list must not be able to reach into a project namespace
    it never showed. Returns True when a fact was actually removed.

    Read-modify-write of the row's JSON, hence `storage.tx` (BEGIN IMMEDIATE): two of these racing
    on the same row would otherwise lose one delete."""
    want = _norm(fact or "")
    if not want or event_id is None:
        return False
    try:
        event_id = int(event_id)
    except (TypeError, ValueError):
        return False
    with _conn() as conn, storage.tx(conn):
        if every:
            row = conn.execute("SELECT data FROM memory WHERE id = ?", (event_id,)).fetchone()
        else:
            row = conn.execute("SELECT data FROM memory WHERE id = ? AND namespace IS ?",
                               (event_id, namespace)).fetchone()
        if not row:
            return False
        event = json.loads(row["data"])
        kept = [f for f in event.get("facts", []) if _norm(f) != want]
        if len(kept) == len(event.get("facts", [])):
            return False
        if kept:
            event["facts"] = kept
            conn.execute("UPDATE memory SET data = ? WHERE id = ?",
                         (json.dumps(event), event_id))
        else:
            conn.execute("DELETE FROM memory WHERE id = ?", (event_id,))
    trace("MEMORY", f"forgot 1 fact from event {event_id}")
    return True


def clear_memory(every=False):
    """Wipe learned facts. Defaults to the GLOBAL store only — exactly what the pre-SQLite
    "clear memory" did (it truncated data/memory.json and left data/memory/<ns>.json alone), so a
    storage migration doesn't silently widen a destructive button's blast radius. `every=True`
    also drops every project namespace. The audit trail is never touched (it's the immutable
    record); solutions/behaviours/knowledge have their own clear paths."""
    with _conn() as conn:
        if every:
            conn.execute("DELETE FROM memory")
        else:
            conn.execute("DELETE FROM memory WHERE namespace IS NULL")


def _remember(cap, request, facts, project=None, verified=None):
    """Append the facts learned from a run, to the project's namespace when set else the global
    store (issue #69). A run that produced nothing durable writes no row at all (issue #55). Facts
    already present anywhere this run can see (global + project) are dropped — never stored twice.

    `verified` is the run's verify verdict (True/False/None-if-unjudged) and is stored ON the row,
    because facts are distilled even from a final attempt that FAILED verification — that is how a
    wrong answer ("vLLM is not deployed in production") became a durable global fact on 2026-07-30.
    Rather than drop those (a failed run can still learn something true), `_memory_context` labels
    them so a later run weighs them accordingly."""
    if not facts:
        trace("MEMORY", "nothing new/durable to remember")
        return
    remembered = []
    with _conn() as conn, storage.tx(conn):
        # de-dupe against everything this run would see (global + project), not just the target
        # namespace, so a project run doesn't re-store a fact that's already global. Inside the
        # write transaction, so a concurrent writer can't slip the same fact in between check
        # and write.
        seen = {_norm(fct) for fct in _recent_facts(conn, 10_000, project)}
        for fct in facts:
            norm = _norm(fct)
            if norm and norm not in seen:
                seen.add(norm)
                remembered.append(fct)
        if remembered:
            event = {
                "at": datetime.datetime.now().isoformat(timespec="seconds"),
                "capability": f"{cap.kind}:{cap.name}", "request": request, "facts": remembered,
            }
            # Only recorded when the run was actually judged and FAILED: keeping the key absent for
            # a pass (and for an unjudged run) leaves every pre-existing row's shape untouched, and
            # the Memory tab / round-trip contract sees exactly what the writers wrote before.
            if verified is False:
                event["unverified"] = True
            conn.execute(
                "INSERT INTO memory (namespace, at, capability, data) VALUES (?, ?, ?, ?)",
                (_memory_ns(project), event["at"], event["capability"], json.dumps(event)))
    if remembered:
        trace("MEMORY", f"remembered {len(remembered)} fact(s)" + (f" [project: {registry.project_namespace(project)}]" if project else ""))
    else:
        trace("MEMORY", "nothing new/durable to remember")


# --- solutions memory (issue #66): how a task was SOLVED, not what was learned -------------
# A second store beside facts: facts are distilled *context* (declarative — "X is in eu-west-1");
# solutions are the *approach that worked* (procedural — "to do X, run Y with flag Z"). Stored
# only on a verified pass, recalled on a similar fresh request and injected as a worked example.

def _keywords(text):
    """Significant words (>3 chars, URLs stripped) for similarity matching — mirrors
    registry.Capability.score so recall ranks a request the same way routing shortlists it."""
    text = re.sub(r"https?://\S+", " ", (text or "").lower())
    return {w for w in re.findall(r"[a-z]+", text) if len(w) > 3}


def _extract_solution(request, cap, result):
    """Distil the reusable APPROACH from a verified run (memory tier). Returns a compact
    summary (≤4 sentences) or "" when the task was trivial / its approach isn't reusable —
    the common case, mirroring the strict facts extractor. Describes the METHOD (key steps,
    commands/flags, gotchas), not the specific result or a transcript replay (that's the audit)."""
    if not result or result == "(no output)":
        return ""
    text = gateway.complete(
        "memory",
        "A task was just completed SUCCESSFULLY and verified. In 4 sentences or fewer, capture the "
        "REUSABLE APPROACH that worked — the key steps, the commands/flags/tools used, and any "
        "gotchas a future similar task should copy. Describe the METHOD, not the specific result, "
        "and do not replay the transcript. If the task was trivial or its approach is not reusable "
        "(the common case), reply exactly: NONE."
        f"\n\nTask: {request}\n\nResult:\n{result[:2000]}",
    ).strip()
    if not text or text.upper().startswith("NONE"):
        return ""
    return text[:600]


def _solution(row):
    """Rebuild the stored dict in its original key order (it's serialized to the Memory tab)."""
    return {"id": row["id"], "at": row["at"], "capability": row["capability"],
            "request": row["request"], "approach": row["approach"]}


def _load_solutions(conn, newest_first=False):
    order = "seq DESC" if newest_first else "seq"
    return [_solution(r) for r in conn.execute(
        f"SELECT id, at, capability, request, approach FROM solutions ORDER BY {order}")]


def recall_solutions(request, limit=2, min_overlap=2):
    """The top past solved-task approaches most similar to `request`, by keyword overlap (same
    tokenization as routing). Returns [] when nothing clears `min_overlap` — so an unrelated
    request injects nothing. Ties break toward the most recent."""
    want = _keywords(request)
    if not want:
        return []
    with _conn() as conn:
        stored = _load_solutions(conn)
    scored = []
    for s in stored:
        overlap = len(want & _keywords(s.get("request", "")))
        if overlap >= min_overlap:
            scored.append((overlap, s.get("at", ""), s))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [s for _, _, s in scored[:limit]]


def _trim(conn, table, keep):
    """Bound a store to its newest `keep` rows by insertion order (the old `items[-MAX:]`)."""
    conn.execute(f"DELETE FROM {table} WHERE id NOT IN "
                 f"(SELECT id FROM {table} ORDER BY seq DESC LIMIT ?)", (keep,))


def _next_seq(conn, table):
    return conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}").fetchone()[0]


def _remember_solution(cap, request, approach):
    """Store one verified approach. De-dupes on the normalized request (re-running the same task
    refreshes its entry instead of piling up) and bounds the store so it can't grow unbounded."""
    if not approach:
        return
    norm = _norm(request)
    with _conn() as conn, storage.tx(conn):
        # drop the stale same-request entry (normalized compare, so it can't be done in SQL)
        for r in conn.execute("SELECT id, request FROM solutions").fetchall():
            if _norm(r["request"] or "") == norm:
                conn.execute("DELETE FROM solutions WHERE id = ?", (r["id"],))
        conn.execute("INSERT INTO solutions (id, seq, at, capability, request, approach) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (uuid.uuid4().hex[:12], _next_seq(conn, "solutions"),
                      datetime.datetime.now().isoformat(timespec="seconds"),
                      f"{cap.kind}:{cap.name}", request, approach))
        _trim(conn, "solutions", _eng()._SOLUTIONS_MAX)
    trace("MEMORY", f"stored a solved-task approach for [{cap.name}]")


def solutions():
    """All stored solution approaches, newest first (for the Memory tab)."""
    with _conn() as conn:
        return _load_solutions(conn, newest_first=True)


def delete_solution(sid):
    with _conn() as conn:
        conn.execute("DELETE FROM solutions WHERE id = ?", (sid,))


def clear_solutions():
    with _conn() as conn:
        conn.execute("DELETE FROM solutions")


# --- behaviour rules (issue #68): how the user wants the agent to WORK ----------------------
# A third memory store: facts are *what is true*, solutions are *how a task was solved*, rules
# are *how to work* (directives like "always run tests before opening a PR"). Rules are captured
# from the user (explicitly, or proposed from a chat correction and confirmed — never stored
# silently), scoped global or to one capability, and injected into runs as directives. They are
# guidance layered on the cap's own instructions — NOT a security control: they never relax the
# write gate or tool allowlists (the gate stays the real guard, as with the write-intent design).

def _behavior(row):
    """Rebuild the stored dict in its original key order (it's serialized to the Memory tab)."""
    return {"id": row["id"], "at": row["at"], "scope": row["scope"], "rule": row["rule"]}


def _load_behaviors(conn, newest_first=False):
    order = "seq DESC" if newest_first else "seq"
    return [_behavior(r) for r in conn.execute(
        f"SELECT id, at, scope, rule FROM behaviors ORDER BY {order}")]


def applicable_behaviors(cap):
    """Rules that apply to a run: every global rule, plus any scoped to this capability
    (`<kind>:<name>`). Order preserved (oldest first) so injection reads stably."""
    scope_id = f"{getattr(cap, 'kind', '')}:{getattr(cap, 'name', '')}" if cap else None
    with _conn() as conn:
        rows = _load_behaviors(conn)
    out = []
    for b in rows:
        sc = b.get("scope", "global")
        if sc == "global" or (scope_id and sc == scope_id):
            out.append(b)
    return out


def behaviors():
    """All stored rules, newest first (for the Memory tab)."""
    with _conn() as conn:
        return _load_behaviors(conn, newest_first=True)


def add_behavior(rule, scope="global"):
    """Store a behaviour rule. De-dupes on (normalized rule, scope) so re-adding the same rule
    refreshes rather than duplicates, and bounds the store so the injected prompt stays lean.
    Returns the stored row (callers count a truthy return as "added")."""
    rule = " ".join((rule or "").split()).strip()
    if not rule:
        return None
    scope = (scope or "global").strip() or "global"
    norm = _norm(rule)
    stored = {"id": uuid.uuid4().hex[:12],
              "at": datetime.datetime.now().isoformat(timespec="seconds"),
              "scope": scope, "rule": rule[:300]}
    with _conn() as conn, storage.tx(conn):
        # normalized compare, so the stale duplicate can't be found in SQL
        for r in conn.execute("SELECT id, rule, scope FROM behaviors").fetchall():
            if _norm(r["rule"] or "") == norm and (r["scope"] or "global") == scope:
                conn.execute("DELETE FROM behaviors WHERE id = ?", (r["id"],))
        conn.execute("INSERT INTO behaviors (id, seq, at, scope, rule) VALUES (?, ?, ?, ?, ?)",
                     (stored["id"], _next_seq(conn, "behaviors"), stored["at"],
                      stored["scope"], stored["rule"]))
        _trim(conn, "behaviors", _eng()._BEHAVIORS_MAX)
    trace("MEMORY", f"stored behaviour rule (scope={scope})")
    return stored


def update_behavior(bid, rule):
    rule = " ".join((rule or "").split()).strip()[:300]
    with _conn() as conn:
        conn.execute("UPDATE behaviors SET rule = ? WHERE id = ?", (rule, bid))


def delete_behavior(bid):
    with _conn() as conn:
        conn.execute("DELETE FROM behaviors WHERE id = ?", (bid,))


def _parse_rule_suggestion(text):
    """Parse the rule-suggestion classifier reply. The model answers NONE (not a behaviour rule —
    the common case) or 'RULE: <imperative>'. Pure, so it's unit-testable. Unrecognised/empty ->
    not a rule (don't propose noise)."""
    text = (text or "").strip()
    if not text or text.upper().startswith("NONE"):
        return {"is_rule": False, "rule": ""}
    m = re.match(r"(?is)^\s*RULE\s*:\s*(.+)$", text)
    rule = " ".join((m.group(1) if m else text).split()).strip()[:300]
    return {"is_rule": bool(rule), "rule": rule}


def suggest_behavior_rule(message, cap_name=None):
    """Decide whether a user message is a CORRECTION / standing instruction about HOW the agent
    should behave on future tasks (issue #68); if so, normalise it into one concise imperative
    rule. Clarify tier. Returns {is_rule, rule, scope_hint}. NOTHING is stored — the caller shows
    the proposal and stores only on the user's confirmation."""
    ctx = f" The message was sent while working with the capability '{cap_name}'." if cap_name else ""
    text = gateway.complete(
        "clarify",
        "Decide whether the user's message below is a CORRECTION or standing instruction about HOW "
        "the agent should work on FUTURE tasks (e.g. 'always run the tests before opening a PR', "
        "'ask before touching prod', 'never force-push'). A one-off task request, a question, or "
        "feedback that only concerns the current result is NOT such a rule.\n"
        "If it IS a durable behaviour rule, reply 'RULE: ' followed by a single concise imperative "
        "sentence capturing it. Otherwise reply exactly: NONE." + ctx + "\n" +
        _eng()._DATA_FENCE_PREAMBLE +
        f"\n\nUser message:\n{_eng()._fenced(message)}",
    )
    out = _parse_rule_suggestion(text)
    out["scope_hint"] = cap_name or "global"
    trace("MEMORY", f"rule suggestion -> {'RULE' if out['is_rule'] else 'none'}")
    return out


# --- memory garbage collection: evict facts/solutions/rules that are no longer true ----------
# All three stores only ever grow (facts unboundedly; solutions/rules are bounded by count, not
# by whether they're still correct) and nothing marks an entry stale when the world it described
# changes. This is an on-demand maintenance pass, triggered ONLY by an explicit Admin button —
# never scheduled, never run inside a normal task. `gc_preview` only PROPOSES candidates; nothing
# is deleted until a human reviews the list and calls `gc_evict` with what they picked (same
# approval-gate philosophy as everything else destructive in Otto).

def _gc_items():
    """Every evictable memory item, normalized to one shape: {store, id, namespace, text,
    context, at}. `id`+`namespace` are exactly the handle `delete_fact` needs to remove ONE fact
    out of the (possibly multi-fact) event row it lives in; solutions/rules are one-row-per-item
    so their own id is already enough. Scans EVERY namespace (global + every project), which is
    why eviction must go through `delete_fact(..., every=True)` rather than the default-scoped
    call the Memory tab's per-fact "forget" button uses."""
    items = []
    for event in memory_events(every=True):
        for fact in event.get("facts", []):
            items.append({"store": "fact", "id": event.get("id"), "namespace": event.get("namespace"),
                          "text": fact, "context": event.get("request", ""), "at": event.get("at", "")})
    for s in solutions():
        items.append({"store": "solution", "id": s["id"], "namespace": None,
                      "text": s.get("approach", ""), "context": s.get("request", ""), "at": s.get("at", "")})
    for b in behaviors():
        items.append({"store": "behavior", "id": b["id"], "namespace": None,
                      "text": b.get("rule", ""), "context": b.get("scope", ""), "at": b.get("at", "")})
    return items


def _parse_gc_classification(text, n):
    """Parse the batch classifier's reply into a list of n {verdict, reason} dicts, one per item
    (KEEP/STALE/VERIFY). Pure (unit-testable). Expects one line per item: '<index>: <VERDICT> -
    <reason>'. Any line that doesn't parse, or an out-of-range index, is simply skipped — every
    slot starts as KEEP and stays there unless a matching line overrides it, so a garbled or
    partial reply defaults to keeping everything rather than evicting on a parse failure."""
    out = [{"verdict": "KEEP", "reason": ""} for _ in range(n)]
    for line in (text or "").splitlines():
        m = re.match(r"\s*(\d+)\s*[:.)]\s*(KEEP|STALE|VERIFY)\b\s*[-:]?\s*(.*)", line, re.I)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < n:
            out[idx] = {"verdict": m.group(2).upper(), "reason": m.group(3).strip()[:300]}
    return out


def _gc_classify_batch(batch):
    """One classifier call over up to a batch of items: KEEP (nothing to suggest it's gone
    stale), STALE (contradicted by another item in the list, superseded, narration/junk rather
    than a real fact, or plainly time-bound and expired — evict without further checking), or
    VERIFY (asserts something about the CURRENT state of a real system that can't be judged from
    text alone and needs a live check). Cheap `memory_gc` tier text completion, no tools — same
    call shape as `_extract_facts`/`judge_qa`. Biased toward KEEP: the prompt is told to default to it
    when unsure, matching `_is_durable_fact`'s "a lost item is cheap, a wrongly evicted one isn't"
    stance, just applied to eviction instead of extraction."""
    listing = "\n".join(
        f"{i + 1}. [{it['store']}] {it['text'][:220]}"
        + (f" (context: {it['context'][:120]})" if it.get("context") else "")
        for i, it in enumerate(batch))
    text = gateway.complete(
        "memory_gc",
        "You are garbage-collecting the long-term memory store of an automation platform. Below is "
        "a numbered list of stored items — learned facts, past solved-task approaches, and standing "
        "behaviour rules. For EACH numbered item decide exactly one of:\n"
        "KEEP - still accurate/useful, nothing suggests it's gone stale.\n"
        "STALE - contradicted by another item in this list, clearly superseded, a narration/junk "
        "line that isn't really a fact, or plainly time-bound and already expired.\n"
        "VERIFY - asserts something about the CURRENT state of a real system (a repo, a service, "
        "a config, whether something exists/is deployed/enabled/reachable) that you cannot judge "
        "from the text alone and would need to check live.\n"
        "Reply with exactly one line per item, in order: '<number>: <KEEP|STALE|VERIFY> - <one-line "
        "reason>'. Default to KEEP when unsure — only flag STALE or VERIFY when you have a concrete "
        "reason.\n" + _eng()._DATA_FENCE_PREAMBLE +
        f"\n\nItems:\n{_eng()._fenced(listing)}",
    )
    return _parse_gc_classification(text, len(batch))


def _parse_gc_verify(text):
    """Parse a live tool-verification reply into (still_true, reason): still_true is True/False,
    or None when unparseable/inconclusive. Pure (same leaked-reasoning stripping as
    `_parse_verdict`). None is treated as 'can't confirm it's false' by the caller — eviction
    never happens on a shrug, only on an explicit FALSE."""
    text = gateway._strip_reasoning(text or "").strip()
    first, _, rest = text.partition("\n")
    token = first.strip().upper()
    reason = (rest.strip() or text)[:300]
    if token.startswith("FALSE"):
        return False, reason
    if token.startswith("TRUE"):
        return True, reason
    return None, reason


def _gc_project_paths():
    """namespace -> registered project path, so a project-scoped fact's live verification runs
    from ITS OWN repo (cwd) rather than wherever the server process happens to be running from —
    load-bearing for a repo-relative claim ("this module was removed") to resolve correctly."""
    return {registry.project_namespace(p): p for p in registry.projects()}


def _gc_verify_live(item):
    """Real tool-verification for one VERIFY-flagged item: a bounded `claude -p` turn with
    read-only tools, checking the claim against the actual system instead of reasoning from its
    wording — on the `memory_gc` tier (`gateway.memory_gc_model_id`), not an invisible default. Returns (still_true, reason) — still_true is None on a timeout/run error/inconclusive
    answer, never treated as false (a maintenance pass that can't reach ground truth must leave
    the item alone, not guess)."""
    cwd = _gc_project_paths().get(item.get("namespace")) if item.get("namespace") else None
    prompt = (
        "Check whether the claim below about the CURRENT state of this system/repo is still true "
        "RIGHT NOW. Use your tools (read the repo, check config/files, run read-only commands) to "
        "verify — do not just reason from the wording. Reply on the first line with exactly one "
        "word, TRUE or FALSE (TRUE = still accurate today, FALSE = no longer accurate), then a "
        "one-line reason.\n" + _eng()._DATA_FENCE_PREAMBLE +
        f"\n\nClaim:\n{_eng()._fenced(item.get('text', ''))}")
    out = _eng()._claude(prompt, allowed_tools=config.READ_TOOLS, model=gateway.memory_gc_model_id(),
                         cwd=cwd, timeout=300, setting_sources=_eng()._setting_sources(cwd))
    if out.get("is_error"):
        return None, "verification run failed"
    return _parse_gc_verify(out.get("result", "") or "")


_GC_MAX_CONCURRENCY = 6   # bounded like gateway.probe_models's un-forced refresh — a real store is
                          # many sequential `claude -p` turns otherwise (measured: 221 stored items
                          # / batch 25 = 9 classify calls, worst case chained one after another).


def _gc_classify_batch_safe(batch):
    """`_gc_classify_batch`, but a batch that fails (model tier down, a malformed reply that still
    raises) degrades to KEEP for its whole batch instead of crashing the entire scan — one bad
    batch out of nine must not cost the other eight their results."""
    try:
        return _gc_classify_batch(batch)
    except Exception as e:
        trace("MEMORY", f"gc classify batch of {len(batch)} failed ({e}) — keeping all of them")
        return [{"verdict": "KEEP", "reason": ""} for _ in batch]


def _gc_verify_live_safe(item):
    """`_gc_verify_live`, but a raised exception (not just an `is_error` result) also degrades to
    'can't confirm it's false' rather than crashing the whole scan."""
    try:
        return _gc_verify_live(item)
    except Exception as e:
        return None, f"verification failed: {e}"


def gc_preview():
    """Scan every stored fact/solution/behaviour rule and propose eviction candidates:
    [{store, id, namespace, text, context, at, reason}]. Read-only — this never deletes anything
    itself, `gc_evict` does that and only on explicit confirmation. Two stages: a cheap batched
    classifier first (KEEP/STALE/VERIFY across everything scanned, batches run CONCURRENTLY —
    each is a real `claude -p` turn, and a store with a couple hundred items is otherwise many
    minutes of them chained one after another), then a BOUNDED number of real tool-verification
    runs for whatever needed VERIFY (also concurrent, same reasoning), capped by
    `memory_gc_max_verify` — a maintenance pass, not an open-ended sweep; anything past the cap is
    left for the next run, reported via `verify_skipped`, never silently dropped."""
    items = _gc_items()
    if not items:
        return {"candidates": [], "scanned": 0, "verify_skipped": 0}
    batch_size = max(1, config.setting("memory_gc_batch_size"))
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    if len(batches) == 1:
        results = [_gc_classify_batch_safe(batches[0])]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(_GC_MAX_CONCURRENCY, len(batches))) as pool:
            results = list(pool.map(_gc_classify_batch_safe, batches))
    verdicts = [v for batch_result in results for v in batch_result]
    candidates = []
    to_verify = []
    for item, v in zip(items, verdicts):
        if v["verdict"] == "STALE":
            candidates.append({**item, "reason": v["reason"] or "flagged as stale/superseded"})
        elif v["verdict"] == "VERIFY":
            to_verify.append(item)
    max_verify = max(0, config.setting("memory_gc_max_verify"))
    verify_skipped = max(0, len(to_verify) - max_verify)
    to_run = to_verify[:max_verify]
    if to_run:
        if len(to_run) == 1:
            verify_results = [_gc_verify_live_safe(to_run[0])]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(_GC_MAX_CONCURRENCY, len(to_run))) as pool:
                verify_results = list(pool.map(_gc_verify_live_safe, to_run))
        for item, (still_true, reason) in zip(to_run, verify_results):
            if still_true is False:
                candidates.append({**item, "reason": reason or "no longer true (verified live)"})
    trace("MEMORY", f"gc scanned {len(items)} item(s), {len(candidates)} eviction candidate(s)"
          + (f", {verify_skipped} VERIFY item(s) skipped this round" if verify_skipped else ""))
    return {"candidates": candidates, "scanned": len(items), "verify_skipped": verify_skipped}


_gc_state_lock = threading.Lock()
_gc_state = {"running": False, "started_at": None, "result": None}


def gc_status():
    """Whether a GC scan is currently in flight, server-side — the browser's own GC_RUNNING flag
    used to be the only record of this, so refreshing the page mid-scan (a real multi-minute
    `claude -p` pass) silently dropped back to the idle button while the scan kept running
    unwatched. This is what a reload polls to reattach instead."""
    with _gc_state_lock:
        return dict(_gc_state)


def gc_start():
    """Kick off `gc_preview` in a background thread and return immediately — `gc_preview` blocking
    the whole HTTP request for its real duration is what left no server-side trace for a refresh
    to find. Refuses to start a second scan while one is already running instead of racing two."""
    with _gc_state_lock:
        if _gc_state["running"]:
            return {"started": False}
        _gc_state["running"] = True
        _gc_state["started_at"] = time.time()
        _gc_state["result"] = None

    def _run():
        try:
            result = gc_preview()
        except Exception as e:  # noqa: BLE001 — surfaced to the UI, not a crashed background thread
            result = {"error": str(e), "candidates": [], "scanned": 0, "verify_skipped": 0}
        with _gc_state_lock:
            _gc_state["running"] = False
            _gc_state["result"] = result

    threading.Thread(target=_run, daemon=True, name="gc-scan").start()
    return {"started": True}


def gc_evict(candidates):
    """Delete confirmed GC candidates — a human reviewed `gc_preview`'s list and picked which to
    remove — and audit the action as one row. Facts go through `delete_fact(..., every=True)`:
    `gc_preview` scans every namespace, not just the global store, so the default-scoped delete
    the Memory tab's per-fact button uses would silently no-op on a project-namespaced fact.
    Returns how many were actually removed."""
    removed = []
    for c in candidates or []:
        store = c.get("store")
        if store == "fact":
            if delete_fact(c.get("id"), c.get("text", ""), every=True):
                removed.append(c)
        elif store == "solution":
            delete_solution(c.get("id"))
            removed.append(c)
        elif store == "behavior":
            delete_behavior(c.get("id"))
            removed.append(c)
    if removed:
        at = datetime.datetime.now().isoformat(timespec="seconds")
        wid = _eng()._next_wid()
        entry = {"at": at, "workflow": wid, "capability": "gc:memory", "risk": "write",
                  "outcome": "ran", "cost_usd": 0, "evicted": len(removed)}
        _append_audit(entry)
        _append_content(wid, at, request="memory garbage collection",
                         result=f"Evicted {len(removed)} item(s).", detail=removed)
        trace("MEMORY", f"gc evicted {len(removed)} item(s)")
    return len(removed)
