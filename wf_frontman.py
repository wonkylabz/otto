"""Slack frontman delegation: the assistant answers, hands task-shaped work to a routed child run,
and relays what happened.

The frontman (`slack.frontman`, the assistant by default) decides delegation itself by ending its
reply with a `DELEGATE:` line (`contracts.parse_delegation`). The child is a normal routed, GATED
`OttoWorkflow` with NO reply_to — the parent relays its outcome, so the thread hears one voice.
Its gate notice still reaches the thread (`notice_to`), which is what lets a Slack "yes" signal it.

Every command here sits behind the recorded `frontman` input AND `workflow.patched`, so a run in
flight before this existed replays unchanged. Mixed into `OttoWorkflow`, imported under
`workflow.unsafe.imports_passed_through()`.
"""
from temporalio import workflow

from wf_runtime import _EXEC_CEILING, _HEARTBEAT, _RETRY_EXEC, _failure_detail

with workflow.unsafe.imports_passed_through():
    import contracts
    from activities import run_capability


def _outcome(res):
    """How a delegated child ended, in `contracts._RELAY_OUTCOMES` vocabulary. PURE."""
    if isinstance(res, BaseException) or not isinstance(res, dict):
        return "failed"
    if res.get("outcome"):
        return res["outcome"]
    nh = res.get("needs_human") or {}
    if nh:
        return "gate_timeout" if nh.get("reason") == "gate_timeout" else "needs_human"
    return "done"


class FrontmanMixin:
    """Delegate a frontman's hand-off to a child run, then relay its outcome."""

    async def _frontman_turn(self, params, out, attempt):
        """`out` unchanged unless this is a frontman reply ending in a delegation; else the relay
        turn's `out`, whose result is the reply to post. Never raises past the relay: every child
        ending, including its failure, is told to the person."""
        if not params.get("frontman"):
            return out
        reply, task = contracts.parse_delegation(out.get("result"))
        if not task or not workflow.patched("frontman-delegate"):
            return out
        wid = workflow.info().workflow_id
        child_id = f"{wid}-d1"
        self._children = [{"id": child_id, "cap": "(routed)", "request": task, "risk": None}]
        try:
            res = await workflow.execute_child_workflow(
                type(self).run,
                {"request": task, "unattended": True, "delegated": True,
                 "approval": params.get("approval", "ask"),
                 # The gate notice is the ONE thing the child posts: it arms the conversation so
                 # a Slack "yes" reaches this child's gate. Everything else the parent relays.
                 "notice_to": params.get("reply_to"),
                 "memory_enabled": params.get("memory_enabled", True),
                 "model_override": params.get("model_override"),
                 "effort": self._effort, "attachments": self._attachments},
                id=child_id)
        except Exception as e:  # noqa: BLE001 - a dead child is an outcome to relay, not a crash
            res = e
        outcome = _outcome(res)
        report = (res.get("result") if isinstance(res, dict)
                  else f"(failed: {_failure_detail(res)})")
        relay = contracts.relay_request(task, outcome, report)
        if reply:
            relay = f"(What you had drafted before handing off: {reply})\n\n" + relay
        cap = self._cap
        try:
            rout = await workflow.execute_activity(
                run_capability,
                {"request": relay, "name": cap["name"], "resume": out.get("session_id"),
                 "wid": wid, "audience": self._audience, "risk": cap["risk"],
                 "effort": self._effort, "approved_plan": self._plan},
                start_to_close_timeout=_EXEC_CEILING, heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY_EXEC)
        except Exception as e:  # noqa: BLE001 - the relay is a courtesy over a settled outcome
            workflow.logger.warning(f"frontman relay failed ({_failure_detail(e)})")
            rout = {"result": "", "session_id": out.get("session_id"), "cost": 0}
        text, _ = contracts.parse_delegation(rout.get("result"))
        if not text or text == "(no output)" or rout.get("is_error"):
            text = contracts.relay_fallback(outcome)
        await self._audit_attempt(
            {"wid": rout.get("workflow") or wid, "request": relay, "name": cap["name"],
             "result": text, "cost": rout.get("cost", 0), "attempt": attempt + 1,
             "tokens": rout.get("tokens"), "model": rout.get("model"),
             "verdict": None, "repo": None}, learn=False)
        return {**rout, "result": text,
                "session_id": rout.get("session_id") or out.get("session_id"),
                "delegated": {"id": child_id, "task": task, "outcome": outcome}}
