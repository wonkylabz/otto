# Repo work — workspaces, PRs, review/QA, terminal state

## Isolated repo workspaces

`workspace.py` — modify code in an isolated shallow clone, never the live checkout. Fresh writes with a matching `repo_hint` auto-promote to repo mode. Pushes a branch, opens a draft PR, tears down after.

- **A cap's own PR wins even when Otto's branch also has work** (`_agent_pr`, resolved up front) — a cap driving its own git and leaving the tree dirty otherwise makes two PRs. Otto's branch is still pushed.
- A cap opening its PR on **Otto's own branch** takes the other path — `gh pr create` fails `already exists`; recover via `_existing_pr_url`/stderr, never dropping `pr_url` to None (which skipped review).
- **The approved plan reaches the PR as a comment, never a committed file** (`workspace.post_plan`) — the reviewer otherwise sees the diff, never what was approved, but that record must not outlive its review. One per PR (`otto-plan`).
- **A resume needs the workspace for its path, not its branch** (`OttoWorkflow._resume_workspace`) — `claude -p --resume` looks history up under the creating cwd. Four tiers: chat's branch → the PR's branch → a fresh default-branch clone at the same path (only if no PR ever opened — a merged branch must dead-end, not discard commits) → not continuable. Provisioning happens *before* the gate for a resume; the deny path tears the clone down.
- **The whole ladder is dead code if the chat never recorded `repo`/`git_run_id`** on the chat row (`chats.finish_run`) — a follow-up then resumes with `cwd=None` and returns `(no output)`. `/api/continue` falls back to `chats.git_identity` when the client sends nothing (a stale tab sends `repo:undefined`); that fallback is the authority, not the client.
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
- **Repo-mode's PR base needs the same refresh** — `workspace.provision` clones `--depth 1` from the local path, so the base is the *local* default-branch ref and can be badly behind. `_refresh_base` fetches + hard-resets onto the real remote default before branching (skipped for `from_branch=True`).
- **Tests must never touch live state** — both `setUpModule`s re-point `PROJECTS_FILE`, the `_DB` aliases and `gateway`/`policy._PATH`; unpinned, a run rewrites repo refs, Admin config, phantom rows (`LiveStoreIsolationTests`).

## Post-PR loops

**Code review** — **default-on for every repo-mode PR** (`params.get("review", True)`): a PR is a PR whoever wrote it. Runs *before* QA; a fail skips QA. Bounded `MAX_REVIEW_ROUNDS=3`. Cap `code-reviewer` (`OTTO_REVIEW_CAP`).

**QA** (opt-in, repo-mode only) — judges pass/fail/inconclusive; FAIL re-provisions, re-runs, re-pushes, re-QAs. Bounded `MAX_QA_ROUNDS=2`. Opt-in = pre-authorized. Cap `qa-tester` (`OTTO_QA_CAP`).

- **A post-PR round stamps `verdict_source` — its verdict is about the PR, not the capability** — a round correctly raising must-fix findings booked the reviewer as failing, and no round raises a needs-you card a human could accept (`ScorecardTests`).
- **An errored fix round ENDS the loop** (`is_error` → inconclusive, not counted) — it commits nothing, so re-reviewing re-runs the same judge over the same diff to the same verdict, spending the whole budget (`web-2bd1a194`).
- **A fix round never runs on the LOCAL backend** (`_FIX_NO_LADDER` → `local_disabled`) — both loops are one-shot, with no rung above them for `LOCAL_FALLBACK` to cover a local death with.
- **A post-PR fix round checks out the PR's head branch, not `otto/<run_id>`** (`_fix_workspace`) — a run amending an existing PR never pushed its own; a round that can't provision goes inconclusive, not Failed.
- **The PR DESCRIPTION describes the change, never the run** (`contracts._PR_BODY_RULE` → `_pr_body_note`) — `pr_copy`'s body is bounded but is not the only writer, and rounds APPEND. Countered in both `fix_critique`s and `review_request` too (`PrBodyContractTests`).

## Terminal state / no silent failure

Errored and timed-out turns are failed attempts → retry → escalate. Verify-exhausted, QA-fail, and budget-hit → `needs_human` → Blocked. Exception: repo-mode with an open PR is advisory-only (Finished, "⚠ unverified"). Delivery is atomic + idempotent (`<!-- otto-run:<id> -->`).

- **Every terminal state writes its own audit row** — `/api/needs-you` reads live Temporal visibility, NOT the trail, so a run that skips `record_terminal` vanishes when history ages out. Needs-human states finalize via `finalize_terminal` (`WorkflowNeedsHumanTests`).
- **A TERMINATE/CANCEL/TIMED_OUT delivers no exception into the workflow**, so its `except Exception` never fires — `server._wf_terminate` writes the row itself, recovering cap/request via `engine.run_origin` (the ONE impl; `server._run_origin` delegates).
- **The Reaper is the backstop** (`ReaperWorkflow`, `reaper` schedule) — `reap_stuck` sweeps board cards and every other OttoWorkflow; a dead or stuck-past-TTL run with no needs-human row gets one, idempotent via the audit trail, bounded by `OTTO_REAP_WINDOW_H` so a first sweep can't flood needs-you. Swarm children (`-s<N>`) are skipped — the parent's row is the signal. Its ntfy line reports run COUNTS by ingress, never raw wids (a `slack-*` wid holds a channel id).

## Retry and accept (`/api/needs-you/*`)

- **Accept and dismiss are opposite verdicts.** Dismiss is UI-only. Accept (`engine.accept_run`) means the judges were wrong and the result stands — it feeds `scorecard`'s `false_fails`, the signal pointing at a bad verify/supervisor prompt rather than a bad capability, and the approach joins `solutions`. `pass_rate` is deliberately unchanged by an acceptance.
- **A retry must inherit the dead run's `reply_to`**, or a retried Slack/GitHub run answers nowhere. `unattended`+`approval` are recovered *separately* — a scheduled retry must keep `auto_approve`, not gate on a screen nobody's watching.
- **A retry that already reached RUN** (`server._run_origin`'s `reached_run`) forces `unattended=True, approval="auto"`, overriding the above — clicking retry on an approved write re-authorizes it instead of re-routing, re-planning and re-gating from scratch and re-burning the plan preview. A run that died before execution has no "ran" row and gets the full restart.
- **A retry must reattach to the dying run's OWN chat thread.** `server._wf_origin_chat_key` recovers `chat_key` from the dying run's Temporal result/status and must be tried before `chats.find_reattach`'s request-text match, which fails silently and forks a new chat holding the real result while the familiar thread stays frozen.
- **A board card's Chat link needs a chat_key, and not every run gets one.** An interactive `web-*` run never gets a server-side one (the browser owns it), and a terminal status outside COMPLETED/RUNNING falls through `_board()` with `chat_key: None`. `chats.origin_run_id` is the backstop — unlike `run_id` (cleared when a turn finishes) it's sticky forever, and `_board()` falls back to it whenever `chat_key` is empty.

Stuck-run recovery: `activities.reap_stuck`+`ReaperWorkflow`, dashboard `/api/needs-you`.
