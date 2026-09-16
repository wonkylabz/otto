# Repo work — workspaces, PRs, review/QA, terminal state

## Isolated repo workspaces

`workspace.py` — modify code in an isolated shallow clone, never the live checkout. Fresh writes with a matching `repo_hint` auto-promote to repo mode. Pushes a branch, opens a draft PR, tears down after.

- **A cap's own PR wins even when Otto's branch also has work** (`_agent_pr`, resolved up front) — a cap driving its own git and leaving the tree dirty otherwise makes two PRs. Otto's branch is still pushed.
- **The clone's own BASE branch is never a cap PR** (`_agent_pr`, `otto.baseBranch`) — a repo whose default branch has an open PR made every run report that stranger's PR and open none of its own (`…default_branch_is_not_the_capabilitys`).
- A cap opening its PR on **Otto's own branch** takes the other path — `gh pr create` fails `already exists`; recover via `_existing_pr_url`/stderr, never dropping `pr_url` to None (which skipped review).
- **The approved plan reaches the PR as a comment, never a committed file** (`workspace.post_plan`) — the reviewer otherwise sees the diff, never what was approved, but that record must not outlive its review. One per PR (`otto-plan`).
- **A resume needs the workspace for its path, not its branch** (`OttoWorkflow._resume_workspace`) — `--resume` looks history up under the creating cwd. Tiers: chat's branch → the PR's branch → a fresh default-branch clone at the same path (no PR ever opened) → not continuable.
- A resume provisions BEFORE the gate and the deny path tears the clone down; a merged branch must dead-end rather than discard commits, so tier 3 is barred once a PR exists.
- **The whole ladder is dead code if the chat never recorded `repo`/`git_run_id`** on the chat row (`chats.finish_run`) — a follow-up resumes with `cwd=None` and returns `(no output)`. `/api/continue` falls back to `chats.git_identity`, which is the authority, not the client.
- `existing_pr=True` resolves the real PR URL to resume against, and skips swarm.
- **A request naming the operator's OWN open PR branches off THAT PR's head** (`workspace.pr_target` → `provision(from_branch=)`, finalize `existing_pr=True`) — a default-branch clone lacks the code, so the run edits the wrong revision into a second PR (`PrTargetTests`).
- **A colleague's PR is never a target** — naming one is weak evidence of intent ("like the one in #480"), and pushing commits into their review is worse than the wrong-base PR this prevents. Fails closed on an unknown viewer.
- **The provisioned tree is checked against the request up front** (`workspace.grounding`) — an absent named file, or a line number it is far too short to have, is the cheapest evidence of a wrong-branch run. Advisory: steers executor and judge, never blocks (`GroundingTests`).
- **Reporting a wrong-branch mismatch is a COMPLETE answer** — "the platform owns git" leaves such a run no legal move, so it deadlocks rather than fails. Say so and stop; never switch branch or substitute the nearest file (`WorkerBranchEscapeHatchTests`).
- **A resume re-points to the PR its message names, only if the restored tree FAILS grounding** (`_resume_workspace` repair) — the branch else comes from chat state, so "fix #106's findings" worked a tree lacking its code. Same path, session survives (`ResumeGroundingTests`).
- **`candidate_repo`'s token boundary includes `_`** — else `platform_stop_weights_agent` reads as the repo `platform`, so a CI request named two repos, went ambiguous and auto-engage silently declined: no clone, nowhere to write (`RepoNameBoundaryTests`).
- **A repo is registered by its URL; a checkout is OPTIONAL** (`registry.project_path`) — URL-only resolves to a shallow clone at `data/repos/<slug>`; a registered checkout wins. `managed_path` stays PURE — `file_safety` calls `projects()` per run (`RepoUrlRegistrationTests`).
- **Only a MANAGED clone may be hard-reset** (`repos.is_managed`) — it exists for its working TREE (`.claude/`, CLAUDE.md), so fetching refs alone serves day-one conventions forever; the same reset on the user's checkout destroys uncommitted work (`RepoUrlRegistrationTests`).
- **Registered checkouts refresh via `git fetch`, never `git pull`** (`workspace.refresh_repos`, `OTTO_REPO_FETCH_AGE_S`=900) — the checkout is the user's own workspace, routinely dirty; `fetch` only touches refs.
- **Repo-mode's PR base needs the same refresh** — `workspace.provision` clones `--depth 1` from the local path, so the base is the *local* default-branch ref, often badly behind. `_refresh_base` hard-resets onto the real remote default first (skipped for `from_branch=True`).
- **Tests must never touch live state** — both `setUpModule`s call `redirect_live_state()`: ONE temp dir stands in for `data/`, `_DATA_STORES` re-derives every store. A hand-kept list leaked twice (`LiveStoreIsolationTests`).

## Post-PR loops

**Code review** — **default-on for every repo-mode PR** (`params.get("review", True)`): a PR is a PR whoever wrote it. Runs *before* QA; a fail skips QA. Bounded `MAX_REVIEW_ROUNDS=3`. Cap `code-reviewer` (`OTTO_REVIEW_CAP`).

**QA** (opt-in, repo-mode only) — judges pass/fail/inconclusive; FAIL re-provisions, re-runs, re-pushes, re-QAs. Bounded `MAX_QA_ROUNDS=2`. Opt-in = pre-authorized. Cap `qa-tester` (`OTTO_QA_CAP`).

- **A post-PR round stamps `verdict_source` — its verdict is about the PR, not the capability** — a round correctly raising must-fix findings booked the reviewer as failing, and no round raises a needs-you card a human could accept (`ScorecardTests`).
- **An errored fix round ENDS the loop** (`is_error` → inconclusive, not counted) — it commits nothing, so re-reviewing re-runs the same judge over the same diff to the same verdict, spending the whole budget (`web-2bd1a194`).
- **A fix round never runs on the LOCAL backend** (`_FIX_NO_LADDER` → `local_disabled`) — both loops are one-shot, with no rung above them for `LOCAL_FALLBACK` to cover a local death with.
- **A post-PR fix round checks out the PR's head branch, not `otto/<run_id>`** (`_fix_workspace`) — a run amending an existing PR never pushed its own; a round that can't provision goes inconclusive, not Failed.
- **The PR's title, body and commit msg describe the CHANGE, never the run** (`_PR_BODY_RULE`; `pr_copy(summary_is_error=)`←the ladder's `is_error`) — an errored `result` is a stderr tail no prefix list catches; rounds APPEND (`PrBodyContractTests`, `PrCopyTests`).

## Terminal state / no silent failure

Errored and timed-out turns are failed attempts → retry → escalate. Verify-exhausted, QA-fail, and budget-hit → `needs_human` → Blocked. Exception: repo-mode with an open PR is advisory-only (Finished, "⚠ unverified"). Delivery is atomic + idempotent (`<!-- otto-run:<id> -->`).

- **Every terminal state writes an audit row AND tells `reply_to`** — needs-you reads live visibility, not the trail, so a skipped `record_terminal` vanishes; `finalize_terminal` pushes to the OWNER only, so a skipped `deliver_result` is silent (`…tells_the_asker`).
- **A TERMINATE/CANCEL/TIMED_OUT delivers no exception into the workflow**, so its `except Exception` never fires — `server._wf_terminate` writes the row itself, recovering cap/request via `engine.run_origin` (the ONE impl; `server._run_origin` delegates).
- **The Reaper is the backstop** (`ReaperWorkflow`) — `reap_stuck` sweeps board cards and every OttoWorkflow; a dead or stuck run with no needs-human row gets one, idempotent, bounded by `OTTO_REAP_WINDOW_H`. Swarm children (`-s<N>`) are skipped — the parent's row is the signal.
- The Reaper's ntfy line reports run COUNTS by ingress, never raw wids — a `slack-*` wid holds a channel id.

## Retry and accept (`/api/needs-you/*`)

- **Accept and dismiss are opposite verdicts.** Dismiss is UI-only. Accept (`engine.accept_run`) means the judges were wrong — it feeds `scorecard`'s `false_fails` (a bad judge, not a bad capability) and the approach joins `solutions`. `pass_rate` is unchanged by design.
- **A retry must inherit the dead run's `reply_to`**, or a retried Slack/GitHub run answers nowhere. `unattended`+`approval` are recovered *separately* — a scheduled retry must keep `auto_approve`, not gate on a screen nobody's watching.
- **A retry that already reached RUN** (`_run_origin`'s `reached_run`) forces `unattended=True, approval="auto"` — clicking retry on an approved write re-authorizes it rather than re-routing, re-planning and re-burning the preview. A run that died earlier gets the full restart.
- **A retry must reattach to the dying run's OWN chat thread.** `server._wf_origin_chat_key` recovers `chat_key` from its Temporal result/status and must be tried before `chats.find_reattach`'s text match, which fails silently and forks a new chat, freezing the familiar thread.
- **A board card's Chat link needs a chat_key, and not every run gets one.** An interactive `web-*` run never gets a server-side one, and a terminal status outside COMPLETED/RUNNING falls through `_board()` with `chat_key: None`. `chats.origin_run_id` is the sticky backstop.

Stuck-run recovery: `activities.reap_stuck`+`ReaperWorkflow`, dashboard `/api/needs-you`.
