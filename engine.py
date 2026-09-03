"""The orchestration engine: ROUTER (#1) + the RUNNER + the durable wrapper.

In real Otto this is a Temporal workflow. Here it's a function, but the shape is
identical: route -> approve (signal) -> run -> audit. The crucial "real" bit: the
RUNNER shells out to `claude -p` to actually execute your real agent/skill.
"""
import concurrent.futures
import contextlib
import datetime
import json
import os
import re
import threading
import time
import uuid

import claude_cli
import config
import conventions
import error_classifier
import gateway
import knowledge
import local_runtime
import mcp_client
import registry
import storage
import supervisor
import workspace
from ui import say, trace
# The audit and memory layers live in audit.py / memory.py; this module re-exports them so
# callers and tests keep addressing engine.X (the whole suite monkeypatches these names HERE —
# both modules resolve their patch-sensitive seams back through this facade at call time,
# see audit._eng / memory._eng).
from audit import (_schema, _conn, _audit_conn, _append_audit, _append_content, _audit,
                   iter_audit_entries, iter_content_entries, scorecard, pr_url_from_run,
                   accept_run, record_terminal, record_skip, run_origin, audit_repo_changes)
from contracts import (_TLDR_SHAPE, _SINGLE_TURN_CONTRACT, _RESUME_CONTRACT, _REPORT_FORMAT,
                       _DIRECT_REPLY_FORMAT, CONVERSATION_AUDIENCE, _output_contract,
                       _invocation, _local_invocation, _LOCAL_CAP_CHARS, _CRITIQUE_FOLD,
                       _repo_scope_note, _repo_source_note, _pr_body_note, _setting_sources,
                       _write_gate_note,
                       _DISCUSSION_TURN_NOTE, _discussion_note, _resume_contract,
                       _DATA_FENCE_PREAMBLE, _fenced, _memory_context, _mcp_notes_note)
from plans import (_PLAN_PROMPT, _extract_step_json, _toposort, _parse_steps, plan_steps,
                   _REPLAN_PROMPT, _step_digest, replan_steps, _PLAN_INSTRUCTION, plan_preview,
                   _PLAN_CONCERN_CAP, _PLAN_CRITIQUE_CHARS, _PLAN_REQUEST_CHARS, _clipped,
                   _parse_plan_concerns, critique_plan, plan_mode_active, _step_prompt,
                   _synthesize_plan, _run_plan_step, _plan_step_caps, run_plan)
from routing import (ROUTE_DESC_CHARS, ROUTE_SHORTLIST, MAX_SWARM, _repo_eligible,
                     _shortlist, route, _parse_plan, decompose, merge, plan)
from intents import (_parse_clarification, clarify, _parse_write_intent,
                     followup_write_intent, _parse_handoff, followup_handoff,
                     request_write_intent, assistant_write_redirect, candidate_repo,
                     repo_edit_intent, _parse_pr_title, _OPERATIONAL_SENTINEL_PREFIXES,
                     _is_operational_sentinel, pr_copy, auto_engage_repo)
from judging import (_parse_verdict, _parse_qa_verdict, _APPROVED_PLAN_CHARS,
                     _approved_plan_note, _grounding_note, _JUDGE_REASONING_RULE,
                     verify, qa_review_request,
                     judge_qa, review_request, judge_review, error_verdict, _is_duplicated,
                     guard_resume_result)
from memory import (_SOLUTIONS_MAX, _BEHAVIORS_MAX, _extract_facts, _extract_solution,
                    _is_durable_fact, _memory_ns, _norm, _remember, _remember_solution,
                    _resolve_project, _keywords, record_attempt, recent_facts, memory_events,
                    delete_fact, clear_memory, recall_solutions, solutions, delete_solution,
                    clear_solutions, applicable_behaviors, behaviors, add_behavior,
                    update_behavior, delete_behavior, suggest_behavior_rule,
                    _parse_rule_suggestion, _parse_gc_classification, _parse_gc_verify,
                    gc_preview, gc_status, gc_start, gc_evict)

# Every engine-owned store lives in ONE WAL-mode SQLite db (issue #103), not JSON — these are the
# hot append paths, and a whole-file read-modify-write (storage.mutate_json) doesn't scale for
# them. Schema + the audit tables live in audit.py (re-exported above); `memory` (learned facts,
# `namespace` NULL for global else a project slug), `solutions` (verified approaches) and
# `behaviors` (user directives) are still homed here. The audit/memory tables keep the whole
# original entry dict in a JSON `data` column — read-back is byte-for-byte the shape callers and
# tests already assert on — with a few columns promoted out of it purely for indexed filtering.
# Tests monkeypatch THIS alias to a temp file; don't reach for config.DB_PATH directly.
# audit.py reads the path through this alias at call time, so those patches cover it too.
_DB = config.DB_PATH
_counter = {"n": 0}
# Per-process namespace for locally-minted run ids. The counter alone collided across the
# server and worker processes (both restart at 0), so two unrelated runs could mint the same
# "wf-0001" — overwriting each other's transcripts and producing ambiguous audit rows.
_RUN_NS = uuid.uuid4().hex[:6]


def _next_wid():
    """Mint a process-unique run id for a run that has no external workflow id (the direct
    path). Temporal runs pass their real workflow id down instead — see OttoWorkflow.run."""
    _counter["n"] += 1
    return f"wf-{_RUN_NS}-{_counter['n']:04d}"


# --- the bridge to real Claude Code ---------------------------------------

def _claude(prompt, allowed_tools=None, model=None, mcp_config_path=None, resume_session=None,
            system_context=None, timeout=900, cwd=None, transcript=None, on_event=None,
            abort=None, meta=None, permission_mode=None, disallowed_tools=None,
            setting_sources=None, steer=None, effort=None):
    """Run a headless Claude Code turn and return its parsed JSON result. `cwd` runs it from
    a specific directory — load-bearing for project-scoped caps, whose skills/agents, scripts,
    and `.mcp.json` only resolve when Claude runs inside their repo. Delegates to
    claude_cli.run_json (the ONE subprocess seam, issue #89); kept as a named engine function
    because the test suites mock exactly `engine._claude`. `transcript` captures the full
    stream-json exchange to that path as it happens; `on_event` is the stream watcher seam
    (the shadow supervisor, issue #143)."""
    return claude_cli.run_json(prompt, allowed_tools=allowed_tools, model=model, timeout=timeout,
                               mcp_config_path=mcp_config_path, resume_session=resume_session,
                               system_context=system_context, cwd=cwd, transcript=transcript,
                               on_event=on_event, abort=abort, steer=steer, meta=meta,
                               permission_mode=permission_mode, effort=effort,
                               disallowed_tools=(config.DISALLOWED_TOOLS
                                                 if disallowed_tools is None else disallowed_tools),
                               setting_sources=setting_sources)


def _usage(out):
    """Token counts for one `claude -p` turn, normalized from its JSON `usage` block.

    On a Claude *subscription* (how Otto auths) there's no per-token bill — these
    tokens are the real scarce resource: they drive the usage-limit window. The
    `total_cost_usd` we also record is only a notional API-equivalent price. Output
    tokens are the heavy/limited ones; cache_read is near-free."""
    u = out.get("usage") or {}
    return {
        "input": u.get("input_tokens", 0) or 0,
        "output": u.get("output_tokens", 0) or 0,
        "cache_read": u.get("cache_read_input_tokens", 0) or 0,
        "cache_write": u.get("cache_creation_input_tokens", 0) or 0,
    }


# --- Runner: actually invoke the capability -------------------------------


def _effective_mcp(cap, base_path):
    """Merge a project cap's repo `.mcp.json` into the active MCP config. Returns
    (config_path, extra mcp__ tool prefixes). Non-project caps pass straight through. The
    repo's servers are added explicitly (not relying on cwd auto-discovery, which `claude -p`
    doesn't honour for unapproved project servers) and allowlisted so the skill can call them."""
    repo_mcp = getattr(cap, "mcp_config", None)
    if not repo_mcp or not os.path.exists(repo_mcp):
        return base_path, []
    try:
        with open(repo_mcp) as f:
            repo = (json.load(f).get("mcpServers") or {})
    except (OSError, ValueError):
        return base_path, []
    if not repo:
        return base_path, []
    merged = {}
    if base_path and os.path.exists(base_path):
        try:
            with open(base_path) as f:
                merged = (json.load(f).get("mcpServers") or {})
        except (OSError, ValueError):
            merged = {}
    merged.update(repo)   # repo servers win on a name clash — they're what this cap needs
    safe = re.sub(r"[^0-9A-Za-z]+", "-", cap.name).strip("-") or "cap"
    path = os.path.join(config.DATA_DIR, f".mcp-{safe}.json")
    storage.write_json(path, {"mcpServers": merged})
    return path, [f"mcp__{n}" for n in repo]


# --- verify -> retry -> escalate ------------------------------------------



def run_attempt(request, cap, *, attempt=1, critique=None, escalate=False, downshift=False,
                extra_tools=None, mcp_config_path=None, resume_session=None, wid=None, cwd=None,
                recall=False, project=None, local_disabled=False, local_disabled_reason=None,
                repo=None, audience=None, approved_plan=None, grounding=None,
                memory_enabled=True,
                model_override=None, discussion=False, supervise_enforce=True, effort=None):
    """One execution attempt via `claude -p`. Builds the invocation (folding in the
    previous critique on a retry) and picks the model (escalated on the final attempt).
    Returns the raw result + metadata; verification and auditing are separate steps so the
    workflow can keep the loop deterministic (LLM work stays in this activity).

    `memory_enabled=False` (a per-chat opt-out) skips memory recall the same way a swarm
    sub-task's `recall=False` does. `model_override` (a per-chat model pick) wins over
    cap_exec/escalate/downshift entirely for this run — resolved once against the pool;
    an unknown name is ignored (falls back to the admin-configured model).

    `effort` is how hard the model thinks (config.EFFORT_LEVELS) — the per-chat pick if there is
    one, else the Admin default. Resolved from the settings store ONLY for callers outside
    Temporal: a workflow always passes the value from its own snapshot, because a store read
    inside a run could serve a different level to attempt 2 than to attempt 1."""
    if not wid:
        wid = _next_wid()
    effort = config.effort_level(effort if effort is not None else config.setting("effort"))
    allowed = (config.WRITE_TOOLS if cap.risk == "write" else config.READ_TOOLS) + (extra_tools or [])
    # Project caps run from their repo and merge that repo's `.mcp.json` + its tools.
    mcp_config_path, cap_mcp_tools = _effective_mcp(cap, mcp_config_path)
    allowed += cap_mcp_tools
    # An explicit cwd (an isolated repo workspace, issue #57) overrides the cap's own cwd, so a
    # global agent can run inside a freshly-cloned repo it doesn't otherwise belong to.
    cwd = cwd or getattr(cap, "cwd", None)

    # Backend choice: `claude -p` (the default) vs the LOCAL agent runtime. Local runs when
    # the resolved execution model (per-chat override > per-cap cap_exec override > phase
    # assignment) is a local pool entry AND the cap needs no repo `.mcp.json` (the runtime has
    # no MCP client — such caps stay on Claude). A resumed session is bound to whichever
    # backend minted its id: "local-…" ids route back to the local runtime regardless of
    # current model assignments.
    override_entry = gateway.resolve_model(model_override) if model_override else None
    if model_override and override_entry is None:
        trace("RUN", f"{wid} model override '{model_override}' isn't a known pool entry — "
                     f"ignoring it, using the admin-configured model")
    exec_entry = override_entry or gateway.exec_model_entry(cap.name)
    use_local = (exec_entry.get("provider") != "claude"
                 and not getattr(cap, "mcp_config", None))
    # THE LOCAL BACKEND CANNOT SERVE A claude.ai CONNECTOR, so a cap that needs one must not
    # run there. `mcp_client` serves stdio servers (New Relic, k8s, Grafana, AWS, Vanta) but a
    # connector's OAuth lives inside Claude Code — there is nothing to spawn. Unguarded, the
    # cap runs anyway and discovers the gap one hallucinated call at a time: run
    # sched-mosaic-9e5e5681 (2026-08-04) put a briefing cap on DeepSeek and spent three
    # attempts, three supervisor kills and ~1.1M input tokens answering "tool is not available
    # in this run". Honours the fallback contract rather than inventing a third behaviour:
    # fallback ON substitutes Claude (the work lands, the audit row shows the move and why),
    # strict mode STOPS the way every other local→Claude site does.
    mcp_blockers = mcp_client.unservable(cap) if (use_local and not resume_session) else []
    if use_local and mcp_blockers:
        why = ("needs MCP servers the local backend cannot serve (" + ", ".join(mcp_blockers)
               + ") — claude.ai connectors are OAuth'd inside Claude Code, so only the Claude "
                 "backend can reach them")
        if not config.setting("local_fallback"):
            return _strict_stop_attempt(
                wid, attempt, gateway.LocalFallbackDisabled(exec_entry["name"], why),
                time.monotonic())
        use_local = False
        fb_forced = {"fallback_from": exec_entry["name"], "fallback_reason": why}
        trace("RUN", f"{wid} {cap.name} {why} — running on Claude")
    else:
        fb_forced = None
    # THIS CAPABILITY HAS ALREADY PROVED IT CANNOT DO THE WORK ON THIS MODEL. Within one run the
    # ladder self-corrects (issue #172 re-dispatches the rest of it to Claude), but nothing
    # remembered that across runs, so every new run re-litigated the same doomed first attempt:
    # `github-pr-review` lost fifteen of them on qwen3.6 while going 13/13 on Claude. Same shape
    # as the connector refusal above — the fallback contract decides, not a third behaviour —
    # and the latch is a circuit breaker: it expires, and the attempt it then lets through
    # re-arms it on a fail or clears it on a pass.
    # `override_entry` is exempt: that is the composer's per-run pick, i.e. a human choosing
    # THIS model for THIS run, right now. Accumulated evidence outranks stored config (the phase
    # default and the Admin cap pin), never a live instruction — and without the exemption there
    # is no way to re-test a latched pairing on purpose.
    if (use_local and not resume_session and not override_entry
            and gateway.cap_local_latched(cap.name, exec_entry["name"])):
        why = (f"{cap.name} has failed verification on {exec_entry['name']} "
               f"{config.setting('cap_local_latch_fails')} times in a row — latched off the "
               f"local backend until it is re-tested")
        if not config.setting("local_fallback"):
            return _strict_stop_attempt(
                wid, attempt, gateway.LocalFallbackDisabled(exec_entry["name"], why),
                time.monotonic())
        use_local = False
        fb_forced = {"fallback_from": exec_entry["name"], "fallback_reason": why}
        trace("RUN", f"{wid} {why} — running on Claude")
    resume_bound = None   # the session's own model, when the resolved one is on the wrong backend
    if resume_session:
        use_local = local_runtime.is_local_session(resume_session)
        # A resumed session is bound to the backend that minted its id, but the model ENTRY is
        # resolved independently — so a cross-backend pick (or a phase assignment that has since
        # moved to Claude) hands a local model id to `claude -p`, or a Claude pool entry to the
        # local runtime, which cannot serve either. The session's own model is the only correct
        # answer here; /api/continue rebinds to a fresh run when the user's pick disagrees.
        if (exec_entry.get("provider") != "claude") != use_local:
            # The model that minted it, else ANY entry on the same backend. ONE implementation
            # (local_runtime.resume_entry) — the plan preview resumes the same session and has
            # to reach the same answer.
            bound = local_runtime.resume_entry(resume_session)
            if bound:
                trace("RUN", f"{wid} {exec_entry['name']} is on the wrong backend for this "
                             f"resumed session — continuing on {bound['name']}")
                override_entry, exec_entry, resume_bound = None, bound, bound
    elif local_disabled and config.setting("local_fallback"):
        # An EARLIER rung this run proved the local backend can't serve this cap at all — the
        # server rejects tool calls (vLLM missing --enable-auto-tool-choice/--tool-call-parser),
        # so every local attempt fails identically. Keep the rest of the ladder on Claude rather
        # than dead-ending the final rung on the same broken server (regression: a run whose
        # attempts 1-2 re-dispatched to Claude but whose final attempt went back to local and
        # errored → a needless needs-human). A WORKING-but-weak local model never trips this and
        # still runs the whole ladder locally (the cost opt-out is preserved).
        use_local = False

    # EVERY local->Claude switch above has to move the MODEL with the backend, not just the
    # dispatch. A per-chat override outranks escalation/downshift/cap_exec below — and unlike
    # them it is a raw pool entry with no Claude guarantee — so left standing it hands a LOCAL
    # model id to `claude -p --model`, which rejects it outright and the CLI's "may not exist or
    # you may not have access to it" becomes the run's entire final answer (web-3a05328f a2:
    # backend=claude, model=qwen38-27b, fallback_from=qwen38-27b — a badge reading X ⇢ X).
    # Dropping the override lets the chain fall to the Claude-only helpers, which is what the
    # escalation was for. `exec_entry` deliberately keeps the local entry so the fallback badge
    # still names what we moved off. The resume branch above does the same via `bound`.
    if override_entry and not use_local and override_entry.get("provider") != "claude":
        trace("RUN", f"{wid} {override_entry['name']} is pinned but this run is on the Claude "
                     f"backend — using the Claude execution model instead")
        override_entry = None

    if resume_session:
        # Continuation — raw follow-up in an existing session; no critique fold-in, no memory.
        # Still reinforce the single-turn contract (via --append-system-prompt, so the user's
        # follow-up text stays untouched): a resumed coordinator otherwise sometimes reports the
        # work as "still running in the background" and promises a relay that can never arrive,
        # since resume is one-shot too.
        # A resumed Slack follow-up is delivered to that person too, so it needs the same
        # direct-reply contract — without it turn 2 of a conversation reverts to report prose.
        invocation, verb = request, "continuing"
        # `discussion` marks a follow-up in a WRITE-bound session that read as a question, so it
        # runs on READ_TOOLS with no approval gate (cap.risk was already narrowed by the caller —
        # that is what actually removes Edit/Write). This note only makes the narrowing legible to
        # the model, so a misread change request comes back as a sentence the user can act on
        # rather than as a tool-permission error.
        sysctx = "\n\n".join(filter(None, [
            # Audience-picked: the default folds in _TLDR_SHAPE, which a brainstorm turn's own
            # contract forbids. See contracts._resume_contract.
            _resume_contract(audience),
            _output_contract(audience) if audience else None,
            # A resumed turn re-uses the session's ORIGINAL system prompt, so the worker
            # contract's wrong-branch clause is not in scope for this message. When the restored
            # tree contradicts the follow-up, this note is the only thing carrying that
            # instruction — and the resume path is where the mismatch is most likely, since the
            # branch comes from what the chat recorded, not from what the message asks about.
            _grounding_note(grounding),
            # The session's ORIGINAL system prompt carried these, but only if turn 1 was an
            # Otto run — and a follow-up can reach for a server the first turn never touched.
            _mcp_notes_note(cap),
            _discussion_note(discussion)]))
    else:
        # The local runtime has no Claude Code around it to resolve `/skill` or subagents, so
        # a skill/agent cap's own markdown is inlined into the invocation instead.
        invocation = _local_invocation(cap, request) if use_local else _invocation(cap, request)
        if critique:
            invocation += _CRITIQUE_FOLD + critique
        # `recall` (a fresh top-level run) adds past solved-task approaches to the context;
        # swarm sub-tasks pass recall=False so they don't pull in unrelated parent-task methods.
        # `cap` brings in the user's behaviour rules (global + this cap's), which apply to every
        # run including sub-tasks (issue #68). `project` brings in that project's facts +
        # standing instructions (issue #69). The output contract is layered on unconditionally
        # (every fresh run, any cap kind) since a custom cap's own prompt never gets
        # _SINGLE_TURN_CONTRACT — `_REPORT_FORMAT`, or `_DIRECT_REPLY_FORMAT` when the result is
        # posted verbatim to a person (audience="slack").
        #
        # Refresh the registered checkouts' remote refs FIRST, so `_repo_source_note`'s instruction
        # to trust `origin/HEAD` over the working tree is actually pointing at current data — in a
        # stale clone `origin/HEAD` lies just as confidently as the tree does. Only where that note
        # applies (a run that will CHOOSE a repo to read; repo-mode is already pinned to a
        # provisioned clone), and skipped once the window says the refs are fresh, so the common
        # case costs nothing. Never fatal — see `workspace.refresh_repos`.
        if not (repo and cwd):
            workspace.refresh_repos()
        sysctx = "\n\n".join(
            filter(None, [_output_contract(audience), _approved_plan_note(approved_plan),
                          _grounding_note(grounding), _write_gate_note(cap),
                          _mcp_notes_note(cap),
                          _repo_scope_note(repo, cwd), _repo_source_note(repo, cwd),
                          _pr_body_note(repo, cwd),
                          _memory_context(request if (recall and memory_enabled) else None,
                                          cap, project)]))
        verb = f"attempt {attempt}"

    # Local execution rung (issue #42): a TOOL-FREE read capability with a local model assigned
    # runs its FIRST attempt as a plain OpenAI-compatible completion — nothing to drive, so no
    # `claude -p`. Strictly attempt 1 and never on escalate/downshift/resume: a verify failure
    # falls up the existing ladder (attempt 2 = the configured Claude tier, final attempt = the
    # strongest Claude), and a write-risk or non-tool-free cap never reaches this branch. A local
    # failure/unavailability returns None and this same attempt runs on Claude instead.
    fb_meta = fb_forced   # set when the CHOSEN local model couldn't run and Claude substitutes
    if (not resume_session and attempt == 1 and not escalate and not downshift
            and cap.risk == "read" and getattr(cap, "tool_free", False)):
        started = time.monotonic()
        try:
            loc = gateway.local_execute(cap.name, invocation, system_context=sysctx)
        except gateway.LocalFallbackDisabled as e:
            return _strict_stop_attempt(wid, attempt, e, started)
        if loc is not None:
            trace("RUN", f"{wid} {verb} [{cap.kind}] {cap.name}  model={loc['model']} (local, tool-free)")
            # `local: True` reaches the verifier so it judges this as a NO-tool attempt —
            # the standard prompt vouches for real tool access, which would launder any
            # tool-shaped output a local model fabricated.
            return {"workflow": wid, "result": str(loc["result"]).strip() or "(no output)",
                    "cost": 0, "tokens": loc["tokens"], "session_id": None,
                    "model": loc["model"], "attempt": attempt, "is_error": False,
                    "supervision": None, "duration_s": time.monotonic() - started, "local": True,
                    "backend": "local"}
        tf = (gateway.load().get("cap_local_exec") or {}).get(cap.name)
        if tf:
            fb_meta = {"fallback_from": tf,
                       "fallback_reason": "tool-free local completion failed or the model is marked down"}

    # Precedence: a per-chat model override beats EVERYTHING below (escalation, downshift,
    # cap_exec) — it's the user's explicit choice for this run. Absent that: a final-attempt
    # escalation (strongest model) beats a soft-budget downshift (cheapest model), which beats
    # the normal per-cap execution model. A LOCAL-backend run is deliberately local-ONLY:
    # escalation/downshift are Claude-tier moves, so retries stay on the same local model and a
    # still-failing run surfaces to a human instead of silently spending Claude tokens the user
    # opted out of — UNLESS the local backend proved tool-incapable this run (local_disabled
    # above forced use_local False → Claude here).
    if use_local:
        model = exec_entry["name"]
        if escalate or downshift:
            trace("RUN", f"{wid} local-only execution — escalation/downshift stays on {model}")
    elif resume_bound:
        model = resume_bound["model"]      # the session's backend decides, not the assignment
    elif override_entry:
        model = override_entry["model"]
        trace("RUN", f"{wid} per-chat model override -> {override_entry['name']}")
    elif escalate:
        model = gateway.escalation_model_id()
        trace("ESCALATE", f"{wid} final attempt -> strongest model {model}")
    elif downshift:
        model = gateway.downshift_model_id()
        trace("DOWNSHIFT", f"{wid} over soft budget -> cheaper model {model}")
    else:
        model = gateway.exec_model_id(cap.name)
    # Record the local→Claude move so the audit trail + UI show the "<local> ⇢ <claude>" badge
    # and WHY, when an earlier rung proved this run can't be served locally at all.
    if local_disabled and not use_local and exec_entry.get("provider") != "claude":
        fb_meta = {"fallback_from": exec_entry["name"],
                   "fallback_reason": local_disabled_reason or
                   ("the local backend could not serve this run (tool calls rejected, or the "
                    "endpoint unreachable) — proven earlier this run; ladder stays on Claude")}
    trace("RUN", f"{wid} {verb} [{cap.kind}] {cap.name}  model={model}"
                 f"{' (local runtime)' if use_local else ''}"
                 f"{f'  effort={effort}' if effort else ''}  tools={allowed}")

    # LLM supervisor (issue #143): watch the live stream on a bounded cadence. In ENFORCE
    # mode (default) a RETRY verdict kills the attempt mid-run and its critique steers the
    # next verify-ladder rung; in shadow mode it only records what it would have done.
    # Fresh attempts only: a resumed session is a mid-conversation reply, not a task attempt
    # in the verify ladder. Both backends feed it through the same on_event seam and honor
    # the same abort switch.
    transcript_path = claude_cli.transcript_path(wid, attempt)
    # `supervise_enforce=False` disarms the kill switch for THIS attempt while leaving the
    # observer running (the verdicts still audit). The ladder owns that decision — it is the only
    # layer that knows whether a rung remains for the critique to steer and how many kills this
    # run has already spent. See config.MAX_SUPERVISOR_KILLS.
    abort = (supervisor.Abort()
             if (config.setting("supervise") and config.setting("supervise_mode") == "enforce"
                 and supervise_enforce and not resume_session)
             else None)
    # Mid-run STEERING (config.SUPERVISE_STEER). Deliberately NOT gated on `supervise_enforce`,
    # which is the LADDER's permission to KILL: that flag is off on the final rung because a kill
    # leaves nothing for the critique to steer, and off past the kill budget because a second
    # kill rescues worse than letting the attempt finish. Neither reason transfers — a steer
    # spends no rung and discards no work, so the final rung is exactly where it is most
    # valuable (it is the last chance to fix the run at all). Its own budget bounds it instead.
    steer_mode = str(config.setting("supervise_steer") or "off").lower()
    steer = (supervisor.Steer(config.setting("max_supervisor_steers"))
             if (config.setting("supervise") and steer_mode == "enforce" and not resume_session)
             else None)
    sup = None if resume_session else supervisor.start(wid, attempt, request, cap,
                                                       transcript=transcript_path, abort=abort,
                                                       cwd=cwd, critique=critique, steer=steer,
                                                       steer_shadow=(steer_mode == "shadow"))
    started = time.monotonic()
    backend = "local" if use_local else "claude"
    local_incapable = False   # this local attempt hit the vLLM tool-call config wall
    if use_local:
        out = local_runtime.run_json(invocation, allowed_tools=allowed, model_entry=exec_entry,
                                     # Same wall clock the Claude path gets: left unpassed, this
                                     # fell to run_json's bare 900s default and cut every long
                                     # local run 200s short of the activity ceiling.
                                     timeout=config.LOCAL_RUN_TIMEOUT_S,
                                     resume_session=resume_session, system_context=sysctx,
                                     cwd=cwd, transcript=transcript_path,
                                     on_event=sup.note if sup else None, abort=abort,
                                     steer=steer,
                                     mcp_servers=mcp_client.servers_for(cap, allowed, request),
                                     mcp_request=request,
                                     # An UNDECLARED cap (general worker/assistant, stock caps)
                                     # only gets tools that actually match the request; a
                                     # declared one keeps filler up to the budget, since its
                                     # grant is explicit and "catch me up" matches no tool name.
                                     mcp_require_score=not mcp_client.declared_servers(cap),
                                     effort=effort)
        # TWO deterministic walls, one escape hatch: the serving stack rejects tool definitions
        # (vLLM missing --enable-auto-tool-choice/--tool-call-parser), or the endpoint is
        # unreachable after the backoffs. Neither is "the model answered badly" — both fail every
        # local attempt IDENTICALLY, so retrying on the same model just spends the ladder to reach
        # the same dead end (run web-e5248517 burned all three attempts on one HTTP 503). A
        # turn-budget or token-limit death is deliberately NOT here: that IS the model working,
        # just not finishing, and a retry folding in the critique can legitimately do better.
        local_wall = None
        if not resume_session:
            if out.get("tools_unsupported"):
                local_wall = ("the local server rejects tool calls — vLLM is missing "
                              "--enable-auto-tool-choice / --tool-call-parser")
            elif out.get("unavailable"):
                local_wall = ("the local model endpoint is unreachable (it stayed down through "
                              "every retry/backoff)")
            elif out.get("wall_reason"):
                # Any OTHER deterministic wall the classifier named — bad credentials, no credit,
                # a 429 or 500 that outlived every backoff. These used to arrive as an anonymous
                # `HTTP 401: …` that the ladder read as a model-quality failure and retried twice
                # more against the same endpoint, so a wrong key cost three attempts and a
                # needs-human banner to report itself.
                local_wall = error_classifier.wall_message(out["wall_reason"])
        if local_wall and not config.setting("local_fallback"):
            # Strict mode: the local backend can't serve this run, and that is the answer — don't
            # quietly re-dispatch the attempt to Claude and report a success the local backend
            # never earned. The ladder stops here (see the strict-stop branches in the ladders).
            if sup:
                sup.finish()
            return _strict_stop_attempt(
                wid, attempt,
                gateway.LocalFallbackDisabled(exec_entry["name"], local_wall), started)
        if local_wall:
            local_incapable = True
            # Run THIS attempt on Claude instead of burning the verify ladder on it. (A resumed
            # local session can't transplant to Claude — its history lives here — so resume keeps
            # the explicit error; that's why `local_wall` is None for a resume.)
            model = gateway.exec_model_id(cap.name)
            backend = "claude"
            fb_meta = {"fallback_from": exec_entry["name"], "fallback_reason": local_wall}
            trace("RUN", f"{wid} {local_wall} — re-dispatching this attempt to Claude ({model})")
            invocation = _invocation(cap, request)
            if critique:
                invocation += _CRITIQUE_FOLD + critique
            out = _claude(invocation, allowed_tools=allowed, mcp_config_path=mcp_config_path,
                          model=model, system_context=sysctx, cwd=cwd,
                          transcript=transcript_path, timeout=config.EXEC_TIMEOUT_S,
                          on_event=sup.note if sup else None, abort=abort, meta=fb_meta,
                          setting_sources=_setting_sources(cwd), effort=effort)
    else:
        out = _claude(invocation, allowed_tools=allowed, mcp_config_path=mcp_config_path,
                      model=model, resume_session=resume_session, system_context=sysctx, cwd=cwd,
                      transcript=transcript_path, timeout=config.EXEC_TIMEOUT_S,
                      on_event=sup.note if sup else None, abort=abort, steer=steer,
                      meta=fb_meta, setting_sources=_setting_sources(cwd), effort=effort)
    duration_s = time.monotonic() - started
    # The CLAUDE backend's deterministic walls: `claude -p` could not authenticate, the
    # subscription's usage limit is spent, or models.json names a model this account cannot
    # serve. Gated on `is_error` — the CLI died before emitting a result event, so this text is
    # the process's own dying words and never model output. Retrying spends the ladder on the
    # same refusal: two scheduled runs (sched-mosaic-3f7943f2, sched-otto-5b0f098f) burned all
    # three rungs on a dead login, and six more attempts across the trail burned rungs on a
    # usage limit whose reset was HOURS away — the final rung escalating the model each time,
    # so the priciest tier was spent on a call that never ran. Not latched for a resume: the
    # ladders don't run one. `claude_wall` is a reason STRING, mirroring how a local wall
    # crosses the boundary — never a second boolean per condition.
    claude_wall = (error_classifier.claude_wall(str(out.get("result", "")))
                   if (backend == "claude" and out.get("is_error") and not resume_session)
                   else None)
    auth_stop = bool(claude_wall)
    if auth_stop:
        trace("AUTH", f"{wid} Claude backend wall ({claude_wall}) — stopping the ladder")
    supervision = sup.finish() if sup else None
    if supervision:
        # One row per supervised attempt — checkpoint count is the denominator, retry
        # verdicts the signal. An ENFORCE kill audits as its own outcome; shadow rows keep
        # collecting false-kill-rate data. Neither is a needs-human row: a killed attempt
        # feeds the verify ladder, it doesn't surface on the Needs-you dashboard.
        killed = supervision.get("killed")
        lines = "; ".join(
            f"@{v['at_s']}s {v['verdict'].upper()}" + (f" — {v['critique']}" if v["critique"] else "")
            for v in supervision["verdicts"])
        steered = supervision.get("steers") or []
        tag = ("supervisor kill" if killed else
               "supervisor steer" if steered else "supervisor shadow")
        if steered:
            lines += "; delivered: " + " | ".join(steered)
        _audit(wid, request, cap, f"[{tag}] {lines}", 0, attempt=attempt,
               outcome=("supervisor_kill" if killed else
                        "supervisor_steer" if steered else "supervisor_shadow"),
               reason=("supervisor_retry" if killed else
                       "supervisor_steer" if steered else
                       ("supervisor_would_retry" if supervision["would_retry"] else None)))
    cost = out.get("total_cost_usd", 0) or 0
    result = str(out.get("result", "")).strip() or "(no output)"
    if resume_session:
        # Resume/follow-up runs skip the verify loop AND the supervisor, so this is their only
        # content check before delivery — a cheap, model-agnostic shape guard (fresh attempts
        # are covered by the verify ladder, so it's scoped to resume).
        result = guard_resume_result(result)
    # `is_error` is set by `_claude` on a timeout / unparseable turn. Surfaced here so the loop
    # treats such an attempt as a FAILED attempt (not valid output fed to the verifier).
    return {"workflow": wid, "result": result, "cost": cost, "tokens": _usage(out),
            "session_id": out.get("session_id"), "model": model, "attempt": attempt,
            # The tools this attempt actually CALLED. Passed to the judge instead of the
            # static allowlist, which is only a floor — see judging.verify's grant block.
            "tools_used": out.get("tools_used") or [],
            "tools_failed": out.get("tools_failed") or [],
            "is_error": bool(out.get("is_error")), "supervision": supervision,
            # What the supervisor actually told the agent mid-run. Lifted out of `supervision`
            # because it is not observability: the verifier MUST see it, or it scores the output
            # against the unamended request and fails the attempt for obeying the supervisor.
            "steers": (supervision or {}).get("steers") or [],
            # A deterministic Claude-backend wall: terminal for every ladder, like
            # local_strict_stop. The boolean drives the ladders; the reason names the remedy.
            "auth_stop": auth_stop, "claude_wall": claude_wall,
            "duration_s": duration_s, "backend": backend,
            "fallback_from": (fb_meta or {}).get("fallback_from"),
            "fallback_reason": (fb_meta or {}).get("fallback_reason"),
            # The local backend can't serve this cap (tool-call flags missing) — the loop
            # latches this and forces the rest of the ladder to Claude via local_disabled.
            "local_incapable": local_incapable or bool(local_disabled and fb_meta),
            # A WRITE cap actually executed on the local backend this attempt (issue #172): the
            # loop uses this + a failed verdict to latch local_disabled and escalate to Claude.
            "write_local": cap.risk == "write" and backend == "local"}


def _strict_stop_attempt(wid, attempt, exc, started):
    """One attempt result for a strict-mode stop (OTTO_LOCAL_FALLBACK=0): the local backend
    couldn't run and Claude is not allowed to cover for it.

    `is_error` keeps it out of the verifier (there is no output to judge), and `local_strict_stop`
    tells every ladder to STOP rather than take another rung — retrying is pointless when the
    endpoint is down and the whole point of the flag is that the failure surfaces instead of being
    worked around. The loud body travels as the result so it lands in the delivered text, the
    audit trail, and the board card unchanged."""
    trace("STRICT", f"{wid} attempt {attempt} stopped — {exc}")
    return {"workflow": wid, "result": exc.message, "cost": 0,
            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
            "session_id": None, "model": exc.model, "attempt": attempt, "is_error": True,
            "supervision": None, "tools_used": [], "tools_failed": [], "duration_s": time.monotonic() - started, "backend": "local",
            "local_strict_stop": True, "auth_stop": False, "fallback_from": None,
            "fallback_reason": None, "local_incapable": False, "write_local": False}


def _ladder_core(request, cap, wid, *, recall, project, remember=True, write_escalate=True,
                 memory_enabled=True, model_override=None, extra_tools=None, mcp_config_path=None,
                 budget=False):
    """THE synchronous verify -> retry -> escalate loop. One body, two adapters: `_run_ladder`
    (plan-mode steps) and `execute` (the chat/test entry point). It used to be written out twice
    in this file, ~85% identical, and the copies had already drifted apart at the verify call —
    one passed `local=` without `project=`, the other `project=` without `local=`, so each was
    missing half of what OttoWorkflow._verify_ladder sends. That is the whole reason this is one
    function now: a duplicated loop does not stay duplicated, it becomes two behaviours.

    OttoWorkflow._verify_ladder is still a THIRD copy and cannot merge into this one — workflow
    code is deterministic and calls activities, not functions. It remains a deliberate mirror;
    a semantic change here must be mirrored there, and vice versa.

    `budget=True` applies the per-run hard/soft cost ceiling. Plan-mode steps pass False (their
    spend is accounted by the plan, not per step) — see the note in `_run_ladder`."""
    n = max(1, config.setting("max_attempts"))
    # Mirrors OttoWorkflow._verify_ladder: a HARNESS death draws on its own bounded budget rather
    # than spending a judged rung, because no judge read it — see the note there.
    spare = max(0, config.setting("max_harness_retries"))
    critique, result, verdict, local_disabled = None, "(no output)", None, False
    local_disabled_reason, strict_stopped, budget_stopped = None, False, False
    auth_stopped, auth_wall = False, None
    cost, tokens_out, attempt, att = 0, 0, 1, None
    judged, attempt, harness_stopped = 0, 0, False
    kills = 0
    max_kills = max(0, config.setting("max_supervisor_kills"))
    while True:
        attempt += 1
        final = judged == n - 1
        # Hard cost ceiling: stop before another attempt (never on attempt 1 — spend starts at 0).
        if budget and config.budget_exceeded(tokens_out, cost, hard=True):
            budget_stopped = True
            break
        downshift = budget and not final and config.budget_exceeded(tokens_out, cost, hard=False)
        # Never arm the kill switch on the FINAL rung: a kill's whole value is the critique it
        # hands the NEXT attempt, and on the last one there is no next — the run just ends holding
        # an aborted partial instead of whatever that attempt would have produced.
        att = run_attempt(request, cap, attempt=attempt, critique=critique, escalate=final,
                          downshift=downshift, extra_tools=extra_tools,
                          mcp_config_path=mcp_config_path, wid=wid, recall=recall, project=project,
                          local_disabled=local_disabled,
                          local_disabled_reason=local_disabled_reason,
                          memory_enabled=memory_enabled, model_override=model_override,
                          supervise_enforce=(not final and kills < max_kills))
        if (att.get("supervision") or {}).get("killed"):
            kills += 1
        local_disabled = local_disabled or att.get("local_incapable", False)
        wid = att["workflow"]
        result = att["result"]
        cost += att["cost"]
        tokens_out += (att.get("tokens") or {}).get("output", 0) or 0
        trace("COST", f"attempt {attempt} cost ${att['cost']:.4f}")
        if att.get("local_strict_stop"):
            # Strict mode (OTTO_LOCAL_FALLBACK=0): local couldn't run and Claude may not cover.
            # Terminal on the spot — no verify (nothing to judge), no further rungs (they'd hit the
            # same dead endpoint), and the reason is the delivered result.
            strict_stopped = True
            record_attempt(wid, request, cap, result, att["cost"], attempt,
                           error_verdict(result), remember=False,
                           tokens=att.get("tokens"), model=att.get("model"), project=project,
                           duration_s=att.get("duration_s"), backend=att.get("backend"))
            break
        if att.get("auth_stop"):
            # A deterministic Claude-backend wall (dead login, spent usage limit, unservable
            # model). Every remaining rung reaches the same refusal. Terminal here — nothing to
            # judge, and the result IS the operator's instruction.
            auth_stopped = True
            auth_wall = att.get("claude_wall")
            result = att["result"] = error_classifier.claude_wall_message(result, auth_wall)
            record_attempt(wid, request, cap, result, att["cost"], attempt,
                           error_verdict(result), remember=False,
                           tokens=att.get("tokens"), model=att.get("model"), project=project,
                           duration_s=att.get("duration_s"), backend=att.get("backend"))
            break
        # An errored/timed-out attempt is a failed attempt — don't verify garbage, just retry.
        verdict = (error_verdict(result) if att.get("is_error")
                   else verify(request, cap, result, project=project,
                               local=att.get("local", False),
                               tools_used=att.get("tools_used"),
                               tools_failed=att.get("tools_failed"),
                               # Mirrored in OttoWorkflow._verify_ladder.
                               steers=att.get("steers")))
        record_attempt(wid, request, cap, result, att["cost"], attempt, verdict,
                       remember=remember and (verdict["passed"] or final),
                       tokens=att.get("tokens"), model=att.get("model"), project=project,
                       duration_s=att.get("duration_s"), backend=att.get("backend"),
                       fallback_from=att.get("fallback_from"),
                       fallback_reason=att.get("fallback_reason"))
        if verdict["passed"]:
            break
        # Safe local write escalation (issue #172): a WRITE cap that ran locally and FAILED verify
        # escalates off local — the rest of the ladder (incl. the final strongest-model rung) runs
        # on Claude instead of retrying on the same weak local model, which would dead-end or ship
        # a shallow PR. A local write that PASSES never reaches here (loop broke above).
        # Strict mode keeps a verify-failed local run LOCAL: the ladder retries on the same local
        # model and lands in needs-human rather than being rescued by Claude.
        # A HARNESS DEATH IS NOT A VERDICT, so it must not banish the run from local either.
        # `verdict["source"] == "harness"` means the attempt errored or ran out of turns and NO
        # judge ever read the work — the same reason the rung accounting below spares it. Measured
        # (`web-a056884d`): deepseek emitted 75k output tokens of reasoning and hit
        # LOCAL_EXEC_MAX_TOKENS with no final answer, which latched the rest of the ladder onto
        # Claude and cost three attempts ending on Opus ($2.60) — for a token ceiling an env var
        # raises. This is the same distinction gateway-backends.md already draws for `local_wall`:
        # a deterministic wall latches, a budget death is the model working and not finishing, so
        # a retry with the critique folded in can do better and stays local.
        if (write_escalate and config.setting("local_fallback") and not local_disabled
                and att.get("write_local") and verdict.get("source") != "harness"):
            local_disabled, local_disabled_reason = True, config.WRITE_LOCAL_ESCALATE_REASON
            trace("ESCALATE", f"{wid} write cap failed verify on local — rest of ladder on Claude")
        critique = verdict["critique"]
        if not final:
            trace("RETRY", f"{wid} attempt {attempt} failed verification — retrying with critique")
        if verdict.get("source") == "harness":
            spare -= 1
            if spare < 0:
                harness_stopped = True
                break
        else:
            judged += 1
            if judged >= n:
                break
    return {"att": att, "wid": wid, "result": result,
            "passed": bool(verdict and verdict["passed"]),
            "critique": None if not verdict else verdict.get("critique"),
            "cost": cost, "tokens_out": tokens_out, "attempts": attempt,
            "strict_stop": strict_stopped, "budget_stop": budget_stopped,
            "auth_stop": auth_stopped, "auth_wall": auth_wall,
            "harness_stop": harness_stopped}


def execute(request, cap, extra_tools=None, mcp_config_path=None, resume_session=None,
           memory_enabled=True, model_override=None):
    """Run a capability through the verify->retry->escalate loop and record every attempt.

    With resume_session, continue that Claude session (the request is the user's raw
    follow-up) as a single shot — no verification, since a mid-conversation reply isn't a
    fresh task to judge. No production ingress calls this since the direct path was removed
    (#278); it is the synchronous entry point the test suite drives, and since the loop body
    moved into `_ladder_core` those tests now exercise the SAME code plan-mode runs in
    production rather than a copy of it.

    `memory_enabled`/`model_override` are per-chat overrides (Otto chat composer) — not
    applied on a resume (memory never applies there; the model is bound to the session)."""
    project = _resolve_project(cap)
    if resume_session:
        att = run_attempt(request, cap, extra_tools=extra_tools,
                          mcp_config_path=mcp_config_path, resume_session=resume_session,
                          project=project)
        record_attempt(att["workflow"], request, cap, att["result"], att["cost"],
                       att["attempt"], None, remember=True,
                       tokens=att.get("tokens"), model=att.get("model"), project=project,
                       duration_s=att.get("duration_s"))
        return {"workflow": att["workflow"], "result": att["result"], "cost": att["cost"],
                "session_id": att["session_id"], "attempts": 1, "verified": None}

    out = _ladder_core(request, cap, None, recall=True, project=project,
                       extra_tools=extra_tools, mcp_config_path=mcp_config_path,
                       memory_enabled=memory_enabled, model_override=model_override, budget=True)
    att = out["att"]
    needs_human = ({"reason": error_classifier.claude_wall_reason(out.get("auth_wall"))}
                   if out["auth_stop"]
                   else ({"reason": config.STRICT_STOP_REASON} if out["strict_stop"]
                   else ({"reason": "budget_exceeded"} if out["budget_stop"]
                         else (None if out["passed"] else
                               {"reason": ("harness_exhausted" if out.get("harness_stop")
                                           else "verify_exhausted")}))))
    return {"workflow": out["wid"], "result": att["result"], "cost": out["cost"],
            "session_id": att["session_id"], "attempts": att["attempt"],
            "verified": out["passed"], "needs_human": needs_human}


def _run_ladder(request, cap, wid, recall=False, project=None, remember=True, write_escalate=True,
               memory_enabled=True, model_override=None):
    """The ladder for ONE plan step. Thin adapter over `_ladder_core` — see there for the loop.
    Returns {result, passed, critique, cost, tokens_out, attempts, strict_stop, auth_stop};
    `run_plan` uses
    the pass/fail + critique per step. `remember=False` skips fact/solution distillation (per-step
    plan runs aren't fresh top-level tasks — the plan's synthesis is what would be worth
    remembering). `write_escalate=False` opts out of the safe-local-write escalation (issue #172):
    plan-mode steps deliberately STAY local and re-plan on exhaustion rather than escalating
    execution to Claude, so run_plan passes False; every top-level ladder keeps the default True.

    `budget=False`: a plan step does NOT carry the per-run cost ceiling. That predates this
    refactor and is preserved deliberately — whether an N-step plan should be able to spend N
    budgets is a real question, but not one a refactor gets to answer silently."""
    out = _ladder_core(request, cap, wid, recall=recall, project=project, remember=remember,
                       write_escalate=write_escalate, memory_enabled=memory_enabled,
                       model_override=model_override, budget=False)
    return {k: out[k] for k in
            ("result", "passed", "critique", "cost", "tokens_out", "attempts", "strict_stop",
             "auth_stop", "auth_wall")}
