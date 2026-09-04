"""The Temporal WORKFLOW — the durable orchestrator.

Mirrors the web flow exactly, but durable and replayable, with the two human moments
as real Temporal SIGNALS:
  route -> clarify (maybe wait for an answer signal) -> approve writes (wait for a
  decision signal) -> run -> record.

Workflow code must be deterministic, so all real work is delegated to activities.
"""
import asyncio
from datetime import timedelta

from temporalio import exceptions, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import config
    import delivery
    # Pure string/enum module — no I/O, no clock — so calling it from workflow code is
    # deterministic and keeps the auth-wall wording in ONE place (engine uses the same function).
    import error_classifier
    # Pure prompt-text module — no I/O, no clock. Only BRAINSTORM_AUDIENCE is read here; the
    # contract text itself is interpolated activity-side (engine._output_contract).
    import contracts
    from activities import (clarify_request, classify_followup, classify_request,
                            cleanup_workspace, deliver_result, detect_repo_changes,
                            estop_check, execute_plan, finalize_terminal, finalize_workspace, judge_qa,
                            merge_results, notify_human, plan_capability, plan_swarm, open_chat,
                            plan_task_steps, poll_board, poll_pr_reviews, poll_slack, pr_head_branch,
                            provision_workspace, resolve_pr_target, check_grounding,
                            qa_capability, reap_stuck, record_attempt, record_chat, record_skip,
                            recover_pr_branch, review_capability, judge_review, route_request,
                            run_capability, snapshot_repos, snapshot_settings, suggest_repo,
                            verify_capability)


def _audience_of(params, reply_to):
    """WHO reads this run's result — the delivery target normally, the MODE when there is one.

    Brainstorm overrides whatever the target implies. A web chat has no `reply_to` at all, so
    `delivery.audience_for` never runs and `_output_contract(None)` falls back to the operator
    REPORT — which is what forced a "**TLDR** — " line onto every turn of a conversation the user
    was still thinking their way through. The capability is the signal here, not the target: the
    mode is a property of what was asked for, not of where the answer lands. Applies to a pinned
    /brainstorm from any ingress, and is re-derived on the resume path (`params["cap"]` is the
    bound cap there) so turn 2 doesn't revert to report prose.

    PURE — dict lookups only, safe on the replayed workflow path."""
    if _is_brainstorm(params.get("cap")):
        return contracts.BRAINSTORM_AUDIENCE
    return delivery.audience_for(reply_to) if reply_to else None


def _repo_of(params):
    """The repo this run targets in an ISOLATED CLONE, or None.

    Brainstorm never engages repo-mode. Repo-mode means "clone it, edit it, open a draft PR", and
    an explicit repo pick forces `cap.risk` to write in `_route_or_resume` — so on a capability
    whose whole contract is "this turn changes nothing" it buys a plan preview, an approval card,
    a clone and an empty PR. Measured: a pinned brainstorm with a repo picked reached
    PLAN_PREVIEW and never ran the turn at all. Reading a repo needs none of that — the cap's
    read tools already reach every registered checkout. Auto-engage is already write-only, so the
    explicit pick is the one path that needed closing. The composer disables the picker under
    Brainstorm; this is the stale-tab backstop.

    PURE — dict lookups only, safe on the replayed workflow path."""
    return None if _is_brainstorm(params.get("cap")) else params.get("repo")


def _may_plan_steps(authored, plan_mode, repo, subtask, cap):
    """May this run ask the planner to decompose the request into atomic steps?

    A runbook brings its OWN graph (`authored`) so it never re-plans; repo-mode and swarm
    sub-tasks always take the single-cap ladder.

    Brainstorm is the fourth exclusion and the one that bit: plan-then-execute WINS over the
    ladder outright (`if plan_list:` in `_run_impl`), so with both toggles on the brainstorm turn
    never ran at all — measured, the whole mode was silently replaced by a decomposition of the
    musing into atomic steps, each with its own verify ladder. The composer makes the two
    mutually exclusive; this is the backstop, and it resolves toward the narrower, read-only,
    cheaper of the two modes.

    PURE — safe on the replayed workflow path (the caller reads the setting from its snapshot)."""
    return (not authored and plan_mode != "off" and not repo and not subtask
            and not _is_brainstorm(cap))


def _verified_of(verdict):
    """The run's `verified` field. None — not False — when nothing judged it (a brainstorm turn),
    matching what the resume path already returns; the UI renders the three states distinctly."""
    return verdict["passed"] if verdict else None


def _is_brainstorm(cap):
    """True for a run pinned to the built-in brainstorm capability — the read-only conversational
    MODE (config.BRAINSTORM_CAP). PURE dict lookup, so it is safe on the replayed workflow path.

    It is reached ONLY by an explicit opt-in: routing._shortlist drops `route_hidden` caps, so
    Router #1 cannot land here. Three things hang off it, all in `_run_impl`/`_route_or_resume`:
    the output contract (contracts._THINKING_PARTNER_FORMAT), skipping the verify ladder, and
    skipping the clarify + write-intent calls."""
    return bool(cap) and cap.get("name") == config.BRAINSTORM_CAP


# Without a retry policy Temporal retries a failing activity forever, so a persistent
# failure (e.g. an activity worker.py never registered) hangs the run at "executing…"
# with no signal. Bound retries so any persistent failure surfaces as a FAILED run in
# seconds. "NotFoundError" (an unregistered activity — see worker.ACTIVITIES) can never
# succeed on retry, so it fails on the first attempt.
_RETRY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=["NotFoundError"])

# EXECUTION activities (run_capability / qa_capability) shell out a full `claude -p` turn:
# tens of minutes, real subscription spend, real side effects for write caps. A Temporal-level
# replay of one — e.g. the worker restarted mid-attempt to pick up new code — would silently
# re-run the whole turn up to 3×: duplicate spend, duplicate side effects, and NO audit row
# for the extra runs. maximum_attempts=1: the verify→retry→escalate loop IS the retry
# mechanism, and it audits every attempt it takes (issue #91). The verify loop catches the
# resulting ActivityError and counts it as a FAILED attempt; in the QA loop it propagates to
# the outer terminal handler (needs-human), which is surfacing, not hiding.
_RETRY_EXEC = RetryPolicy(maximum_attempts=1)

# What a DEAD WORKER costs a run in flight. Every long execution activity beats on a timer
# (activities._heartbeating), so Temporal notices a killed/restarted worker in this window
# instead of at start_to_close — which is what makes the ceilings below raisable at all: they
# used to double as the stall a restart cost, pinning execution to 20 minutes. Generous next to
# the 30s beat, because a beat is one lock-free call and a false positive kills a real attempt.
_HEARTBEAT = timedelta(minutes=3)

# The execution ceiling — one constant, not four copies, because config.EXEC_TIMEOUT_S has to
# stay under it with headroom and a drifting copy silently removes that headroom
# (`ExecutionHeartbeatTests`). A literal, not a read of config: activity options are replayed,
# so deriving one from the environment makes a worker with a different env replay differently.
_EXEC_CEILING = timedelta(minutes=40)

# Why a post-PR fix round never runs on the LOCAL backend. Everywhere else a failing local model
# is covered by a Claude rung (config.LOCAL_FALLBACK): the verify ladder retries and escalates,
# so a local death costs a rung, not the run. Both post-PR fix loops are one-shot — a single
# `run_capability` with no retry and no escalation above it — so that promise is unmet here and a
# local failure is simply the end of the round. Measured on run web-2bd1a194: a fix round on a
# 22k-line settings.kts burned 944s and 1.68M input tokens before dying at the local model's
# output-token wall with zero commits, and nothing existed to cover for it.
_FIX_NO_LADDER = ("post-PR fix rounds run on Claude: they are one-shot, with no ladder rung "
                  "left to cover a local-model failure")

# Banners prepended to a delivered result when the run ended needing a human, keyed by reason.
_NEEDS_HUMAN_BANNER = {
    "verify_exhausted": "⚠️ **Needs human review** — this did not pass automated verification "
                        "after all attempts. Treat the result below as unverified.",
    "qa_fail": "⚠️ **Needs human review** — post-PR QA FAILED. The draft PR was left open for you.",
    "qa_inconclusive": "⚠️ **Needs human review** — post-PR QA was INCONCLUSIVE. The draft PR was "
                       "left open for you.",
    "review_fail": "⚠️ **Needs human review** — the PR code review still has unaddressed findings "
                   "after all fix rounds. The draft PR was left open for you.",
    "review_inconclusive": "⚠️ **Needs human review** — the PR code review was INCONCLUSIVE. The "
                           "draft PR was left open for you.",
    "gate_timeout": "⚠️ **Nobody approved this in time** — it needed a human decision before "
                    "anything could run, the approval window closed, and it was declined rather "
                    "than run unreviewed. Nothing was executed. Re-send it if you still want it.",
    "harness_exhausted": "⚠️ **Needs human review** — every attempt died in the harness "
                         "(timeout or worker crash), so nothing was ever judged. This is an Otto "
                         "failure, not the capability's — check the transcript for where it hung.",
    "budget_exceeded": "⚠️ **Needs human review** — this run hit its cost/token budget and was "
                       "stopped before completing.",
    # Strict local mode: the body below it is config.strict_stop_message, which already spells out
    # the model, the failure and the fix — this line only has to say "nothing ran".
    # Claude auth wall: the body below it is error_classifier.claude_auth_message, which carries
    # the CLI's own words and the fix, so this line only has to name the culprit.
    config.AUTH_STOP_REASON: "⛔ **Stopped — Claude could not authenticate.** Not a capability "
                             "failure and not a crash: the subscription session on the worker "
                             "host expired. Re-authenticate and retry.",
    config.STRICT_STOP_REASON: "⛔ **Stopped — nothing ran.** The local model could not do the "
                               "work and `OTTO_LOCAL_FALLBACK=0` forbids Claude from covering "
                               "for it. No Claude tokens were spent.",
    # The other two Claude-backend walls. Each needs its own line for the same reason the auth
    # one does: the remedy differs, and "harness_exhausted" named none of them.
    "claude_usage_limit": "⛔ **Stopped — Claude's usage limit is spent.** Not a capability "
                          "failure and not a crash: the subscription hit its cap. The body "
                          "below names the reset time; retry after it.",
    "claude_model_unavailable": "⛔ **Stopped — Claude cannot serve the configured model.** Not "
                                "a capability failure: the model named in Admin → Models does "
                                "not exist or this subscription has no access to it.",
}


def _failure_detail(exc, limit=400):
    """The REAL cause of a failed run, not Temporal's wrapper sentence.

    `str(ActivityError)` is the constant string "Activity task failed" — every fact about what
    broke lives one level down, on `.cause` (the ApplicationError carrying the original message
    and the original exception's type name). Recording `str(e)` therefore stamped that same
    placeholder into the audit row, the Chat thread and the owner push at once; measured over the
    trail it was the single most common terminal detail in the store, and the only other copy of
    the traceback is the worker log, which lives in /tmp and is truncated on every restart. A
    `workflow_error` was, in practice, undiagnosable.

    Walks the cause chain outermost-first, naming the ACTIVITY (or child workflow) that failed
    instead of repeating Temporal's generic line, and joins the links with " <- ". Pure attribute
    reads and string work, so it is safe in deterministic workflow code."""
    parts, seen, e = [], set(), exc
    while e is not None and id(e) not in seen and len(parts) < 6:
        seen.add(id(e))
        msg = str(getattr(e, "message", None) or e).strip()
        if isinstance(e, exceptions.ActivityError) and getattr(e, "activity_type", None):
            part = f"activity {e.activity_type} failed"
        elif isinstance(e, exceptions.ChildWorkflowError) and getattr(e, "workflow_type", None):
            part = f"child workflow {e.workflow_type} failed"
        else:
            # An ApplicationError's `type` is the ORIGINAL exception's class name, which is the
            # whole story for the ones that carry no message of their own: a bare KeyError arrives
            # as "'max_plan_revisions'" and means nothing until it is labelled "KeyError".
            kind = getattr(e, "type", None) if isinstance(e, exceptions.ApplicationError) \
                else type(e).__name__
            part = f"{kind}: {msg}" if kind and kind not in msg else (msg or kind or "")
        if part and part not in parts:
            parts.append(part)
        # `.cause` is Temporal's own link; `__cause__` covers a plain Python chain underneath it.
        e = getattr(e, "cause", None) or e.__cause__
    detail = " <- ".join(parts) or str(exc) or type(exc).__name__
    return detail if len(detail) <= limit else detail[:limit - 1] + "\u2026"


@workflow.defn
class OttoWorkflow:
    def __init__(self):
        self._cap = None
        self._question = None
        self._clarification = None     # set by provide_clarification() signal
        self._decision = None          # set by approve() signal
        self._plan_feedback = None     # set by revise_plan() signal — free-text change request
        self._plan_revisions = 0       # revision rounds spent so far at the current gate
        self._replanning = False       # True only while a revision round's re-preview is in flight
        self._awaiting_clarification = False
        self._awaiting_approval = False
        self._risk_reason = None       # WHY the approval gate fired — shown on the gate card
        # True when a write-bound session's follow-up was re-read as a DISCUSSION turn (a
        # question / brainstorm that mutates nothing), so this turn drops to read tools and
        # skips the plan preview + gate entirely. Per-turn only — the chat's cap is re-resolved
        # from the registry on the next follow-up, so nothing about the session is de-escalated.
        self._discussion = False
        self._attempt = 0          # current verify->retry attempt (0 until execution starts)
        self._verified = None      # last verify verdict (None until first attempt judged)
        self._swarm = False        # True once this run fans out into a parallel sub-task swarm
        self._children = []        # [{id, cap, request, risk}] of the swarm's child workflows
        self._repo = None          # repo this run targets in an isolated workspace (issue #57/#59)
        self._git_run_id = None    # ORIGINAL run whose workspace path/branch a resume re-provisions
        self._plan = None          # pre-approval plan preview (concrete operations) for the gate
        self._pr_target = {}       # {number,url,branch} when the request works ON an open PR
        self._grounding = []       # request claims the provisioned tree contradicts (advisory)
        self._plan_concerns = []   # what the plan critic found wrong with it (advisory, gate-only)
        self._plan_model = None    # which model WROTE that plan — shown on the gate card
        self._qa = None            # post-PR QA loop state {state, round} once it starts
        self._review = None        # post-PR code-review loop state {state, round} once it starts
        self._chat_key = None      # Chat sidebar thread id for unattended runs (board/schedule/event)
        self._audience = None      # "slack" when the result is posted verbatim to a person
        self._request = None       # the run's request text (for the terminal finalizer)
        self._terminal = None      # set when the run ended needing a human {reason} (status query)
        self._needs_human = None   # {reason} when delivered-unverified / QA-fail / budget stop
        # True when the ladder ran out of HARNESS retries without a judge ever reading an
        # attempt — the run failed on timeouts/crashes, not on the capability's work.
        self._harness_stop = False
        self._spent = {"output": 0, "cost": 0.0}   # running token/cost total for the budget (Tier 1)
        # Per-run snapshot of the UI-editable runtime settings, taken once via the
        # snapshot_settings activity (see _run_impl). Deterministic code reads THIS, never
        # config.setting() — a live store read would let an Admin edit change a branch mid-run and
        # diverge on replay. Defaults keep the workflow usable if an older worker lacks the activity.
        self._settings = dict(config.SETTINGS_FALLBACK)
        # Effort level for this run (composer pick > Admin default). Bound for real from the
        # snapshot in _run_impl; initialized here so nothing can reach it before that.
        self._effort = config.SETTINGS_FALLBACK["effort"]
        # Per-stage wall-clock timing {label: {"start": epoch_ms, "dur": epoch_ms|None}}, keyed on
        # the pipe labels the UI renders (DECOMPOSE/ROUTER/CLARIFY/PLAN/GATE/RUN). Surfaced via
        # status() so a
        # client that left and reattached (chat switch, page reload) can restore each stage's timer
        # from real elapsed time instead of resetting it to the moment of reattach (issue #117).
        self._times = {}

    def _setting(self, name):
        """Read ONE runtime setting from this run's snapshot. ALWAYS go through here — never
        `self._settings[name]`. The snapshot is the snapshot_settings ACTIVITY's recorded result,
        replayed verbatim from history, so a run already in flight when a new key shipped has a
        snapshot missing that key entirely: bare indexing then KeyErrors on every replay forever
        and no worker restart can fix an already-poisoned history (observed live: web-95917757
        crash-looped on exactly this). config.SETTINGS_FALLBACK is an import-time constant, so
        the fallback read stays deterministic. Guarded by test_core.SettingsSnapshotAccessTests."""
        return self._settings.get(name, config.SETTINGS_FALLBACK[name])

    def _bind_composer(self, params):
        """Bind the per-chat composer picks: memory on/off, the model override, the effort level.

        Called AFTER the settings snapshot, because effort falls back to the Admin default and
        that default may only be read from the snapshot — `config.setting()` reads a mutable
        store, so a mid-run edit would send a replay down a different branch than history
        recorded. Precedence lives in `config.resolve_effort`; `_setting` (never a bare index)
        keeps a run that was already in flight when this key shipped from KeyErroring on every
        replay forever. The other two are explicit run inputs and need no snapshot at all."""
        self._memory_enabled = params.get("memory_enabled", True)
        self._model_override = params.get("model_override")
        self._effort = config.resolve_effort(params.get("effort"), self._setting("effort"))

    async def _gate_wait(self, cond):
        """Wait at the approval gate for `cond`, bounded by the `gate_timeout_h` setting.
        Returns True when the human answered, False when the window closed.

        An unbounded wait here is only safe when whoever asked can SEE the approval card. Slack
        can't: a write-intent DM parks a workflow whose only gate UI is the web app, so the run
        neither answers nor refuses — it just stops existing as far as the asker is concerned.
        Timing out is not an approval; the caller declines the run and surfaces it. 0 restores
        the old unbounded wait for anyone who wants it."""
        hours = self._setting("gate_timeout_h")
        if not hours or hours <= 0:
            await workflow.wait_condition(cond)
            return True
        try:
            await workflow.wait_condition(cond, timeout=timedelta(hours=hours))
        except asyncio.TimeoutError:
            return False
        return True

    def _now_ms(self):
        return int(workflow.now().timestamp() * 1000)

    def _enter(self, label):
        self._times[label] = {"start": self._now_ms(), "dur": None}

    def _leave(self, label):
        t = self._times.get(label)
        if t is not None and t.get("dur") is None:
            t["dur"] = self._now_ms() - t["start"]

    async def _resume_workspace(self, repo, git_run_id, git_branch, request=None):
        """Re-provision a repo-mode chat's isolated clone so a follow-up can be answered. Returns
        the workspace dict, or None when this conversation genuinely cannot be continued.

        WHY A RESUME NEEDS A WORKSPACE AT ALL: it is the PATH, not the branch contents. Otto tears
        the clone down after every run, but `claude -p --resume <session>` looks up its on-disk
        session history under the cwd it was created in — so the follow-up must run from the exact
        same `data/workspaces/<git_run_id>` path or there is no conversation left to resume. That's
        why every tier below keys on `git_run_id` and never mints a path of its own.

        Four tiers, cheapest first:
          1. the branch recorded on the chat (`git_branch`, #146) — the fast path;
          2. the branch of the PR the original run opened, recovered via `gh` (an agent-managed cap
             like sre-minion pushes its OWN branch), then Otto's deterministic `otto/<run_id>`;
          3. a FRESH clone of the repo's default branch at the same path, when tiers 1-2 found
             nothing to check out AND the original run never opened a PR;
          4. nothing — a PR existed and its branch is gone (merged/deleted). Not continuable.

        Tier 3 is the one that was missing, and its absence made a correct run unaskable. A run
        that legitimately produced NO commits — ci#66 on 2026-08-04 (`web-b97b623a`), where
        the approved plan gated implementation on an unanswered question and the cap rightly only
        posted a comment — never pushed `otto/<run_id>` anywhere, so tier 1 had no branch recorded,
        tier 2's `git ls-remote` found neither branch nor PR, and the chat dead-ended on "the
        isolated workspace for this task's branch no longer exists". The user could not even ask
        "why didn't you implement it?". Nothing was lost in that case BECAUSE nothing was pushed,
        so a clean clone at the same path restores the session with no work to discard — whereas a
        MERGED branch's clone would silently drop the commits a follow-up might amend, which is why
        tier 3 is gated on "no PR was ever opened" rather than applied unconditionally."""
        ws = None
        if git_branch:
            try:
                ws = await workflow.execute_activity(
                    provision_workspace,
                    {"repo": repo, "run_id": git_run_id, "from_branch": True,
                     "branch": git_branch},
                    start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
                    retry_policy=_RETRY)
            except exceptions.ActivityError:
                ws = None
        recovered_pr = False
        if ws is None:
            rec = await workflow.execute_activity(
                recover_pr_branch, {"wid": git_run_id, "repo": repo},
                start_to_close_timeout=timedelta(seconds=90), retry_policy=_RETRY)
            cand = rec.get("branch")
            recovered_pr = bool(cand or rec.get("pr_url"))
            fallbacks = []
            if cand and cand != git_branch:
                fallbacks.append(cand)
            if not git_branch:
                fallbacks.append(None)              # provision defaults to otto/<run_id>
            for branch in fallbacks:
                try:
                    ws = await workflow.execute_activity(
                        provision_workspace,
                        {"repo": repo, "run_id": git_run_id, "from_branch": True,
                         "branch": branch},
                        start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
                        retry_policy=_RETRY)
                    break
                except exceptions.ActivityError:
                    ws = None
        # Tier 3: nothing to check out and nothing was ever pushed — the run made no commits, so
        # give the session its path back on a clean default-branch clone (from_branch=False).
        if ws is None and not recovered_pr:
            try:
                ws = await workflow.execute_activity(
                    provision_workspace, {"repo": repo, "run_id": git_run_id},
                    start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
                    retry_policy=_RETRY)
                workflow.logger.info(
                    f"resume: no branch or PR was ever pushed for {git_run_id} — "
                    "re-provisioned a clean clone so the session can be continued")
            except exceptions.ActivityError:
                ws = None
        # REPAIR TIER. Everything above picks the branch from what the CHAT recorded, never from
        # what this message says — so "ci#106 got the following review, pls fix the
        # findings" re-checked out the original run's branch and worked on a tree with none of
        # #106's code in it (`web-a6122d6c`). The fix is narrow on purpose: only when the tree we
        # just restored FAILS the grounding check, and only toward the operator's own open PR, do
        # we re-point. That makes it a repair for a demonstrable mismatch rather than a guess
        # about intent — a follow-up merely mentioning a PR ("like we did in #106") leaves a
        # grounded tree alone. Same path either way, so the session history survives.
        if ws and request:
            self._grounding = (await workflow.execute_activity(
                check_grounding, {"path": ws["path"], "request": request},
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_RETRY)).get("notes") or []
            if self._grounding:
                target = (await workflow.execute_activity(
                    resolve_pr_target, {"repo": repo, "request": request},
                    start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)) or {}
                branch = target.get("branch")
                if branch and branch != ws.get("branch"):
                    try:
                        moved = await workflow.execute_activity(
                            provision_workspace,
                            {"repo": repo, "run_id": git_run_id, "from_branch": True,
                             "branch": branch},
                            start_to_close_timeout=timedelta(minutes=15),
                            heartbeat_timeout=_HEARTBEAT, retry_policy=_RETRY)
                        workflow.logger.info(
                            f"resume: tree did not match the request; re-pointed to PR "
                            f"#{target.get('number')}'s branch {branch}")
                        ws, self._pr_target = moved, target
                        # Re-check on the branch we moved to: if it now matches, the note must go,
                        # or the run is told its tree is wrong when it no longer is.
                        self._grounding = (await workflow.execute_activity(
                            check_grounding, {"path": ws["path"], "request": request},
                            start_to_close_timeout=timedelta(seconds=60),
                            retry_policy=_RETRY)).get("notes") or []
                    except exceptions.ActivityError:
                        pass          # keep the tree we had; the note below still warns the run
        return ws

    @workflow.run
    async def run(self, params) -> dict:
        """Thin wrapper: run the real body, but if it raises (an activity that FAILED after its
        retries, or a child-workflow failure that propagates), record a durable terminal audit row
        and move the board card to Blocked BEFORE re-raising — so the run surfaces on the Needs-you
        dashboard instead of vanishing. This covers the caught-exception case; a genuinely dead
        worker (which can't run this except block) is backstopped by the external ReaperWorkflow."""
        if isinstance(params, str):
            params = {"request": params}
        self._request = params.get("request", "")
        try:
            out = await self._run_impl(params)
            # A run may declare the line its report must lead with. Applied HERE because this is
            # the one funnel every success path returns through — `_run_impl` has a dozen of
            # them — and because the Board card and "Read full result" render this value, not
            # the chat's copy. Pure string work, so replay-safe.
            prefix = params.get("report_prefix")
            if prefix and isinstance(out, dict) and out.get("result"):
                out["result"] = contracts.lead_with(out["result"], prefix)
            return out
        except Exception as e:  # noqa: BLE001 - NOT bare: CancelledError (BaseException) still propagates
            self._terminal = {"reason": "workflow_error"}
            try:
                await workflow.execute_activity(
                    finalize_terminal,
                    {"wid": workflow.info().workflow_id, "request": self._request,
                     "cap": self._cap, "reason": "workflow_error",
                     "detail": _failure_detail(e),
                     "reply_to": params.get("reply_to"), "repo": self._repo},
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
            except Exception:  # noqa: BLE001 - finalizer is best-effort; never mask the original error
                pass
            # Finalize the Chat thread on the failure path too (issue #79): _open_chat wrote a
            # pending "working…" placeholder at the start of an unattended run; without this a
            # mid-run failure never reaches the success-path _record_chat and orphans that
            # placeholder as a perpetual spinner. Rewrite it into an error marker instead.
            try:
                await self._record_chat(
                    params, self._request,
                    f"❌ **This run failed** — {_failure_detail(e)}", None, self._cap)
            except Exception:  # noqa: BLE001 - best-effort; never mask the original error
                pass
            raise

    async def _finalize_pr(self, ws, request, result, cap, repo):
        """Repo-mode tail: push the branch, open (or update) the draft PR, tear the clone
        down, and fold the outcome into the report. Returns `(pr, result)` — extracted from
        `_run_impl` verbatim; the activity order it issues is part of the replay history."""
        self._enter("PR")
        pr = await workflow.execute_activity(
            finalize_workspace,
            {"run_id": workflow.info().workflow_id, "title": request[:120],
             "head": ws["head"], "summary": (result or "")[:1500],
             # Working ON an open PR: push back to ITS branch and skip `gh pr create`, or
             # the run opens a second PR for a change that belongs on the first. `branch` is
             # required here — finalize otherwise looks at `otto/<run_id>`, which this run
             # never created.
             "existing_pr": bool(self._pr_target.get("branch")),
             "branch": self._pr_target.get("branch"),
             # The approved plan rides into the PR as a comment, so the reviewer sees what
             # was approved next to the diff it produced. None for unattended `auto`
             # (no gate, no plan) — post_plan then posts nothing.
             "plan": self._plan, "request": request, "cap": cap["name"],
             "concerns": self._plan_concerns},
            start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
            retry_policy=_RETRY)
        await workflow.execute_activity(
            cleanup_workspace, {"run_id": workflow.info().workflow_id},
            start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
        if pr.get("pr_url") and pr.get("detail") == "opened by the capability":
            result += (f"\n\n**PR** in `{repo}` (opened by the capability on branch "
                       f"`{pr['branch']}`): {pr['pr_url']}")
        elif pr.get("pr_url") and self._pr_target.get("branch"):
            # The run worked ON an existing PR, so it pushed a commit rather than opening
            # anything. Saying "opened draft PR" here would report a second PR that does not
            # exist, and hide the one decision the reader most needs to see: that Otto chose
            # to work on someone else's branch rather than off the default.
            result += (f"\n\n**Updated PR #{self._pr_target.get('number')}** in `{repo}` "
                       f"(the request named it, so this ran on its branch "
                       f"`{self._pr_target['branch']}`): {pr['pr_url']}")
        elif pr.get("pr_url"):
            result += f"\n\n**Opened draft PR** in `{repo}`: {pr['pr_url']}"
        elif pr.get("pushed"):
            result += f"\n\nPushed branch `{pr['branch']}` in `{repo}` ({pr.get('detail', '')})."
        else:
            result += f"\n\n_No PR opened by Otto: {pr.get('detail', 'nothing to push')}._"
        self._leave("PR")
        return pr, result

    async def _plan_and_gate(self, params, request, cap, approval, unattended, reply_to,
                             repo, git_run_id, resume, resume_ws, authored_doc):
        """The write path: plan preview, `critique_plan`, and the approval gate (including
        `revise_plan` re-previews). Returns `(done, request)` — `done` is a terminal result
        dict when the gate DECLINED or expired and the run must stop, else None; `request`
        carries any folded-in revision feedback.

        A pure extraction from `_run_impl`: locals are passed rather than read off `self`
        because `repo` is reassigned by repo-mode auto-engage AFTER `self._repo` is set."""
        if cap["risk"] == "write":
            if not self._risk_reason:
                self._risk_reason = f"'{cap['name']}' is a write-classified capability"
            if approval == "auto":
                # Pre-authorized, whoever pressed start. This deliberately does NOT also require
                # `unattended`: "auto" is only ever set by a path that opted in (a runbook's
                # auto_approve, an events.py rule, a board run, a retry re-authorizing a write
                # that already reached RUN, or the composer's "Auto approve" toggle, which is
                # the human at the keyboard pre-authorizing their own chat and is the ONE thing
                # a browser can set — as a boolean, never as this string). Requiring both
                # meant the UI's "Run now" ran
                # the SAME auto-approve runbook as its cron fire but gated it, because run_now
                # defaults to unattended=False; the operator's pre-authorization was carried into
                # the workflow and then ignored, after a full read-only preview pass had been
                # spent. `unattended` keeps its own meaning — nobody is watching — which is what
                # delivery, clarify and verify's dead-end rule read it for.
                pass                                    # pre-authorized — run it
            elif unattended and approval == "skip":
                await workflow.execute_activity(
                    record_skip, {"request": request, "name": cap["name"],
                                  "wid": workflow.info().workflow_id},
                    start_to_close_timeout=timedelta(seconds=30), retry_policy=_RETRY)
                skipped = f"skipped — {cap['name']} is a write (approval off)"
                await self._record_chat(params, request, skipped, None, cap)
                return {"result": skipped, "session_id": None, "cap": cap,
                        "times": self._times}, request
            else:
                # Interactive gate, OR unattended approval == "ask" — wait for a human decision.
                # When unattended, the pending workflow shows up under "Waiting on you" on the
                # Board, where the operator approves/denies (same `approve` signal as the web gate).
                # Plan-first: run a STRICTLY read-only preview pass and surface the concrete
                # operations in the gate, so the human approves what will actually happen — not
                # just the capability name (the gate otherwise fires before any operation is known).
                # For a FRESH repo-mode run the preview reads the live checkout (read-only) and the
                # real run provisions its isolated clone only AFTER approval below; a RESUME instead
                # previews inside the clone re-provisioned above, which is where its session lives.
                # This is its own timed stage, distinct from GATE (the human's approval wait) and
                # from the early DECOMPOSE check (the swarm fan-out planner) — it's a full agentic
                # `claude -p` pass and can run for minutes, so folding it into either of those
                # made the pipeline diagram misrepresent when the real planning work happens.
                #
                # The gate isn't just approve/decline: the human can also send free-text feedback
                # (revise_plan signal) instead of a decision — "only touch dev, not prod" — which
                # gets folded into the request and re-previewed, so the gate shows a NEW plan +
                # critique reflecting the change rather than asking the human to approve/decline
                # blind or restart the whole run over a one-line correction. Bounded by
                # max_plan_revisions (0 disables the affordance): once spent, further feedback
                # signals are dropped and only a decision moves the loop forward.
                max_revisions = max(0, self._setting("max_plan_revisions"))
                gate_expired = False
                while True:
                    self._enter("PLAN")
                    if authored_doc:
                        # A runbook's own prose IS the plan — a human wrote it, so generating one
                        # with a multi-minute preview pass would be spending money to paraphrase
                        # the thing we were handed. Shown at the gate verbatim; consumed here, so
                        # a "request changes" round falls through to the real previewer below with
                        # the feedback folded in (the author's text no longer describes the run).
                        self._plan, self._plan_concerns = authored_doc, []
                        authored_doc = None
                    else:
                        preview = await workflow.execute_activity(
                            plan_capability,
                            {"request": request, "name": cap["name"], "repo": repo, "resume": resume,
                             "cwd": resume_ws["path"] if resume_ws else None,
                             "wid": workflow.info().workflow_id,
                             # The preview's cwd is the DEFAULT branch; this tells it where the
                             # code actually is and to read it with `gh pr diff`.
                             "pr": self._pr_target, "effort": self._effort},
                            # 17min: engine.plan_preview's own timeout (900s/15min) + ~2min margin
                            # for the critique pass after it — must stay above the preview's timeout
                            # or the activity kills it before it can return "" cleanly.
                            start_to_close_timeout=timedelta(minutes=17),
                            heartbeat_timeout=_HEARTBEAT, retry_policy=_RETRY)
                        self._plan = preview.get("plan") or None
                        self._plan_concerns = preview.get("concerns") or []
                        self._plan_model = preview.get("model")
                        # The preview is a full agentic pass with real spend — count it toward the
                        # run's budget like any attempt, so a hard-budget-exceeding preview stops the
                        # run at the ladder's first check instead of running for free outside the
                        # accounting.
                        self._account(preview)
                    self._leave("PLAN")
                    # The re-preview has landed, so `self._plan` is the plan this round asks about.
                    # Until this point plan_revisions was already bumped while _plan still held the
                    # PREVIOUS round's text — a client treating the counter as "a new plan is up"
                    # repaints the stale one ~1s after signalling and reads as "nothing happened".
                    self._replanning = False
                    # GATE starts HERE, not before the loop — it marks the human's wait, which
                    # only begins once there's something to look at. Entering it earlier made the
                    # preview's own multi-minute generation time read as "waiting for approval",
                    # which is exactly the double-counting the PLAN/GATE split was meant to fix.
                    self._enter("GATE")
                    self._awaiting_approval = True
                    await self._notify(f"Approval needed: {cap['name']}",
                                       cap=cap, repo=repo, reply_to=reply_to,
                                       unattended=unattended,
                                       note=(f"{len(self._plan_concerns)} plan concern(s)"
                                             if self._plan_concerns else None),
                                       detail=request,
                                       tags=["warning"],
                                       priority="max", kind="approval",   # a run is parked
                                       wid=workflow.info().workflow_id)
                    if not await self._gate_wait(
                            lambda: self._decision is not None
                            or self._plan_feedback is not None):
                        gate_expired = True
                        break
                    if self._decision is not None:
                        break
                    # A revise_plan signal fired. Over budget: drop it silently and keep waiting —
                    # the human can still approve/decline the plan already on screen.
                    if self._plan_revisions >= max_revisions:
                        self._plan_feedback = None
                        if not await self._gate_wait(lambda: self._decision is not None):
                            gate_expired = True
                        break
                    self._plan_revisions += 1
                    self._replanning = True
                    request = (f"{request}\n\n(the human reviewing your plan asked for this "
                              f"change before approving — revise the plan accordingly) "
                              f"{self._plan_feedback}")
                    self._plan_feedback = None
                    # Close out THIS round's gate-wait before looping back to re-plan, so a
                    # revision's re-preview time attributes to PLAN, not to the gate wait either.
                    self._leave("GATE")
                    # loop: re-preview + re-critique against the amended request, re-show the gate
                self._awaiting_approval = False
                self._leave("GATE")
                if not self._decision:
                    # Two ways to get here, and they are NOT the same event. A human declined —
                    # audit a plain denial, nothing needs anyone. Or nobody ever answered: that
                    # one needs a human by definition, so it takes the terminal path instead
                    # (needs-human row + owner push + board card to Blocked) and, unlike a
                    # decline, tells the reply target — the Slack thread that asked is where the
                    # silence was actually felt.
                    if not gate_expired:
                        await workflow.execute_activity(
                            record_skip, {"request": request, "name": cap["name"],
                                          "wid": workflow.info().workflow_id},
                            start_to_close_timeout=timedelta(seconds=30), retry_policy=_RETRY)
                    # A denied RESUME provisioned its clone before the gate (above) — tear it down,
                    # or a declined follow-up leaks a checkout that only the TTL sweep would collect.
                    if resume_ws:
                        await workflow.execute_activity(
                            cleanup_workspace, {"run_id": git_run_id},
                            start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
                    msg = "Declined — nothing was run."
                    if gate_expired:
                        self._terminal = {"reason": "gate_timeout"}
                        msg = _NEEDS_HUMAN_BANNER["gate_timeout"]
                        await workflow.execute_activity(
                            finalize_terminal,
                            {"wid": workflow.info().workflow_id, "request": request, "cap": cap,
                             "reason": "gate_timeout", "reply_to": reply_to, "repo": repo,
                             "unattended": unattended},
                            start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
                        if reply_to:
                            # A person waiting in Slack gets a person's answer: the banner names
                            # an approval window they never saw (see `_shape_result`).
                            said = msg
                            if self._audience == contracts.CONVERSATION_AUDIENCE:
                                said = (f"Sorry — I couldn't get this cleared in time, so I "
                                        f"haven't done anything. {config.OWNER_NAME} will need "
                                        f"to pick it up.")
                            await workflow.execute_activity(
                                deliver_result,
                                {"reply_to": reply_to, "result": said, "cap": cap,
                                 "run_id": workflow.info().workflow_id, "session_id": None},
                                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
                    await self._record_chat(params, request, msg, resume, cap)
                    return ({"result": msg, "session_id": resume, "cap": cap,
                             "needs_human": self._terminal, "times": self._times}, request)
        return None, request

    async def _route_or_resume(self, params, request, resume, unattended, approval,
                               reply_to, repo, repo_hint):
        """Decide WHAT runs: a resume reuses the bound capability, a fresh request goes
        through decompose (swarm) -> route -> clarify -> the write-intent guard.

        Returns `(done, cap, request, subtask)` — `done` is a terminal result dict when the
        request fanned out into a swarm and this workflow is finished, else None."""
        # `subtask` marks a child workflow spawned by a swarm: it must NOT decompose again
        # (that would recurse) and skips clarification (the planner already scoped it). Bound
        # ABOVE the resume/fresh split: inline, the resume path left it unbound and simply
        # never read it (it returns first), but this method's return tuple always reads it.
        subtask = params.get("subtask", False)
        if resume:
            # Continuation — skip routing/clarification, reuse the bound capability + session.
            cap = params["cap"]
            self._cap = cap
            # A resumed session is bound to one cap's risk for life, but the TURN in front of us
            # is its own thing — so re-assess JUST the follow-up and let it move the risk in
            # EITHER direction. Routing and clarification stay skipped.
            #
            # Up (read -> write): an emergent write ("now publish those comments") must hit the
            # approval gate below instead of riding in auto-approved. Runs for UNATTENDED resumes
            # too (a Slack thread follow-up is the least-trusted input Otto takes — someone
            # else's words, with nobody watching), where a WRITE verdict + approval "ask" parks
            # it on the Needs-you board for the owner.
            #
            # Down (write -> read) is the DISCUSSION TURN. A repo-mode chat binds a write cap for
            # the life of the conversation, so every follow-up in it used to pay the full write
            # path: a 15-minute read-only plan preview and an approval card — for "why did you use
            # a mutex there?". The gate is there to guard a mutation; a turn that asks for none
            # has nothing to guard, and the wait was the whole reason a ticket conversation
            # stopped feeling like a conversation. Unattended it was worse than slow: an
            # `approval == "skip"` Slack follow-up to a write session was DISCARDED unanswered
            # ("skipped — … is a write").
            #
            # Downgrading is not just "don't gate": the risk drops for real, so `run_capability`
            # hands this turn config.READ_TOOLS. That ordering is deliberate — the classifier is
            # a haiku call and can be wrong, so what stands behind it is the toolset, not the
            # verdict. A misread "add a test for that" costs one turn that answers instead of
            # editing (and says so, see contracts._DISCUSSION_TURN_NOTE), never an ungated write.
            # Skipped under approval == "auto": the human already pre-authorized this chat's
            # writes, and stripping their tools would break the toggle they set.
            downgradeable = cap["risk"] == "write" and approval != "auto"
            # Brainstorm never re-classifies. The whole mode is "this turn changes nothing", and
            # the up-classify has no redirect on a bound session — a write-shaped musing on turn 4
            # ("so we'd just delete the QA loop then?") would bump the session to write and park
            # the conversation behind an approval card for work nobody asked to start. Asking for
            # the work itself is a NEW request in a normal chat, not a follow-up in this one.
            if not _is_brainstorm(cap) and (cap["risk"] == "read" or downgradeable):
                emergent = await workflow.execute_activity(
                    classify_followup,
                    {"message": request, "name": cap["name"], "repo": repo},
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
                if emergent.get("write") and cap["risk"] == "read":
                    cap = {**cap, "risk": "write"}
                    self._cap = cap
                    self._risk_reason = (f"this follow-up asks for a write action, so the "
                                         f"read session ('{cap['name']}') is gated for approval")
                elif not emergent.get("write") and downgradeable:
                    cap = {**cap, "risk": "read"}
                    self._cap = cap
                    self._discussion = True
        else:
            pinned = params.get("cap")

            # Swarm planning: a fresh, un-pinned request that isn't already a sub-task may
            # decompose into several INDEPENDENT capability runs that execute in parallel as
            # child workflows. A single cohesive task returns no fan-out and falls through to
            # the normal single-capability path below (no regression). Repo-mode (or a repo
            # candidate that may auto-engage it) is a focused single-cap task on one repo, so it
            # skips fan-out — parallel children would collide in the same clone anyway.
            self._enter("DECOMPOSE")
            if not pinned and not subtask and not repo and not repo_hint:
                plan = await workflow.execute_activity(
                    plan_swarm, request, start_to_close_timeout=timedelta(seconds=180),
                    retry_policy=_RETRY)
                subtasks = plan.get("subtasks") or []
                if len(subtasks) >= 2:
                    self._leave("DECOMPOSE")
                    swarm = await self._run_swarm(params, request, subtasks,
                                                  unattended, approval)
                    return swarm, None, request, None
            self._leave("DECOMPOSE")

            # A pinned capability (e.g. an event rule routing an alert straight to `incident`)
            # skips Router #1; otherwise route normally. Pass the repo context (explicit or
            # candidate) so repo-scoped project caps of that repo stay eligible (engine._repo_eligible).
            self._enter("ROUTER")
            cap = pinned or await workflow.execute_activity(
                route_request, {"request": request, "repo": repo or repo_hint},
                start_to_close_timeout=timedelta(seconds=180), retry_policy=_RETRY)
            self._cap = cap
            # Modifying a repo is inherently a write — force the gate even if the chosen cap
            # reads as read (the fresh-route write-intent guard below is then redundant for repo).
            if repo:
                cap = {**cap, "risk": "write"}
                self._cap = cap
                self._risk_reason = f"targets repo '{repo}' — repo edits always need approval"
            self._leave("ROUTER")
            # Clarification — when a human is present (interactive), OR for an unattended run that
            # opted in via `clarify` (the GitHub board: a human-in-the-loop work queue, where a
            # missing detail should PAUSE the ticket for input, not silently barrel ahead and then
            # mark it Done). A sub-task never clarifies (the planner already scoped it). When it
            # pauses, the run sits in awaiting_clarification — so the board card stays "In Progress"
            # (delivery only fires on completion) and the Otto Board surfaces it under "Waiting on
            # you". The answer arrives via the same provide_clarification signal (the board card's
            # "Open conversation" reattaches the chat to this live workflow).
            # Brainstorm asks its own questions, in-band and in the reply the user is already
            # reading (contracts._THINKING_PARTNER_FORMAT: "ending on a question is a complete
            # answer"). A separate clarify call ahead of it is a second LLM round-trip that
            # blocks the very first turn on a modal prompt — the opposite of what the mode is for.
            if not subtask and not _is_brainstorm(cap) and (not unattended or params.get("clarify")):
                self._enter("CLARIFY")
                clar = await workflow.execute_activity(
                    clarify_request, {"request": request, "name": cap["name"]},
                    start_to_close_timeout=timedelta(seconds=180), retry_policy=_RETRY)
                if clar.get("question"):
                    self._question = clar["question"]
                    self._awaiting_clarification = True
                    # The QUESTION is content too — it's Otto-written but routinely quotes the
                    # request back ("which of the two prod-a endpoints did you mean?"), so it
                    # rides on `detail` with the request, not in the always-sent lines.
                    await self._notify("Otto needs an answer",
                                       cap=cap, repo=repo, reply_to=reply_to,
                                       unattended=unattended,
                                       note="waiting on a clarification",
                                       detail=f"{clar['question']}\n\nTask: {request}",
                                       tags=["question"],
                                       priority="max", kind="clarify",   # unbounded wait
                                       wid=workflow.info().workflow_id)
                    await workflow.wait_condition(lambda: self._clarification is not None)
                    self._awaiting_clarification = False
                    request = f"{request}\n\n(clarification) {self._question} -> {self._clarification}"
                self._leave("CLARIFY")

            # Router #1 can misroute a write-intent request to a read-classified capability,
            # which would then auto-run ungated (and still mutate state via Bash). Re-assess the
            # request itself (the fresh-route analogue of the resumed-session guard above) so an
            # emergent write hits the approval gate below instead of riding in auto-approved.
            # Brainstorm is exempt: the guard exists because Router #1 can misroute write intent
            # onto a read cap, and routing cannot reach brainstorm at all (route_hidden). Left in,
            # it actively breaks the mode — `intent["redirect"]` is skipped for a PINNED cap, so a
            # write-shaped musing ("should we just delete the QA loop?") would fall through to the
            # risk bump and park a conversation behind an approval card. The toolset is still the
            # real guard: cap.risk stays "read", so this turn holds no Edit/Write either way.
            if cap["risk"] == "read" and not unattended and not _is_brainstorm(cap):
                intent = await workflow.execute_activity(
                    classify_request, {"request": request, "name": cap["name"]},
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
                if intent.get("write"):
                    if intent.get("redirect") and not pinned:
                        # The general assistant only ANSWERS (its prompt forbids acting), so a
                        # risk bump alone would gate a run that then refuses the task — swap in
                        # the general worker instead. A pinned /assistant is respected as-is.
                        cap = intent["redirect"]
                        self._cap = cap
                        self._risk_reason = ("the request asks for a write action — the "
                                             "write-intent guard redirected it from the "
                                             f"read-only assistant to '{cap['name']}'")
                    else:
                        cap = {**cap, "risk": "write"}
                        self._cap = cap
                        self._risk_reason = (f"the request itself asks for a write action — "
                                             f"'{cap['name']}' is read-classified, but the "
                                             "write-intent guard gates the request")
        return None, cap, request, subtask

    async def _run_impl(self, params) -> dict:
        # Back-compat: a bare string means a fresh request.
        if isinstance(params, str):
            params = {"request": params}
        request = params["request"]
        # Global pause (estop.py), checked BEFORE the first side effect — no chat thread, no
        # workspace clone, no settings snapshot. Every in-process ingress already refuses while
        # paused; this catches the one that can't be, a Temporal Schedule firing from the server.
        # A paused run exits as an ordinary finished run and deliberately sets no `_terminal`:
        # an operator who pressed pause doesn't want the Needs-you dashboard filling with rows
        # about their own pause.
        try:
            est = await workflow.execute_activity(
                estop_check, {},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1))
        except exceptions.ActivityError:
            est = {}
        if est.get("engaged"):
            reason = (est.get("reason") or "").strip()
            msg = "Paused — Otto is stopped for new work" + (f" ({reason})" if reason else "")
            return {"result": msg, "session_id": None, "cap": None,
                    "paused": True, "times": self._times}
        resume = params.get("resume")
        # Unattended = no human at trigger time (Temporal Schedule or an event/webhook). Older
        # callers pass `scheduled`; keep it as an alias.
        unattended = params.get("unattended", params.get("scheduled", False))
        # Write-approval mode for unattended runs: "auto" (pre-authorized) | "ask" (defer to a
        # human, surfaced on the Board) | "skip" (default). `auto_approve=True` maps to "auto".
        approval = params.get("approval") or ("auto" if params.get("auto_approve") else "skip")
        reply_to = params.get("reply_to")               # where to deliver the result (unattended)
        # WHO reads the result. It picks the run's output contract and the judge's rules
        # (engine._output_contract / engine.verify), so a result delivered to a person in a live
        # exchange is written as a reply rather than as Otto's internal report about them.
        # See `_audience_of` for how the target and the MODE combine; pure, so it is safe here.
        self._audience = _audience_of(params, reply_to)
        # Runbook: a HUMAN-authored plan handed in whole, already parameter-substituted by
        # runbooks.render(). `steps` is the dependency graph engine.run_plan executes (no
        # plan_task_steps call, no LLM tail re-plan — see run_plan's `replan` flag); `doc` is the
        # author's prose, which stands in for the plan preview at the gate AND rides into
        # execution + the verify judge as `approved_plan`, exactly like an approved preview would.
        authored_steps = params.get("steps") or []
        authored_doc = (params.get("doc") or "").strip() or None
        # Bind it as the approved plan NOW, not only inside the gate branch: an auto-approved
        # runbook (every cron fire) never reaches the gate, and the doc must still reach execution
        # and the judge — otherwise the one path where nobody is watching is the one path that
        # runs without the author's instructions.
        self._plan = authored_doc
        # Snapshot the UI-editable settings ONCE, before any branch reads them (see
        # activities.snapshot_settings for why this is an activity and not a live config read).
        # Tolerates an older worker without the activity registered: the code defaults stand in.
        try:
            self._settings = await workflow.execute_activity(
                snapshot_settings, {},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1))
        except exceptions.ActivityError:
            pass
        self._bind_composer(params)
        # Persist this run into a Chat sidebar thread (scheduled/event runs have no browser to
        # record their own turns). `chat_key` is a stable id (e.g. the schedule id) so the first
        # run creates the chat and later runs append; `chat_labels` tag its provenance.
        chat_key = params.get("chat_key")
        self._chat_key = chat_key
        # Open the Chat thread NOW (before routing/execution) so an unattended run is visible and
        # accessible the moment Otto starts the ticket, not only when it finishes. _record_chat
        # finalizes it later. An UNATTENDED resume (a Slack thread follow-up) appends its turn to
        # the existing thread the same way — chats.start_run appends when the chat exists — so the
        # sidebar shows the question, not just the answer. Interactive resumes carry no chat_key
        # (the browser records their turns), so they're unaffected.
        if chat_key:
            await self._open_chat(params, request)
        # Repo workspace (issue #57): when set (and allowlisted at ingress), a fresh run executes
        # in an ISOLATED clone of that repo and finishes by pushing a branch + opening a draft PR.
        # Modifying code is inherently a write, so it always hits the approval gate. Auto-engage
        # (repo_hint, below) is fresh-only, but an explicit `repo` also flows through a RESUME: a
        # follow-up to a repo-mode chat re-provisions the SAME workspace (see the resume branch)
        # instead of trying to reuse the clone already torn down after the original run.
        repo = _repo_of(params)
        self._repo = repo
        # The workflow id this run's git identity (workspace path + branch name) is keyed on —
        # the run that ORIGINALLY provisioned the clone, stable across follow-ups so a resume
        # reuses the same branch/path instead of minting a new one under this workflow's own id.
        git_run_id = params.get("git_run_id")
        self._git_run_id = git_run_id
        # The actual branch that carries the run's work — usually `otto/<git_run_id>`, but a
        # capability that drove its own git (e.g. sre-minion) may have opened its PR on a branch
        # of its own choosing instead (see `workspace._agent_pr`). Threaded from the ORIGINAL
        # run's result so a resume re-provisions the branch that actually has the PR, not a
        # guessed default that was never pushed.
        git_branch = params.get("git_branch")
        # A repo CANDIDATE (e.g. a board ticket tied to a registered repo) that did NOT explicitly
        # request repo-mode. If the run turns out to be a WRITE, we auto-engage repo-mode against
        # it below — so a write agent edits an ISOLATED clone (+ draft PR) instead of mutating the
        # live local checkout, even without an explicit repo-edit label / repo pick. A read run is
        # left untouched (no needless clone). Allowlisting is enforced upstream (poll_board).
        repo_hint = params.get("repo_hint") if not resume else None

        done, cap, request, subtask = await self._route_or_resume(
            params, request, resume, unattended, approval, reply_to, repo, repo_hint)
        if done is not None:
            return done

        # Interactive auto-detect: the web composer has no structured repo signal (unlike the
        # board), so the repo picker was a confusing upfront choice. Instead, for a fresh
        # INTERACTIVE write with no repo picked, detect whether the request unambiguously names a
        # registered repo AND actually edits its code (suggest_repo: pure name-match → cheap
        # edit-intent LLM) and, if so, set repo_hint so we auto-engage below — transparently (the
        # gate shows the clone target). The picker stays as an explicit OVERRIDE. Skipped on resume
        # (repo-mode is fresh-only) and for sub-tasks; the board path already carries repo_hint.
        if (not repo and not repo_hint and not resume and not params.get("subtask")
                and not unattended and cap["risk"] == "write"):
            sug = await workflow.execute_activity(
                suggest_repo, {"request": request, "name": cap["name"]},
                start_to_close_timeout=timedelta(seconds=90), retry_policy=_RETRY)
            repo_hint = sug.get("repo")

        # Auto-engage repo-mode: a fresh WRITE run tied to a registered repo candidate runs in an
        # ISOLATED clone (+ draft PR) instead of mutating the live local checkout — even without an
        # explicit repo-edit label / repo pick. Decided AFTER routing + write-intent so a read run
        # never needlessly clones. (The `if repo:` force-write above already covers explicit repo.)
        # (repo_hint is None on resume / when no candidate, so this short-circuits before the
        # fresh-branch-only `subtask` local is referenced.)
        if repo_hint and not repo and not params.get("subtask") and cap["risk"] == "write":
            repo = repo_hint
            self._repo = repo
            workflow.logger.info(f"auto-engaging repo-mode for write run -> {repo}")

        # A repo-mode chat's clone is torn down after every run, so a follow-up has to get it back
        # before anything else touches it — the plan preview below `claude -p --resume`s the session,
        # and that lookup is keyed on the cwd the session was created in. Done HERE, ahead of the
        # gate, rather than in the resume branch after it: with the preview running from the LIVE
        # checkout instead, it found no session history, returned nothing at all (0 tokens, 0 cost —
        # `web-642786ff`, 2026-08-04) and the gate rendered with no plan on it, which reads exactly
        # like a preview that decided there was nothing to do. Cost of the reordering is a clone
        # provisioned before the human answers; the deny path below tears it back down.
        resume_ws = None
        if resume and repo and git_run_id:
            resume_ws = await self._resume_workspace(repo, git_run_id, git_branch,
                                                     request=request)
            if resume_ws is None:
                # Not continuable. Say so WITHOUT gating first: asking someone to approve a
                # follow-up that cannot run is a worse dead end than the dead end itself.
                result = ("⚠️ Can't continue this conversation — the isolated workspace for "
                          "this task's branch no longer exists (it may have been merged or "
                          "deleted). Start a new task instead.")
                await self._record_chat(params, request, result, resume, cap)
                return {"result": result, "session_id": resume, "cap": cap, "attempts": 1,
                        "verified": None, "chat_key": chat_key, "repo": repo,
                        "git_run_id": git_run_id, "git_branch": git_branch, "times": self._times}

        # WHICH BRANCH this run is really about, resolved BEFORE the plan preview rather than at
        # provision time. Two things depend on it and the preview is the earlier one: it runs from
        # the repo's live checkout (the default branch), so without this it plans against a tree
        # that does not contain the code — `web-a6122d6c` spent the full 909s ceiling doing
        # exactly that. Cheap (`gh` only, no clone) and cached on self, so provisioning below
        # reuses it instead of asking again.
        if repo and not resume and not params.get("subtask"):
            self._pr_target = await workflow.execute_activity(
                resolve_pr_target, {"repo": repo, "request": request},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY) or {}

        # Approval for writes.
        done, request = await self._plan_and_gate(
            params, request, cap, approval, unattended, reply_to,
            repo, git_run_id, resume, resume_ws, authored_doc)
        if done is not None:
            return done

        # A resumed session is a raw follow-up in an ongoing conversation, not a fresh
        # task to judge — run it once, no verification. Bracketed as its own RUN span (mirroring
        # the fresh path below) so the client's totalDurMs() has a `times` entry to measure —
        # without it, every follow-up in a session showed a session id but no elapsed time.
        if resume:
            self._enter("RUN")
            # The clone was re-provisioned before the gate (see `_resume_workspace`) — the
            # not-continuable case already returned there, so reaching here with a repo means `ws`
            # is real. Don't fall back to running a repo-scoped capability with no cwd: it would
            # `claude -p --resume` from the wrong directory, find no session history, return
            # "(no output)", and (session_id then empty) wedge the chat into repeating the same
            # doomed attempt on every future message.
            ws = resume_ws
            out = await workflow.execute_activity(
                run_capability, {"request": request, "name": cap["name"], "resume": resume,
                                 "wid": workflow.info().workflow_id,
                                 "cwd": ws["path"] if ws else None, "repo": repo,
                                 "audience": self._audience, "approved_plan": self._plan,
                                 # Where the restored tree still contradicts this follow-up
                                 # (_resume_workspace's repair tier could not fix it). A resume
                                 # sends the raw message, so the session's history holds the
                                 # ORIGINAL system prompt — the worker contract's "if the code
                                 # isn't here, say so and stop" clause is not in scope for this
                                 # turn, and this note is what carries that instruction in-band.
                                 "grounding": self._grounding,
                                 # This TURN's risk, which is not always the registry's: a
                                 # discussion follow-up in a write-bound session runs read-only
                                 # (see the classify block above). The activity re-resolves the
                                 # cap by name, so without this the downgrade would skip the gate
                                 # and still hand out Edit/Write — the one combination that must
                                 # never happen.
                                 # Effort rides a resume too: it is a per-TURN flag, not a
                                 # session binding the way the model is.
                                 "risk": cap["risk"], "effort": self._effort},
                start_to_close_timeout=_EXEC_CEILING, heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY_EXEC)
            result = out["result"]
            if ws:
                pr = await workflow.execute_activity(
                    finalize_workspace,
                    {"run_id": git_run_id, "title": request[:120], "head": ws["head"],
                     "existing_pr": True, "branch": ws["branch"]},
                    start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
                    retry_policy=_RETRY)
                await workflow.execute_activity(
                    cleanup_workspace, {"run_id": git_run_id},
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
                if pr.get("pushed") and pr.get("pr_url"):
                    result += f"\n\n**Updated PR** on `{pr['branch']}`: {pr['pr_url']}"
                elif pr.get("pushed"):
                    result += f"\n\nPushed follow-up changes to the existing PR on `{pr['branch']}`."
            self._leave("RUN")
            await workflow.execute_activity(
                record_attempt,
                {"wid": out["workflow"], "request": request, "name": cap["name"],
                 "result": result, "cost": out.get("cost", 0), "attempt": 1,
                 "tokens": out.get("tokens"), "model": out.get("model"),
                 "verdict": None, "remember": True, "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
            # An UNATTENDED resume has no on-screen audience either (a Slack thread follow-up):
            # deliver it and finalize its Chat turn, exactly as the fresh path below does. Without
            # this the answer to a follow-up reached only the audit log.
            if reply_to:
                delivered = await workflow.execute_activity(
                    deliver_result, {"reply_to": reply_to, "result": result, "cap": cap,
                                     "run_id": workflow.info().workflow_id,
                                     "session_id": out.get("session_id")},
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
                if delivered and delivered.get("failed"):
                    await workflow.execute_activity(
                        finalize_terminal,
                        {"wid": out["workflow"], "request": request, "cap": cap,
                         "reason": "delivery_failed", "detail": delivered.get("status", ""),
                         "reply_to": None, "repo": repo},
                        start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
            await self._record_chat(params, request, result, out.get("session_id"), cap)
            return {"result": result, "session_id": out.get("session_id"),
                    "cap": cap, "attempts": 1, "verified": None, "chat_key": chat_key,
                    # Carried into the RESULT, not just the live status query: a turn this short
                    # can finish before the browser polls once, and a reload after it lands reads
                    # only this dict — where a downgraded turn would otherwise render as
                    # "read-only · auto-approved", crediting a toggle the user never set.
                    "discussion": self._discussion,
                    "repo": repo, "git_run_id": git_run_id if repo else None,
                    "git_branch": ws["branch"] if ws else git_branch,
                    "cost": out.get("cost", 0), "times": self._times}

        # Repo-mode: provision an isolated clone (approved above) and run the capability inside
        # it. Provisioned AFTER the gate, so a denied/skipped write never clones anything.
        ws, cwd, pre_snap = None, None, None
        if repo:
            # WHICH BRANCH the clone starts from. Normally a fresh one off the default, but a
            # request naming an OPEN pull request in this repo is asking for work on THAT PR's
            # code, which only exists on its head branch (`workspace.pr_target`). Without this
            # the run gets a default-branch tree that does not contain what it was asked to
            # change, and — measured on `web-d2438694` — either burns the attempt looking for a
            # sanctioned way to reach it or silently edits the default branch's unrelated
            # version and opens a SECOND PR against the wrong base. Already resolved above the
            # gate (the preview needs it too); this just reads the cached answer.
            target = self._pr_target.get("branch")
            ws = await workflow.execute_activity(
                provision_workspace,
                {"repo": repo, "run_id": workflow.info().workflow_id,
                 "from_branch": bool(target), "branch": target},
                start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY)
            cwd = ws["path"]
            # Does the tree actually contain what the request is about? Deterministic, advisory,
            # and cheap — it is the only part of the pipeline that asks the question at all, and
            # it asks BEFORE the money is spent. Threaded into execution and the judge below.
            got = await workflow.execute_activity(
                check_grounding, {"path": cwd, "request": request},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
            self._grounding = got.get("notes") or []
        elif not subtask and not unattended:
            # NOT repo-mode, human present: snapshot registered repos so an in-place edit to a
            # live checkout (via Bash) is flagged afterwards instead of happening silently — the
            # "forgot to pick a repo" case (issue #59). Unattended automation is deliberately
            # configured, so it's left unsnapshotted (a noted limitation).
            snap = await workflow.execute_activity(
                snapshot_repos, {}, start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
            pre_snap = snap.get("snap") or None

        # Run under the REAL Temporal workflow id (not an engine-minted counter id), so the
        # audit trail, transcripts, and board cards all correlate on the same identifier.
        wid, out, verdict, attempt = workflow.info().workflow_id, None, None, 1
        # Plan-then-execute (design doc 2026-07-16): a strong model (Claude) decomposes the task
        # into atomic steps a LOCAL executor runs one at a time. Engaged only for a fresh, non-repo,
        # single-cap run when plan mode is active — the plan_task_steps activity makes that decision
        # (keeping the model/config lookup out of the deterministic workflow) and returns [] to fall
        # through to the normal verify ladder (no regression). v1 SKIPS repo-mode: an isolated-clone
        # + step loop is a noted follow-up, so repo runs always take the single-cap ladder below.
        # A runbook supplies its OWN graph, so it skips the planner entirely — and stays engaged
        # regardless of plan_mode/repo, because those gate whether Otto should *invent* a plan,
        # which has no bearing on whether it should run one a human already wrote.
        plan_list, authored = authored_steps, bool(authored_steps)
        if _may_plan_steps(authored, self._setting("plan_mode"), repo, subtask, cap):
            sres = await workflow.execute_activity(
                plan_task_steps,
                {"request": request, "name": cap["name"], "requested": params.get("plan_mode", False)},
                start_to_close_timeout=timedelta(seconds=180), retry_policy=_RETRY)
            plan_list = sres.get("steps") or []

        self._enter("RUN")
        if plan_list:
            # Execute the whole plan in ONE activity (engine.run_plan: per-step verify ladder +
            # bounded re-plan + synthesis). Coarser durability than the per-attempt ladder — a
            # worker crash re-runs the plan — but each step is audited as it goes and plan
            # execution is local/cheap (acceptable v1 cut; _RETRY_EXEC surfaces a lost activity
            # rather than silently re-running mid-plan, issue #91).
            self._attempt = 1
            try:
                pout = await workflow.execute_activity(
                    execute_plan,
                    {"request": request, "name": cap["name"], "steps": plan_list, "wid": wid,
                     "model_override": self._model_override, "authored": authored,
                     "repo": repo},
                    start_to_close_timeout=timedelta(minutes=90),
                    heartbeat_timeout=_HEARTBEAT, retry_policy=_RETRY_EXEC)
            except exceptions.ActivityError:
                pout = {"result": "(plan execution failed — the worker died or the run timed out)",
                        "passed": False, "cost": 0, "tokens": None, "steps_run": 0,
                        "budget_stop": False}
            self._account(pout)
            out = {"result": pout["result"], "session_id": None, "workflow": wid}
            verdict = {"passed": bool(pout.get("passed"))}
            attempt = pout.get("steps_run") or 1
            self._verified = verdict["passed"]
            if pout.get("budget_stop"):
                self._needs_human = {"reason": "budget_exceeded"}
            elif pout.get("auth_stop"):
                self._needs_human = {
                    "reason": error_classifier.claude_wall_reason(pout.get("auth_wall"))}
            elif pout.get("strict_stop"):
                self._needs_human = {"reason": config.STRICT_STOP_REASON}
            # Record a top-level attempt row under the bare wid (steps audited under wid-sN),
            # so the run-detail view, needs-you retry, and memory extraction all find the run.
            await workflow.execute_activity(
                record_attempt,
                {"wid": wid, "request": request, "name": cap["name"], "result": pout["result"],
                 "cost": pout.get("cost", 0), "attempt": attempt, "tokens": pout.get("tokens"),
                 "model": None, "verdict": verdict, "remember": verdict["passed"], "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
        else:
            out, verdict, attempt, wid = await self._verify_ladder(
                request, cap, cwd, repo, recall=not subtask, unattended=unattended)
        self._leave("RUN")

        result = out["result"]
        # Notes ABOUT THE RUN, in Otto's own vocabulary — for the operator's record only, see
        # `_shape_result`. Anything the READER needs (a PR url) goes in `result`, not here.
        notes = []
        # Whether automated verification passed. The verify_exhausted needs-human decision is
        # DEFERRED to after the repo finalize below: a repo-mode run's deliverable is a DRAFT PR
        # that by design awaits human review on GitHub, so a failed verify there is advisory (the
        # PR is the human gate) — only a run with nothing downstream to catch a bad result blocks.
        passed = bool(verdict and verdict.get("passed"))
        pr = None
        # Repo-mode: push the branch + open a draft PR, then tear down the workspace (always —
        # even on a poor result — so clones don't leak; a stale-sweep backstops a hard failure).
        if ws:
            pr, result = await self._finalize_pr(ws, request, result, cap, repo)

        # Post-PR code-review loop: get a strict Claude PR review, fold must/should-fix findings
        # into a fix on the SAME branch, re-review — bounded rounds (sre-minion's Phase 5-7,
        # platform-owned). DEFAULT-ON for EVERY repo-mode PR now, not just the general worker:
        # a PR is a PR whoever wrote it, and the caps most likely to be trusted without the box
        # ticked are the heavyweight agents whose diffs are largest. Measured (web-5f9319cd): a
        # 428-line sre-minion PR that contradicted its own approved plan and carried an
        # apply-breaking chart-selector bug shipped with NO independent diff review, because
        # review was opt-in for anything but the worker and the composer box was unticked.
        # The web path only ever sends review:true or omits the key (server.py never writes False),
        # so this flips the ABSENT case; an ingress passing review:False explicitly still opts out.
        # Pre-authorized (the reviewer is read-only; the fix runs use the already-approved write
        # cap). Only when a PR was actually opened. Runs BEFORE QA so code-review findings are
        # fixed before empirical validation.
        review = None
        if pr and pr.get("pr_url") and params.get("review", True):
            self._enter("REVIEW")
            review = await self._run_review_loop(request, cap, repo, pr["pr_url"])
            notes.append(self._review_summary(review))
            self._leave("REVIEW")
            # A review that didn't come back clean needs a human (PR stays draft).
            if review.get("state") in ("fail", "inconclusive"):
                self._needs_human = {"reason": f"review_{review['state']}"}

        # Post-PR QA loop (opt-in): empirically validate the PR with the QA capability and, on a
        # FAIL, fold its findings into a fix on the SAME branch and re-QA — bounded rounds. The
        # whole loop is pre-authorized (enabling QA is the grant). Only when a PR was actually
        # opened (nothing to validate otherwise), and not when the review already flagged it.
        qa = None
        if params.get("qa") and pr and pr.get("pr_url") and not self._needs_human:
            self._enter("QA")
            qa = await self._run_qa_loop(request, cap, repo, pr["pr_url"])
            notes.append(self._qa_summary(qa))
            self._leave("QA")
            # QA that didn't cleanly pass needs a human (PR stays draft) — same Blocked routing.
            if qa.get("state") in ("fail", "inconclusive"):
                self._needs_human = {"reason": f"qa_{qa['state']}"}

        # Everything left is bookkeeping and delivery, but it is not instant — a Slack post that
        # retries, a needs-human finalize and an in-place-edit scan all happen here, and with no
        # span open the board card showed no stage at all for it (the same "stalled run" read the
        # stage chip exists to prevent).
        self._enter("DELIVER")

        # Deferred verify_exhausted decision (see `passed` above). A failed automated verify only
        # BLOCKS (needs-human → Blocked column, banner, Needs-you) when there's nothing downstream
        # to catch a bad result. When a repo-mode DRAFT PR opened, the PR itself is the human
        # review gate: the run COMPLETES (card → Review) and the failed verify is surfaced as an
        # advisory note on the result, not a hold. (budget_exceeded / qa_* already set above win.)
        pr_opened = bool(pr and pr.get("pr_url"))
        # No verdict = no judge ever ran (brainstorm), so there is no failed verify to defer —
        # unguarded, the mode's first reply lands in needs-human and the chat renders Blocked.
        if verdict is not None and not passed and not self._needs_human:
            if pr_opened:
                notes.append("\n\n_⚠️ Automated verification didn't pass — the draft PR is open for "
                             "your review; check it carefully before merging._")
            else:
                self._needs_human = {"reason": ("harness_exhausted" if self._harness_stop
                                                else "verify_exhausted")}

        # NOT repo-mode: flag any in-place edits the run made to a registered live checkout, so
        # they're visible (and audited) rather than silent (issue #59).
        in_place = None
        if pre_snap:
            det = await workflow.execute_activity(
                detect_repo_changes,
                {"before": pre_snap, "wid": wid, "request": request},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
            in_place = det.get("changed") or None
            if in_place:
                names = ", ".join(c["name"] for c in in_place)
                notes.append(f"\n\n⚠️ _Edited live checkout(s) outside an isolated workspace: "
                             f"**{names}**. Pick the repo in the composer to run in a clone + PR instead._")

        # A WORKER run without repo-mode cannot deliver: the worker never runs git itself
        # (repo-mode owns commit/branch/PR), so its edits are stranded UNCOMMITTED in whatever
        # cwd it ran in and "the change is ready" reads as success. Say so explicitly and name
        # the fix — auto-engage can only match repos REGISTERED on this machine.
        if cap["name"] == config.WORKER_CAP and not ws:
            notes.append("\n\n⚠️ _No isolated workspace was engaged, so nothing was committed and "
                         "no PR could be opened (the worker never runs git itself). Register the "
                         "target repo under Admin → Project repos — auto-detect only matches "
                         "registered repos — or pick it in the composer, then retry._")

        # A needs-human run finalizes exactly like every other terminal state (the banner itself
        # is `_shape_result`'s). This path used to only push a notification, so verify_exhausted /
        # qa_* / review_* / budget_exceeded left NO durable audit row — /api/needs-you saw them
        # solely through live Temporal visibility, and the trail showed nothing but failed
        # attempts once history aged out. ONE notifier per terminal state (the finalizer's).
        if self._needs_human:
            self._terminal = dict(self._needs_human)
            await workflow.execute_activity(
                finalize_terminal,
                {"wid": wid, "request": request, "cap": cap,
                 "reason": self._needs_human["reason"], "reply_to": reply_to, "repo": repo,
                 "unattended": unattended},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)

        reply, result = self._shape_result(result, notes)
        # Deliver the result to its reply target (unattended runs have no on-screen audience).
        if reply_to:
            # Card destination: Blocked when it needs a human; else Review when a draft PR is
            # awaiting (keyed on whether a PR ACTUALLY opened — so an auto-engaged repo-mode run
            # still routes to Review, and a label'd ticket that produced no PR doesn't); else Done.
            if isinstance(reply_to, dict) and reply_to.get("kind") == "github_issue":
                reply_to = {**reply_to, "repo_edit": bool(pr and pr.get("pr_url")),
                            "blocked": bool(self._needs_human)}
            delivered = await workflow.execute_activity(
                deliver_result, {"reply_to": reply_to, "result": reply, "cap": cap,
                                 "run_id": workflow.info().workflow_id,
                                 # Lets a conversational sink (a Slack thread) record what a
                                 # follow-up needs to CONTINUE this run instead of starting cold.
                                 "session_id": out.get("session_id")},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
            # A failed/partial delivery would otherwise lose the result silently — record a durable
            # terminal row so it surfaces on the Needs-you dashboard.
            if delivered and delivered.get("failed"):
                await workflow.execute_activity(
                    finalize_terminal,
                    {"wid": wid, "request": request, "cap": cap, "reason": "delivery_failed",
                     "detail": delivered.get("status", ""), "reply_to": None, "repo": repo},
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
        await self._record_chat(params, request, result, out.get("session_id"), cap)
        self._leave("DELIVER")

        # Opt-in clean-finish push (OTTO_NTFY_ON_COMPLETE; the activity drops it when off), and
        # UNATTENDED ONLY — see run-pipeline.md. Needs-human runs already pushed above; swarm
        # children stay quiet (one ping per task, not per sub-task).
        if not self._needs_human and not params.get("subtask") and unattended:
            await self._notify(f"Otto finished: {cap['name']}",
                               cap=cap, repo=repo, reply_to=reply_to, unattended=unattended,
                               note=(f"PR: {pr['pr_url']}" if pr and pr.get("pr_url") else None),
                               detail=request,
                               tags=["white_check_mark"], kind="complete", priority="low",
                               wid=workflow.info().workflow_id)

        return {"result": result, "session_id": out.get("session_id"), "cap": cap,
                "needs_human": self._needs_human, "attempts": attempt,
                "verified": _verified_of(verdict), "pr": pr,
                "repo": repo, "git_run_id": workflow.info().workflow_id if repo else None,
                "git_branch": (pr.get("branch") if pr else None) if repo else None,
                "in_place": in_place, "qa": qa, "review": review, "chat_key": chat_key,
                "cost": self._spent["cost"], "times": self._times}

    def _shape_result(self, result, notes):
        """Split what the READER gets from what the RECORD keeps: `(reply, record)`.

        A CONVERSATION audience (a person in a Slack exchange) gets the answer alone. Everything
        else here is written in Otto's own vocabulary — workspaces, verify rounds, Admin screens,
        `_NEEDS_HUMAN_BANNER` — and a colleague can act on none of it; the human who CAN act reads
        the record, on the chat thread and Needs-you. Measured (slack-D06G601G0R1-1788479715): a
        "Register the target repo under Admin → Project repos" footer posted under a reply to a
        third party, on a request that was never about a repo. The notes are appended AFTER the
        model wrote the reply, so the output contract (`_DIRECT_REPLY_FORMAT`) can't police them."""
        record = result
        if self._needs_human:
            record = _NEEDS_HUMAN_BANNER.get(
                self._needs_human["reason"], "⚠️ **Needs human review.**") + "\n\n" + record
        record += "".join(str(n) for n in notes if n)
        if self._audience == contracts.CONVERSATION_AUDIENCE:
            return result, record
        return record, record

    def _account(self, out):
        """Add one activity's output-token + USD spend to the run's budget accumulator."""
        self._spent["output"] += (out.get("tokens") or {}).get("output", 0) or 0
        self._spent["cost"] += out.get("cost", 0) or 0

    async def _brainstorm_turn(self, request, cap, cwd, repo):
        """ONE unjudged attempt, for the brainstorm mode. The sibling of `_verify_ladder` with the
        ladder removed — same activity, same budget accounting, same audit row; no judge, no
        retry, no escalation. Returns `(out, attempt)`; the caller supplies `verdict = None`.

        The supervisor is disarmed too: `supervise_enforce` is documented as armed only while a
        rung remains for its critique to steer, and here there is none — a kill would discard the
        only attempt and leave the user's message unanswered."""
        wid, attempt = workflow.info().workflow_id, 1
        self._attempt = attempt
        try:
            out = await workflow.execute_activity(
                run_capability,
                {"request": request, "name": cap["name"], "attempt": attempt,
                 "critique": None, "escalate": False, "downshift": False,
                 "wid": wid, "cwd": cwd, "repo": repo,
                 "recall": True, "audience": self._audience,
                 "approved_plan": None, "grounding": self._grounding,
                 "memory_enabled": self._memory_enabled,
                 "model_override": self._model_override, "effort": self._effort,
                 "supervise_enforce": False},
                start_to_close_timeout=_EXEC_CEILING, heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY_EXEC)
        except exceptions.ActivityError:
            out = {"workflow": wid, "attempt": attempt, "is_error": True,
                   "result": "(execution activity failed — worker died or attempt timed out)",
                   "cost": 0, "tokens": None, "model": None}
        wid = out["workflow"]
        self._account(out)
        self._verified = None
        # `remember=True`: a brainstorm is where decisions get made, which is exactly what
        # engine._is_durable_fact is biased to keep. Nothing else records this turn — there is
        # no judged attempt row behind it.
        await workflow.execute_activity(
            record_attempt,
            {"wid": wid, "request": request, "name": cap["name"], "result": out["result"],
             "cost": out.get("cost", 0), "attempt": attempt, "tokens": out.get("tokens"),
             "model": out.get("model"), "verdict": None,
             "duration_s": out.get("duration_s"), "backend": out.get("backend"),
             "remember": True, "repo": repo},
            start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
        return out, attempt

    async def _verify_ladder(self, request, cap, cwd, repo, recall, unattended=False):
        """verify -> retry -> escalate for ONE task. Lives in the workflow (deterministic); every
        LLM call / file write is delegated to an activity. Accumulates spend into self._spent,
        honours the hard/soft budget (setting self._needs_human on a hard stop), and latches
        local_disabled so the rest of the ladder runs on Claude when EITHER an attempt proves the
        local backend can't serve the cap (the vLLM server rejects tool calls) OR a WRITE cap
        failed verify on the local backend (issue #172 — escalate off local rather than dead-end
        or ship a shallow PR). Returns (out, verdict, attempt, wid). Extracted so plan-then-execute
        can be a clean sibling.

        Brainstorm short-circuits here rather than at the call site: this method IS the "run the
        task, come back with (out, verdict, attempt, wid)" seam, and `_run_impl` is deterministic
        replay code held to a branch ratchet that a fourth dispatch arm would break."""
        if _is_brainstorm(cap):
            # ONE attempt, no judge. The ladder scores an attempt against the request's implied
            # acceptance criteria — a brainstorm has none, and the two verdicts it would invent
            # are both wrong here: a reply that ends on a question trips the unattended dead-end
            # rule, and a reply offering options instead of a conclusion reads as an unfinished
            # task. Retrying either folds the critique in and produces exactly the report the mode
            # exists to avoid, then re-judges THAT, at 2-3x the cost and latency of the turn the
            # user actually wanted. The user is sitting right there and can push back in one
            # line, which is both a better verifier and the whole premise of the mode.
            out, attempt = await self._brainstorm_turn(request, cap, cwd, repo)
            return out, None, attempt, out["workflow"]
        n = max(1, self._setting("max_attempts"))
        # HARNESS deaths get their OWN bounded budget. They are not judgements — no judge read any
        # output — so spending a rung of `n` on one both shortens the real ladder and drags the
        # final-rung model escalation forward onto a timeout, which escalating cannot fix. Measured
        # over the trail, 21% of recorded verify failures were harness deaths. `judged` therefore
        # drives `final`/exhaustion; `attempt` stays the PHYSICAL index (transcript filename, audit
        # row) and keeps incrementing, so two attempts never collide on it.
        spare = max(0, self._setting("max_harness_retries"))
        wid, critique, out, verdict, attempt = workflow.info().workflow_id, None, None, None, 1
        local_disabled, local_disabled_reason = False, None
        judged, attempt = 0, 0
        kills = 0
        max_kills = max(0, self._setting("max_supervisor_kills"))
        while True:
            attempt += 1
            final = judged == n - 1
            self._attempt = attempt
            # Hard cost ceiling: stop BEFORE launching another attempt and route to needs-human.
            # (Never fires on attempt 1 — spend starts at 0 and a 0 budget knob is disabled.)
            if config.budget_exceeded(self._spent["output"], self._spent["cost"], hard=True,
                                      snapshot=self._settings):
                self._needs_human = {"reason": "budget_exceeded"}
                break
            # Soft threshold: finish remaining attempts on a cheaper tier (unless this is the final
            # escalation attempt, which takes precedence — hard-stop > escalate > downshift).
            downshift = (not final and
                         config.budget_exceeded(self._spent["output"], self._spent["cost"],
                                                hard=False, snapshot=self._settings))
            try:
                out = await workflow.execute_activity(
                    run_capability,
                    {"request": request, "name": cap["name"], "attempt": attempt,
                     "critique": critique, "escalate": final, "downshift": downshift,
                     "wid": wid, "cwd": cwd, "repo": repo, "local_disabled": local_disabled,
                     "local_disabled_reason": local_disabled_reason,
                     # Recall past solved-task approaches on a fresh top-level run; a swarm sub-task
                     # (subtask=True) skips it so it doesn't pull in the parent task's methods (issue #66).
                     "recall": recall, "audience": self._audience,
                     # The plan the human approved at the gate — carried into EXECUTION, not just
                     # shown on the card. None when no gate fired (unattended auto-approve, reads).
                     "approved_plan": self._plan,
                     # Where the tree disagrees with the request (a named file that isn't there,
                     # a line number it is far too short to have). Handed over so the attempt
                     # REPORTS the mismatch instead of quietly editing the nearest thing it finds.
                     "grounding": self._grounding,
                     # Per-chat composer overrides (memory checkbox + model picker).
                     "memory_enabled": self._memory_enabled, "model_override": self._model_override,
                     "effort": self._effort,
                     # Arm the supervisor's kill switch only while a rung remains for its critique
                     # to steer and the run has kills left to spend. Mirrors engine._ladder_core.
                     "supervise_enforce": (not final and kills < max_kills)},
                    start_to_close_timeout=_EXEC_CEILING, heartbeat_timeout=_HEARTBEAT,
                    retry_policy=_RETRY_EXEC)
            except exceptions.ActivityError:
                # The activity itself died (worker restarted mid-attempt, hard timeout, crash).
                # Never re-run it at the Temporal layer — count it as a FAILED attempt so the
                # bounded, audited ladder takes the next shot instead (issue #91).
                out = {"workflow": wid or workflow.info().workflow_id,
                       "result": "(execution activity failed — worker died or attempt timed out)",
                       "is_error": True, "cost": 0, "tokens": None, "model": None,
                       "attempt": attempt}
            wid = out["workflow"]
            if out.get("killed_by_supervisor"):
                kills += 1
            local_disabled = local_disabled or out.get("local_incapable", False)
            self._account(out)
            if out.get("local_strict_stop"):
                # Strict mode (OTTO_LOCAL_FALLBACK=0): local couldn't run and Claude may not
                # cover. Terminal here — no verify (nothing to judge) and no further rungs (they'd
                # hit the same dead endpoint). Mirrors engine.execute. Still audited as an attempt,
                # so the run's story is complete on the board and in the Debug drawer.
                self._needs_human = {"reason": config.STRICT_STOP_REASON}
                self._verified = False
                verdict = {"passed": False, "source": "harness",
                           "critique": "stopped: " + str(out["result"])[:200]}
                await workflow.execute_activity(
                    record_attempt,
                    {"wid": wid, "request": request, "name": cap["name"],
                     "result": out["result"], "cost": out.get("cost", 0), "attempt": attempt,
                     "tokens": out.get("tokens"), "model": out.get("model"), "verdict": verdict,
                     "duration_s": out.get("duration_s"), "backend": out.get("backend"),
                     "remember": False, "repo": repo},
                    start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
                break
            if out.get("auth_stop"):
                # A deterministic Claude-backend wall: a dead login, a spent usage limit, or a
                # model this subscription cannot serve. Every remaining rung reaches the same
                # refusal, so this is terminal here rather than three harness deaths deep, with
                # its own reason instead of "harness_exhausted (timeout or worker crash)", which
                # names the wrong culprit and hides a one-line fix. Mirrors engine._ladder_core.
                wall = out.get("claude_wall")
                self._needs_human = {"reason": error_classifier.claude_wall_reason(wall)}
                self._verified = False
                out["result"] = error_classifier.claude_wall_message(out.get("result", ""), wall)
                verdict = {"passed": False, "source": "harness",
                           "critique": f"stopped: Claude backend wall ({wall or 'auth'})"}
                await workflow.execute_activity(
                    record_attempt,
                    {"wid": wid, "request": request, "name": cap["name"],
                     "result": out["result"], "cost": out.get("cost", 0), "attempt": attempt,
                     "tokens": out.get("tokens"), "model": out.get("model"), "verdict": verdict,
                     "duration_s": out.get("duration_s"), "backend": out.get("backend"),
                     "remember": False, "repo": repo},
                    start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
                break
            if out.get("is_error"):
                # Errored/timed-out attempt: a failed attempt, not valid output. Skip the verifier
                # (don't judge the "(timed out)" string) and let the retry/escalation ladder run.
                # A supervisor-killed attempt steers the next rung with the supervisor's own
                # critique (mirrors engine.error_verdict — kept inline: workflow code is
                # deterministic-only, and this is pure string work).
                res = str(out["result"])
                if "(aborted by supervisor:" in res:
                    reason = res.split("(aborted by supervisor:", 1)[1].strip().rstrip(")").strip()
                    verdict = {"passed": False, "source": "supervisor",
                               "critique": ("the mid-run supervisor stopped the previous attempt "
                                            f"because it was off-course: {reason} — take a "
                                            "different approach this time.")}
                else:
                    verdict = {"passed": False, "source": "harness",
                               "critique": "prior attempt errored or timed out: " + res[:200]}
            else:
                verdict = await workflow.execute_activity(
                    verify_capability,
                    {"request": request, "name": cap["name"], "result": out["result"],
                     "repo": repo, "local": out.get("local", False),
                     "unattended": unattended, "audience": self._audience,
                     "approved_plan": self._plan, "grounding": self._grounding,
                     # What the attempt actually called. Mirrors engine._ladder_core.
                     "tools_used": out.get("tools_used"),
                     "tools_failed": out.get("tools_failed"),
                     # Mid-run supervisor corrections this attempt was given: the request the
                     # judge scores against is the AMENDED one. Mirrors engine._ladder_core.
                     "steers": out.get("steers")},
                    start_to_close_timeout=timedelta(seconds=180), retry_policy=_RETRY)
            await workflow.execute_activity(
                record_attempt,
                {"wid": wid, "request": request, "name": cap["name"], "result": out["result"],
                 "cost": out.get("cost", 0), "attempt": attempt,
                 "tokens": out.get("tokens"), "model": out.get("model"), "verdict": verdict,
                 "duration_s": out.get("duration_s"), "backend": out.get("backend"),
                 "fallback_from": out.get("fallback_from"),
                 "fallback_reason": out.get("fallback_reason"),
                 "remember": verdict["passed"] or final, "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
            self._verified = verdict["passed"]
            if verdict["passed"]:
                break
            # Safe local write escalation (issue #172): a WRITE cap that ran locally and FAILED
            # verify escalates off local — the rest of the ladder (incl. the final strongest-model
            # rung) runs on Claude instead of retrying the same weak local model, which would
            # dead-end or ship a shallow PR. Mirrors engine.execute; latched like local_disabled.
            # Strict mode keeps a verify-failed local run LOCAL — it retries on the same local
            # model and lands in needs-human rather than being rescued by Claude.
            # ... but NOT on a harness death: no judge read the attempt, so there is no evidence
            # about the model. Spared from the rung accounting ten lines below for that exact
            # reason, and it must be spared here too — otherwise a local model that merely ran out
            # of tokens is banished from the rest of the run (`web-a056884d`: 75k output tokens,
            # finish_reason=length, then three Claude attempts ending on Opus). Mirrors
            # engine._ladder_core, and the budget-death rule in gateway-backends.md.
            if (self._setting("local_fallback") and not local_disabled
                    and out.get("write_local") and verdict.get("source") != "harness"):
                local_disabled = True
                local_disabled_reason = config.WRITE_LOCAL_ESCALATE_REASON
            critique = verdict["critique"]
            # Spend a rung. A HARNESS death draws on `spare` instead: it is not evidence about the
            # capability, so it neither shortens the judged ladder nor triggers escalation. A
            # SUPERVISOR kill DOES spend a judged rung — enforce-mode is a deliberate intervention
            # that config.MAX_VERIFY_ATTEMPTS is documented to bound.
            if verdict.get("source") == "harness":
                spare -= 1
                if spare < 0:
                    self._harness_stop = True
                    break
            else:
                judged += 1
                if judged >= n:
                    break
        return out, verdict, attempt, wid

    async def _notify(self, title, *, cap=None, repo=None, reply_to=None, unattended=False,
                      note=None, detail=None, tags=None, kind=None, priority="high", wid=None):
        """Best-effort owner push (issue #92) on a human-blocking transition. Swallows every
        failure — including notify_human not being registered on an older worker — because a
        notification must never fail or stall the run it announces. `kind="complete"` pushes
        are opt-in (OTTO_NTFY_ON_COMPLETE) — the ACTIVITY drops them when the flag is off,
        so the workflow schedules it unconditionally and stays deterministic across flag flips.

        A push leaves the machine, so the two kinds of text are split at this signature and stay
        split all the way down (`privacy.context_lines` -> `delivery.notify`): `cap`/`repo`/
        `reply_to`/`note` describe the run in Otto's own vocabulary and are always sent, while
        `detail` is request/ticket/message CONTENT and is dropped unless OTTO_NTFY_DETAIL is on.
        Pass the request as `detail`, never folded into `title` or `note` — that is the whole
        control, and `test_core.NotificationContentTests` greps these call sites to enforce it.

        A push a human is BLOCKED behind is retried; a "complete" one is not. One attempt was
        right when every push was informational, but the gate push is the alert for a run that
        will sit for gate_timeout_h and then auto-decline — a single transient ntfy failure
        silently costs the whole run, and nothing anywhere records that the phone never rang.
        Duplicates from a retry are swallowed by `delivery.notify`'s dedupe window."""
        blocking = kind != "complete"
        try:
            await workflow.execute_activity(
                notify_human, {"title": title, "cap": cap, "repo": repo, "reply_to": reply_to,
                               "unattended": unattended, "note": note, "detail": detail,
                               "tags": tags, "kind": kind, "priority": priority, "wid": wid},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3 if blocking else 1))
        except Exception:  # noqa: BLE001 - strictly best-effort
            pass

    async def _fix_workspace(self, repo, run_id, pr_url):
        """Provision the clone a post-PR fix round commits into. Returns the workspace, or None
        when the branch carrying the work can't be checked out.

        Two things `from_branch=True` alone gets wrong here. It defaults to `otto/<run_id>`,
        but a run asked to amend an EXISTING pull request pushes to that PR's branch and never
        pushes its own — so the fetch asks for a ref that was never created. And it raises,
        which for a post-PR loop is fatal in the wrong direction: the work is already committed,
        pushed and reviewed by the time either loop runs, so an unprovisionable fix round must
        degrade to inconclusive rather than fail the workflow and report a finished run as
        Failed (`web-346d40a5`)."""
        branch = None
        if pr_url:
            got = await workflow.execute_activity(
                pr_head_branch, {"repo": repo, "pr_url": pr_url},
                start_to_close_timeout=timedelta(seconds=90), retry_policy=_RETRY)
            branch = got.get("branch")
        try:
            return await workflow.execute_activity(
                provision_workspace,
                {"repo": repo, "run_id": run_id, "from_branch": True, "branch": branch},
                start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY)
        except exceptions.ActivityError:
            return None

    @staticmethod
    def _fix_error(fix):
        """One-line reason a post-PR fix round produced nothing, for the human-facing critique.
        Clipped WITH a marker — an unmarked cut manufactures a defect the reader then blames."""
        res = str(fix.get("result") or "").strip() or "(no output)"
        return res[:300] + " […clipped]" if len(res) > 300 else res

    async def _run_qa_loop(self, request, cap, repo, pr_url):
        """Validate the opened PR with the QA capability; on a FAIL, fold its findings into a
        fix on the SAME branch and re-QA. Returns {state, rounds, qa_cap, critique?} where
        state is pass | fail | inconclusive | unavailable. Bounded by config.MAX_QA_ROUNDS fix
        rounds (the QA cap runs at most MAX_QA_ROUNDS + 1 times). PASS ends clean; INCONCLUSIVE
        or a still-FAIL after the budget stops for a human (PR stays draft). Pre-authorized —
        the QA cap and each fix run execute without re-gating (enabling QA is the grant)."""
        rounds = max(0, self._setting("max_qa_rounds"))
        run_id = workflow.info().workflow_id
        qa_cap_name = None
        for rnd in range(rounds + 1):       # round 0 = initial QA; then up to `rounds` fix+re-QA
            self._qa = {"state": "qa", "round": rnd}
            qa_out = await workflow.execute_activity(
                qa_capability,
                {"pr_url": pr_url, "repo": repo, "request": request, "wid": f"{run_id}-qa{rnd}"},
                start_to_close_timeout=timedelta(minutes=30), heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY_EXEC)
            if qa_out.get("missing"):
                self._qa = {"state": "unavailable", "round": rnd}
                return {"state": "unavailable", "rounds": rnd, "qa_cap": qa_out.get("qa_cap")}
            self._account(qa_out)
            qa_cap_name = qa_out.get("qa_cap")
            verdict = await workflow.execute_activity(
                judge_qa, {"request": request, "result": qa_out["result"], "repo": repo},
                start_to_close_timeout=timedelta(seconds=180), retry_policy=_RETRY)
            # Audit the QA pass as its own attempt (passed only on an outright PASS).
            # `source: "qa"` for the same reason the review loop stamps "review": this is
            # judge_qa's verdict on the PR's BEHAVIOUR, not a verify verdict on the QA
            # capability, and only source=="judge" is evidence about a capability.
            await workflow.execute_activity(
                record_attempt,
                {"wid": qa_out["workflow"], "request": f"[QA] {request}", "name": qa_cap_name,
                 "result": qa_out["result"], "cost": qa_out.get("cost", 0), "attempt": rnd + 1,
                 "tokens": qa_out.get("tokens"), "model": qa_out.get("model"),
                 "duration_s": qa_out.get("duration_s"),
                 "verdict": {"passed": verdict["verdict"] == "pass",
                             "critique": verdict.get("critique", ""), "source": "qa"},
                 "remember": False, "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
            self._qa = {"state": verdict["verdict"], "round": rnd}
            if verdict["verdict"] == "pass":
                return {"state": "pass", "rounds": rnd, "qa_cap": qa_cap_name}
            if verdict["verdict"] == "inconclusive":
                return {"state": "inconclusive", "rounds": rnd, "qa_cap": qa_cap_name,
                        "critique": verdict.get("critique", "")}
            # FAIL — fix on the same branch unless the round budget is spent.
            if rnd == rounds:
                return {"state": "fail", "rounds": rnd, "qa_cap": qa_cap_name,
                        "critique": verdict.get("critique", "")}
            self._qa = {"state": "fixing", "round": rnd}
            ws = await self._fix_workspace(repo, run_id, pr_url)
            if ws is None:
                return {"state": "inconclusive", "rounds": rnd, "qa_cap": qa_cap_name,
                        "critique": "QA failed but the fix round could not run — the branch "
                                    "carrying this PR's work could not be checked out.\n"
                                    + verdict.get("critique", "")}
            fix_critique = ("The QA validation of this PR did not pass. Address these findings, "
                            "committing the fix to the current branch (do NOT open a new PR). "
                            "The findings are about the CODE: fix those. Do not append "
                            "a section to the PR description recording this round — edit "
                            "the description in place only if what the PR does has "
                            "actually changed.\n"
                            f"{verdict.get('critique', '')}")
            fix = await workflow.execute_activity(
                run_capability,
                {"request": request, "name": cap["name"], "attempt": 1, "critique": fix_critique,
                 "cwd": ws["path"], "wid": f"{run_id}-fix{rnd}", "repo": repo,
                 "local_disabled": True, "local_disabled_reason": _FIX_NO_LADDER,
                 "approved_plan": self._plan, "model_override": self._model_override,
                 "effort": self._effort},
                start_to_close_timeout=_EXEC_CEILING, heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY_EXEC)
            self._account(fix)
            await workflow.execute_activity(
                finalize_workspace,
                {"run_id": run_id, "title": request[:120], "head": ws["head"],
                 "existing_pr": True, "branch": ws["branch"]},
                start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY)
            await workflow.execute_activity(
                cleanup_workspace, {"run_id": run_id},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
            await workflow.execute_activity(
                record_attempt,
                {"wid": fix["workflow"], "request": f"[QA-fix r{rnd + 1}] {request}",
                 "name": cap["name"], "result": fix["result"], "cost": fix.get("cost", 0),
                 "attempt": rnd + 1, "tokens": fix.get("tokens"), "model": fix.get("model"),
                 "duration_s": fix.get("duration_s"),
                 "verdict": None, "remember": False, "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
            if fix.get("is_error"):
                # Twin of the review loop's check — an unfinished fix round cannot move a
                # verdict QA already reached, so re-QAing only spends the budget. Stop for
                # a human. Same post-finalize placement, for the same reason.
                return {"state": "inconclusive", "rounds": rnd, "qa_cap": qa_cap_name,
                        "critique": "QA failed and the fix round did not finish — "
                                    f"{self._fix_error(fix)}\n" + verdict.get("critique", "")}
        return {"state": "fail", "rounds": rounds, "qa_cap": qa_cap_name}

    def _qa_summary(self, qa):
        """Human-facing appendix describing the QA outcome, appended to the result."""
        if not qa:
            return ""
        state, cap, rounds = qa.get("state"), qa.get("qa_cap") or "QA", qa.get("rounds", 0)
        fixes = f" after {rounds} fix round{'s' if rounds != 1 else ''}" if rounds else ""
        crit = (qa.get("critique") or "").strip()
        crit = f"\n\n{crit[:800]}" if crit else ""
        if state == "pass":
            return f"\n\n✅ **{cap}: PASS**{fixes} — the PR is empirically validated and safe to merge."
        if state == "inconclusive":
            return (f"\n\n⚠️ **{cap}: INCONCLUSIVE**{fixes} — couldn't be proven either way; "
                    f"PR left draft for a human.{crit}")
        if state == "unavailable":
            return f"\n\n_QA was requested but the QA capability (`{cap}`) isn't registered/enabled._"
        return f"\n\n❌ **{cap}: still FAILING**{fixes} — PR left draft for human review.{crit}"

    async def _run_review_loop(self, request, cap, repo, pr_url):
        """Code-review the opened PR with the review capability; on must/should-fix findings,
        fold them into a fix on the SAME branch and re-review. Returns {state, rounds,
        review_cap, critique?} where state is pass | fail | inconclusive | unavailable. Bounded
        by config.MAX_REVIEW_ROUNDS fix rounds (the reviewer runs at most MAX_REVIEW_ROUNDS + 1
        times). PASS (clean) ends; INCONCLUSIVE or still-FAIL after the budget stops for a human
        (PR stays draft). Pre-authorized — the reviewer is read-only and each fix run reuses the
        already-approved write capability. This is the structural twin of _run_qa_loop."""
        rounds = max(0, self._setting("max_review_rounds"))
        run_id = workflow.info().workflow_id
        review_cap_name = None
        for rnd in range(rounds + 1):       # round 0 = initial review; then up to `rounds` fix+re-review
            self._review = {"state": "review", "round": rnd}
            rev_out = await workflow.execute_activity(
                review_capability,
                {"pr_url": pr_url, "repo": repo, "request": request, "wid": f"{run_id}-rev{rnd}"},
                start_to_close_timeout=timedelta(minutes=30), heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY_EXEC)
            if rev_out.get("missing"):
                self._review = {"state": "unavailable", "round": rnd}
                return {"state": "unavailable", "rounds": rnd,
                        "review_cap": rev_out.get("review_cap")}
            self._account(rev_out)
            review_cap_name = rev_out.get("review_cap")
            verdict = await workflow.execute_activity(
                judge_review, {"request": request, "result": rev_out["result"], "repo": repo},
                start_to_close_timeout=timedelta(seconds=180), retry_policy=_RETRY)
            # Audit the review pass as its own attempt (passed only on an outright clean PASS).
            # `source: "review"` is load-bearing, not a label: this verdict is judge_review's
            # opinion of THE PR, and a review that correctly finds must-fix findings is a
            # SUCCESSFUL review. Recorded sourceless it read as a verify verdict about the
            # reviewer, so 16 of github-pr-review's 33 recorded failures were really "Otto's own
            # PR wasn't clean" — and since a review round raises no needs-you card, no human
            # could ever accept one, so false_fails stayed 0 and the scorecard pointed at the
            # capability instead. `scorecard` counts source=="judge" only.
            await workflow.execute_activity(
                record_attempt,
                {"wid": rev_out["workflow"], "request": f"[review] {request}",
                 "name": review_cap_name, "result": rev_out["result"],
                 "cost": rev_out.get("cost", 0), "attempt": rnd + 1,
                 "tokens": rev_out.get("tokens"), "model": rev_out.get("model"),
                 "duration_s": rev_out.get("duration_s"),
                 "verdict": {"passed": verdict["verdict"] == "pass",
                             "critique": verdict.get("critique", ""), "source": "review"},
                 "remember": False, "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
            self._review = {"state": verdict["verdict"], "round": rnd}
            if verdict["verdict"] == "pass":
                return {"state": "pass", "rounds": rnd, "review_cap": review_cap_name}
            if verdict["verdict"] == "inconclusive":
                return {"state": "inconclusive", "rounds": rnd, "review_cap": review_cap_name,
                        "critique": verdict.get("critique", "")}
            # FAIL (must/should-fix findings) — fix on the same branch unless the budget is spent.
            if rnd == rounds:
                return {"state": "fail", "rounds": rnd, "review_cap": review_cap_name,
                        "critique": verdict.get("critique", "")}
            self._review = {"state": "fixing", "round": rnd}
            ws = await self._fix_workspace(repo, run_id, pr_url)
            if ws is None:
                return {"state": "inconclusive", "rounds": rnd, "review_cap": review_cap_name,
                        "critique": "Review raised findings but the fix round could not run — "
                                    "the branch carrying this PR's work could not be checked "
                                    "out.\n" + verdict.get("critique", "")}
            fix_critique = ("A code review of this PR raised findings. Address them, committing "
                            "the fix to the current branch (do NOT open a new PR). "
                            "The findings are about the CODE: fix those. Do not append "
                            "a section to the PR description recording this round — edit "
                            "the description in place only if what the PR does has "
                            "actually changed.\n"
                            f"{verdict.get('critique', '')}")
            fix = await workflow.execute_activity(
                run_capability,
                {"request": request, "name": cap["name"], "attempt": 1, "critique": fix_critique,
                 "cwd": ws["path"], "wid": f"{run_id}-revfix{rnd}", "repo": repo,
                 "local_disabled": True, "local_disabled_reason": _FIX_NO_LADDER,
                 "approved_plan": self._plan, "model_override": self._model_override,
                 "effort": self._effort},
                start_to_close_timeout=_EXEC_CEILING, heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY_EXEC)
            self._account(fix)
            await workflow.execute_activity(
                finalize_workspace,
                {"run_id": run_id, "title": request[:120], "head": ws["head"],
                 "existing_pr": True, "branch": ws["branch"]},
                start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY)
            await workflow.execute_activity(
                cleanup_workspace, {"run_id": run_id},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
            await workflow.execute_activity(
                record_attempt,
                {"wid": fix["workflow"], "request": f"[review-fix r{rnd + 1}] {request}",
                 "name": cap["name"], "result": fix["result"], "cost": fix.get("cost", 0),
                 "attempt": rnd + 1, "tokens": fix.get("tokens"), "model": fix.get("model"),
                 "duration_s": fix.get("duration_s"),
                 "verdict": None, "remember": False, "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
            if fix.get("is_error"):
                # The fix round errored or timed out, so it did not finish addressing the
                # findings. Re-reviewing runs the same judge over a diff it already failed and
                # spends the rest of the budget reaching the same verdict (run web-2bd1a194: a
                # 944s fix round died at the local model's output wall having committed nothing,
                # and the loop re-reviewed anyway). Stop for a human, mirroring the `ws is None`
                # path. Checked AFTER finalize on purpose: a round that committed and THEN died
                # keeps its commits (`_resume_workspace`'s rule — dead-end, never discard), and
                # finalize is a no-op when it committed nothing. Not counted as a round, so the
                # summary cannot advertise a fix that did not land.
                return {"state": "inconclusive", "rounds": rnd, "review_cap": review_cap_name,
                        "critique": "Review raised findings but the fix round did not finish — "
                                    f"{self._fix_error(fix)}\n" + verdict.get("critique", "")}
        return {"state": "fail", "rounds": rounds, "review_cap": review_cap_name}

    def _review_summary(self, review):
        """Human-facing appendix describing the code-review outcome, appended to the result."""
        if not review:
            return ""
        state = review.get("state")
        cap, rounds = review.get("review_cap") or "review", review.get("rounds", 0)
        fixes = f" after {rounds} fix round{'s' if rounds != 1 else ''}" if rounds else ""
        crit = (review.get("critique") or "").strip()
        crit = f"\n\n{crit[:800]}" if crit else ""
        if state == "pass":
            return f"\n\n✅ **Code review ({cap}): clean**{fixes} — no blocking findings."
        if state == "inconclusive":
            return (f"\n\n⚠️ **Code review ({cap}): INCONCLUSIVE**{fixes} — couldn't assess it; "
                    f"PR left draft for a human.{crit}")
        if state == "unavailable":
            return (f"\n\n_Code review was requested but the review capability (`{cap}`) isn't "
                    "registered/enabled._")
        return (f"\n\n❌ **Code review ({cap}): unaddressed findings**{fixes} — PR left draft for "
                f"human review.{crit}")

    async def _run_swarm(self, params, request, subtasks, unattended, approval):
        """Fan out into one child OttoWorkflow per sub-task, run them CONCURRENTLY, then
        merge their results into a single response. Each child is a normal pinned-capability
        run, so it gates its OWN write independently (surfaced on the Board, approved with the
        same `approve` signal) and audits its own attempts. The bounded child count comes from
        the planner (engine.MAX_SWARM)."""
        self._swarm = True
        parent_id = workflow.info().workflow_id
        self._children = [{"id": f"{parent_id}-s{i + 1}", "cap": s["cap"]["name"],
                           "request": s["request"], "risk": s["cap"]["risk"]}
                          for i, s in enumerate(subtasks)]

        async def _spawn(child, sub):
            return await workflow.execute_child_workflow(
                OttoWorkflow.run,
                {"request": sub["request"], "cap": sub["cap"], "subtask": True,
                 "unattended": unattended, "approval": approval,
                 # Carry the parent chat's composer overrides into every child (memory doesn't
                 # apply to sub-tasks anyway — recall is off for subtask=True — but the model
                 # override should still bind, same model for the whole chat's work).
                 "memory_enabled": params.get("memory_enabled", True),
                 "model_override": params.get("model_override"),
                 "effort": self._effort},
                id=child["id"])

        results = await asyncio.gather(
            *[_spawn(c, s) for c, s in zip(self._children, subtasks)],
            return_exceptions=True)

        parts = []
        swarm_cost = 0
        for child, res in zip(self._children, results):
            if isinstance(res, BaseException):
                result = f"(sub-task failed: {type(res).__name__})"
            elif isinstance(res, dict):
                result = res.get("result")
                swarm_cost += res.get("cost", 0) or 0
            else:
                result = str(res)
            parts.append({"cap": child["cap"], "request": child["request"], "result": result})

        merged = await workflow.execute_activity(
            merge_results, {"request": request, "parts": parts, "audience": self._audience},
            start_to_close_timeout=timedelta(seconds=180), retry_policy=_RETRY)
        result = merged["result"]

        cap = {"name": "swarm", "kind": "swarm", "risk": "read"}
        self._cap = cap
        # Unattended swarms (scheduled/event) have no on-screen audience — deliver + record.
        if params.get("reply_to"):
            await workflow.execute_activity(
                deliver_result, {"reply_to": params["reply_to"], "result": result, "cap": cap},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
        await self._record_chat(params, request, result, None, cap)
        # Same opt-in clean-finish push as the single-cap tail — once for the whole swarm.
        await self._notify(f"Otto finished: swarm ({len(parts)} sub-tasks)",
                           cap=cap, reply_to=params.get("reply_to"), unattended=unattended,
                           detail=request,
                           tags=["white_check_mark"], kind="complete", priority="default",
                           wid=workflow.info().workflow_id)
        return {"result": result, "session_id": None, "cap": cap, "attempts": 1,
                "verified": None, "swarm": parts, "chat_key": self._chat_key,
                "cost": swarm_cost, "times": self._times}

    async def _open_chat(self, params, request):
        """Create this run's Chat sidebar thread up front (request + pending placeholder) so it's
        accessible while the run is in flight. Finalized by _record_chat on completion."""
        if not params.get("chat_key"):
            return
        await workflow.execute_activity(
            open_chat,
            {"chat_key": params["chat_key"], "title": params.get("chat_title"),
             "labels": params.get("chat_labels"), "request": request,
             # The live workflow id, so reopening this chat reattaches to the still-running run
             # (and can answer a clarification / approve a write). Cleared by record_chat at the end.
             "run_id": workflow.info().workflow_id},
            start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)

    async def _record_chat(self, params, request, result, session_id, cap):
        """Finalize this run's Chat sidebar thread, if the trigger asked for one."""
        if not params.get("chat_key"):
            return
        # A run that chose silence still belongs in the sidebar — that's where the owner audits what
        # Otto did on their behalf — but as the decision, not as the raw sentinel. `is_no_reply` is
        # pure string work, so it's replay-safe here (unlike config.setting(), see CLAUDE.md).
        if config.is_no_reply(result):
            result = "_(nothing to reply — Otto stayed silent)_"
        else:
            # Same declared lead line as the returned result (see OttoWorkflow.run). The chat is
            # written before that return, so it needs its own call — and a turn that chose
            # silence must not get a heading bolted onto the silence.
            result = contracts.lead_with(result, params.get("report_prefix"))
        await workflow.execute_activity(
            record_chat,
            {"chat_key": params["chat_key"], "title": params.get("chat_title"),
             "labels": params.get("chat_labels"), "request": request,
             "result": result, "session_id": session_id, "cap": cap,
             # The repo-mode git identity, WITHOUT which this thread is not continuable: a
             # follow-up re-provisions the torn-down clone at `data/workspaces/<git_run_id>`, and
             # `claude -p --resume` only finds the session under the cwd that created it. A
             # browser-driven chat records these client-side; a workflow-opened chat (`chat_key` —
             # a needs-you retry, any unattended run) has no browser, so omitting them here left
             # the follow-up resuming from Otto's own directory and returning `(no output)`.
             # `git_run_id` falls back to THIS workflow (a fresh repo-mode run provisioned its
             # clone under its own id — same value the fresh return path reports); a resume keeps
             # the original it was handed. Gated on `self._repo` so a non-repo run stores neither.
             "repo": self._repo,
             "git_run_id": ((self._git_run_id or workflow.info().workflow_id)
                            if self._repo else None)},
            start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)

    @workflow.signal
    async def provide_clarification(self, answer: str):
        self._clarification = answer

    @workflow.signal
    async def approve(self, decision: bool):
        self._decision = decision

    @workflow.signal
    async def revise_plan(self, feedback: str):
        self._plan_feedback = feedback

    @workflow.query
    def status(self) -> dict:
        return {
            "cap": self._cap,
            "question": self._question,
            "awaiting_clarification": self._awaiting_clarification,
            "awaiting_approval": self._awaiting_approval,
            "risk_reason": self._risk_reason,
            "discussion": self._discussion,
            "times": self._times,
            "attempt": self._attempt,
            "verified": self._verified,
            "swarm": self._swarm,
            "children": self._children,
            "repo": self._repo,
            "plan": self._plan,
            "plan_concerns": self._plan_concerns,
            "plan_model": self._plan_model,
            "plan_revisions": self._plan_revisions,
            "replanning": self._replanning,
            "max_plan_revisions": max(0, self._setting("max_plan_revisions")),
            "qa": self._qa,
            "review": self._review,
            "chat_key": self._chat_key,
            "needs_human": self._needs_human,
            "terminal": self._terminal,
            "spent": self._spent,
        }


@workflow.defn
class BoardPollWorkflow:
    """Tiny workflow fired by the `board-poll` Temporal Schedule: it just runs the `poll_board`
    activity, which lists the Ready column and starts one OttoWorkflow per ticket. Kept
    separate from OttoWorkflow because a poll fans out N runs rather than handling one request
    (all the IO + workflow-starting lives in the activity, so this stays deterministic)."""

    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            poll_board, {}, start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=_HEARTBEAT, retry_policy=_RETRY)


@workflow.defn
class SlackPollWorkflow:
    """Fired by the `slack-poll` Temporal Schedule: runs the `poll_slack` activity, which detects
    new DMs / @-mentions from allowlisted people and starts one OttoWorkflow per message. Kept
    separate from OttoWorkflow because a poll fans out N runs (all IO in the activity, so this
    stays deterministic) — the Slack sibling of BoardPollWorkflow."""

    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            poll_slack, {}, start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=_HEARTBEAT, retry_policy=_RETRY)


@workflow.defn
class PrReviewPollWorkflow:
    """Fired by the `pr-review-poll` Temporal Schedule: runs the `poll_pr_reviews` activity, which
    finds pull requests waiting on the operator's review and starts one read-only review run per
    new request. The PR-review sibling of BoardPollWorkflow — same shape, different GitHub signal
    (a pending review request rather than a card parked in Ready)."""

    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            poll_pr_reviews, {}, start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=_HEARTBEAT, retry_policy=_RETRY)


@workflow.defn
class ReaperWorkflow:
    """Fired by the `reaper` Temporal Schedule: runs the `reap_stuck` activity, which finds board
    cards stuck In Progress whose workflow has died / hung and moves them to Blocked. This is the
    backstop the in-workflow finalizer can't provide (a dead worker can't run its own except
    block). Kept separate from BoardPollWorkflow — a different cadence and concern."""

    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            reap_stuck, {}, start_to_close_timeout=timedelta(minutes=5), retry_policy=_RETRY)
