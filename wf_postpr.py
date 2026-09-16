"""The post-PR loops — code review and QA — as ONE parameterised fix loop.

`_run_review_loop` and `_run_qa_loop` were ~110 lines each and identical modulo the activity
names, the audit labels, the wid suffix and four sentences of English (issue #58). Every
invariant in `repo-work.md` that binds a post-PR round — the verdict's `source`, an errored
round ending the loop, no LOCAL backend under a one-shot round, checking out the PR's OWN head
branch — therefore had to be fixed twice, and a fix applied to one copy read as applied to both.

`_LOOPS` holds everything the two rounds do NOT share; `_run_fix_loop` holds everything they do.
The activity SEQUENCE is unchanged in both directions, which is what keeps an in-flight history
replaying identically across this refactor.

Mixed into `workflows.OttoWorkflow`, which is where `self._setting`, `self._account`, `self._plan`
and the composer bindings live. Imported under `workflow.unsafe.imports_passed_through()`: this is
deterministic workflow code that must not be re-imported into the sandbox behind the class that
inherits it.
"""
from datetime import timedelta

from temporalio import exceptions, workflow

from wf_runtime import (_EXEC_CEILING, _FIX_NO_LADDER, _HEARTBEAT, _JUDGE_CEILING, _RETRY,
                        _RETRY_EXEC)

with workflow.unsafe.imports_passed_through():
    from activities import (cleanup_workspace, finalize_workspace, judge_qa, judge_review,
                            pr_head_branch, provision_workspace, qa_capability,
                            record_attempt, review_capability, run_capability)


# What the fix round is TOLD, minus the sentence naming which judge raised the findings. Shared
# on purpose: it is the operative instruction, and the half that has to be right. The critique it
# is concatenated with is model-written and has been observed asking for exactly what the last two
# sentences forbid — "update the PR description (append a section, don't wipe it)" — so the
# immediate instruction has to contradict that, in both loops, in one place
# (`test_repo.PrBodyContractTests`).
_FIX_ORDER = ("committing the fix to the current branch (do NOT open a new PR). "
              "The findings are about the CODE: fix those. Do not append "
              "a section to the PR description recording this round — edit "
              "the description in place only if what the PR does has "
              "actually changed.\n")

# Everything the two loops do NOT share. `cap_field` is BOTH the key the capability activity
# returns its resolved cap name under and the key this loop returns it under, so the two summary
# renderers keep reading exactly what they read before.
#
# `source` is load-bearing, not a label: this verdict is judge_review/judge_qa's opinion of THE
# PR, and a review that correctly finds must-fix findings is a SUCCESSFUL review. Recorded
# sourceless it read as a verify verdict about the reviewer, so 16 of github-pr-review's 33
# recorded failures were really "Otto's own PR wasn't clean" — and since a post-PR round raises no
# needs-you card, no human could ever accept one, so false_fails stayed 0 and the scorecard
# pointed at the capability instead. `scorecard` counts source=="judge" only.
_LOOPS = {
    "review": {
        "attr": "_review",
        "rounds_setting": "max_review_rounds",
        "capability": review_capability,
        "judge": judge_review,
        "cap_field": "review_cap",
        "source": "review",
        "wid": "rev",
        "fix_wid": "revfix",
        "label": "review",
        "fix_label": "review-fix",
        "fix_lead": "A code review of this PR raised findings. Address them, ",
        "blocked_lead": "Review raised findings but",
        "unfinished_lead": "Review raised findings but",
    },
    "qa": {
        "attr": "_qa",
        "rounds_setting": "max_qa_rounds",
        "capability": qa_capability,
        "judge": judge_qa,
        "cap_field": "qa_cap",
        "source": "qa",
        "wid": "qa",
        "fix_wid": "fix",
        "label": "QA",
        "fix_label": "QA-fix",
        "fix_lead": "The QA validation of this PR did not pass. Address these findings, ",
        "blocked_lead": "QA failed but",
        "unfinished_lead": "QA failed and",
    },
}


class PostPrMixin:
    """The review and QA loops that run AFTER a repo-mode draft PR is opened."""

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

    async def _run_fix_loop(self, kind, request, cap, repo, pr_url):
        """Judge the opened PR with `kind`'s capability; on an adverse verdict, fold its findings
        into a fix on the SAME branch and re-judge. Returns {state, rounds, <cap_field>,
        critique?} where state is pass | fail | inconclusive | unavailable.

        Bounded by the loop's own rounds setting (the judging capability runs at most rounds + 1
        times). PASS ends clean; INCONCLUSIVE or a still-adverse verdict after the budget stops
        for a human (PR stays draft). Both loops are pre-authorized — enabling them IS the grant,
        the reviewer is read-only, and each fix run reuses the already-approved write capability.
        """
        spec = _LOOPS[kind]
        rounds = max(0, self._setting(spec["rounds_setting"]))
        run_id = workflow.info().workflow_id
        cap_field, cap_name = spec["cap_field"], None
        for rnd in range(rounds + 1):    # round 0 = the initial pass; then up to `rounds` fix+redo
            setattr(self, spec["attr"], {"state": spec["source"], "round": rnd})
            out = await workflow.execute_activity(
                spec["capability"],
                {"pr_url": pr_url, "repo": repo, "request": request,
                 "wid": f"{run_id}-{spec['wid']}{rnd}"},
                start_to_close_timeout=_EXEC_CEILING, heartbeat_timeout=_HEARTBEAT,
                retry_policy=_RETRY_EXEC)
            if out.get("missing"):
                setattr(self, spec["attr"], {"state": "unavailable", "round": rnd})
                return {"state": "unavailable", "rounds": rnd, cap_field: out.get(cap_field)}
            self._account(out)
            cap_name = out.get(cap_field)
            verdict = await workflow.execute_activity(
                spec["judge"], {"request": request, "result": out["result"], "repo": repo},
                start_to_close_timeout=_JUDGE_CEILING, retry_policy=_RETRY)
            # Audit the round as its own attempt (passed only on an outright PASS), stamped with
            # the verdict's own `source` — see `_LOOPS` for why that field is load-bearing.
            await workflow.execute_activity(
                record_attempt,
                {"wid": out["workflow"], "request": f"[{spec['label']}] {request}",
                 "name": cap_name, "result": out["result"], "cost": out.get("cost", 0),
                 "attempt": rnd + 1, "tokens": out.get("tokens"), "model": out.get("model"),
                 "duration_s": out.get("duration_s"),
                 "verdict": {"passed": verdict["verdict"] == "pass",
                             "critique": verdict.get("critique", ""), "source": spec["source"]},
                 "remember": False, "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
            setattr(self, spec["attr"], {"state": verdict["verdict"], "round": rnd})
            if verdict["verdict"] == "pass":
                return {"state": "pass", "rounds": rnd, cap_field: cap_name}
            critique = verdict.get("critique", "")
            if verdict["verdict"] == "inconclusive":
                return {"state": "inconclusive", "rounds": rnd, cap_field: cap_name,
                        "critique": critique}
            # FAIL (must/should-fix findings) — fix on the same branch unless the budget is spent.
            if rnd == rounds:
                return {"state": "fail", "rounds": rnd, cap_field: cap_name,
                        "critique": critique}
            setattr(self, spec["attr"], {"state": "fixing", "round": rnd})
            ws = await self._fix_workspace(repo, run_id, pr_url)
            if ws is None:
                return {"state": "inconclusive", "rounds": rnd, cap_field: cap_name,
                        "critique": f"{spec['blocked_lead']} the fix round could not run — the "
                                    "branch carrying this PR's work could not be checked out.\n"
                                    + critique}
            fix = await workflow.execute_activity(
                run_capability,
                {"request": request, "name": cap["name"], "attempt": 1,
                 "critique": spec["fix_lead"] + _FIX_ORDER + critique,
                 "cwd": ws["path"], "wid": f"{run_id}-{spec['fix_wid']}{rnd}", "repo": repo,
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
                {"wid": fix["workflow"], "request": f"[{spec['fix_label']} r{rnd + 1}] {request}",
                 "name": cap["name"], "result": fix["result"], "cost": fix.get("cost", 0),
                 "attempt": rnd + 1, "tokens": fix.get("tokens"), "model": fix.get("model"),
                 "duration_s": fix.get("duration_s"),
                 "verdict": None, "remember": False, "repo": repo},
                start_to_close_timeout=timedelta(seconds=120), retry_policy=_RETRY)
            if fix.get("is_error"):
                # The fix round errored or timed out, so it did not finish addressing the
                # findings. Re-judging runs the same judge over a diff it already failed and
                # spends the rest of the budget reaching the same verdict (run web-2bd1a194: a
                # 944s fix round died at the local model's output wall having committed nothing,
                # and the loop re-reviewed anyway). Stop for a human, mirroring the `ws is None`
                # path. Checked AFTER finalize on purpose: a round that committed and THEN died
                # keeps its commits (`_resume_workspace`'s rule — dead-end, never discard), and
                # finalize is a no-op when it committed nothing. Not counted as a round, so the
                # summary cannot advertise a fix that did not land.
                return {"state": "inconclusive", "rounds": rnd, cap_field: cap_name,
                        "critique": f"{spec['unfinished_lead']} the fix round did not finish — "
                                    f"{self._fix_error(fix)}\n" + critique}
        return {"state": "fail", "rounds": rounds, cap_field: cap_name}

    async def _run_review_loop(self, request, cap, repo, pr_url):
        """Code-review the opened PR, fixing must/should-fix findings on the same branch.
        Runs BEFORE QA so review findings are fixed before empirical validation."""
        return await self._run_fix_loop("review", request, cap, repo, pr_url)

    async def _run_qa_loop(self, request, cap, repo, pr_url):
        """Empirically validate the opened PR, fixing QA failures on the same branch."""
        return await self._run_fix_loop("qa", request, cap, repo, pr_url)

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
