"""The audit trail: Otto's immutable run record, plus every engine-owned table's schema.

Extracted from engine.py (which re-exports everything here — callers and tests keep using
engine.X). Two tables it owns outright: `audit` (operational metadata — model, duration, cost,
outcome) and `audit_content` (full request/result text, kept separate so the Audit tab and the
compliance trail aren't dominated by chat-shaped content, correlated by workflow id + `at`/
`attempt`). It also owns _schema/_conn for the shared WAL-mode SQLite db (issue #103) that the
memory/solutions/behaviors stores (still in engine.py) share.
"""
import contextlib
import datetime
import json
import os
import re

import config
import gateway
import registry
import storage
from ui import trace


def _eng():
    """The engine facade. engine.py re-exports this module's API and the ENTIRE test suite
    monkeypatches attributes there (engine._DB, engine.iter_content_entries,
    engine._extract_solution, ...) — so any value or cross-call a test may intercept must be
    resolved through the facade at call time, never bound at import."""
    import engine
    return engine


def audit_repo_changes(wid, request, changed):
    """Audit in-place edits a non-repo-mode run made to a registered repo's LIVE checkout — the
    unsafe path that isolated workspaces are meant to replace (issue #59). Written as a normal
    audit row (so the Audit tab renders it) flagged with `in_place`/`repos`."""
    if not changed:
        return
    os.makedirs(config.DATA_DIR, exist_ok=True)
    names = ", ".join(c["name"] for c in changed)
    summary = ("In-place edit detected — this run modified live checkout(s) OUTSIDE an isolated "
               f"workspace: {names}. Select the repo in the composer to run in a clone + PR instead.")
    at = datetime.datetime.now().isoformat(timespec="seconds")
    entry = {
        "at": at, "workflow": wid,
        "capability": "guard:in-place-edit", "risk": "write", "outcome": "ran",
        "cost_usd": 0, "in_place": True, "repos": [c["name"] for c in changed],
    }
    _append_audit(entry)
    _append_content(wid, at, request=request, result=summary, detail=changed)
    trace("GUARD", f"in-place repo edit flagged: {names}")


def _schema(conn):
    """Every engine-owned table in the shared db (issue #103). `CREATE … IF NOT EXISTS` on each
    connect, so there's no migration step to forget."""
    conn.execute("""CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        workflow TEXT NOT NULL,
        capability TEXT,
        verified INTEGER,
        data TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_workflow ON audit(workflow)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_capability ON audit(capability)")
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_content (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        workflow TEXT NOT NULL,
        attempt INTEGER,
        data TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_content_workflow ON audit_content(workflow)")
    # Learned facts. `namespace` NULL = the global store; a value = one project's namespace (what
    # used to be data/memory/<ns>.json). `data` keeps the whole original event dict so
    # /api/memory's {at, capability, request, facts[]} rows stay byte-identical.
    conn.execute("""CREATE TABLE IF NOT EXISTS memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        namespace TEXT,
        at TEXT,
        capability TEXT,
        data TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_ns ON memory(namespace, id)")
    # Solved-task approaches and behaviour rules: flat closed-key records, so plain columns.
    # `seq` is insertion order — it drives "newest first" and the bounded-store trim.
    conn.execute("""CREATE TABLE IF NOT EXISTS solutions (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        at TEXT,
        capability TEXT,
        request TEXT,
        approach TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS behaviors (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        at TEXT,
        scope TEXT,
        rule TEXT
    )""")


@contextlib.contextmanager
def _conn():
    conn = storage.sqlite_connect(_eng()._DB)
    try:
        _schema(conn)
        yield conn
    finally:
        conn.close()


def _audit_conn():
    conn = storage.sqlite_connect(_eng()._DB)
    _schema(conn)
    return conn


def iter_audit_entries():
    """Every audit entry, oldest first. `data` is the full original entry dict, so this yields
    the exact same shape callers have always gotten from the JSONL-backed version."""
    conn = _audit_conn()
    try:
        rows = conn.execute("SELECT data FROM audit ORDER BY id ASC").fetchall()
    finally:
        conn.close()
    for row in rows:
        yield json.loads(row["data"])


def iter_content_entries():
    """Every audit-content entry (full request/result text), oldest first."""
    conn = _audit_conn()
    try:
        rows = conn.execute("SELECT data FROM audit_content ORDER BY id ASC").fetchall()
    finally:
        conn.close()
    for row in rows:
        yield json.loads(row["data"])


# A post-PR ROUND's own workflow id (`workflows._run_review_loop` / `_run_qa_loop` mint
# "<run>-rev<N>" and "<run>-qa<N>"). Those rounds carry a judge_review/judge_qa verdict on THE
# PR, not a verify verdict on the capability that produced the review — see the `source` note at
# both call sites. Rows written before those loops stamped `verdict_source` have no field to
# read, and this is the correlate that survives in the trail: the same trick as `killed` below,
# and the same rule — retroactive only where the evidence is on the row itself.
# Their FIX rounds ("-revfix<N>", "-fix<N>") deliberately aren't here: they record `verdict:
# None`, so they never reach a verdict filter at all.
_POST_PR_WID = re.compile(r"-(rev|qa)\d+$")


def scorecard(entries):
    """Per-capability reliability aggregates from the audit trail (issue #102) — the evidence
    base for the "cheapest capable model per unit of work" north star and for judging whether a
    local execution model is good enough for a given cap. Groups attempt rows (rows carrying an
    `attempt`) by workflow into RUNS, then rolls runs up per capability: verify pass rate,
    average attempts-to-pass, escalation rate, how often the chosen exec model fell back, and
    per-run cost/token averages. Runs with no verify verdict at all (pure resume/continuation)
    are excluded from the RELIABILITY aggregates — they aren't fresh judged tasks — but they DO
    count in `used`, which answers the plain "how many times has this capability run" and is the
    only figure that exists for a cap whose runs were all resumes. Pure over an iterable so it's
    unit-testable against a fixture trail; keyed on the audit `kind:name` with the bare `name`
    for UI join.

    Only a JUDGE verdict counts toward reliability. A supervisor kill and a harness death are
    both recorded as `verified=False` because the ladder needs a failed-attempt verdict to steer
    on (`judging.error_verdict`), but neither is evidence about the capability — one is Otto
    stopping the run, the other is a timeout. Counting them collapsed three different things into
    one number: measured over the trail, 98 of 291 recorded verify failures had no judge behind
    them, inflating every cap's fail rate — the very number you would tune the judge against."""
    runs, accepted_wids, killed = {}, set(), set()
    for e in entries:
        # A human-override row carries no `attempt` (it isn't an execution) — collect it before
        # the attempt filter drops it, so a failed run a human ACCEPTED can be told apart from
        # one the capability genuinely got wrong.
        if e.get("outcome") == "human_accepted" and e.get("workflow"):
            accepted_wids.add(e["workflow"])
        # Same trick for a supervisor kill, which writes its own outcome row alongside the attempt
        # row. This is what makes the correction retroactive: rows written before `verdict_source`
        # existed carry no source, but their kill row is right there in the trail (43 of the 44
        # historical kills correlate on workflow+attempt). A historical HARNESS death has no such
        # marker and stays miscounted — only `verdict_source` fixes those, going forward.
        if e.get("outcome") == "supervisor_kill" and e.get("workflow"):
            killed.add((e["workflow"], e.get("attempt")))
        if e.get("attempt") is None or not e.get("capability"):
            continue
        runs.setdefault((e["capability"], e.get("workflow")), []).append(e)
    per_cap = {}

    def _agg(cap):
        return per_cap.setdefault(cap, {
            "capability": cap, "name": cap.split(":", 1)[-1], "used": 0, "runs": 0, "passed": 0,
            "escalated": 0, "fell_back": 0, "attempts_sum": 0, "atp_sum": 0, "atp_n": 0,
            "cost_sum": 0.0, "tokens_sum": 0, "models": {}, "recent": [], "last_at": "",
            "accepted": 0})
    # `used` counts every distinct run, judged or not — so a cap that only ever gets resumed still
    # gets a card (with zero reliability figures) instead of vanishing from the table entirely.
    for (cap, _wid), rows in runs.items():
        agg = _agg(cap)
        agg["used"] += 1
        agg["last_at"] = max(agg["last_at"], max((r.get("at") or "") for r in rows))

    def _by_judge(r):
        """True when this attempt's verdict came from the verifier. A row written before
        `verdict_source` existed carries none: assume judge (that WAS the only verdict recorded,
        so history does not silently re-rate itself) unless the trail shows a supervisor kill for
        the same attempt, or the workflow id shows it was a post-PR round."""
        src = r.get("verdict_source")
        if src:
            return src == "judge"
        if _POST_PR_WID.search(r.get("workflow") or ""):
            return False
        return (r.get("workflow"), r.get("attempt")) not in killed

    for (cap, wid), rows in runs.items():
        rows.sort(key=lambda r: r.get("attempt") or 0)
        judged = [r for r in rows if r.get("verified") is not None and _by_judge(r)]
        if not judged:
            continue
        agg = _agg(cap)
        agg["runs"] += 1
        run_attempts = max((r.get("attempt") or 0) for r in rows)
        agg["attempts_sum"] += run_attempts
        passed = any(r.get("verified") is True for r in judged)
        # A run the judges failed and a human then accepted: the JUDGE was wrong, not the cap.
        accepted = not passed and wid in accepted_wids
        if accepted:
            agg["accepted"] += 1
        if passed:
            agg["passed"] += 1
            agg["atp_sum"] += next(r["attempt"] for r in rows if r.get("verified") is True)
            agg["atp_n"] += 1
        first_model, last_model = rows[0].get("model"), rows[-1].get("model")
        if first_model and last_model and first_model != last_model:
            agg["escalated"] += 1
        if any(r.get("fallback_from") for r in rows):
            agg["fell_back"] += 1
        if first_model:
            agg["models"][first_model] = agg["models"].get(first_model, 0) + 1
        run_cost = sum((r.get("cost_usd") or 0) for r in rows)
        run_tok = sum(((r.get("tokens") or {}).get("output") or 0) for r in rows)
        agg["cost_sum"] += run_cost
        agg["tokens_sum"] += run_tok
        at = max((r.get("at") or "") for r in rows)
        agg["recent"].append({"at": at, "cost_usd": round(run_cost, 4), "tokens": run_tok,
                              "attempts": run_attempts, "passed": passed, "model": last_model,
                              "accepted": accepted})
    out = []
    for agg in per_cap.values():
        n = agg["runs"]
        # A used-but-never-judged cap keeps `used` and leaves every reliability figure empty —
        # a 0% pass rate would read as "this capability fails", not "nothing judged it".
        out.append({
            "capability": agg["capability"], "name": agg["name"], "used": agg["used"], "runs": n,
            "pass_rate": round(agg["passed"] / n, 3) if n else None,
            # Runs the judges failed but a human accepted — a low pass_rate with a high
            # false_fails is a JUDGE problem (verify prompt / supervisor), not a cap problem.
            "accepted": agg["accepted"],
            "false_fails": round(agg["accepted"] / (n - agg["passed"]), 3)
                           if n > agg["passed"] else 0.0,
            "avg_attempts": round(agg["attempts_sum"] / n, 2) if n else None,
            "avg_attempts_to_pass": round(agg["atp_sum"] / agg["atp_n"], 2) if agg["atp_n"] else None,
            "escalation_rate": round(agg["escalated"] / n, 3) if n else None,
            "fallback_rate": round(agg["fell_back"] / n, 3) if n else None,
            "avg_cost_usd": round(agg["cost_sum"] / n, 4) if n else None,
            "avg_output_tokens": int(agg["tokens_sum"] / n) if n else None,
            "models": agg["models"], "last_at": agg["last_at"],
            "recent": sorted(agg["recent"], key=lambda r: r["at"])[-12:]})
    out.sort(key=lambda c: (c["used"], c["runs"]), reverse=True)
    return out


_GH_PR_URL = r"https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/\d+"
# Anchored to the exact markers workflows.py appends around a PR *this run itself* opened or
# pushed to (`**Opened draft PR**`/`**Updated PR**`/`(opened by the capability`) — a bare URL
# mention has no anchor and is never matched, since a run's own report routinely cites an
# unrelated PR as context (e.g. "blocked on #451/PR #452") without having opened it.
_PR_URL_RE = re.compile(
    r"(?:\*\*Opened draft PR\*\*|\*\*Updated PR\*\*|\(opened by the capability[^)]*\))"
    rf"[^\n]*?({_GH_PR_URL})")


def pr_url_from_run(wid):
    """The GitHub PR URL a completed run opened or pushed to itself, recovered from its stored
    result text (audit-content log), or None. Lets a resumed repo-mode chat re-provision on the
    EXACT branch that PR lives on when the branch wasn't recorded on the chat — an agent-managed
    cap (e.g. sre-minion) pushed its OWN branch, or the chat predates git_branch capture. Pure
    over the content log (no network); the caller resolves the URL to a branch. Returns the
    freshest URL for the wid (a later QA/fix round mentions the same PR last).

    Only matches a URL anchored to one of workflows.py's own "I opened/updated this PR" markers
    — never a bare URL mentioned in prose (issue: a read-only investigation citing an unrelated
    blocking PR as context was mistaken for "the PR this run opened", sending a follow-up's
    resume onto a different chat's branch entirely)."""
    if not wid:
        return None
    found = None
    for e in _eng().iter_content_entries():
        if e.get("workflow") != wid:
            continue
        for m in _PR_URL_RE.finditer(str(e.get("result") or "")):
            found = m.group(1)
    return found


def _append_audit(entry):
    verified = entry.get("verified")
    conn = _audit_conn()
    try:
        conn.execute(
            "INSERT INTO audit (at, workflow, capability, verified, data) VALUES (?, ?, ?, ?, ?)",
            (entry["at"], entry["workflow"], entry.get("capability"),
             None if verified is None else int(bool(verified)), json.dumps(entry)))
        conn.commit()
    finally:
        conn.close()


def _append_content(wid, at, request=None, result=None, attempt=None, detail=None, critique=None):
    """Full request/result text for one audit row, correlated back to it by workflow id + `at`
    (+ `attempt` when there is one). Also carries the verify `critique` (LLM text — belongs here,
    not in the operational metadata log) so the run-detail view can show WHY an attempt failed.
    Skips the literal 'DENIED' placeholder (already conveyed by the audit row's outcome field)
    and writes nothing at all when there's no real content to keep."""
    entry = {"at": at, "workflow": wid}
    if attempt is not None:
        entry["attempt"] = attempt
    if request:
        entry["request"] = request
    if result and result != "DENIED":
        entry["result"] = str(result)[:50000]
    if detail is not None:
        entry["detail"] = detail
    if critique:
        entry["critique"] = str(critique)[:4000]
    if len(entry) <= 2:
        return
    conn = _audit_conn()
    try:
        conn.execute(
            "INSERT INTO audit_content (at, workflow, attempt, data) VALUES (?, ?, ?, ?)",
            (entry["at"], entry["workflow"], attempt, json.dumps(entry)))
        conn.commit()
    finally:
        conn.close()


def _audit(wid, request, cap, result, cost, attempt=None, verified=None, tokens=None,
           model=None, repo=None, outcome=None, reason=None, needs_human=None, duration_s=None,
           backend=None, fallback_from=None, fallback_reason=None, critique=None,
           verdict_source=None, verdict_model=None):
    at = datetime.datetime.now().isoformat(timespec="seconds")
    entry = {
        "at": at, "workflow": wid,
        "capability": f"{cap.kind}:{cap.name}", "risk": cap.risk,
        # Default outcome is ran/denied; a terminal record (failure, needs-human, budget) passes
        # an explicit outcome so the Needs-you dashboard can find runs that need attention.
        "outcome": outcome or ("denied" if result == "DENIED" else "ran"),
        "cost_usd": cost,
    }
    if reason:
        entry["reason"] = reason
    if needs_human is not None:
        entry["needs_human"] = bool(needs_human)
    # The repo an isolated-workspace run targeted (issue #59) — so the audit shows it.
    if repo:
        entry["repo"] = repo
    # Token usage is the real resource on a subscription; model lets the Audit tab
    # segment tokens by tier (so a cheap cap silently running on Opus stays visible).
    # Normalized to the CANONICAL model id (gateway.model_id): the Claude paths hand over an id
    # and every local path hands over the pool-entry LABEL, so left raw the column holds two
    # namespaces for one model and nothing downstream can group by it. `_audit` is the funnel
    # both loops and both backends already pass through, so it is the one place that can hold
    # the invariant. The label is kept beside it when it differs — it is what the operator
    # actually configured, and it is the join key for `fallback_from` and `record_health`.
    if model:
        entry["model"] = gateway.model_id(model)
        if entry["model"] != model:
            entry["model_entry"] = model
    # Which execution backend ran the attempt, and — when the CHOSEN model couldn't run —
    # what it fell back from and why (the Audit tab surfaces both; user-requested).
    if backend:
        entry["backend"] = backend
    if fallback_from:
        entry["fallback_from"] = fallback_from
        entry["fallback_reason"] = fallback_reason or ""
    if tokens:
        entry["tokens"] = tokens
    # How long the attempt actually took to run — the operational signal that matters more than
    # the chat-shaped output itself.
    if duration_s is not None:
        entry["duration_s"] = round(duration_s, 1)
    if attempt is not None:
        entry["attempt"] = attempt
    if verified is not None:
        entry["verified"] = verified
        # WHO reached the verdict — "judge", or the supervisor/harness impostors a failed attempt
        # is dressed as so the ladder can steer on it. On the entry (not just audit_content, where
        # the critique lives) because `scorecard` reads audit rows only and must be able to tell
        # a judgement about the capability from a run that simply died.
        if verdict_source:
            entry["verdict_source"] = verdict_source
        # WHICH judge reached it. `model` on this row is the model that RAN the attempt; a
        # verdict is a second model's opinion of it, and the trail recorded no way to tell them
        # apart. Without this a false_fail cannot be attributed — a bad judge model and a bad
        # capability leave identical rows.
        # Same normalization as `model` above, and the whole reason it matters: the verify
        # judge's model arrives from `gateway.last()`, which stores the LABEL, so a judge and an
        # executor on the identical model recorded two different strings and false_fails could
        # not be split by judge after all.
        if verdict_model:
            entry["verdict_model"] = gateway.model_id(verdict_model)
    _append_audit(entry)
    _append_content(wid, at, request=request, result=result, attempt=attempt, critique=critique)


def accept_run(wid, request, cap, result="", repo=None):
    """Record a human ACCEPTING a run the automated judges failed (Needs-you → Accept).

    This is the only label Otto can't produce for itself, and until it existed the click was a
    Dismiss — indistinguishable from "stale, hide it". Two consumers: `scorecard` separates a
    capability that failed from a JUDGE that failed (see `false_fails` there), and the run's
    approach joins `solutions` — a human-accepted result is a stronger signal than a verify pass,
    which is the only thing that store took before. `cap` follows record_terminal's contract (a
    `kind:name` string / dict / None) since the caller recovers it from the audit trail, not the
    registry. The audit row is the part that must land: distilling the solution is a memory-tier
    LLM call and is best-effort, so a dead gateway can't lose the acceptance."""
    if isinstance(cap, dict):
        capname, risk = f"{cap.get('kind','?')}:{cap.get('name','?')}", cap.get("risk", "?")
    elif isinstance(cap, str):
        capname, risk = cap, "?"
    else:
        capname, risk = "?:?", "?"
    at = datetime.datetime.now().isoformat(timespec="seconds")
    entry = {"at": at, "workflow": wid, "capability": capname, "risk": risk,
             "outcome": "human_accepted", "cost_usd": 0}
    if repo:
        entry["repo"] = repo
    _append_audit(entry)
    _append_content(wid, at, request=request, result=result)
    trace("ACCEPT", f"{wid} accepted by a human over the automated verdict")
    kind, _, name = capname.partition(":")
    shim = registry.Capability(kind or "?", name or capname, "")
    shim.risk = risk
    try:
        eng = _eng()
        eng._remember_solution(shim, request, eng._extract_solution(request, shim, result))
    except Exception as e:  # noqa: BLE001 - the acceptance is recorded either way
        trace("ACCEPT", f"{wid} solution not distilled: {str(e)[:120]}")
    return {"ok": True}


def record_terminal(wid, request, cap, reason, detail="", repo=None):
    """Write a durable terminal audit row for a run that ended needing a human — a FAILED/dead
    workflow, verify-exhaustion, a QA fail/inconclusive, a budget stop, or a failed delivery.

    This is the ONE durable signal for those states: a closed-failed Temporal workflow can't be
    queried, so the audit log (which the Needs-you dashboard reads) is where 'this needs you'
    lives. `cap` may be a name string, a `{kind,name,risk}` dict, or None (a failure before
    routing) — so this builds the row directly rather than requiring a full Cap object."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    if isinstance(cap, dict):
        capname = f"{cap.get('kind','?')}:{cap.get('name','?')}"
        risk = cap.get("risk", "?")
    elif isinstance(cap, str):
        capname, risk = cap, "?"
    else:
        capname, risk = "?:?", "?"
    at = datetime.datetime.now().isoformat(timespec="seconds")
    entry = {
        "at": at, "workflow": wid, "capability": capname, "risk": risk,
        "outcome": "needs_human", "reason": reason, "needs_human": True,
        "cost_usd": 0,
    }
    if repo:
        entry["repo"] = repo
    _append_audit(entry)
    _append_content(wid, at, request=request, result=detail)
    trace("TERMINAL", f"{wid} needs-human: {reason}")


def run_origin(wid):
    """The request text + capability + repo that started a workflow, recovered from the audit
    trail. A Needs-you card only carries the OUTCOME (result/verified/needs_human) — not what
    kicked the run off — so retrying (or writing a terminal row for) it means reconstructing the
    original ask. The capability/repo live in the `audit` table; the request text lives in
    `audit_content` (the audit table stays pure operational metadata — see _append_content).

    Also returns `reached_run`: whether any attempt under this wid actually ran (outcome "ran",
    _audit's default) — proof the run passed its approval gate (or never needed one), as opposed
    to dying during routing/clarify/planning or being declined. A retry can only skip straight
    back to execution when this is True; there is nothing to reuse otherwise.

    The ONE implementation — server._run_origin and the reaper's general sweep both read this."""
    capname, repo, reached_run = None, None, False
    for e in _eng().iter_audit_entries():
        if e.get("workflow") != wid:
            continue
        if e.get("capability"):
            capname = e["capability"]
        if e.get("repo"):
            repo = e["repo"]
        if e.get("outcome") == "ran":
            reached_run = True
    request = None
    for e in _eng().iter_content_entries():
        if e.get("workflow") != wid:
            continue
        if e.get("request"):
            request = e["request"]
    return request, capname, repo, reached_run


def record_skip(request, cap, reason="DENIED", wid=None):
    """Audit a run that was declined / skipped (no execution).

    `wid` is the run's REAL workflow id. Minting a fresh one here (the old unconditional
    behaviour) filed every denial under an orphan `wf-<hex>-NNNN`, so the row correlated with
    nothing: not the plan preview the human was reading when they declined, not the chat, not
    the board card, not `/api/run/detail`. Seven real declines sat in the trail that way, and
    the previewed runs they belonged to read as silently abandoned. The mint stays as the
    fallback for a caller with no workflow of its own."""
    _audit(wid or _eng()._next_wid(), request, cap, reason, 0)
