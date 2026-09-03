"""Temporal ACTIVITIES — the side-effecting steps.

Thin wrappers over the engine so the Temporal path and the web path share ONE
implementation: routing via the gateway, clarification, MCP config, execution, and
recording. Anything non-deterministic (Claude calls, file IO) lives here, never in
the workflow.
"""
import contextlib
import contextvars
import copy
import functools
import os
import re
import threading
import time

from temporalio import activity

import config
import storage
import engine
import policy
import registry
import workspace

_caps = None


@contextlib.contextmanager
def _heartbeating(what, every_s=None):
    """Beat on a timer for as long as a long BLOCKING activity runs.

    This is not progress reporting — it is the only way Temporal learns the WORKER died.
    Without it, a killed or restarted worker is invisible until `start_to_close_timeout`,
    so that ceiling doubles as the stall a restart costs and cannot be raised. With it, the
    call site's `heartbeat_timeout` is what a death costs, and `start_to_close` is free to be
    the honest task ceiling (`ExecutionHeartbeatTests`).

    Deliberately a timer and not per-turn progress: a single legitimate tool call (a long
    `terraform` run, a slow clone) emits nothing for minutes, and killing THAT is a worse bug
    than the one this fixes. `start_to_close` stays the bound on a wedged CLI.

    Activities are sync and run in worker.py's thread pool, so the beat needs its own thread —
    and a bare thread does NOT inherit contextvars, which is where temporalio keeps the
    activity context. Hence `copy_context()`: `activity.heartbeat()` raises `RuntimeError`
    (not in an activity) from a naive thread. Outside an activity entirely (direct calls,
    tests) this no-ops rather than pretending."""
    if not activity.in_activity():
        yield
        return
    ctx = contextvars.copy_context()
    stop = threading.Event()
    started = time.monotonic()

    def beat():
        while not stop.wait(every_s if every_s is not None else config.HEARTBEAT_EVERY_S):
            try:
                ctx.run(activity.heartbeat, what, int(time.monotonic() - started))
            except Exception:  # noqa: BLE001 - a beat must never take the activity down
                continue      # NOT `return`: one transient miss would silently stop beating,
                              # and heartbeat_timeout would then kill a healthy attempt

    t = threading.Thread(target=beat, name=f"otto-heartbeat-{what}", daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


def _heartbeats(what, every_s=None):
    """`_heartbeating` as a decorator, so an activity declares "I block for a long time" at its
    definition. Sits UNDER @activity.defn — functools.wraps keeps the registered name."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with _heartbeating(what, every_s):
                return fn(*a, **kw)
        return wrapper
    return deco


def _capabilities():
    global _caps
    if _caps is None:
        _caps = registry.load()
        registry.apply_policy(_caps, policy.load())
    return _caps


def _cap(name):
    caps = _capabilities()
    c = next((c for c in caps if c.name == name), None)
    # Tolerate a "kind:name" form (e.g. a config default like "agent:sre-qa"): catalogue cap
    # names are bare (agents/skills keep their frontmatter name), so an exact match on a
    # prefixed string would silently miss and report the cap as unavailable.
    if c is None and ":" in name:
        bare = name.split(":", 1)[1]
        c = next((c for c in caps if c.name == bare), None)
    return c


def _project_root(payload):
    """Resolve the run's repo context (a `repo` name/path) to a registered repo ROOT, so routing
    can keep repo-scoped project caps eligible only when the run actually targets their repo.
    Accepts a bare string (legacy: no repo) or {request, repo}."""
    if isinstance(payload, str):
        return None
    repo = payload.get("repo")
    return (workspace.resolve(repo) or {}).get("path") if repo else None


@activity.defn
def route_request(payload) -> dict:
    request = payload if isinstance(payload, str) else payload.get("request", "")
    cap = engine.plan(request, _capabilities(), project_root=_project_root(payload))
    activity.logger.info(f"routed -> [{cap.kind}] {cap.name} ({cap.risk})")
    return {"kind": cap.kind, "name": cap.name, "risk": cap.risk}


@activity.defn
def plan_swarm(payload) -> dict:
    """Swarm planner: decide whether a fresh request fans out into several independent
    sub-tasks. Returns {subtasks: [{request, cap}]}; an empty list means 'don't fan out'
    (the workflow then takes the single-capability path). Caps are resolved from the trusted
    registry, like route_request — never taken from a client."""
    request = payload if isinstance(payload, str) else payload.get("request", "")
    tasks = engine.decompose(request, _capabilities(), project_root=_project_root(payload))
    subtasks = [{"request": t["request"],
                 "cap": {"name": t["cap"].name, "kind": t["cap"].kind, "risk": t["cap"].risk}}
                for t in tasks]
    if subtasks:
        activity.logger.info(f"swarm plan -> {len(subtasks)} sub-tasks: "
                             + ", ".join(s["cap"]["name"] for s in subtasks))
    return {"subtasks": subtasks}


@activity.defn
def merge_results(payload: dict) -> dict:
    """Synthesize the swarm's sub-task results into one coherent response. `audience` shapes it:
    this text is what gets DELIVERED, so a swarm answering someone on Slack ends as a reply, not as
    a report about them (the children stay report-shaped — only this function reads them)."""
    return {"result": engine.merge(payload["request"], payload.get("parts", []),
                                   audience=payload.get("audience"))}


@activity.defn
def plan_task_steps(payload: dict) -> dict:
    """Plan-then-execute PLANNER (design doc 2026-07-16): decide whether to decompose this run
    into atomic steps a LOCAL executor runs one at a time, and if so return the ordered step
    list. The activation check (config.PLAN_MODE + whether the executor is local) lives HERE so
    the deterministic workflow never does a model/config lookup. Returns {steps: [...]}; an EMPTY
    list means 'not plan mode / not worth planning' and the workflow falls through to the normal
    verify ladder. The planner itself is pinned to Claude (engine.plan_steps)."""
    cap = _cap(payload["name"])
    if cap is None or not engine.plan_mode_active(cap, requested=payload.get("requested", False)):
        return {"steps": []}
    steps = engine.plan_steps(payload["request"], cap)
    if steps:
        activity.logger.info(f"plan-then-execute -> {len(steps)} atomic steps for [{cap.name}]")
    return {"steps": steps}


@activity.defn
@_heartbeats("plan-exec")
def execute_plan(payload: dict) -> dict:
    """Execute a planned chain of atomic steps (from plan_task_steps) on the LOCAL executor, one
    step at a time, with dependency-scoped context injection + per-step verify + bounded re-plan
    (engine.run_plan). Runs the WHOLE plan in ONE activity: the durability granularity is coarse
    (a worker crash re-runs the plan from the top), but every step's attempts are AUDITED as they
    go and plan execution is local/cheap — an acceptable v1 cut. The workflow uses _RETRY_EXEC
    (max_attempts=1) so a lost activity surfaces as a failed run rather than silently re-running
    mid-plan (issue #91). Returns {result, passed, cost, tokens, steps_run, replans, budget_stop,
    strict_stop}.

    `authored=True` marks a plan a HUMAN wrote (a runbook) rather than plan_task_steps: it turns
    OFF the LLM tail re-plan and turns ON per-step capability resolution. `_cap` is the trusted
    registry lookup, so a step naming a cap that no longer exists stops the plan before it spends
    anything (surfaced here as a normal failed run, not an activity crash)."""
    cap = _cap(payload["name"])
    project = engine._resolve_project(cap, payload.get("repo"))
    authored = bool(payload.get("authored"))
    try:
        out = engine.run_plan(payload["request"], cap, payload["steps"],
                              wid=payload.get("wid"), project=project,
                              model_override=payload.get("model_override"),
                              replan=not authored,
                              resolve_cap=_cap if authored else None)
    except ValueError as e:      # unresolvable per-step cap — nothing ran, so nothing is half-done
        return {"result": f"This runbook could not start: {e}", "passed": False, "cost": 0,
                "tokens": None, "steps_run": 0, "replans": 0, "budget_stop": False,
                "strict_stop": False, "auth_stop": False, "auth_wall": None}
    return {"result": out["result"], "passed": out["passed"], "cost": out["cost"],
            "tokens": out["tokens"], "steps_run": out["steps_run"],
            "replans": out["replans"], "budget_stop": out["budget_stop"],
            "strict_stop": out.get("strict_stop", False),
            "auth_stop": out.get("auth_stop", False),
            "auth_wall": out.get("auth_wall")}


@activity.defn
def clarify_request(payload: dict) -> dict:
    cap = _cap(payload["name"])
    return {"question": engine.clarify(payload["request"], cap) if cap else None}


@activity.defn
def classify_followup(payload: dict) -> dict:
    """Re-assess a resumed follow-up for write intent. Read BOTH ways by the workflow: an
    emergent write under a read-bound session ("now publish those comments") is gated, and a
    pure question under a WRITE-bound session (every follow-up in a repo-mode ticket chat) drops
    to a read-only discussion turn instead of paying a plan preview + gate."""
    cap = _cap(payload["name"])
    return {"write": engine.followup_write_intent(payload["message"], cap,
                                                  repo=payload.get("repo"))}


@activity.defn
@_heartbeats("plan")
def plan_capability(payload: dict) -> dict:
    """Pre-approval PLAN pass: a strictly read-only run that returns the concrete operations the
    capability would perform, surfaced in the approval gate so the human approves the actual plan
    rather than just the cap name. For a repo-mode run the cwd is the targeted repo's LIVE
    checkout (read-only — config.PLAN_TOOLS has no Bash/Edit/Write), so the plan can name real
    files; otherwise it's the cap's own cwd.

    The plan is then CRITIQUED (engine.critique_plan) in the same activity, so the gate shows the
    human what would go wrong alongside what would happen — a plan is the one artefact with no
    verify ladder behind it, and its dangerous failures (enforcing before callers are ready, a
    copied rule that matches nothing, a restriction that also cuts off a metrics scrape) read as
    perfectly reasonable in a numbered list. Advisory: it never blocks or edits the plan.

    Returns {plan, concerns, cost, tokens} (empty string / empty list / 0 / None if none)."""
    cap = _cap(payload["name"])
    if cap is None:
        return {"plan": "", "concerns": [], "cost": 0, "tokens": None}
    cwd = payload.get("cwd")
    if not cwd and payload.get("repo"):
        r = workspace.resolve(payload["repo"])
        cwd = r["path"] if r else None
    preview = engine.plan_preview(payload["request"], cap, cwd=cwd,
                                  resume_session=payload.get("resume"),
                                  wid=payload.get("wid"),
                                  # The open PR this request works on, resolved before the gate:
                                  # the preview's cwd is the DEFAULT branch, so without this the
                                  # planner reasons about a tree missing the code (web-a6122d6c).
                                  pr=payload.get("pr"),
                                  # Same level the execution will run at, so the human approves
                                  # the plan the run actually follows.
                                  effort=payload.get("effort"))
    plan = preview["plan"]
    crit = engine.critique_plan(payload["request"], cap, plan,
                                project=engine._resolve_project(cap, payload.get("repo")))
    return {"plan": plan, "concerns": crit["concerns"],
            "cost": preview["cost"], "tokens": preview["tokens"],
            # WHO wrote the plan the human is about to approve. Invisible until now, which is
            # how a silent downgrade to the cheapest tier went unnoticed.
            "model": preview.get("model")}


@activity.defn
def suggest_repo(payload: dict) -> dict:
    """Interactive auto-detect of repo-mode: if a request UNAMBIGUOUSLY names one registered repo
    AND actually edits its code, return that repo so the workflow can auto-engage an isolated
    clone + draft PR — no upfront repo picker needed. Returns {repo: name|None}. The pure
    name-match (candidate_repo) gates the (cheap) edit-intent LLM call, so the classifier only
    runs when a repo is actually named."""
    names = [r["name"] for r in workspace.git_repos()]
    repo = engine.auto_engage_repo(payload.get("request", ""), names, payload.get("name"))
    return {"repo": repo}


@activity.defn
def classify_request(payload: dict) -> dict:
    """Re-assess a freshly-routed request for write intent, so a write-intent request that
    Router #1 misrouted to a read-classified capability still hits the approval gate. When the
    misroute landed on the general ASSISTANT (which only answers — a risk bump would gate a run
    that then refuses the task), also return the general worker as a `redirect` candidate; the
    workflow applies it only for a routed (non-pinned) cap."""
    cap = _cap(payload["name"])
    write = engine.request_write_intent(payload["request"], cap)
    out = {"write": write}
    if write and cap is not None:
        swap = engine.assistant_write_redirect(cap, _capabilities())
        if swap is not None:
            out["redirect"] = {"kind": swap.kind, "name": swap.name, "risk": swap.risk}
    return out


def _mcp():
    """Build the active MCP tool list + on-disk config (shared by every attempt)."""
    pol = policy.load()
    mcp_tools = [f"mcp__{n}" for n in policy.enabled_mcps(pol)]
    active = policy.active_mcp_config(pol)
    mcp_path = None
    if active:
        # Fixed path shared by every concurrent run in this worker; the content is
        # run-invariant (policy.load()), so an atomic replace is enough — a plain open("w")
        # let a peer's `claude -p --mcp-config` read the file mid-truncate.
        mcp_path = os.path.join(config.DATA_DIR, ".mcp-active.json")
        storage.write_json(mcp_path, active)
    return mcp_tools, mcp_path


@activity.defn
@_heartbeats("provision")
def provision_workspace(payload: dict) -> dict:
    """Clone an allowlisted repo into an isolated workspace (issue #57). `from_branch` checks
    out the run's existing remote branch instead of a fresh one (post-PR QA fix round)."""
    import workspace
    return workspace.provision(payload["repo"], payload["run_id"],
                               from_branch=payload.get("from_branch", False),
                               branch=payload.get("branch"))


@activity.defn
@_heartbeats("finalize")
def finalize_workspace(payload: dict) -> dict:
    """Commit remaining changes, push the branch, open a draft PR. Best-effort (never raises).
    `existing_pr` pushes to update an already-open PR (a QA fix) and skips `gh pr create`.
    A fresh PR's title/body are drafted from the request + the run's result summary
    (engine.pr_copy, memory tier, raw-request fallback) instead of the raw request verbatim.
    `plan` (the approved plan, when the run had one) is posted as a PR comment, never committed
    into the target repo — see workspace.post_plan."""
    import workspace
    title, body = payload.get("title"), None
    if not payload.get("existing_pr"):
        copy = engine.pr_copy(payload.get("title") or "", summary=payload.get("summary"))
        title, body = copy["title"], copy["body"]
    return workspace.finalize(payload["run_id"], title=title,
                              base_head=payload.get("head"),
                              existing_pr=payload.get("existing_pr", False),
                              branch=payload.get("branch"), body=body,
                              plan=payload.get("plan"), request=payload.get("request"),
                              cap=payload.get("cap"), concerns=payload.get("concerns"))


@activity.defn
def recover_pr_branch(payload: dict) -> dict:
    """Recover the exact branch a resumed repo-mode run should re-provision from, when the chat
    didn't record git_branch (an agent-managed cap pushed its own branch, or an older chat).
    Reads the original run's result from the audit trail, extracts the PR it opened, and resolves
    that PR's head branch via `gh` — scoped to the same allowlisted repo, so it can never point a
    follow-up at another repo's PR. Returns {branch, pr_url} (branch None when the PR exists but
    its head can't be resolved) or {} when the run opened no PR at all."""
    import workspace
    url = engine.pr_url_from_run(payload.get("wid"))
    if not url:
        return {}
    # Report the PR even when its branch can't be resolved (deleted after merge, or a `gh` failure).
    # The resume ladder's last tier keys on "was anything ever pushed?" — collapsing this case to {}
    # would read as "no PR", sending a MERGED run's follow-up to a clean clone and silently dropping
    # the commits it might amend. Absence of a branch is a dead end there; absence of a PR is not.
    return {"branch": workspace.pr_branch(payload.get("repo"), url), "pr_url": url}


@activity.defn
def pr_head_branch(payload: dict) -> dict:
    """The head branch of an already-open PR — what a post-PR fix round must commit onto. NOT
    `otto/<run_id>`: a run asked to amend an existing PR pushes to THAT PR's branch, so Otto's
    own branch was never pushed and re-provisioning it fetches a ref that does not exist.
    Scoped to the allowlisted repo by `workspace.pr_branch`; {branch: None} when unresolvable."""
    import workspace
    return {"branch": workspace.pr_branch(payload.get("repo"), payload.get("pr_url"))}


@activity.defn
def resolve_pr_target(payload: dict) -> dict:
    """The OPEN pull request a FRESH request asks to work on, so repo-mode branches off THAT
    PR's head instead of the default branch. {} when the request names none — which is the
    overwhelmingly common case and the behaviour that predates this activity.

    An activity, not workflow code, because it shells out to `gh`: the answer depends on live
    GitHub state and must be recorded once in history, not re-derived on every replay."""
    import workspace
    return workspace.pr_target(payload.get("repo"), payload.get("request")) or {}


@activity.defn
def check_grounding(payload: dict) -> dict:
    """Concrete claims the request makes about the tree that the provisioned tree contradicts —
    a named file that isn't there, a line number the file is far too short to have. Advisory:
    the note steers the executor and the judge, it never blocks a run. See workspace.grounding."""
    import workspace
    return {"notes": workspace.grounding(payload.get("path"), payload.get("request"))}


@activity.defn
def cleanup_workspace(payload: dict) -> None:
    import workspace
    workspace.cleanup(payload["run_id"])


@activity.defn
def snapshot_repos(payload: dict) -> dict:
    """git state of registered repos before a non-repo-mode run, to detect in-place edits (#59)."""
    import workspace
    return {"snap": workspace.snapshot()}


@activity.defn
def detect_repo_changes(payload: dict) -> dict:
    """Compare the live checkouts against a pre-run snapshot; audit + return any that changed."""
    import workspace
    changed = workspace.diff(payload.get("before") or {}, workspace.snapshot())
    if changed:
        engine.audit_repo_changes(payload.get("wid"), payload.get("request", ""), changed)
    return {"changed": changed}


@activity.defn
@_heartbeats("run")
def run_capability(payload: dict) -> dict:
    """One execution attempt. The verify->retry loop is driven by the workflow, which
    passes the attempt number, the previous critique, and whether to escalate the model."""
    cap = _cap(payload["name"])
    # This turn's risk, when the workflow decided it differs from the registry's (today: a
    # discussion follow-up in a write-bound session — see workflows' resume classify block).
    # COPY the cap: `_capabilities()` memoizes one list for the worker's whole lifetime, so
    # mutating the shared object would silently re-risk that capability for every later run.
    # Only ever narrows — a payload asking to WIDEN read->write is ignored, so the gate can
    # never be bypassed by anything that reaches this dict.
    discussion = payload.get("risk") == "read" and cap is not None and cap.risk == "write"
    if discussion:
        cap = copy.copy(cap)
        cap.risk = "read"
    mcp_tools, mcp_path = _mcp()
    project = engine._resolve_project(cap, payload.get("repo"))   # issue #69
    att = engine.run_attempt(
        payload["request"], cap,
        attempt=payload.get("attempt", 1), critique=payload.get("critique"),
        escalate=payload.get("escalate", False), downshift=payload.get("downshift", False),
        extra_tools=mcp_tools,
        mcp_config_path=mcp_path, resume_session=payload.get("resume"), wid=payload.get("wid"),
        cwd=payload.get("cwd"), recall=payload.get("recall", False), project=project,
        local_disabled=payload.get("local_disabled", False),
        local_disabled_reason=payload.get("local_disabled_reason"), repo=payload.get("repo"),
        audience=payload.get("audience"),
        # The plan a human approved at the gate (workflows._plan). Absent for unattended
        # auto-approve, reads and resumes — engine treats None as "no plan was agreed".
        approved_plan=payload.get("approved_plan"),
        # Where the checked-out tree contradicts the request (workspace.grounding). Advisory —
        # it steers what the attempt REPORTS, it never blocks the run.
        grounding=payload.get("grounding"),
        # Per-chat overrides (Otto chat composer): default on/none, so an older caller that
        # never sends these keys behaves exactly as before.
        memory_enabled=payload.get("memory_enabled", True),
        model_override=payload.get("model_override"),
        effort=payload.get("effort"),
        discussion=discussion,
        supervise_enforce=payload.get("supervise_enforce", True))
    return {"workflow": att["workflow"], "result": att["result"], "cost": att["cost"],
            "tokens": att.get("tokens"), "model": att.get("model"),
            "session_id": att.get("session_id"), "attempt": att["attempt"],
            "is_error": att.get("is_error", False), "duration_s": att.get("duration_s"),
            "local": att.get("local", False), "backend": att.get("backend"),
            "fallback_from": att.get("fallback_from"),
            "fallback_reason": att.get("fallback_reason"),
            "local_incapable": att.get("local_incapable", False),
            "write_local": att.get("write_local", False),
            # The tools this attempt actually CALLED — the judge's real grant. Must be listed
            # HERE or it never reaches the workflow: this dict is a whitelist, not a passthrough.
            "tools_used": att.get("tools_used") or [],
            "tools_failed": att.get("tools_failed") or [],
            # Corrections the mid-run supervisor delivered into this attempt. Listed HERE for the
            # same reason as tools_used — this dict is a whitelist, and the verify activity below
            # cannot judge an amended request it never hears about.
            "steers": att.get("steers") or [],
            # Strict mode (OTTO_LOCAL_FALLBACK=0): local failed and Claude may not cover for it —
            # the workflow ladder stops on this instead of taking another rung.
            "local_strict_stop": att.get("local_strict_stop", False),
            # A deterministic Claude-backend wall (dead login, spent usage limit, unservable
            # model) — terminal for the ladder, same shape as above. `claude_wall` names WHICH,
            # so the needs-human row and the operator's message carry the right remedy.
            "auth_stop": att.get("auth_stop", False),
            "claude_wall": att.get("claude_wall"),
            # Whether the supervisor KILLED this attempt. The workflow used to infer it by
            # sniffing the "(aborted by supervisor:" sentinel out of the result text; it needs
            # the fact itself to bound kills per run (config.MAX_SUPERVISOR_KILLS).
            "killed_by_supervisor": bool((att.get("supervision") or {}).get("killed"))}


@activity.defn
def snapshot_settings(payload: dict) -> dict:
    """Resolve every UI-editable runtime setting ONCE, at run start (config.settings_snapshot).

    Why an activity and not a plain `config.setting()` call in the workflow: the store is mutable
    (Admin writes it live), and deterministic workflow code that re-read it could take a different
    branch on replay than history recorded — e.g. a ladder that started with max_attempts=3 and
    replays after a worker restart with 2. An activity's RESULT is recorded in history, so the
    snapshot replays identically forever, and an Admin edit applies to the NEXT run."""
    return config.settings_snapshot()


@activity.defn
def estop_check(payload: dict) -> dict:
    """Is the global pause engaged? An `os.stat`, wrapped as an activity because the workflow
    can't touch the filesystem.

    The in-process ingresses (board poll, Slack poll, `/api/submit`, webhook, run-now) all check
    `estop.blocked` themselves and never reach a workflow — this is the backstop for the one
    ingress that has no in-process step: a Temporal SCHEDULE fires from the Temporal server, so a
    cron run is already a live workflow before any Otto code sees it. Checking here is what makes
    the pause cover every ingress rather than four of five."""
    import estop
    return estop.status()


@activity.defn
def verify_capability(payload: dict) -> dict:
    """Judge a completed attempt — returns {passed, critique}. The run's repo (if any)
    resolves to a registered project so the judge sees that repo's own CLAUDE.md
    conventions — always on, no config (the PR #251 gap)."""
    cap = _cap(payload["name"])
    return engine.verify(payload["request"], cap, payload["result"],
                         project=engine._resolve_project(cap, payload.get("repo")),
                         local=payload.get("local", False),
                         unattended=payload.get("unattended", False),
                         audience=payload.get("audience"),
                         approved_plan=payload.get("approved_plan"),
                         grounding=payload.get("grounding"),
                         tools_used=payload.get("tools_used"),
                         tools_failed=payload.get("tools_failed"),
                         steers=payload.get("steers"))


@activity.defn
@_heartbeats("qa")
def qa_capability(payload: dict) -> dict:
    """Run the configured QA capability (default agent:sre-qa, config.QA_CAP) against an opened
    PR, for the post-PR QA loop. PRE-AUTHORIZED: enabling the loop is the grant, so this runs
    without a gate (sre-qa self-limits its apply/destroy to dev/staging). Returns the QA
    transcript + metadata, or {missing:True} if the QA cap isn't registered/enabled."""
    cap = _cap(config.QA_CAP)
    if cap is None:
        activity.logger.info(f"QA requested but cap '{config.QA_CAP}' is not registered/enabled")
        return {"missing": True, "qa_cap": config.QA_CAP}
    mcp_tools, mcp_path = _mcp()
    req = engine.qa_review_request(payload["pr_url"], payload.get("repo"), payload["request"])
    att = engine.run_attempt(req, cap, attempt=1, extra_tools=mcp_tools,
                             mcp_config_path=mcp_path, wid=payload.get("wid"))
    return {"workflow": att["workflow"], "result": att["result"], "cost": att["cost"],
            "tokens": att.get("tokens"), "model": att.get("model"), "duration_s": att.get("duration_s"),
            "qa_cap": cap.name, "qa_risk": cap.risk}


@activity.defn
def judge_qa(payload: dict) -> dict:
    """Classify a QA transcript into {verdict: pass|fail|inconclusive, critique}, judged
    against the target repo's own CLAUDE.md conventions (QA loop is repo-mode only)."""
    return engine.judge_qa(payload["request"], payload["result"],
                           project=engine._resolve_project(None, payload.get("repo")))


@activity.defn
@_heartbeats("review")
def review_capability(payload: dict) -> dict:
    """Run the configured review capability (default: the stock code-reviewer, config.REVIEW_CAP)
    against an opened PR, for the post-PR code-review loop. PRE-AUTHORIZED (enabling the loop is
    the grant); the reviewer is read-only (it inspects the PR via `gh`). Returns the review
    transcript + metadata, or {missing:True} if the review cap isn't registered/enabled."""
    cap = _cap(config.REVIEW_CAP)
    if cap is None:
        activity.logger.info(f"review requested but cap '{config.REVIEW_CAP}' is not "
                             "registered/enabled")
        return {"missing": True, "review_cap": config.REVIEW_CAP}
    mcp_tools, mcp_path = _mcp()
    req = engine.review_request(payload["pr_url"], payload.get("repo"), payload["request"])
    att = engine.run_attempt(req, cap, attempt=1, extra_tools=mcp_tools,
                             mcp_config_path=mcp_path, wid=payload.get("wid"))
    return {"workflow": att["workflow"], "result": att["result"], "cost": att["cost"],
            "tokens": att.get("tokens"), "model": att.get("model"),
            "duration_s": att.get("duration_s"), "review_cap": cap.name, "review_risk": cap.risk}


@activity.defn
def judge_review(payload: dict) -> dict:
    """Classify a review transcript into {verdict: pass|fail|inconclusive, critique} (pass=clean,
    fail=has must/should-fix findings), judged against the target repo's own CLAUDE.md
    conventions (the review loop is repo-mode only)."""
    return engine.judge_review(payload["request"], payload["result"],
                               project=engine._resolve_project(None, payload.get("repo")))


@activity.defn
def record_attempt(payload: dict) -> None:
    """Audit one attempt (with its verdict); distil memory when it's the final/passing one."""
    cap = _cap(payload["name"])
    if cap:
        engine.record_attempt(payload["wid"], payload["request"], cap, payload["result"],
                              payload.get("cost", 0), payload.get("attempt", 1),
                              payload.get("verdict"), remember=payload.get("remember", False),
                              tokens=payload.get("tokens"), model=payload.get("model"),
                              repo=payload.get("repo"), duration_s=payload.get("duration_s"),
                              backend=payload.get("backend"),
                              fallback_from=payload.get("fallback_from"),
                              fallback_reason=payload.get("fallback_reason"),
                              project=engine._resolve_project(cap, payload.get("repo")))


@activity.defn
def deliver_result(payload: dict) -> dict:
    """Send a finished unattended run's result to its reply target (webhook, etc.)."""
    import delivery
    reply_to = payload.get("reply_to") or {}
    status = delivery.deliver(reply_to, payload.get("result", ""),
                              (payload.get("cap") or {}).get("name"), run_id=payload.get("run_id"))
    activity.logger.info(f"delivered result -> {status}")
    # A Slack conversation Otto just answered in becomes CONTINUABLE: record the session id, the
    # capability, and the reply text (which the handoff classifier reads next turn) so the next
    # message resumes this conversation. Also clears the in-flight marker, whatever the delivery
    # outcome — a jammed conversation would swallow every later message.
    if reply_to.get("kind") == "slack_thread":
        import slack
        # A silent turn still binds the session (the next message must resume it), but it must not
        # become `last_reply` — that's what the handoff classifier reads next turn, and "NO_REPLY"
        # is not something Otto said. Falsy leaves the previous reply in place.
        result = payload.get("result", "")
        slack.record_conversation_session(
            reply_to.get("channel"), reply_to.get("thread_ts"),
            session=payload.get("session_id"), cap=payload.get("cap"),
            last_reply="" if config.is_no_reply(result) else result)
    # A failed/partial delivery is reported here (not raised — delivery never fails the run) so the
    # workflow can record a terminal audit row instead of the result silently vanishing.
    failed = ("failed" in status.lower()) or ("could not" in status.lower())
    return {"status": status, "failed": failed}


@activity.defn
def notify_human(payload: dict) -> dict:
    """Push-notify the owner that a run is blocked on them (issue #92): awaiting approval,
    awaiting clarification, or ended needs-human. Best-effort — delivery.notify never raises.
    A `kind: "complete"` payload (clean finish) is opt-in via OTTO_NTFY_ON_COMPLETE —
    delivery.notify drops it when the flag is off (env is read here, not in the workflow,
    so a flag flip can't cause a replay nondeterminism).

    The always-sent metadata lines are built HERE rather than in the workflow: `privacy` decides
    how coarsely a source may be named on a public topic (a Slack channel id is a pointer into a
    private DM; a webhook URL routinely carries a token), and that policy belongs next to the
    redaction it sits beside — not spread across replay-safe workflow code where a later edit
    would not see it. `detail` (the request text) is passed straight through and dropped by
    delivery.notify unless OTTO_NTFY_DETAIL is on."""
    import delivery
    import privacy
    lines = privacy.context_lines(cap=payload.get("cap"), repo=payload.get("repo"),
                                  reply_to=payload.get("reply_to"),
                                  unattended=payload.get("unattended", False),
                                  extra=[payload.get("note")])
    # Approve/Deny buttons, minted HERE for the same reason the lines are: a token is random, so
    # it can never be minted in replay-safe workflow code. Off unless OTTO_NTFY_ACTIONS is on.
    actions = None
    if payload.get("kind") == "approval" and config.NTFY_ACTIONS and payload.get("wid"):
        token = delivery.mint_action_token(payload["wid"])
        if token:
            actions = delivery.gate_actions(token)
    sent = delivery.notify(payload.get("title", ""), lines=lines,
                           detail=payload.get("detail"),
                           tags=payload.get("tags"),
                           priority=payload.get("priority", "high"),
                           kind=payload.get("kind"),
                           wid=payload.get("wid"),
                           actions=actions)
    return {"sent": sent}


@activity.defn
def finalize_terminal(payload: dict) -> dict:
    """Finalize a run that ended needing a human (a caught workflow failure, verify-exhaustion, a
    QA fail/inconclusive, or a budget stop). Writes a durable terminal audit row and, for a
    board-sourced run, moves the stuck card OUT of In Progress to the Blocked column and stamps a
    `needs-human` label (the label survives a board with no Blocked column). Never raises."""
    import board
    import delivery
    wid = payload.get("wid")
    engine.record_terminal(wid, payload.get("request", ""), payload.get("cap"),
                           payload.get("reason", "failed"), detail=payload.get("detail", ""),
                           repo=payload.get("repo"))
    import privacy
    # The terminal `detail` is free text of unknown provenance — a caught exception string, a
    # delivery status carrying the Slack channel id, a strict-stop message. The REASON is already
    # in the title, so the detail goes in the content-gated half with the request rather than in
    # the always-sent lines.
    delivery.notify(f"Otto needs you: {payload.get('reason', 'failed')}",
                    lines=privacy.context_lines(cap=payload.get("cap"), repo=payload.get("repo"),
                                                reply_to=payload.get("reply_to"),
                                                unattended=payload.get("unattended", False)),
                    detail="\n".join(x for x in (payload.get("detail"),
                                                 payload.get("request")) if x),
                    tags=["rotating_light"],
                    # HIGH, not max: the run is already over. Nothing is parked behind this one,
                    # so it is a post-mortem — reading it an hour later costs nothing, and pushing
                    # it at the same urgency as a live gate is what makes the gate stop meaning
                    # anything (128 of these in 30 days).
                    priority="high", kind="terminal",
                    wid=wid)
    reply_to = payload.get("reply_to") or {}
    moved = labelled = False
    if reply_to.get("kind") == "github_issue":
        repo, number = reply_to.get("repo"), reply_to.get("number")
        blocked_col = reply_to.get("blocked_col")
        option_id = (reply_to.get("status_options") or {}).get(blocked_col)
        moved = board.set_status_raw(reply_to.get("project_id"), reply_to.get("status_field_id"),
                                     reply_to.get("item_id"), option_id)
        labelled = board.add_label(repo, number, "needs-human")
    return {"audited": True, "moved": moved, "labelled": labelled}


@activity.defn
def open_chat(payload: dict) -> None:
    """Open the Chat sidebar thread at the START of an unattended run (keyed by `chat_key`), so
    the conversation is visible/accessible the moment Otto begins the ticket — not only when it
    finishes. Records the request + a pending placeholder; record_chat finalizes it later."""
    import chats
    chats.start_run(
        payload.get("chat_key"), payload.get("request", ""),
        title=payload.get("title"), labels=payload.get("labels"), cap=payload.get("cap"),
        run_id=payload.get("run_id"))


@activity.defn
def record_chat(payload: dict) -> None:
    """Finalize an unattended run's Chat thread (keyed by `chat_key`): fill in the result and
    session_id/cap so the thread is resumable. Pairs with open_chat (start of run). A scheduled
    job that re-fires opens + finalizes a fresh turn each time. `labels` tag its provenance."""
    import chats
    chats.finish_run(
        payload.get("chat_key"), payload.get("result", ""),
        session_id=payload.get("session_id"), cap=payload.get("cap"),
        repo=payload.get("repo"), git_run_id=payload.get("git_run_id"))


@activity.defn
def record_skip(payload: dict) -> None:
    cap = _cap(payload["name"])
    if cap:
        engine.record_skip(payload["request"], cap, payload.get("reason", "DENIED"),
                           wid=payload.get("wid"))


@activity.defn
@_heartbeats("board")
def poll_board(payload: dict) -> dict:
    """Poll the configured GitHub Projects board: pick up every issue parked in the Ready
    column as an unattended OttoWorkflow, claim it (move out of Ready), and return what was
    picked. Idempotent — a deterministic `gh-issue-<n>` workflow id means re-polling a still-
    Ready card never double-runs it. The result is delivered back to the issue by
    delivery._github_issue (see the `reply_to` enrichment below)."""
    import board
    import estop
    # Before load(), and before any card is read: claiming moves the card Ready -> In Progress,
    # so picking up while paused would strand it in a column nothing is working on.
    if estop.blocked("board"):
        return {"picked": [], "paused": True}
    cfg = board.load()
    if not board.enabled(cfg):
        return {"picked": [], "disabled": True}
    meta = board.project_meta(cfg)
    picked, skipped = [], []
    for issue in board.list_ready(cfg):
        n = issue.get("number")
        if n is None:
            continue
        params = board.issue_to_request(issue, cfg)
        # Resolve a pinned capability from the TRUSTED registry — never take risk from a label
        # (mirrors the event ingress in server._handle_event).
        if params.get("cap"):
            c = _cap(params["cap"])
            params["cap"] = ({"name": c.name, "kind": c.kind, "risk": c.risk} if c else None)
        # Repo-mode only for an allowlisted (registered) repo; otherwise run without it. Same
        # guard for `repo` (explicit repo-edit label) and `repo_hint` (the auto-engage candidate)
        # — never clone an unregistered repo (mirrors the event-ingress allowlist).
        if params.get("repo") or params.get("repo_hint"):
            import workspace
            if params.get("repo") and not workspace.resolve(params["repo"]):
                activity.logger.info(
                    f"issue #{n}: repo '{params['repo']}' not registered — running without repo-mode")
                params["repo"] = None
                params["reply_to"]["repo_edit"] = False
            if params.get("repo_hint") and not workspace.resolve(params["repo_hint"]):
                activity.logger.info(
                    f"issue #{n}: repo '{params['repo_hint']}' not registered — no auto repo-mode")
                params["repo_hint"] = None
        # Enrich reply_to with the ids delivery needs to move the card when the run finishes.
        params["reply_to"].update({
            "project_id": meta.get("project_id"),
            "status_field_id": meta.get("status_field_id"),
            "status_options": meta.get("options") or {},
            "review_col": (cfg.get("columns") or {}).get("review"),
            "done_col": (cfg.get("columns") or {}).get("done"),
            "blocked_col": (cfg.get("columns") or {}).get("blocked"),
        })
        wid = f"gh-issue-{n}"
        if board.start_run(wid, params):
            board.set_status(cfg, meta, issue.get("item_id"), (cfg.get("columns") or {}).get("active"))
            picked.append(n)
        else:
            skipped.append(n)
    if picked:
        activity.logger.info(f"board: picked up {len(picked)} issue(s): {picked}")
    return {"picked": picked, "skipped": skipped}


@activity.defn
@_heartbeats("slack")
def poll_slack(payload: dict) -> dict:
    """Poll Slack for new DMs / @-mentions from allowlisted people, start one unattended
    OttoWorkflow per message (deterministic `slack-<ch>-<ts>` id → REJECT_DUPLICATE makes a
    re-poll idempotent), and post the interim ack in-thread. The result is delivered back to the
    thread by delivery._slack. Advances a channel's read cursor only for a message it actually
    handled (started, duplicate, or answered as a pleasantry) so a transient start failure — or a
    failed greeting post — is retried next poll. Never raises.

    Continuity is per CONVERSATION, not per message (slack.conversation_key): a DM is one
    conversation and a channel thread is one, so a message arriving in either RESUMES the session
    that last answered there instead of starting a cold run. Which cursor advances follows the
    transport — a thread reply advances its own conversation cursor, because its ts is later than any
    still-unhandled top-level message and advancing the CHANNEL cursor on it would make Otto deaf to
    those.

    Two things a resume must not swallow. A message that delegates a NEW task re-routes as a fresh
    run (engine.followup_handoff — the same guard the web path uses): a resumed session keeps its cap
    for life and never engages repo-mode or the review loop, so a new task inside an old conversation
    would be done by whichever cap happened to answer first. And with no resumable session at all
    (first contact, or the previous run died) the fresh run carries the conversation transcript, so
    it can still resolve "it"/"that" instead of asking."""
    import estop
    import slack
    # Before slack.poll(), which is not read-only: it seeds first-sight cursors and stamps
    # `last_poll`. Skipping it entirely is what makes the downtime guard do the right thing on
    # release — a pause reads as exactly what it was (Otto wasn't listening), so DOWNTIME_S
    # elapses and the backlog is marked seen instead of answered hours late.
    if estop.blocked("slack"):
        return {"picked": [], "paused": True}
    cfg = slack.load()
    if not slack.enabled(cfg):
        return {"picked": [], "disabled": True}
    ack = cfg.get("ack_template") or slack._ACK_DEFAULT
    hello = cfg.get("greeting_template") or slack._GREETING_DEFAULT
    picked, skipped, greeted, resumed, handed_off = [], [], [], [], []
    # A backlog catch-up (or just several fast messages) can return multiple picks for the SAME
    # thread/DM in one poll() call — one ack per pick then reads as "On it… On it… On it…" stuttering
    # ahead of the actual replies. One ack per conversation per poll pass is enough to say "I'm on it".
    acked_ts = set()
    for msg in slack.poll(cfg):
        rec = msg.get("conversation")                   # the conversation's record, or None
        in_thread = bool(msg.get("in_thread"))
        root = msg.get("thread_ts") or msg["ts"]
        # Where Otto's own reply goes (channel level in a DM, in-thread in a channel) — the ack has
        # to land in the same place as the answer, so both come from slack.reply_target.
        ack_ts = slack.reply_target(msg).get("thread_ts")

        def _seen(msg=msg):
            slack.mark_seen(msg)

        # A pleasantry with no request never becomes a run — answering it costs one post instead
        # of a verify ladder that dead-ends in a needs-human banner (see slack.is_pleasantry).
        # Mid-conversation ("thanks!") it gets no reply at all: greeting_template introduces Otto
        # to a stranger, and re-introducing itself as a conversation's last word is noise.
        if slack.is_pleasantry(msg.get("text")):
            mid_conversation = bool(rec and rec.get("session"))
            if mid_conversation:
                _seen()
                greeted.append(slack.wid_for(msg))
            elif slack.post(msg["channel"], hello, thread_ts=ack_ts):
                _seen()
                greeted.append(slack.wid_for(msg))
            continue

        params, resume, handoff = None, False, None
        bound = _cap((rec or {}).get("cap", {}).get("name")) if rec else None
        if rec and rec.get("session") and bound:
            # A NEW task inside an existing conversation must not run inside the bound session (see
            # the docstring). Classified against the last reply Otto sent here, exactly as
            # /api/continue does; anything unclear stays a continuation.
            handoff = engine.followup_handoff(msg["text"], rec.get("last_reply"), bound)
            if not handoff:
                # Re-resolve the bound capability from the TRUSTED registry (the stored copy is
                # Otto's own, but the cap may have been renamed, disabled or removed since).
                params = slack.to_followup(
                    msg, {**rec, "cap": {"name": bound.name, "kind": bound.kind,
                                         "risk": bound.risk}}, cfg)
                resume = True
        if params is None:
            # A fresh run: no resumable session, or a handoff that must be routed from scratch.
            # Either way it carries what came before it in this conversation rather than guessing at
            # what "the other one" means — a DM's top level reads the channel, a thread reads the
            # thread. Fetched here (an activity may do I/O) and passed into the PURE to_request.
            if handoff:
                earlier = []        # the classifier already resolved the references it needed
            elif in_thread or (msg.get("thread_ts") and msg["thread_ts"] != msg["ts"]):
                earlier = slack.thread_context(msg["channel"], msg["thread_ts"])
            else:
                earlier = slack.channel_context(msg["channel"], msg["ts"])
            msg = {**msg, "thread": [ln for ln in earlier if msg["text"] not in ln]}
            params = slack.to_request(msg, cfg)
            if handoff:
                # The classifier resolved the references, so the routed request stands alone.
                params["request"] = handoff
            # Resolve a pinned cap from the TRUSTED registry — never take risk from a Slack payload.
            if params.get("cap"):
                c = _cap(params["cap"])
                params["cap"] = ({"name": c.name, "kind": c.kind, "risk": c.risk} if c else None)
            if rec and rec.get("wid"):
                params["chat_key"] = rec["wid"]         # same conversation, new session
        wid = slack.wid_for(msg)
        status = slack.start_run(wid, params)
        if status == "started":
            # Keyed on (channel, ack_ts) — a bare DM's ack_ts is always None, so keying on ack_ts
            # alone would wrongly suppress the ack for a SECOND person's DM in the same poll pass.
            ack_key = (msg["channel"], ack_ts)
            if ack_key not in acked_ts:
                slack.post(msg["channel"], slack._FOLLOWUP_ACK if resume else ack, thread_ts=ack_ts)
                acked_ts.add(ack_key)
            # Track this conversation from now on (or refresh it), marking the run in flight so the
            # next message waits for it to deliver instead of racing its session.
            slack.watch_conversation(msg["channel"], msg["thread_ts"] if in_thread else None,
                                     wid=None if rec else wid,
                                     seen=msg["ts"] if in_thread else None, pending=True)
            if not in_thread:
                slack.record_seen(msg["channel"], msg["ts"])
            picked.append(wid)
            if resume:
                resumed.append(wid)
            if handoff:
                handed_off.append(wid)
        elif status == "duplicate":
            _seen()                             # advance past an already-handled message
            skipped.append(wid)
        # status == "failed": leave the cursor so it's retried next poll.
    if picked or greeted:
        activity.logger.info(
            f"slack: picked up {len(picked)} message(s) ({len(resumed)} continuing a conversation, "
            f"{len(handed_off)} handed off as a new task), greeted {len(greeted)}")
    return {"picked": picked, "skipped": skipped, "greeted": greeted, "resumed": resumed,
            "handed_off": handed_off}


def _reap_state(wid):
    """Classify a stuck card's workflow: 'dead' (FAILED/TERMINATED/TIMED_OUT/CANCELED),
    'stale' (RUNNING past the stuck-TTL), or 'alive' (leave it). Conservative: any uncertainty
    (describe raises, transient error) returns 'alive' so a live run is never wrongly blocked.
    A COMPLETED workflow is left alone too — its delivery already ran and reported any move
    failure (see delivery status handling), so the reaper doesn't mislabel a clean pass."""
    import datetime as _dt
    import temporal_client as tc

    async def _q():
        c = await tc.client()
        h = c.get_workflow_handle(wid)
        desc = await h.describe()   # raises if the workflow id is unknown
        name = desc.status.name if desc.status else "RUNNING"
        if name in ("FAILED", "TERMINATED", "TIMED_OUT", "CANCELED"):
            return "dead"
        if name == "COMPLETED":
            return "alive"
        start = getattr(desc, "start_time", None)
        if start and config.STUCK_TTL_H:
            age_h = (_dt.datetime.now(_dt.timezone.utc) - start).total_seconds() / 3600
            if age_h > config.STUCK_TTL_H:
                return "stale"
        return "alive"

    try:
        return tc.run(_q())
    except Exception:  # noqa: BLE001 - unknown id / transient — don't wrongly reap a live run
        return "alive"


def _list_otto_workflows(window_h, limit=500):
    """Recent OttoWorkflow executions as [{wid, status, age_h}], bounded by `window_h` (and a
    hard row cap). Tries a server-side StartTime filter first; visibility stores that reject it
    fall back to a client-side filter over a capped scan. Never raises — the general sweep is
    best-effort, and a listing failure must not take the board sweep down with it."""
    import datetime as _dt
    import temporal_client as tc

    async def _q():
        c = await tc.client()
        now = _dt.datetime.now(_dt.timezone.utc)
        since = (now - _dt.timedelta(hours=window_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows, scanned = [], 0
        try:
            it = c.list_workflows(f"WorkflowType = 'OttoWorkflow' AND StartTime > '{since}'")
            server_filtered = True
        except Exception:  # noqa: BLE001 - older visibility store; filter client-side below
            it = c.list_workflows('WorkflowType = "OttoWorkflow"')
            server_filtered = False
        async for wf in it:
            scanned += 1
            start = getattr(wf, "start_time", None)
            age_h = (now - start).total_seconds() / 3600 if start else 0.0
            if server_filtered or age_h <= window_h:
                rows.append({"wid": wf.id,
                             "status": (wf.status.name if wf.status else "RUNNING"),
                             "age_h": age_h})
            if len(rows) >= limit or scanned >= max(limit * 4, 2000):
                break
        return rows

    try:
        return tc.run(_q())
    except Exception:  # noqa: BLE001
        return []


@activity.defn
@_heartbeats("pr-review")
def poll_pr_reviews(payload: dict) -> dict:
    """Poll GitHub for pull requests with a PENDING review request on the operator, and start one
    unattended review run per newly-requested PR (`pr_review.decide` is the state machine; it is
    pure and unit-tested, this is its shell).

    Nothing is written to GitHub here: the review lands in an Otto chat thread and waits for the
    operator to post it from Events → GitHub → PR reviews. The state entry is stamped with the
    workflow id BEFORE the workflow starts, so a poll that dies mid-fan-out leaves a row the UI
    can still find rather than an orphaned run."""
    import estop
    import pr_review
    # Before the search and before any state advances — a PR marked "reviewed" while paused would
    # never be picked up again (the request is still pending, so `decide` sees no new request).
    if estop.blocked("pr_review"):
        return {"picked": [], "paused": True}
    cfg = pr_review.load()
    if not pr_review.enabled(cfg):
        return {"picked": [], "disabled": True}
    prs = pr_review.list_requested(cfg)
    if prs is None:
        return {"picked": [], "search_failed": True}
    to_run, next_state = pr_review.decide(
        prs, pr_review.state(), now=time.time(), max_new=int(cfg.get("max_per_poll") or 3))
    pr_review.write_state(next_state)
    picked, skipped = [], []
    for pr, rnd in to_run:
        key = pr_review.entry_key(pr["repo"], pr["number"])
        wid = pr_review.run_id(pr["repo"], pr["number"], rnd)
        params = pr_review.pr_to_request(pr, cfg, rnd)
        # Resolve the pinned cap against the TRUSTED registry, exactly as the board poll does —
        # a config-named cap must never carry its own risk into the run.
        c = _cap(params["cap"]) if params.get("cap") else None
        if not c:
            activity.logger.info(
                f"pr-review: cap '{params.get('cap')}' is not available — skipping {key}")
            skipped.append(key)
            continue
        params["cap"] = {"name": c.name, "kind": c.kind, "risk": c.risk}
        pr_review.update_entry(key, {"wid": wid, "chat_key": params["chat_key"],
                                     "cap": c.name, "posted_at": None, "dismissed": False})
        if pr_review.start_run(wid, params):
            picked.append(key)
        else:
            skipped.append(key)
    if picked:
        activity.logger.info(f"pr-review: started {len(picked)} review(s): {picked}")
    # Auto-post rides the SAME poll, so the estop check at the top of this activity covers it:
    # an unattended write to someone else's PR must stop when every other ingress does.
    posted = pr_review.auto_post_ready(cfg)
    if posted:
        activity.logger.info(
            f"pr-review: auto-posted {len(posted)} review(s): "
            f"{[(p['key'], 'approved' if p['approved'] else 'commented') for p in posted]}")
    return {"picked": picked, "skipped": skipped, "posted": posted}


# A swarm child ("<parent>-s<N>") dies with its parent (parent-close policy) — the parent's
# terminal row is the human-facing signal, so sweeping children too would add N noise cards.
_SWARM_CHILD_RE = re.compile(r"-s\d+$")


@activity.defn
def reap_stuck(payload: dict) -> dict:
    """Sweep for runs whose workflow has DIED (FAILED/TERMINATED/TIMED_OUT/CANCELED) or has run
    past the stuck-TTL, and write the terminal audit row the in-workflow finalizer never could —
    it cannot fire when the worker itself died or the workflow was force-terminated. Two passes:

    * Board cards stuck In Progress (`gh-issue-*`): moved to Blocked + `needs-human` label +
      terminal row. Idempotent — a card already out of In Progress is no longer listed.
    * Every OTHER OttoWorkflow (`web-*`/`sched-*`/`slack-*`/…), which used to be invisible here:
      a dead/stale one gets the same terminal row (recovering its origin via engine.run_origin)
      so it reaches /api/needs-you instead of vanishing. Idempotent via the audit trail — a wid
      that already has a needs-human row (its own finalizer, server._wf_terminate, or a previous
      sweep) is skipped. Bounded by config.REAP_WINDOW_H. Runs even with the board disabled.

    Never raises."""
    import board
    cfg = board.load()
    board_on = board.enabled(cfg)
    reaped = []
    if board_on:
        meta = board.project_meta(cfg)
        blocked_option = (meta.get("options") or {}).get((cfg.get("columns") or {}).get("blocked"))
        for stub in board.list_in_column(cfg, "active"):
            n = stub.get("number")
            if n is None:
                continue
            wid = f"gh-issue-{n}"
            state = _reap_state(wid)
            if state == "alive":
                continue
            board.set_status_raw(meta.get("project_id"), meta.get("status_field_id"),
                                 stub.get("item_id"), blocked_option)
            board.add_label(stub.get("repo"), n, "needs-human")
            engine.record_terminal(
                wid, f"GitHub issue #{n}", None,
                reason=("workflow_dead" if state == "dead" else "stuck_timeout"),
                detail=f"reaper moved stuck card #{n} to Blocked (workflow {state})",
                repo=stub.get("repo"))
            reaped.append(n)

    # General sweep. The audited-wid set is built ONCE (one audit scan, not one per workflow).
    swept = []
    audited = {e.get("workflow") for e in engine.iter_audit_entries() if e.get("needs_human")}
    for row in _list_otto_workflows(config.REAP_WINDOW_H):
        wid, status = row.get("wid") or "", row.get("status")
        if wid.startswith("gh-issue-") or _SWARM_CHILD_RE.search(wid) or wid in audited:
            continue
        if status in ("FAILED", "TERMINATED", "TIMED_OUT", "CANCELED"):
            reason, state = "workflow_dead", status.lower()
        elif status == "RUNNING" and config.STUCK_TTL_H and row.get("age_h", 0) > config.STUCK_TTL_H:
            reason, state = "stuck_timeout", "stale"
        else:
            continue
        request, capname, repo, _reached = engine.run_origin(wid)
        engine.record_terminal(
            wid, request, capname, reason=reason,
            detail=f"reaper: workflow {state} with no terminal audit row", repo=repo)
        swept.append(wid)

    if reaped or swept:
        activity.logger.info(f"reaper: surfaced {len(reaped)} card(s) {reaped} "
                             f"+ {len(swept)} run(s) {swept}")
        import delivery
        lines = []
        if reaped:
            # Issue numbers only — no ticket content — so these need no detail gate.
            lines.append("Issues: " + ", ".join(f"#{n}" for n in reaped))
        if swept:
            # Counts by ingress only: a slack-* wid embeds a channel id, which must not reach
            # the ntfy broker (same rule as privacy.source_line).
            kinds = {}
            for w in swept:
                k = w.split("-", 1)[0]
                kinds[k] = kinds.get(k, 0) + 1
            lines.append("Runs: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))
        # DEFAULT priority and no wid: a sweep is a digest of runs that already failed, and its
        # landing place is the Needs-you dashboard, not any one run.
        delivery.notify(f"Otto reaper: {len(reaped) + len(swept)} stuck run(s) surfaced",
                        lines=lines, tags=["rotating_light"],
                        priority="default", kind="reaper")
    out = {"reaped": reaped, "swept": swept}
    if not board_on:
        out["disabled"] = True
    return out
