"""Temporal call options, terminal copy and failure decoding shared by `workflows.OttoWorkflow`
and the mixins it is composed from (`wf_postpr`, `wf_repo`, `wf_swarm`).

These were `workflows.py` module constants until the class was split into mixins. They live here
because a mixin cannot import `workflows` (that is the circular import the split exists to avoid)
and because a second copy of a ceiling is exactly the drift `ExecutionHeartbeatTests` guards
against — `config.EXEC_TIMEOUT_S` has to stay under `_EXEC_CEILING` with headroom, and a duplicate
removes it silently.

PURE and import-time only — no I/O, no clock, no env read. Activity options are REPLAYED from
history, so anything derived from the environment would make a worker with a different env replay
differently; every value below is a literal for that reason. Imported under
`workflow.unsafe.imports_passed_through()`.
"""
from datetime import timedelta

from temporalio import exceptions
from temporalio.common import RetryPolicy

import config



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

# The JUDGE ceiling. A judge activity is not one model call: `judging.confirm_adverse` re-samples
# an adverse verdict `judge_confirmations` (3) times, each bounded by LOCAL_TIMEOUT_S (60) +
# CLAUDE_TIER_TIMEOUT_S (120), and `verify` additionally derives the repo-conventions digest on a
# cache miss. The old 180s ceiling was one call's worth, so a judge that actually re-sampled was
# killed by Temporal after the attempt had already executed — `_RETRY` then re-ran the whole
# chain 3x (spend) and finally filed the run `workflow_error`. A literal, like _EXEC_CEILING:
# activity options are replayed, so deriving one from the environment makes a worker with a
# different env replay differently. Kept in step with the three settings by
# `ExecutionHeartbeatTests` (issue #35).
_JUDGE_CEILING = timedelta(minutes=10)

# The PLAN-PREVIEW ceiling. `plans.plan_preview` runs a 900s agentic pass; a LOCAL preview that
# walls late re-previews on Claude for another 900s, and `critique_plan` adds a tier call after
# that. 17 minutes covered one pass only (issue #34).
_PLAN_CEILING = timedelta(minutes=35)

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
    "swarm_child_failed": "⚠️ **Needs human review** — one or more sub-tasks of this swarm died "
                          "in the harness, so the merged answer below was synthesized from a "
                          "hole. The failed sub-task(s) are named in the record.",
    "delivery_failed": "⚠️ **Needs human review** — the result was produced but could not be "
                       "delivered to its reply target. It is recorded here; nobody downstream "
                       "has seen it.",
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
