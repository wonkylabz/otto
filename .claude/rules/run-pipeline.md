# Run pipeline — plan gate, ladder, verify, supervisor

`workflows.py`+`worker.py` (Temporal, the only path — #278).

- **Never bare-index `self._settings`** — read every setting through `self._setting(name)`, which falls back to `config.SETTINGS_FALLBACK`. Same for `config.budget_exceeded(snapshot=)` (`test_core.SettingsSnapshotAccessTests`).
- **A snapshot missing a key poisons the run forever** — `self._settings` is a recorded activity result replayed verbatim, so a run in flight when a key was added KeyErrors on every replay. No restart fixes it; only `temporal workflow terminate` + resubmit.

## Plan-first approval preview

A read-only plan (`--permission-mode plan`, scoped tools) before running. Skipped whenever `approval:"auto"`, and on a discussion turn (routing-capabilities.md).

- **Pre-authorization does not also require `unattended`** — "auto" is only ever set by a trusted opt-in path, so gating it anyway made "Run now" gate the same runbook its cron fire runs, after a 900s preview (`PreAuthorizedGateTests`).
- **A plan is enumerated in edit order but must be approved in deploy order.** `_PLAN_INSTRUCTION` demands: load-bearing unknown as step 1, a precondition on any step changing behaviour for existing callers, blast radius by name, a re-read of mirrored config, every AC covered, a closing "Risks & assumptions".
- **The plan preview runs from the LIVE checkout, before provisioning** — for a request about an open PR it reads the DEFAULT branch. `plans._pr_branch_note` names it and points at `gh pr diff` (already in `PLAN_TOOLS`); the target resolves above the gate (`PlanBranchNoteTests`).
- **The preview writes its own transcript** (`claude_cli.plan_transcript_path`) — it is a full agentic pass, and with nothing on disk the board's model chip (it resolves the model BY reading one) stayed blank for the whole 15-minute ceiling (`PlanVisibilityTests`).
- **`engine.critique_plan`** judges the plan for what a competent plan *hides* (enforcement ahead of its precondition, collateral damage, no-op step, uncovered AC, no rollback). Advisory — every failure path returns `[]`. Told the planner had no live-system access.
- `server._wf_state`'s gate fields are a whitelist — a new `OttoWorkflow.status` field is invisible to the UI until named there (`GateStateForwardingTests`).
- **The approved plan is carried into execution and the judge** (`self._plan` → `run_capability`/`verify_capability` → `_approved_plan_note`/`verify(approved_plan=)`) — otherwise execution re-derives its approach and verify judges the raw request, so an approved ordering can be violated and still pass. A run that departs must say so. None for unattended `auto`/resume (`ApprovedPlanBindingTests`).
- A plan can prescribe work the executor cannot do (no `kubectl`/`aws-vault` in the worker env) — `_approved_plan_note` requires such a step be reported unverified, never claimed done. Diagnose by counting `tool_use` blocks in the transcript, never by trusting the report.
- The plan must be the **last** thing the cap says — only the final turn is captured.
- Preview timeout is 900s; `plan_capability`'s activity timeout must stay well above it (17min).
- A failed pass yields no plan text, never its error sentinel rendered as one; an empty plan at the gate shows an explicit note rather than a bare approval card.
- **The gate wait is BOUNDED** (`_gate_wait`, `gate_timeout_h`=24h, 0=off) — an ingress whose asker can't see the approval card parks forever otherwise. Expiry DECLINES: `gate_timeout` needs-human + a word to `reply_to`, never an approval (`GateDeadlineAndDenialIdentityTests`).
- **A decline is audited under the run's OWN wid** — `record_skip` minting a fresh one orphans the row from the preview the human declined, the chat and the board card.
- **"Request changes" (`revise_plan`)** folds free-text feedback into the request and re-previews, bounded by `max_plan_revisions` (3).
- **Only `replanning` says a revised plan is UP** — `plan_revisions` bumps before the re-preview runs, so a client polling it repaints the OLD plan and clears its note, making a multi-minute re-plan read as dropped feedback (`PlanRevisionFeedbackTests`).

## Swarm / fan-out

`engine.decompose`+`plan_swarm` — cheap planner decides if a request is really independent sub-tasks (`[]` = single; ≥2 `{cap,request}`, `MAX_SWARM=5`). Each child is a separate pinned-cap workflow gating its own writes.

## Plan-then-execute

`config.PLAN_MODE`, default off — dependency-ordered steps run by wave (`PLAN_MAX_PARALLEL=3`). One failed step replans the tail (`PLAN_MAX_REPLANS=2`); several → needs-human.

## Verify → retry → escalate

Attempt → judge (pass/fail + critique) → retry folding the critique in, up to `MAX_VERIFY_ATTEMPTS` (3); the final attempt escalates model. Resumed sessions skip verify.

- **Repo-conventions injection** (`conventions.py`) — the judge is a bare `gateway.complete` with no cwd, so it gets a digest of the target repo's CLAUDE.md plus a precedence rule (conventions override the request; a faithfully-implemented rule violation is a FAIL). The executor gets this free via `claude -p`'s cwd. Cached in `data/conventions.json` keyed on mtime+size.
  - **Ranked against the request, never truncated in document order** — first-that-fit makes the enforced subset a function of section order, so reordering CLAUDE.md swaps what the judge enforces. `select_rules` states how many it dropped: a silent trim reads as "those were all the rules". Cache holds the FULL extraction; `_CACHE_V` forces re-derivation (`ConventionsBudgetTests`).
  - **An operator instruction is not a convention rule** (`conventions._judgeable`) — the judge reads only the finished output, so "restart the worker" is satisfiable by no diff; held, it fails compliant work and steers the retry at nothing (`ConventionsNoiseTests`).
  - **`conventions._SOURCES` is the whole input set.** A rule in a file it doesn't list is invisible to every judge — extend it when adding a rules file, or enforcement silently drops.
- **A judge shown only `name (description[:160])` invents defects** — "no tool access", or a cap-mandated limit read as invented, whose critique widens the blast radius. `verify` adds the grant, `cap_contract_block` the rules (`JudgeCapContractTests`).
- **A cap over `_CAP_CONTRACT_CHARS` is RANKED per request, never truncated** — an over-budget cap silently makes the judge enforce a different subset of the contract on every run, so the stock reviewer is kept whole and trimmed to fit (`StockReviewCapContractTests`).
- **A tool is in the grant only once it RETURNS** (`_grant_list`/`_refused_note`) — `READ_TOOLS` omits a cap's own `tools:` and inherited connectors, so a judge reading it as closed fails real connector work as fabricated; but an ungranted connector refuses every call, so crediting a mere attempt turns a truthful "source blocked" into an invented excuse (`JudgeToolGrantTests`).
- **An adverse judge verdict must REPRODUCE before it is acted on** (`judging.confirm_adverse`) — `claude -p` has no temperature/seed, so one sample is a coin flip on a good result; a PASS never re-samples (`JudgeConfirmationTests`).
  - **All FOUR judges route through it**, `judge_review`/`judge_qa` included — both were bare `gateway.complete`, and one FAILed a PR then PASSed the identical commit 21min later (`web-2bd1a194`). Non-pass is adverse: INCONCLUSIVE dead-ends a clean PR too.
- **Every model-bound excerpt is clipped WITH a marker, never a bare slice** — an unmarked cut manufactures the defect it is then failed for. `_clipped` for a prompt that JUDGES, `_clipped_input` for one that FEEDS work — opposite instructions (`StepInputTruncationTests`).
- **A failed run records its CAUSE, never `str(e)`** (`workflows._failure_detail`) — `str(ActivityError)` is the fixed string "Activity task failed"; the real error hangs off `.cause`, and the worker log holding the traceback lives in /tmp (`FailureDetailTests`).
- **An ABSENT verdict is not a failed one** (`workflows._verified_of`; the `verdict is not None` gate on verify_exhausted) — an unjudged turn has `passed` False, so needs-human and the `verified` badge both call it a failure (`BrainstormRunTests`).
- **A completion push is UNATTENDED-only, tiered below the two that park a run** — an interactive run's answer is already on the reader's screen, so pushing it trains the eye past the gate push (`WorkflowUnattendedTests`, `…does_not_push_on_completion`).
- **A supervisor kill and a harness death are not judgements** — both record `verified=False` so the ladder can steer; `verdict["source"]` marks who decided and `scorecard` counts judge verdicts only (`ScorecardTests`).
- **A harness death must not spend a ladder rung** — no judge read it, so burning one shortens the ladder and drags the final escalation onto a timeout. It draws on `max_harness_retries`; a kill still spends one, enforce-mode is bounded by design (`HarnessDeathLadderTests`).
- **Unattended dead-end rule** — with nobody present, a report whose bottom line is a question is a FAIL. `audience="slack"` *replaces* this rule (a question back is a legitimate answer there); it instead fails leaked scaffolding, wrong-audience replies, and "check this yourself".

## LLM supervisor

`supervisor.py` — watches mid-attempt on a bounded cadence (`SUPERVISOR_EVERY_S`). SHADOW (default) or ENFORCE (kill+restart on RETRY, bounded by `MAX_VERIFY_ATTEMPTS`). Unparseable verdict → CONTINUE. Resumes are never supervised. `OTTO_SUPERVISE=0` disables.

- **The LADDER arms the kill switch** (`supervise_enforce`, `max_supervisor_kills`=1) — never on the final rung (nothing left for the critique to steer) and never past the budget; a 2nd kill rescues worse than letting the attempt finish (`SupervisorKillBudgetTests`).
- **A steer corrects a live attempt; a kill discards it** (`supervisor.Steer`, `supervise_steer`) — delivered into the running session, so no rung is spent. Not gated on `supervise_enforce`: the final rung is where it is worth most (`SteerLadderArmingTests`).
- **A steer must reach `verify(steers=)`** or the verifier fails the attempt for obeying the supervisor. The STEER verdict exists only where the prompt offered it, first line only — it is obeyed verbatim (`SteeredRunVerifyTests`, `SupervisorSteerTests`).
- **Streaming stdin changes `claude -p`'s exit contract** — it waits for another message after `result` instead of EOF-ing, so the read loop must end the turn and close stdin, or a successful attempt reports `(timed out)` (`ClaudeSteerTests`).
- **The supervisor must see the verifier's critique** (`critique=` threaded through `run_attempt`→`supervisor.start`) or the two judges fight — a retry steered by "split this into a follow-up" is killed by a supervisor still judging the unamended request.
