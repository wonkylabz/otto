# Ingress — Slack, GitHub board, schedules, webhooks

Five adapters normalizing into one `OttoWorkflow`: web chat (`web/index.html`+`server.py`), Temporal Schedules (`scheduler.py`), webhooks (`events.py`), GitHub board (`board.py`), Slack (`slack.py`). Split: **interactive** (clarify, wait for approval) vs **unattended** (deliver to `reply_to`).

## Slack

`slack.py` — answers DMs/mentions via user-token polling, not a bot; allowlisted users/channels only. Replies always post as the token owner. State in `data/slack-state.json`.

- **Cursor/conversation/poll decisions live in `slack_state.py`, pure** — no I/O, clock, or env; `slack.py` is the shell passing tunables at call time. Invariants guarded by `SlackStateMachineTests`.
- **A cursor must be a Slack ts** (10 digits + exactly 6 decimals, `slack._slack_ts`) — a 7-decimal cursor makes `conversations.history` return 0 messages with `ok:True` forever.
- **Downtime guard** (`DOWNTIME_S`=300, `RESUME_GRACE_S`=120) — a cursor means "read up to here", true only while polling runs; a wider gap marks old messages seen so an outage doesn't dump backlog as fresh replies. A slow in-flight run is not downtime.
- **First sight of a channel isn't its first message** — seed the cursor at `now - RESUME_GRACE_S`, and don't `continue` after seeding.
- **A pleasantry never starts a run** (`slack.is_pleasantry`) — narrow predicate, any `?`/digit/URL/mention bails out.
- **A thread Otto replied in is watched** (`slack._poll_threads`) — `conversations.history` omits thread replies. `conversations.replies` includes the parent and treats `oldest` as inclusive, so filter `ts > cursor` yourself. One turn at a time via a pending flag (`PENDING_STALE_S`=1800).
- **Continuity is per-conversation** (`slack.conversation_key`) — a DM keys on the channel, a channel thread on `channel|thread_ts`. Keying a DM on the thread breaks continuity.
- **A new task in an old conversation is handed off, not resumed** (`engine.followup_handoff`) — resume binds the session's cap for life and skips repo-mode/verify/review.
- `channel_context`/`thread_context` are the cold-start fallback only, not the continuity mechanism.
- `allow_self` is scoped to the owner's own self-DM (`slack._self_test`) — raw, it also answers the owner inside a third party's DM.
- Allowlist entries may be labelled (`U01ABCDE2FG  #alex`); strip via `slack.entry_id`/`allow_ids`, never compare raw.

## GitHub board

`board.py` — Projects v2 as an async queue. Ready→In Progress on pickup, result comment + move to Review/Done. **Moving to Ready is approval** (`hold` flips to `ask`). Config `data/board.json`.

## GitHub PR reviews

`pr_review.py` — the GitHub ingress's PULL half. Polls `gh search prs --review-requested=@me`,
runs the stock read-only reviewer per PR, and parks the result in a chat thread
(`gh-pr-<owner>-<repo>-<n>`) — where a human posts it, or `auto_post` does on the next poll. Config
`data/pr-review.json`, state `data/pr-review-state.json`, schedule `pr-review-poll`.

- **A pending review request is a STATE, not an event** — the search lists only PRs still awaiting you, so in/out transitions are the whole machine; a failed search returns None, never `[]`, or one bad poll re-reviews the queue (`PrReviewStateMachineTests`).
- **The review is submitted as a REVIEW, never an issue comment** (`pr_review.post_review`) — submitting is what clears the pending request, the loop's only reset; a comment leaves it pending forever and no re-request is ever seen (`PrReviewPostingTests`).
- **Nothing published ever comes from the client** — `/api/pr-review/post` accepts a KEY and nothing else; the API is unauthenticated, so a body or an approve flag off the request would let any page write to a colleague's PR as the operator (`PrReviewWiringTests`).
- **An approval is decided from the cap's verdict LINE, and fails closed** (`verdict_of`) — it is published in the operator's name and unblocks a merge, so a substring match approves on "I would approve this once the leak is fixed" (`PrReviewPostingTests`).
- **The click and the unattended sweep share `publish`** — a manual post that comments while `auto_post` approves is a divergence nobody notices until it has approved something (`PrReviewPublishTests`).
- **A report's lead line is DECLARED, never asked for** (`report_prefix` → `contracts.lead_with`) — a prompt asking for a heading is a request a local model drops, and the chat copy and the returned result are written from DIFFERENT places (`PrReviewWiringTests`).
- **Scope is a MODE, never a tick count** (`pf2-scope-any`/`-only`) — an empty allowlist means EVERY repo, which unticked boxes read as "none"; saving "only these" with nothing picked is refused rather than stored (`PrReviewWiringTests`).
- **Nit-level findings are stripped from the PUBLISHED body, never from the chat** (`strip_nitpicks`, `post_nitpicks`) — it parses model markdown, so it fails safe: a trim that loses the verdict or empties the review returns the original (`PrReviewPublishTests`).
- **`auto_post` publishes only a reply that ENDS on a verdict** (`has_verdict`) — `ready` just means the run wrote something, and a crash writes something too; the human pressing the button has read it, the unattended paths have not (`PrReviewPublishTests`).
- **A finished run delivers its OWN review** (`reply_to: github_pr`, set only when `auto_post` is on) — the poll sweep is the backstop for a failed post or a downtime gap, and riding it alone cost up to a full interval (`PrReviewPublishTests`).

## Runbooks / scheduler

`runbooks.py` owns the definition (`data/runbooks.json`), `scheduler.py` the Temporal-schedule layer. A runbook is a superset of a schedule: `steps:[]` = a saved request, `+doc` = the same with prose, `steps:[…]` = a human-authored dependency graph. Legacy `data/schedules.json` migrates under its ORIGINAL id (`scheduler.migrate_legacy`) — re-keying orphans every live `sched-<id>`.

- **A human-authored graph is never re-planned** (`engine.run_plan(replan=False)`) — rewriting a person's plan delivers something they never approved. With no tail repair, `write_escalate` flips ON: escalating the model is the only recovery.
- **Per-step caps resolve up front or the plan never starts** (`engine._plan_step_caps`) — failing at step 7 has already spent the money.
- **A runbook's `doc` IS its approved plan** — bound to `self._plan` *before* the gate, so it rides into execution and the judge and replaces the plan-preview pass.
- **A cron and a required param with no default are mutually exclusive** (`runbooks.normalize`) — an empty substitution turns "decommission {{env}}" into an unscoped instruction. Same reason an unknown placeholder is left verbatim.
- **The store keeps a cap NAME, never its risk** — resolved via `runbooks.resolve_cap` at fire time, so a reclassified cap gates next run instead of firing forever under a stale `read`.
- **"Run now" starts a workflow directly, not `ScheduleHandle.trigger()`** — a schedule's action args are frozen at creation, so triggering runs the defaults while the operator watches the form they just filled in. That loses `ScheduleOverlapPolicy.SKIP`, so `scheduler._in_flight` re-enforces no-stacking. An on-demand runbook has no Schedule object and stays editable with Temporal down.

**Temporal Schedules** are durable and out-of-process; they fire whenever Temporal server + worker are up. Crons use server timezone (`OTTO_SCHEDULE_TZ`). `data/runbooks.json` is source-of-truth; `scheduler.reconcile()` rebuilds at startup and GCs any schedule whose runbook lost its cron. `scheduler` shadows `list()` — use `[*x]`.

## Cross-ingress

- **Every mutating POST is origin-checked** (`server.Handler._csrf_ok`) — the API is unauthenticated by design, so without it any page the user visits can start a pinned WRITE run or approve its own gate cross-site. Absent `Origin` = allowed (curl/tests/webhooks); `/api/events/` is exempt (its HMAC is its auth); escape hatch `OTTO_ALLOWED_ORIGINS` (`test_integration.CsrfOriginGuardTests`).

## Adding an ingress

Adding a reply-target kind needs a `privacy.source_line` branch AND a `delivery.AUDIENCE` entry, or it silently falls back to `report`.

- **A new ingress must consult `estop.blocked` before it reads state, not just before it starts the workflow** — the pause has to land before a card moves Ready→In Progress or a cursor advances (`EstopCoverageTests`).
- **A Schedule fires from the Temporal server, so it has no in-process step to check** — `activities.estop_check` at the top of `_run_impl` is the pause's backstop for cron. A paused run returns finished, never needs-human.
- **A paused Slack poll must skip `slack.poll` entirely** — not stamping `last_poll` makes `DOWNTIME_S` treat the pause as what it was, so release drops backlog instead of answering it hours late.
