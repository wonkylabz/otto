"""Starting an OttoWorkflow from a polled ingress — the one copy.

`board.py`, `slack.py` and `pr_review.py` each carried their own `start_run`, ~25 lines that were
identical but for the estop key, the trace tag and how the outcome was spelled. Every one of them
had to re-remember the same four things: that a missing Temporal is a soft failure, that the pause
is consulted BEFORE a workflow exists, that the id is deterministic so REJECT_DUPLICATE makes a
re-poll idempotent, and that "already started" is the expected case rather than an error.

That is a checklist, and a checklist copied three times is a checklist that will be copied a
fourth time with one item missing. `EstopCoverageTests` already makes the same argument about
`server._wf_start`: the check belongs at the choke point, so a new route inherits it by
construction.

NOT here, deliberately: `server._wf_start` and `scheduler._run_now`. Both are coroutines already
running on Temporal's loop, and this module's `tc.run` is the sync wrapper that must never be
called from one — it deadlocks the loop and every later Temporal call in the process
(`test_integration.NeedsYouLoopSafetyTests`). They also carry their own distinct semantics: a
raised `Paused` for the web's 409, and `_in_flight` for "run now"'s no-stacking rule.
"""
from ui import trace

# A polled ingress has three outcomes, and conflating the middle one is a real bug: a cursor may
# advance past a DUPLICATE (that message is being handled) but never past a FAILURE (the pause is
# on, or Temporal is down — the message must still be there when it lifts).
STARTED = "started"
DUPLICATE = "duplicate"
FAILED = "failed"


def start_run(wid, params, *, estop_key, trace_tag):
    """Start an unattended OttoWorkflow. Returns STARTED | DUPLICATE | FAILED. Never raises.

    `wid` must be DETERMINISTIC for the thing being handled — the issue, the message, the PR
    round. That plus REJECT_DUPLICATE is what makes a re-poll that raced the state write a no-op
    instead of a second run."""
    import estop
    import temporal_client as tc
    if not tc.OK:
        return FAILED
    # Last gate before a workflow exists. The polling activity refuses earlier — before a card
    # moves or a cursor advances — and this covers every OTHER caller of the same function.
    if estop.blocked(estop_key):
        return FAILED
    from temporalio.common import WorkflowIDReusePolicy

    async def _go():
        from workflows import OttoWorkflow
        c = await tc.client()
        await c.start_workflow(OttoWorkflow.run, params, id=wid, task_queue=tc.TASK_QUEUE,
                               id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        return STARTED

    try:
        return tc.run(_go())
    except Exception as e:  # noqa: BLE001 - already-started is the common, expected case
        if "already" in str(e).lower():
            return DUPLICATE
        trace(trace_tag, f"start_run {wid} failed: {str(e)[:140]}")
        return FAILED
