"""Swarm fan-out: one child `OttoWorkflow` per sub-task, run concurrently and merged.

Split out of `workflows.OttoWorkflow` verbatim (issue #58); the activity order is part of the
replay history. The child workflow type is reached as `type(self).run` rather than by importing
`OttoWorkflow` — the parent IS one, and importing the class here would be the circular import the
mixin split exists to avoid. Temporal records the same workflow type name either way, so an
in-flight swarm replays unchanged.

Mixed into `OttoWorkflow`, and imported under `workflow.unsafe.imports_passed_through()`.
"""
import asyncio
from datetime import timedelta

from temporalio import workflow

from wf_runtime import _NEEDS_HUMAN_BANNER, _RETRY, _failure_detail

with workflow.unsafe.imports_passed_through():
    from activities import deliver_result, finalize_terminal, merge_results


class SwarmMixin:
    """Fan a request out into parallel child runs and merge what they come back with."""

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
                # `type(self).run` IS `OttoWorkflow.run` — reached through the instance because
                # a mixin cannot import the class it is mixed into. Same workflow type name in
                # history, so a swarm in flight across this refactor replays unchanged.
                type(self).run,
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
        failed_children = []
        for child, res in zip(self._children, results):
            if isinstance(res, BaseException):
                # `type(res).__name__` is "ChildWorkflowError" — Temporal's wrapper, never the
                # cause. That is the exact placeholder `_failure_detail` exists to unwrap, and
                # it was the whole record the merge (and the reader) got of a dead sub-task.
                result = f"(sub-task failed: {_failure_detail(res)})"
                failed_children.append(child["id"])
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
        if failed_children:
            # A swarm that lost a sub-task is not a clean finish: the merge is synthesized from
            # a hole. Without this the parent finished Done with needs_human=None while the
            # child's own needs-human row sat on the Needs-you dashboard.
            self._needs_human = {"reason": "swarm_child_failed",
                                 "detail": ", ".join(failed_children)}
            self._terminal = dict(self._needs_human)
            # Every terminal state writes its OWN audit row, or the run vanishes from
            # /api/needs-you when Temporal visibility ages out.
            await workflow.execute_activity(
                finalize_terminal,
                {"wid": workflow.info().workflow_id, "request": request, "cap": cap,
                 "reason": "swarm_child_failed", "detail": self._needs_human["detail"],
                 "reply_to": params.get("reply_to"), "repo": params.get("repo"),
                 "unattended": unattended},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
        record = result
        if self._needs_human:
            record = _NEEDS_HUMAN_BANNER["swarm_child_failed"] + "\n\n" + result
        # Unattended swarms (scheduled/event) have no on-screen audience — deliver + record.
        if params.get("reply_to"):
            await workflow.execute_activity(
                deliver_result, {"reply_to": params["reply_to"], "result": result, "cap": cap},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=_RETRY)
        await self._record_chat(params, request, record, None, cap)
        # Same opt-in clean-finish push as the single-cap tail — once for the whole swarm, and
        # only when it actually finished clean: the finalizer above already pushed otherwise.
        if not self._needs_human:
            await self._notify(f"Otto finished: swarm ({len(parts)} sub-tasks)",
                               cap=cap, reply_to=params.get("reply_to"), unattended=unattended,
                               detail=request,
                               tags=["white_check_mark"], kind="complete", priority="default",
                               wid=workflow.info().workflow_id)
        return {"result": record, "session_id": None, "cap": cap, "attempts": 1,
                "verified": None, "swarm": parts, "chat_key": self._chat_key,
                "needs_human": self._terminal,
                "cost": swarm_cost, "times": self._times}
