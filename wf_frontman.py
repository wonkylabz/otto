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
    import config
    import contracts
    from activities import run_capability


def _outcome(res):
    """How a delegated child ended, in `contracts._RELAY_OUTCOMES` vocabulary. PURE."""
    if isinstance(res, BaseException) or not isinstance(res, dict):
        return "failed"
    if res.get("outcome"):
        return res["outcome"]
    if res.get("paused"):
        return "paused"
    nh = res.get("needs_human") or {}
    if nh:
        return "gate_timeout" if nh.get("reason") == "gate_timeout" else "needs_human"
    return "done"


class FrontmanMixin:
    """Delegate a frontman's hand-off to a child run, then relay its outcome."""

    def _parse_delegation(self, text):
        """`contracts.parse_delegation`, logging an oversized hand-off: the person only sees
        "say it in full", which is wrong advice for a task that was already complete."""
        n = contracts.delegation_overflow(text)
        if n:
            workflow.logger.warning(f"delegation dropped: task is {n} chars, cap {contracts.DELEGATE_MAX}")
        return contracts.parse_delegation(text)

    def _handoff_task(self, text):
        """The task a `HANDOFF_ASK` turn wrote; multi-line only for runs recorded with the patch."""
        if not workflow.patched("handoff-multiline"):
            return self._parse_delegation(text)[1]
        task = contracts.parse_handoff(text)
        body = contracts.handoff_body(text)
        if task is None and body and len(body) > contracts.DELEGATE_MAX:
            workflow.logger.warning(f"delegation dropped: task is {len(body)} chars, cap {contracts.DELEGATE_MAX}")
        return task

    async def _frontman_turn(self, params, out, attempt):
        """`out` unchanged unless this is a frontman reply ending in a delegation; else the relay
        turn's `out`, whose result is the reply to post. Never raises past the relay: every child
        ending, including its failure, is told to the person."""
        if not params.get("frontman"):
            return out
        reply, task = self._parse_delegation(out.get("result"))
        # A fronted web turn whose write-intent guard said WRITE never ran: the request IS the task.
        # A handoff turn wrote nothing but the task, so there is no draft to relay.
        if out.get("handoff"):
            reply, task = "", out["handoff"]
        if not task or not workflow.patched("frontman-delegate"):
            return out
        wid = workflow.info().workflow_id
        child_id = f"{wid}-d1"
        # `delegated` is what the chat reads to surface this child's gate on the parent
        # (server._delegate_gate) — the id shape is this module's business, not the server's.
        self._children = [{"id": child_id, "cap": "(routed)", "request": task, "risk": None,
                           "delegated": True}]
        try:
            res = await workflow.execute_child_workflow(
                type(self).run,
                # Framing AFTER the task, so routing still ranks the task's own words.
                {"request": task + (contracts.DELEGATED_FRAMING if params.get("reply_to")
                                    else contracts.DELEGATED_FRAMING_WEB), "unattended": True,
                 "delegated": True,
                 # A web parent's reader is watching the chat: no "Otto finished" push on top.
                 "quiet_done": not params.get("reply_to"),
                 # The fast lane follows the ASKER, decided by the Slack ingress (issue #193), and is
                 # judged on the asker's OWN words: this task text is the model's, written after
                 # reading whatever it read, so an injected DELEGATE never picks its own lane.
                 "trusted_asker": bool(params.get("trusted_asker")) and not out.get("force_gate"),
                 "asker_text": params.get("asker_text"),
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
        # Settled: the chat stops looking this child up on every poll (server._wf_state).
        self._children[0]["done"] = True
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
        if (not text or text == "(no output)" or rout.get("is_error")
                or config.is_no_reply(text)):
            text = contracts.relay_fallback(outcome)
        await self._audit_attempt(
            {"wid": rout.get("workflow") or wid, "request": relay, "name": cap["name"],
             "result": text, "cost": rout.get("cost", 0), "attempt": attempt + 1,
             "tokens": rout.get("tokens"), "model": rout.get("model"),
             "verdict": None, "repo": None}, learn=False)
        return {**rout, "result": text,
                "session_id": rout.get("session_id") or out.get("session_id"),
                "delegated": {"id": child_id, "task": task, "outcome": outcome}}
