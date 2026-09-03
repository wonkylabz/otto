"""The planners: gate-time plan preview + critique, and both plan-then-execute systems.

Extracted from engine.py (which re-exports everything here — callers and tests keep using
engine.X, same facade contract as the other extracted layers). Owns the ordered-steps
decomposition (`plan_steps`/`replan_steps`), the approval gate's read-only plan preview
(`plan_preview`) and its critique judge (`critique_plan`), and the plan-mode runner
(`run_plan` and helpers). `_clipped` lives here; judging.py reaches it through the facade.
"""
import concurrent.futures
import json
import re
import time

import claude_cli
import config
import conventions
import gateway
import local_runtime
import registry
from audit import _audit
from contracts import _invocation, _setting_sources
from memory import _resolve_project
from ui import say, trace


def _eng():
    """The engine facade — tests monkeypatch attributes there (engine._claude,
    engine._run_ladder, engine.replan_steps, ...), so patch-sensitive values and cross-calls
    resolve through it at call time, never bind at import. Same contract as the other
    extracted layers' _eng."""
    import engine
    return engine


# --- Planner: decompose into an ordered chain of atomic steps -------------
# Plan-then-execute mode (design doc 2026-07-16): a STRONG model (Claude) breaks a big task
# into atomic steps a weak LOCAL model can run one at a time, threading outputs forward by
# declared dependency (`needs`/`produces`) — NOT a growing context blob. Distinct from the
# swarm decompose above (independent parallel deliverables across different caps); this is a
# dependency-ordered chain run under ONE executor cap. Kept alongside, gated by config.PLAN_MODE.

_PLAN_PROMPT = (
    "You are a senior PLANNER. A SMALL, WEAK language model (limited context window, easily "
    "derailed) will execute the plan you write, ONE step at a time. It cannot hold the whole "
    "task in its head — so break the request into ATOMIC steps, each completable in a single "
    "focused turn: one file read/edit, one command, one lookup, one small transformation. If a "
    "step would need the executor to juggle more than ~2 prior results at once, split it.\n\n"
    "The executor runs each step in ISOLATION: it sees only the step you write plus the outputs "
    "of the specific earlier steps you declare it depends on. So each step's `goal` must be "
    "self-contained, its `context` must carry any static facts it needs (paths, names, "
    "constraints you already know), and `needs` must list the ids of every earlier step whose "
    "output this step consumes. Do NOT assume the executor remembers anything you didn't wire in.\n\n"
    "The executor is: {executor}\nIts available tools: {tools}\n\n"
    "Reply with ONLY a JSON array (no prose, no code fence) of at most {max_steps} step objects, "
    "in dependency order. Each object:\n"
    '  {{"id": "s1", "goal": "<imperative, what THIS step must achieve>", '
    '"context": "<static facts the executor needs; may be empty>", '
    '"needs": ["<ids of earlier steps whose output this consumes>"], '
    '"produces": "<short name for this step\'s output>", '
    '"done_when": "<crisp condition that means this step succeeded>"}}\n'
    "If the request is genuinely a SINGLE atomic action, return an array with one step.\n\n"
    "Request: {request}"
)


def _extract_step_json(text):
    """Pull the JSON array of steps out of a planner reply that may be wrapped in prose or a
    ```json fence. Accepts a bare array or a {\"steps\": [...]} object. Returns a list, or None
    when nothing parseable is found. PURE."""
    text = gateway._strip_reasoning(text or "")   # a leaked <think> stream never precedes the JSON
    # Prefer a fenced block if present, else the first '[' … last ']' span.
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    span = text[text.find("["):text.rfind("]") + 1] if "[" in text and "]" in text else ""
    if span:
        candidates.append(span)
    candidates.append(text)
    for c in candidates:
        try:
            data = json.loads(c)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            data = data.get("steps")
        if isinstance(data, list):
            return data
    return None


def _toposort(steps):
    """Order steps so every step follows the ones it `needs`. Returns [] if the graph has a
    cycle (can't order → no usable plan). Stable: preserves the planner's order among steps
    with no ordering constraint between them. PURE."""
    by_id = {s["id"]: s for s in steps}
    indeg = {s["id"]: sum(1 for n in s["needs"] if n in by_id) for s in steps}
    deps = {sid: [] for sid in by_id}
    for s in steps:
        for n in s["needs"]:
            if n in by_id:
                deps[n].append(s["id"])
    queue = [s for s in steps if indeg[s["id"]] == 0]   # original order preserved
    out, seen = [], set()
    while queue:
        s = queue.pop(0)
        if s["id"] in seen:
            continue
        seen.add(s["id"])
        out.append(s)
        for dep in deps[s["id"]]:
            indeg[dep] -= 1
            if indeg[dep] == 0:
                queue.append(by_id[dep])
    return out if len(out) == len(steps) else []


def _parse_steps(text, max_steps=None, available=None):
    """Parse the strong planner's reply into an ordered list of normalized step dicts. PURE (no
    LLM) so it's unit-testable. Each step becomes
    {id, goal, context, needs, produces, done_when}. Returns a topologically-ordered list, or
    [] when the reply is unparseable/empty or its dependency graph has a cycle — [] means 'no
    usable plan' and the caller falls through to single-turn execution (mirrors _parse_plan).
    Lenient like _parse_plan: a bad step is dropped, not fatal; a `needs` id that references no
    known step is dropped (never dangles). `available` is the set of ALREADY-COMPLETED step ids
    (from a re-plan): a new step may reference them in `needs` (their outputs are in the store),
    and a new step reusing a completed id is dropped as a duplicate (never shadows a done step)."""
    max_steps = config.PLAN_MAX_STEPS if max_steps is None else max_steps
    available = available or set()
    raw = _extract_step_json(text)
    if not isinstance(raw, list):
        return []
    steps, ids = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal") or "").strip()
        if not goal:
            continue
        sid = str(item.get("id") or "").strip() or f"s{len(steps) + 1}"
        if sid in ids or sid in available:    # duplicate / shadows a completed step → drop
            continue
        ids.add(sid)
        needs = [str(n).strip() for n in (item.get("needs") or []) if str(n).strip()]
        steps.append({
            "id": sid,
            "goal": goal,
            "context": str(item.get("context") or "").strip(),
            "needs": needs,
            "produces": str(item.get("produces") or "").strip() or sid,
            "done_when": str(item.get("done_when") or "").strip(),
        })
        if len(steps) >= max_steps:
            break
    known = ids | available
    for s in steps:                           # drop dangling / self references (external kept)
        s["needs"] = [n for n in s["needs"] if n in known and n != s["id"]]
    return _toposort(steps)


def plan_steps(request, cap, force_claude=True):
    """Strong-model PLANNER for plan-then-execute mode: decompose `request` into an ordered
    chain of atomic steps the (weak, local) executor `cap` can run one at a time. Returns the
    ordered step list, or [] meaning 'not worth planning' (0/1 step) — the caller then runs a
    single execution turn as today (mirrors decompose() returning []). Pinned to Claude by
    default (force_claude): a capable model must write the plan the cheap executor follows."""
    tools = config.WRITE_TOOLS if getattr(cap, "risk", "read") == "write" else config.READ_TOOLS
    executor = f"{cap.name} — {cap.description[:400]}" if cap else "a general-purpose agent"
    prompt = _PLAN_PROMPT.format(
        request=request, executor=executor, tools=", ".join(tools),
        max_steps=config.PLAN_MAX_STEPS)
    try:
        text = gateway.plan_complete(prompt) if force_claude else gateway.complete("plan", prompt)
    except Exception as e:  # noqa: BLE001 - planner unusable -> fall through to single-turn
        trace("PLAN", f"planner call failed ({e}); no plan -> single-turn")
        return []
    steps = _parse_steps(text)
    if len(steps) < 2:
        trace("PLAN", "single atomic task -> no plan (single-turn execution)")
        return []
    trace("PLAN", f"planned {len(steps)} atomic steps: " + " -> ".join(s["id"] for s in steps))
    return steps


_REPLAN_PROMPT = (
    "You are re-planning a multi-step task after a step FAILED verification. A SMALL, WEAK model "
    "executes the steps one at a time; a failed step usually means it was too big or "
    "under-specified for that executor. Repair the REST of the plan: return the steps to run "
    "NEXT, breaking the failed work into smaller/clearer atomic pieces, inserting any missing "
    "prerequisite, and re-ordering what remains. Keep the same atomic granularity rules as "
    "before (each step: one focused action, self-contained, with its dependencies wired via "
    "`needs`).\n\n"
    "The executor is: {executor}\nIts available tools: {tools}\n\n"
    "TASK: {request}\n\n"
    "ALREADY COMPLETED (their outputs are available — reference their ids in `needs` to reuse "
    "them; do NOT redo them):\n{done}\n\n"
    "THE STEP THAT FAILED:\n{failed}\nVerifier's critique of why it failed:\n{critique}\n\n"
    "STILL PENDING (were planned but not yet run):\n{pending}\n\n"
    "Reply with ONLY a JSON array (no prose, no fence) of at most {max_steps} NEW step objects, "
    "in dependency order, each: "
    '{{"id":"r1","goal":"...","context":"...","needs":["<new or completed step ids>"],'
    '"produces":"...","done_when":"..."}}. Use FRESH ids (not any completed id). If the task '
    "cannot be salvaged, return an empty array []."
)


def _step_digest(step, result=None, limit=None):
    """One-line-ish digest of a step for a re-plan prompt: its id + goal, and (when given) a
    truncated output. Keeps the re-plan context bounded."""
    line = f"- [{step['id']}] {step['goal']}"
    if result is not None:
        body, cut = _clipped_input(result or "(no output)",
                                   limit or config.PLAN_ARTIFACT_CHARS)
        line += f"\n    output: {body}{cut}"
    return line


def replan_steps(request, cap, done, failed_step, critique, pending):
    """Re-plan the REMAINING tail after a step exhausted its verify ladder (design decision #3:
    retry, don't fail). The strong planner (Claude) sees the goal, the COMPLETED steps (+ their
    outputs, so it can reference/reuse them), the failed step + the verifier's critique, and what
    was still pending; it returns a repaired tail — it may subdivide the failed step, insert a
    prerequisite, or reorder. Completed step ids are `available` so the new tail can wire `needs`
    to their outputs. Returns the new ordered step list, or [] if no usable repair (caller then
    stops for a human). Pinned to Claude (the repair needs the capable model just like the plan)."""
    completed_ids = {c["step"]["id"] for c in done}
    done_block = "\n".join(_step_digest(c["step"], c["result"]) for c in done) or "  (none yet)"
    pending_block = "\n".join(_step_digest(s) for s in pending) or "  (none)"
    tools = config.WRITE_TOOLS if getattr(cap, "risk", "read") == "write" else config.READ_TOOLS
    executor = f"{cap.name} — {cap.description[:400]}" if cap else "a general-purpose agent"
    prompt = _REPLAN_PROMPT.format(
        request=request, executor=executor, tools=", ".join(tools),
        done=done_block, pending=pending_block,
        failed=_step_digest(failed_step), critique=(critique or "(no critique)")[:1500],
        max_steps=config.PLAN_MAX_STEPS)
    try:
        text = gateway.plan_complete(prompt)
    except Exception as e:  # noqa: BLE001 - planner unusable -> no repair, stop for a human
        trace("PLAN", f"re-plan call failed ({e}); no repair")
        return []
    tail = _parse_steps(text, available=completed_ids)
    trace("PLAN", f"re-plan produced {len(tail)} new step(s)")
    return tail


_PLAN_INSTRUCTION = (
    "\n\n--- IMPORTANT: this is a PLAN PREVIEW, not execution. Do NOT make any changes or do "
    "anything with side effects. Only inspect as needed — read files, search, and view any "
    "referenced GitHub issue/PR with `gh issue view` / `gh pr view` / `gh pr diff` (the only "
    "commands available to you; read the ticket rather than planning blind) — then describe "
    "what you WOULD do.\n\n"
    "Produce a concrete, numbered plan of the operations you would perform: the specific files "
    "you'd create or edit, the commands you'd run, and any external effects (commits, pushed "
    "branches, PRs, comments, API calls, deployments). Name actual files/commands where you can.\n\n"
    "Order the steps the way they must actually HAPPEN, not the order you thought of them:\n"
    "- Resolve the load-bearing UNKNOWN first. If one unverified fact decides whether the "
    "approach works at all, finding out is step 1 — never a verification step at the end.\n"
    "- A step that changes behaviour for existing callers, traffic, or data comes AFTER the "
    "steps that make it safe. Say what must be TRUE before each such step (observability in "
    "place, every caller confirmed migrated, a proven rollback). If the change enforces, "
    "restricts, denies, deletes, or migrates, phase it — observe first, enforce once the "
    "observation says it's safe. Prefer more steps over a shorter plan that lands enforcement "
    "before evidence.\n"
    "- Blast radius: for anything you'd change, restrict, or replace, name what ELSE currently "
    "depends on it and how the change avoids breaking them. The dependants that bite are the "
    "non-human ones nobody lists — a metrics scrape, a health check or liveness probe, a "
    "synthetic monitor, a cron or batch job, a CI pipeline, a cache warmer — plus other "
    "environments and callers outside the code you're editing. If a step rejects, restricts or "
    "removes something, say what reaches it that was never going to satisfy the new condition. "
    "Scope the change to what it's meant to affect rather than assuming nothing else is there.\n"
    "- If you copy or mirror something that already exists, read the original and say which parts "
    "must CHANGE in the copy — what made it correct where it is (a scope, a path, a name, an "
    "assumption about its caller) is usually what makes the copy a silent no-op elsewhere.\n"
    "- If the request or ticket has acceptance criteria, every one of them must be covered by a "
    "step, including the non-code ones (telling owners, checking an unrelated consumer is "
    "unaffected). List any you are deliberately leaving out.\n\n"
    "Keep it as short as the work honestly allows — no filler steps — but do not drop a phase to "
    "hit a length. After the numbered plan, add a final short section headed 'Risks & "
    "assumptions': what you could not verify from here and what the plan would break if you got "
    "it wrong.\n\n"
    "Write the numbered plan itself in your REPLY, in full, and make it the LAST thing you say. "
    "A human reads your final message verbatim on an approval screen and has nothing else — so "
    "do not save the plan to a file and summarise it, do not describe the plan's structure "
    "instead of giving it, do not refer to a document you wrote elsewhere, and do not delegate "
    "this to a sub-agent or keep working after the plan (a later message REPLACES the plan as "
    "your result, and the human then approves a note about the plan instead of the plan). "
    "The reply is the plan. Nothing before the first step and nothing after the risks.")


def _local_preview(invocation, resume_session, cwd, effort=None):
    """The plan preview for a session the LOCAL backend minted. Returns (out, model, backend) in
    `_claude`'s shape, so the caller's accounting, audit row and empty-plan fallback are unchanged.

    Two things the Claude path gets for free and this one has to build:

    * `--permission-mode plan` has no local equivalent, so read-only rests on the tool set alone.
      config.PLAN_TOOLS narrows to Read/Grep/Glob here — `_offered_tools` matches literal tool
      names, so the three scoped `Bash(gh … view:*)` rules are simply never offered, which is the
      right way round: this runtime's Bash is NOT permission-scoped (`_deny_guard` covers
      Write/Edit only), so an unscoped one before approval would be worse than none.
    * print-mode `--resume` COPIES a session forward; this runtime resumes in place and saves the
      turn back. So preview against a throwaway fork — otherwise the plan instruction and the
      plan itself land in the history the approved run resumes next.

    No fork (a swept or empty session file) means there is no conversation to inherit, and a
    context-free preview of a raw follow-up is the nonsense plan the gate must not show — return
    no plan and let the caller fall back to displaying the invocation."""
    entry = local_runtime.resume_entry(resume_session)
    if not entry:
        # The pool has no local model left. NEVER fall through to `claude -p --resume` with a
        # `local-` id: that is the failure this whole branch exists to stop.
        trace("PLAN", "no local model in the pool for this session — no preview")
        return {"result": "", "is_error": True}, None, None
    fork = local_runtime.fork_session(resume_session)
    if not fork:
        trace("PLAN", f"local session {resume_session} has no history to preview against")
        return {"result": "", "is_error": True}, entry.get("name"), "local"
    trace("PLAN", f"preview on the local backend ({entry.get('name')}) — session fork {fork}")
    try:
        out = local_runtime.run_json(invocation, allowed_tools=config.PLAN_TOOLS,
                                     model_entry=entry, timeout=900, resume_session=fork,
                                     cwd=cwd, effort=effort)
    finally:
        local_runtime.drop_session(fork)
    return out, entry.get("name"), "local"


def _pr_branch_note(pr):
    """What to tell a planner whose working tree does NOT contain the code it must plan against.

    The preview runs BEFORE provisioning, from the repo's live checkout (`plan_capability`), so
    for a request about an open PR it reads the default branch — measured on `web-a6122d6c`,
    where 909s of preview (the full 15-minute ceiling) went into planning a change to
    `infra_stop_weights_agent`, which does not appear anywhere in that tree. Execution gets the
    right branch; the planner cannot, without cloning before the human has approved anything.

    So point it at the diff instead. `config.PLAN_TOOLS` already grants `gh pr view` and
    `gh pr diff`, which is the whole content of that branch, read-only, at no extra plumbing."""
    if not pr or not pr.get("number"):
        return ""
    return (
        f"\n\nIMPORTANT — THE CODE THIS REQUEST IS ABOUT IS NOT IN YOUR WORKING DIRECTORY.\n"
        f"It lives on open pull request #{pr['number']} (branch `{pr.get('branch')}`), while the "
        f"directory you are reading is this repo's DEFAULT branch. Do not conclude the code is "
        f"missing, already fixed, or different from what the request describes — you are simply "
        f"looking at the wrong branch.\n"
        f"Read the actual code with `gh pr diff {pr['number']}` (and `gh pr view "
        f"{pr['number']}` for its description), and plan against THAT. The run this plan is for "
        f"will execute on branch `{pr.get('branch')}`, so write the plan as it applies there.\n")


def plan_preview(request, cap, cwd=None, resume_session=None, wid=None, pr=None, effort=None):
    """Pre-approval dry run: a STRICTLY read-only agentic pass that returns a concrete,
    numbered plan of the operations the capability WOULD perform — so the human approves the
    actual operations, not just the capability name (the gate otherwise fires before any
    operation is known). Runs with config.PLAN_TOOLS (no Edit/Write; Bash only as scoped
    read-only gh issue/PR views, so ticket-driven tasks plan from the real ticket), so it
    cannot mutate anything before approval — even with `cwd` at a live checkout. Best-effort: the
    returned `plan` is "" if it produces nothing usable (the gate then falls back to showing the
    invocation). Returns {plan, cost, tokens} — cost/tokens so the caller can count this pass's
    real spend against the run's budget, same shape run_capability's output uses.

    With resume_session (a follow-up that escalated a read session into a write, e.g. "add
    important findings only as inline comments" after a PR review), --resume the session and
    send the raw follow-up — mirroring run_attempt's resume invocation — so the preview inherits
    the conversation context (which PR, its findings). Without it the preview runs context-free
    and the gate surfaces a nonsense plan asking "which PR?" mid-conversation. Plan mode forks a
    throwaway read-only session (print-mode --resume copies forward), so the real run's later
    resume of the same base session is unaffected.

    A resume follows the session's BACKEND, exactly as run_attempt's does. `claude -p --resume`
    rejects a `local-` id outright ("is not a UUID and does not match any session title"), so
    previewing a local session on Claude spent ~1.5s to come back is_error and the write gate
    rendered with NO plan on it at all — 0 tokens, $0 (web-ce430e45, 2026-08-25) — which reads
    exactly like a preview that decided there was nothing to do.

    `effort` runs the preview at the same level the execution it previews will run at — the plan a
    human approves must be the plan the run then follows, and a max-effort run planned at the
    default effort is approving a weaker plan than the one that would be produced."""
    cwd = cwd or getattr(cap, "cwd", None)
    effort = config.effort_level(effort if effort is not None else config.setting("effort"))
    invocation = ((request if resume_session else _invocation(cap, request))
                  + _pr_branch_note(pr) + _PLAN_INSTRUCTION)
    trace("PLAN", f"preview for [{cap.kind}] {cap.name}  cwd={cwd or '-'}"
                  f"{' (resume)' if resume_session else ''}")
    # 900s (15min): raised from 600s after a real ticket (ci#66) timed out at the old
    # ceiling with nothing to show for it — `plan_capability`'s activity timeout must stay above
    # this (workflows.py) or the activity kills the preview before it ever gets to return "".
    if local_runtime.is_local_session(resume_session):
        out, model, backend = _local_preview(invocation, resume_session, cwd, effort=effort)
    else:
        # The PREVIEW tier, not the execution model. Reading the execution assignment here made
        # the phase's model a side effect of a different setting — and, when that setting was a
        # local model, silently the cheapest Claude in the pool (see gateway.TASKS).
        model, backend = gateway.preview_model_id(), None
        out = _eng()._claude(invocation, allowed_tools=config.PLAN_TOOLS, model=model, cwd=cwd,
                  timeout=900, permission_mode="plan", resume_session=resume_session,
                  setting_sources=_setting_sources(cwd), effort=effort,
                  # The preview is a real agentic pass; capture it like an attempt. Without a
                  # transcript the board's model chip has nothing to read and stays blank for
                  # the entire (up to 15-minute) phase, and the tool calls it makes to reach a
                  # plan cannot be reviewed afterwards at all.
                  transcript=claude_cli.plan_transcript_path(wid) if wid else None)
    cost = out.get("total_cost_usd", 0) or 0
    tokens = _eng()._usage(out)
    # The preview is a full agentic pass, so its spend must hit the audit trail / /api/costs
    # like any attempt — it was previously invisible, which read as "phases are local, where do
    # my Claude credits go?". Best-effort; never blocks the gate.
    try:
        _audit(wid or "preview", request, cap, "", cost, outcome="plan_preview", model=model,
               tokens=tokens, backend=backend)
    except Exception:  # noqa: BLE001 - accounting must never break the approval gate
        pass
    # A FAILED pass has no plan, whatever text came back with it. `claude_cli` reports a timeout /
    # crash / abort as is_error with a sentinel result ("(timed out)", a stderr tail), and those
    # used to reach the gate rendered under "Planned operations" — which reads as a plan that says
    # the run will time out, rather than as a preview that didn't happen. Falling back to "" gets
    # the honest "no preview, here's the invocation" gate the docstring promises.
    plan = str(out.get("result", "")).strip()
    if out.get("is_error") or plan == "(no output)":
        trace("PLAN", f"no usable preview — {plan[:80] or 'empty'}")
        plan = ""
    # {plan, cost, tokens}: cost/tokens let the caller (the Temporal workflow) count this pass's
    # spend against the run's budget, same shape `_account`/`run_capability` outputs already use.
    return {"plan": plan, "cost": cost, "tokens": tokens, "model": model}


_PLAN_CONCERN_CAP = 5
# How much of the plan/request the critic reads. Generous on purpose: the plan instruction now
# asks for phases, blast radius and a risks section, so a real plan runs long — the first live
# one was 9358 chars against the old 6000 cap. A cut plan doesn't just hide findings, it MANUFACTURES
# one: the critic faithfully reported "the plan text cuts off mid-step 9" as a defect, which was
# true of its input and false of the plan (steps 9-12 and the whole risks section existed). Judging
# a few thousand extra chars on the verify tier is far cheaper than a false concern at a gate.
_PLAN_CRITIQUE_CHARS = 24_000
_PLAN_REQUEST_CHARS = 6_000
# How much of ONE step's output the final synthesis reads.
_SYNTH_STEP_CHARS = 3_000


def _clipped_input(text, limit):
    """(text, note) for a prompt that FEEDS work — a prior step's output, a digest — rather than
    one that judges it. Same contract as `_clipped` and the same reason (an unmarked cut is read
    as the source's own content), but the instruction is the opposite: a judge must ignore what
    it cannot see, whereas an executor must NOT silently build a count, a diff or a "complete"
    list on top of a hole. Both failure modes were live: a step handed a Helm table cut mid-row
    either reported a confident total over the visible rows or refused the whole step, and the
    verifier then failed it for a truncation Otto itself introduced."""
    text = text or ""
    if len(text) <= limit:
        return text, ""
    return (text[:limit],
            "\n\n[CUT at {} of {} characters — the rest was NOT passed to you. What is missing is "
            "UNKNOWN, not absent: do not present a total, a diff or a complete list built on this "
            "without stating plainly which part you could not see.]".format(limit, len(text)))


def _clipped(text, limit):
    """(text, note) for a judge prompt — the text bounded to `limit`, plus an explicit marker when
    it was actually cut, so a truncation can never be read as the source's own defect."""
    text = text or ""
    if len(text) <= limit:
        return text, ""
    return (text[:limit],
            "\n\n[TRUNCATED FOR REVIEW at {} of {} characters — the rest was NOT shown to you. "
            "Do NOT report the cut-off, missing later steps, or anything you cannot see as a "
            "finding; judge only what is above.]".format(limit, len(text)))


def _parse_plan_concerns(text):
    """Pull the concern list out of the plan critic's reply. Pure (unit-testable). Accepts the
    `- ` bullets the prompt asks for, tolerates `*`/`1.` bullets and a stray heading, and treats
    a reply that is (or begins) NONE as clean. Returns at most _PLAN_CONCERN_CAP strings.

    Biased toward returning NOTHING when unsure: these render as a warning block on the approval
    gate, so prose the model wrapped around a clean verdict must not become a fake concern. A
    missed concern costs what the critic exists to catch; a fabricated one trains the human to
    skip the block, which costs every future concern."""
    text = gateway._strip_reasoning(str(text or "")).strip()
    if not text:
        return []
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        stripped = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)
        if stripped == line:                          # not a bullet — heading or prose, skip
            if re.match(r"^none\b", line, re.I):
                return []
            continue
        stripped = stripped.strip()
        if re.match(r"^none$", stripped, re.I):
            continue
        if len(stripped) > 8:
            out.append(stripped)
        if len(out) >= _PLAN_CONCERN_CAP:
            break
    return out


def critique_plan(request, cap, plan, project=None):
    """Judge the PLAN PREVIEW before the human approves it, and return {concerns: [str]} to
    surface on the gate. The plan was the one artefact in Otto that nothing reviewed: an attempt
    gets verify -> retry -> escalate, a PR gets the review and QA loops, but the plan was
    generated once, best-effort, and rendered raw for the human to catch problems in unaided.

    What this is looking for is specifically what a competent plan hides — it is not a second
    opinion on the approach. A faithful, well-written plan can still land enforcement before the
    callers it enforces on are ready, copy a scoped rule into a context where it matches nothing,
    or restrict a port that something else (a metrics scrape, a health check) is quietly using.
    Those read as fine in a numbered list and only bite on apply, which is why a fresh reader
    asked "what breaks?" catches them when the planner writing the list did not.

    The critic is told the planner ran READ-ONLY and could not reach live systems (no cluster,
    cloud, or metrics access — config.PLAN_TOOLS is Read/Grep/Glob plus scoped `gh` views), so
    "confirm X live before the enforcing step" is a legitimate concern while "you failed to check
    the live cluster" is not a defect. `project` injects the target repo's own CLAUDE.md the same
    way verify() does.

    Advisory only, and deliberately so: it never blocks, never edits the plan, and never gates —
    the human still approves or declines. Best-effort, so a dead gateway costs the warning block
    and nothing else."""
    plan = (plan or "").strip()
    if not plan:
        return {"concerns": []}
    trace("PLAN", f"critiquing plan for [{cap.kind}] {cap.name}")
    conv = conventions.judge_block(project, request) if project else None
    plan_text, plan_note = _clipped(plan, _PLAN_CRITIQUE_CHARS)
    req_text, req_note = _clipped(request, _PLAN_REQUEST_CHARS)
    try:
        text = gateway.complete(
            "verify",
            "You are reviewing an implementation PLAN before a human approves it for execution. "
            "The plan has NOT run yet. Your job is to find what would go WRONG if it ran exactly "
            "as written — not to propose a better approach, restate the plan, or comment on "
            "style. Assume the author is competent; the failures worth catching are the ones a "
            "plausible-looking plan hides.\n\n"
            + (conv + "\n\n" if conv else "")
            + f"Capability that would execute it: {cap.name} ({cap.description[:160]})\n\n"
            "The plan was written by a READ-ONLY preview pass with no access to live systems — it "
            "could read the repo and the linked ticket, but not a cluster, cloud account, "
            "dashboard, or traffic data. So 'the plan should confirm X live before the step that "
            "depends on it' is a valid concern; 'the plan did not inspect production' is not.\n\n"
            "Report ONLY things that are blocking or significant:\n"
            "- a step that changes behaviour for existing callers, traffic, or data before the "
            "step that makes it safe (enforcement, denial, deletion, or migration landing ahead "
            "of the observation or migration that would prove it safe)\n"
            "- collateral damage: something that depends on what is being changed or restricted "
            "and is not accounted for. Weight the dependants a plan author forgets over the ones "
            "it already names — non-human callers above all (a metrics scrape, a health check or "
            "liveness probe, a synthetic monitor, a cron or batch job, a CI pipeline, a cache "
            "warmer, a webhook retry), plus other environments, and callers living outside the "
            "code being edited. Ask specifically: if this rejects, restricts or removes something, "
            "what reaches it that was never going to satisfy the new condition?\n"
            "- a step that would silently do NOTHING as written, because what made the original "
            "correct (a scope, a path, a name, an assumption about its caller) didn't carry\n"
            "- an unverified assumption the whole approach rests on, resolved late or not at all\n"
            "- a stated requirement or acceptance criterion no step covers\n"
            "- an irreversible or hard-to-undo step with no rollback\n\n"
            f"Request:\n{req_text}{req_note}\n\n"
            f"Plan:\n{plan_text}{plan_note}\n\n"
            "Reply with the single word NONE if you find nothing blocking or significant. "
            f"Otherwise reply with at most {_PLAN_CONCERN_CAP} lines, each starting with '- ', "
            "most serious first. Each line: what breaks, and what the plan should do instead. Be "
            "specific and name the step. No preamble, no closing remarks, nothing else.",
        )
    except Exception as e:  # noqa: BLE001 - the critic must never break the approval gate
        trace("PLAN", f"critique unavailable — {e}")
        return {"concerns": []}
    concerns = _parse_plan_concerns(text)
    trace("PLAN", f"{len(concerns)} concern(s)" if concerns else "no blocking concerns")
    return {"concerns": concerns}


# --- Plan-then-execute runner (design doc 2026-07-16) ---------------------

def plan_mode_active(cap, requested=False):
    """Should this run use plan-then-execute (Claude plans atomic steps, local executes)?
    Gated by config.PLAN_MODE so it's off unless deliberately enabled:
      off        -> never
      opt-in     -> only when the run explicitly requested it (requested=True)
      auto-local -> requested, OR the resolved executor for `cap` is a LOCAL model (the natural
                    gate — plan-then-execute exists to make weak local executors handle big tasks;
                    a Claude executor doesn't need it, so it stays invisible there)."""
    mode = config.setting("plan_mode")
    if mode == "off":
        return False
    if requested:
        return True
    if mode == "auto-local":
        return gateway.exec_model_entry(cap.name).get("provider") != "claude"
    return False


def _step_prompt(request, step, store):
    """Assemble ONE step's prompt with SELECTIVE dependency-scoped injection (design decision
    #1): a tiny always-present goal header (the overall task), this step's own goal/context/
    done-condition, and ONLY the outputs of the steps it declared in `needs` — each truncated
    to config.PLAN_ARTIFACT_CHARS. Deliberately NOT a growing "context so far" blob: weak models
    have small windows and are hurt more by irrelevant context than helped by completeness."""
    out = [f"You are executing ONE step of a larger task. Do ONLY this step — not the whole task.",
           f"\nOVERALL TASK (for orientation only): {request}",
           f"\n\nYOUR STEP [{step['id']}]: {step['goal']}"]
    if step.get("context"):
        out.append(f"\nContext: {step['context']}")
    for nid in step["needs"]:
        prior = store.get(nid)
        if prior:
            body, cut = _clipped_input(prior, config.PLAN_ARTIFACT_CHARS)
            out.append(f"\n\n--- Output of prior step {nid} (use this) ---\n{body}{cut}")
    if step.get("done_when"):
        out.append(f"\n\nThis step is complete when: {step['done_when']}")
    out.append("\n\nReport this step's concrete output only.")
    return "".join(out)


def _synthesize_plan(request, results):
    """Fold the per-step outputs of a plan into ONE coherent answer (verify tier, like the swarm
    merge). `results` is a list of {step, result, passed, superseded}. SUPERSEDED steps (a failed
    step whose work a re-plan redid) are omitted — they're not part of the delivered outcome.
    Never claims success when a non-superseded step failed."""
    live = [c for c in results if not c.get("superseded")]
    if not live:
        return "(plan produced no steps)"
    if len(live) == 1 and live[0]["passed"]:
        return live[0]["result"] or "(no output)"
    def _block(i, c):
        # f-string all the way down: a step goal or output legitimately contains "%" ("cut spend
        # by 20%"), and %-formatting a string that already interpolated it raises at runtime.
        body, cut = _clipped_input(c["result"] or "(no output)", _SYNTH_STEP_CHARS)
        flag = "" if c["passed"] else " — FAILED verification"
        return (f"### Step {i + 1} [{c['step']['id']}]{flag}\n"
                f"Goal: {c['step']['goal']}\nOutput:\n{body}{cut}")

    blocks = "\n\n".join(_block(i, c) for i, c in enumerate(live))
    failed = [c for c in live if not c["passed"]]
    text = gateway.complete(
        "verify",
        "An agent executed a multi-step plan for one task, one step at a time. Synthesise the "
        "step outputs into a SINGLE coherent answer for the user — don't just concatenate. "
        "Preserve every concrete outcome (IDs, links, statuses, numbers). If a step failed, "
        "state plainly what was and wasn't accomplished; do NOT claim the task is complete.\n\n"
        f"Original task: {request}\n\n{blocks}",
    ).strip()
    if failed:
        text += ("\n\n⚠️ Plan incomplete — a step failed verification and could not be repaired. "
                 "This needs human review.")
    return text or "(plan synthesis produced no output)"


def _run_plan_step(request, step, store, cap, wid, project, model_override=None,
                   write_escalate=False):
    """Run ONE plan step through the verify->retry->escalate ladder. Reads only the `store`
    snapshot handed to it (the outputs of its already-completed `needs`), so it holds no loop
    state and is safe to call CONCURRENTLY for independent steps in the same dependency wave.
    `cap`/`project` are this STEP's executor (run_plan resolves a per-step `cap` name up front),
    not necessarily the run's."""
    step_req = _step_prompt(request, step, store)
    return _eng()._run_ladder(step_req, cap, f"{wid}-{step['id']}", recall=False,
                       project=project, remember=False, write_escalate=write_escalate,
                       model_override=model_override)


def _plan_step_caps(steps, cap, project, resolve_cap):
    """Resolve each step's executor ONCE, up front: {step_id: (cap, project)}.

    A step may name its own capability (`step["cap"]`) — that's what makes a human-authored
    runbook able to span caps ("renew the cert" then "verify the cluster"), where an LLM-authored
    plan runs every step on the one routed cap. Resolution happens BEFORE any step runs and an
    unresolvable name aborts the whole plan (raises), because failing at step 7 of 8 has already
    spent the money and half-applied the work; a cap that was deleted or disabled since the
    runbook was saved must stop it at the door. Returns None if every step uses the run's cap."""
    named = [s for s in steps if (s.get("cap") or "").strip()]
    if not named or not resolve_cap:
        return None
    out, missing = {}, []
    for s in named:
        name = s["cap"].strip()
        c = resolve_cap(name)
        if c is None:
            missing.append(f"[{s['id']}] {name}")
            continue
        out[s["id"]] = (c, _resolve_project(c))
    if missing:
        raise ValueError("step(s) name a capability that is unavailable (deleted, disabled, or "
                         "renamed since this was saved): " + ", ".join(missing))
    for s in steps:                       # steps with no `cap` of their own run on the run's cap
        out.setdefault(s["id"], (cap, project))
    return out


def run_plan(request, cap, steps, wid=None, project=None, model_override=None,
             replan=True, resolve_cap=None):
    """Execute a plan (from plan_steps, or authored by a human as a runbook) on executor `cap`,
    threading each step's output forward to the steps that declared it in `needs`. Every step runs
    through the SAME verify->retry->escalate ladder as a normal run. When a step EXHAUSTS its
    ladder we RE-PLAN the remaining tail (design decision #3: retry, don't fail) — bounded by
    config.PLAN_MAX_REPLANS — preserving completed work; only an unrepairable failure (budget
    spent, or the planner can't salvage it) stops the plan and surfaces for a human. The write gate
    is applied ONCE by the caller — steps inherit the approved cap.

    **`replan=False` for a HUMAN-AUTHORED graph** (a runbook): re-planning rewrites the tail with
    an LLM, which is right for a plan an LLM wrote and wrong for one a person wrote deliberately —
    silently substituting different steps for the ones they authored is a violation of intent, and
    the re-plan prompt's whole framing ("the executor is weak, the step was too big") doesn't
    apply. A failed step then just fails the plan for a human. Because that removes the only
    recovery path, `write_escalate` is turned back ON when replan is off — with no tail repair
    coming, escalating the model IS the recovery.

    `resolve_cap(name) -> cap|None` enables PER-STEP capabilities: a runbook step may name its own
    cap, so one graph can span several ("renew the cert", then "verify the cluster"). Resolved up
    front by `_plan_step_caps`; an unresolvable name raises before anything runs.

    WAVE-PARALLEL: the plan is toposorted, so we walk it in dependency WAVES — every pending step
    whose `needs` are already satisfied runs together (up to config.PLAN_MAX_PARALLEL at once) in
    an activity thread pool over the blocking `claude -p`/local-runtime turns. A linear plan
    degrades to one step per wave (identical to the old sequential path). Results are integrated in
    PLAN order (not completion order) so synthesis stays deterministic. A wave with a SINGLE failure
    re-plans as before; a wave with MULTIPLE simultaneous failures is treated as unrepairable-in-
    parallel and surfaced for a human (safe, occasionally pessimistic — one failure per wave is the
    common case and is byte-identical to the sequential path).

    Returns a dict: {result (synthesized), passed (no unrecovered failure), cost, tokens,
    steps_run, replans, budget_stop}. `passed=False` (or budget_stop) tells the caller to route
    the run to needs-human. Budget is enforced BETWEEN waves (per-run, like the single-task loop)."""
    if not wid:
        wid = _eng()._next_wid()
    store, results = {}, []
    pending = list(steps)
    replans, total_cost, spent_out, budget_stop = 0, 0, 0, False
    strict_stop = auth_stop = False
    auth_wall = None
    max_par = max(1, config.PLAN_MAX_PARALLEL)
    step_caps = _plan_step_caps(steps, cap, project, resolve_cap)
    escalate = not replan          # no tail repair coming -> the ladder's escalation is recovery

    def _exec(step, snap):
        c, p = step_caps.get(step["id"], (cap, project)) if step_caps else (cap, project)
        return _run_plan_step(request, step, snap, c, wid, p,
                              model_override=model_override, write_escalate=escalate)

    while pending:
        # Hard cost ceiling: stop before the next wave (never on the first — spend starts at 0).
        if config.budget_exceeded(spent_out, total_cost, hard=True):
            budget_stop = True
            trace("PLAN", f"{wid} hard budget ceiling reached — stopping plan for a human")
            break
        # Dependency wave: every pending step whose needs are in `store` can run now. Toposort
        # guarantees at least the head is runnable, so this never deadlocks (defensive fallback
        # to the head keeps a malformed graph from hanging).
        runnable = [s for s in pending if all(n in store for n in s["needs"])] or [pending[0]]
        wave = runnable[:max_par]
        snap = dict(store)     # every step in the wave sees the SAME completed outputs
        if len(wave) == 1:
            trace("PLAN", f"{wid} step [{wave[0]['id']}] {wave[0]['goal'][:80]}  "
                          f"({len(pending) - 1} queued)")
            outcomes = {wave[0]["id"]: _exec(wave[0], snap)}
        else:
            trace("PLAN", f"{wid} wave of {len(wave)} concurrent steps: "
                          f"{', '.join(s['id'] for s in wave)}  ({len(pending) - len(wave)} queued)")
            outcomes = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as pool:
                futs = {pool.submit(_exec, s, snap): s for s in wave}
                for fut in concurrent.futures.as_completed(futs):
                    outcomes[futs[fut]["id"]] = fut.result()
        # Integrate the wave in PLAN order (deterministic results/synthesis, not completion order).
        wave_failed = []
        for s in wave:
            outcome = outcomes[s["id"]]
            total_cost += outcome["cost"]
            spent_out += outcome["tokens_out"]
            store[s["id"]] = outcome["result"]
            entry = {"step": s, "result": outcome["result"], "passed": outcome["passed"],
                     "superseded": False}
            results.append(entry)
            if outcome.get("strict_stop"):
                strict_stop = True
            if outcome.get("auth_stop"):
                auth_stop = True
                auth_wall = auth_wall or outcome.get("auth_wall")
            if not outcome["passed"]:
                wave_failed.append((entry, outcome["critique"]))
        for s in wave:
            pending.remove(s)
        if auth_stop:
            # Claude rejected our credentials: the same wall as strict mode below, one layer up.
            # Re-planning the tail would send every repaired step at the same dead login.
            trace("PLAN", f"{wid} Claude rejected our credentials — stopping the plan")
            break
        if strict_stop:
            # Strict mode: the local backend is unavailable, so re-planning the tail is theatre —
            # every repaired step would hit the same dead endpoint. Stop with the reason intact.
            trace("PLAN", f"{wid} local backend unavailable and Claude fallback is disabled — "
                          f"stopping the plan")
            break
        if not wave_failed:
            continue
        if len(wave_failed) > 1:
            trace("PLAN", f"{wid} {len(wave_failed)} steps in one wave failed — stopping for a human")
            break
        if not replan:
            # Human-authored graph: its steps are the intent, so repairing them with an LLM would
            # deliver something the author never approved. Stop and surface instead.
            trace("PLAN", f"{wid} [{wave_failed[0][0]['step']['id']}] failed and this plan was "
                          f"authored by a human (no re-plan) — stopping for a human")
            break
        # A step exhausted its ladder. Re-plan the remaining tail rather than failing — bounded.
        if replans >= config.PLAN_MAX_REPLANS:
            trace("PLAN", f"{wid} re-plan budget ({config.PLAN_MAX_REPLANS}) spent — stopping for a human")
            break
        failed_entry, critique = wave_failed[0]
        fid = failed_entry["step"]["id"]
        new_tail = _eng().replan_steps(request, cap, results, failed_entry["step"], critique, pending)
        if not new_tail:
            trace("PLAN", f"{wid} [{fid}] failed; re-plan produced no usable repair — stopping")
            break
        replans += 1
        failed_entry["superseded"] = True     # the repaired tail redoes this failed step's work
        trace("PLAN", f"{wid} re-planned after [{fid}] failed -> {len(new_tail)} step(s) "
                      f"(replan {replans}/{config.PLAN_MAX_REPLANS})")
        pending = new_tail
    result = _synthesize_plan(request, results)
    unrecovered = any(not c["passed"] and not c["superseded"] for c in results)
    passed = (bool(results) and not unrecovered and not budget_stop and not strict_stop
              and not auth_stop)
    return {"result": result, "passed": passed, "cost": total_cost,
            "tokens": {"output": spent_out}, "steps_run": len(results),
            "replans": replans, "budget_stop": budget_stop, "strict_stop": strict_stop,
            "auth_stop": auth_stop, "auth_wall": auth_wall}
