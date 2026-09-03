# Cost, privacy, memory, report shaping

## Per-run cost budget

`BUDGET_SOFT/HARD` (tokens/USD, 0=off). Soft → downshift tier; hard → stop, needs-human. Swarm children budget separately.

## Egress privacy

`privacy.py` — four paths leave the box: ntfy, Slack, GitHub comment, webhook.

- **`privacy.redact`** — deterministic, unconditional, at every egress (JWT/Bearer/API-key/PEM/URL-creds/secret-named k/v). Must stay the ONE implementation (`supervisor.redact` is an alias) and idempotent (a Slack reply passes two choke points). Fails closed. Block Kit bypasses the scrub by design — build blocks from already-redacted text, don't walk the tree.
- **Write each pattern's test from the vendor's REAL key format, never from the regex.** A fixture shaped to match the pattern proves nothing: `sk-ABCDEF…` passed while every `sk-ant-api03-…` leaked, because the alnum-only body stopped at the hyphen. Assert the exact output, not `assertNotIn`.
- **Content minimization on push** — `delivery.notify(title, *, lines=, detail=)` is keyword-only: `lines` (Otto's own vocabulary) always sends; `detail` (request/ticket content) only with `OTTO_NTFY_DETAIL` — ntfy is a third-party broker and the topic name is its only credential. `privacy.source_line` names `repo#n`, never a Slack channel id or webhook URL.
- **A push deep-links to the run it is about** — `delivery.notify` sets `click` to `<OTTO_CLICK_URL>#run=<wid>`, opened by the UI's `openDeepLink`. Landing on the home tab makes the reader hunt for what they were just told (`NtfyTests`, `…deep_links_to_that_run`).
- **A blocking push is RETRIED and its outcome recorded** (`_record_health`) — a silently-failed gate push parks the run until the deadline declines it. Its dedupe window must stay SHORT: a re-previewed plan re-pushes the same title (`NtfyTests`, `…not_suppress_its_own_retry`).
- **An action button carries a single-use per-run token, never a run id** (`delivery.mint_action_token`, `POST /api/gate/<token>`) — the grant rides on the broker, so it is only as private as the topic name (`NtfyTests`, `…action_token_is_single_use`).
- **A third-party Slack reader is not the owner** — a colleague's DM still gets the owner's memory to ground on, but the reply must not recite it, leak credentials (say *where* they live), or quote across conversations. Prompt-level (`_DIRECT_REPLY_FORMAT` + `verify`'s audience block); `redact` is the guard.

## Memory + audit

Five stores, all tables in `data/otto.db` (SQLite/WAL): memory (facts), solutions (verified-pass approaches, cap 200), behaviors (advisory user directives, cap 50, never a security control), knowledge (chunked+embedded docs, RAG or keyword fallback), audit (`audit`+`audit_content`, immutable). Per-project isolation via a `namespace` column (NULL=global). Legacy `data/*.json` are frozen forensic copies, read by nothing.

- **Stale secondary sources are the standing failure mode for question-shaped runs.** `_REPORT_FORMAT` requires a primary source (repo/cluster/cloud API) for any current-state claim, forbids concluding non-existence from absence in a note, and demands a dated hedge when only a secondary source was reached.
- **A READ run has no cwd anchor and can pick the wrong sibling clone** (a stale `repo2` beside `repo`). `engine._repo_source_note` pins it to the registered checkouts, forbids picking one by directory listing, and states a checkout is a snapshot — check `origin/HEAD`.
- **The assistant cap's prompt and the memory-context header must agree**: stable facts (how something works, past decisions) can be answered from context; current-state facts (exists/deployed/enabled/reachable) must be re-checked with tools, tool result wins. A contradiction between the two texts is how one bad fact keeps being served back.
- **Facts are dated and explicitly not authoritative** (`recent_facts(dated=True)`) — the newer of two conflicting facts wins; don't drop the date.
- **Extraction rejects narration** (`engine._is_durable_fact`) — biased toward rejecting; a lost fact is cheap, a junk row evicts a real one from every future run.
- `recent_facts`: global rows first then project's, limit tails the concatenation; dedupe against the global+project union. Ranking is keyword-overlap against the request (`_FACT_QUERY_STOP` strips filler; IDF alone misfires in a small corpus).
- **A fact from a verify-failed run is still stored, but labelled `unverified`** and rendered as a lead — a failed run can still learn something true.
- `/api/memory`+`clear`+`delete` are global-only by design; `every=True` reaches per-project.
- **A single fact can be forgotten** (`engine.delete_fact`) — matched on `id`+normalized text, since a row holds 0-3 facts and `clear_memory` alone made one wrong fact cost every right one. Emptying a row deletes it.
- **Memory GC** (`engine.gc_preview`/`gc_evict`, Admin → Memory) — on-demand only, never scheduled. `gc_preview` only proposes; nothing is deleted until a human confirms via `gc_evict`, which evicts facts through `delete_fact(..., every=True)`.
- **Both GC calls ride the `memory_gc` tier** — GC is the one pass that DELETES memory; its classifier rode `verify`, its live check was hardcoded, so neither showed in the Admin matrix. The live half needs `claude -p`, so a local pick degrades like `preview` (`MemoryGcTests`).
- **GC is two staged passes**: one classifier call per `memory_gc_batch_size` items (KEEP/STALE/VERIFY), then ≤`memory_gc_max_verify` tool-verification turns on VERIFY items — excess is left for next run, not dropped. Both run on a bounded pool; unparseable or raising ⇒ KEEP.

## Final-report shaping

`contracts.py` — every invocation carries guidance on *how* to report: a labelled `**TLDR**` line (forbidding Otto's own vocabulary — "the gate", "AC #1" — the reader has never heard of it) plus a `**What you need to do**` line, omitted when nothing is needed or every clean run grows a footer that trains the eye past the one that matters. In `_TLDR_SHAPE`, interpolated into `_REPORT_FORMAT`/`_SINGLE_TURN_CONTRACT`/`_RESUME_CONTRACT`.

- **A retry must never narrate itself** (`_CRITIQUE_FOLD`) — only the LAST attempt is delivered, so a "corrected result" preamble or a "where the previous attempt went wrong" section describes a run nobody saw. Fixed on the executor; judging it instead burns a rung on cosmetics.
- **A cap that prescribes its own output format outranks `_TLDR_SHAPE`** (`_CAP_FORMAT_WINS`) — the judge already enforces the cap's contract as supreme, so an attempt obeying Otto's shape instead is FAILed for it, and the two contracts exhaust the ladder between them.
- **Who reads the result picks the contract** (`delivery.AUDIENCE`→`audience_for`→`engine._output_contract`): `"conversation"` (a person who can reply) gets `_DIRECT_REPLY_FORMAT`; `"report"` (operator/durable record) gets `_REPORT_FORMAT`. A new reply-target kind must declare its audience (grep-based test); unknown falls back to `report`.
- In a swarm the audience applies to the *merge*, not the children — children stay report-shaped so there's one coherent report to synthesize.
- The audience is derived once (`OttoWorkflow._run_impl`) and threaded through run, verify **and the resume branch**, or turn 2 of a Slack conversation reverts to report prose.
- **A third audience, `brainstorm`, is set from the CAP not the target** (`workflows._audience_of` → `_THINKING_PARTNER_FORMAT`) — a web chat has no `reply_to` at all, so it fell back to `_REPORT_FORMAT` (`BrainstormModeTests`).
- **The RESUME contract must be picked by audience too** (`contracts._resume_contract`) — `_RESUME_CONTRACT` folds in `_TLDR_SHAPE` on every resumed turn, so a mode forbidding the TLDR reads fine on turn 1 and reverts on turn 2 (`BrainstormModeTests`).
- All three texts shaping a Slack reply (output contract, `slack.to_request` framing, `verify`'s judge) must agree on who the reader is — the framing text can silently win over the contract.
- **A conversational run can choose to say nothing** (`config.NO_REPLY`/`is_no_reply`) — pure filler has no keyword-safe detection, so the model decides and a strict sentinel-match lets `verify` pass it deterministically and `delivery._slack` post nothing. Conversational audience only. A silent turn still binds the session but must never become `last_reply`.

## Scorecard

`/api/stats`: `used` = all-time runs, but pass/escalation/fallback rates and avg cost are over **judged runs only** — the two denominators differ, and "judged" excludes supervisor kills and harness deaths (`verdict_source`).

- **A verdict records WHICH judge reached it** (`verdict_source` + `verdict_model`) — without the model, a bad capability and a bad judge leave identical rows, so `false_fails` can't be split by judge.
- **`model` and `verdict_model` are canonical model IDs, normalized in `_audit`** (`gateway.model_id`) — local paths hand over a pool entry's editable LABEL, and a second namespace in the column `scorecard` groups by never joins a judge to its executor (`ModelIdNamespaceTests`).
- **Judge-side spend is booked by the gateway, not the trail** — an audit row exists only for an execution attempt, so `gateway._claude_tier` is the ONE place a tier call's cost lands (`stats().overhead_usd`); a bare `_claude_complete` is invisible (`GatewayCostLedgerTests`).
