"""Repo-mode's two workspace seams: re-provisioning a chat's clone for a follow-up, and the
tail that pushes the branch and opens (or updates) the draft PR.

Split out of `workflows.OttoWorkflow` verbatim (issue #58) — the activity order each one issues
is part of the replay history, so neither may be reordered to read more nicely. The post-PR fix
loops that run after `_finalize_pr` live in `wf_postpr`.

Mixed into `OttoWorkflow`, and imported under `workflow.unsafe.imports_passed_through()`: this is
deterministic workflow code that must not be re-imported into the sandbox behind the class that
inherits it.
"""
from datetime import timedelta

from temporalio import exceptions, workflow

from wf_runtime import _HEARTBEAT, _RETRY

with workflow.unsafe.imports_passed_through():
    from activities import (check_grounding, cleanup_workspace, finalize_workspace,
                            provision_workspace, recover_pr_branch, resolve_pr_target)


class RepoFlowMixin:
    """Provisioning a resumed repo chat's clone, and the repo-mode PR tail."""

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

    async def _finalize_pr(self, ws, request, result, cap, repo, is_error=False):
        """Repo-mode tail: push the branch, open (or update) the draft PR, tear the clone
        down, and fold the outcome into the report. Returns `(pr, result)` — extracted from
        `_run_impl` verbatim; the activity order it issues is part of the replay history."""
        self._enter("PR")
        pr = await workflow.execute_activity(
            finalize_workspace,
            {"run_id": workflow.info().workflow_id, "title": request[:120],
             "head": ws["head"], "summary": (result or "")[:1500],
             # The last rung's attempt ERRORED, so `result` is a report about the run (a turn
             # budget, a timeout, a stderr tail), not about the diff. The PR still opens — the
             # branch can hold real commits from an earlier rung — but its copy is drafted from
             # the request alone. Observed: bobo#39 shipped titled for Otto's own turn budget.
             "summary_is_error": bool(is_error),
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
