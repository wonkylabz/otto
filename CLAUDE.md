# Otto — repo guide for Claude Code

Local agentic orchestrator: a chat UI routes a request to one of the user's real Claude Code subagents/skills, clarifies if needed, gates writes for approval, runs it via `claude -p`, and records to memory + audit. Durable execution via Temporal.

`README.md` is the user-facing overview. This file is the working guide for editing the code.

## Why this project exists

Temporal supplies durability (retries, signals, schedules). Otto supplies what makes a run *trustworthy*: routing, approval gates, verification, isolated repo clones, learning (memory/solutions/behaviors), cost control, and recovery — stuck runs surface to humans, never vanish.

## Docs are three tiers — put a new rule in the lowest one that works

**This file is RESIDENT (every session pays for it); `.claude/rules/*.md` are FETCHED per layer and digested into every convention judge; `docs/*.md` are REFERENCED only by a session that follows a pointer.** A constraint any edit could violate belongs here; one that bites inside a layer belongs in that layer's file; how to run, install or test belongs in `docs/`.

Ceilings are enforced as ratchets (`test_core.ClaudeMdBudgetTests`). Before editing any of these files, read **`docs/maintaining-docs.md`** — it carries the rule format, the tier test, and how to move bytes between tiers.

**Formerly "Mosaic"** — a stray `mosaic`/`MOSAIC` in a running install is pre-rename state, not a bug.

## Run / verify

Full detail in **`docs/operating.md`** (setup, service, restart discipline, global pause) and **`docs/testing.md`** (suite, regression corpus, writing a guard test). Resident essentials:

- `./run.sh` starts Temporal + `worker.py` + `server.py`; `./install.sh` sets a machine up from scratch.
- **Tests are `./.venv/bin/python -m unittest`, never bare `python3`** — Temporal tests self-skip without `temporalio`, so bare `python3` reports a green `OK (skipped=93)` having tested nothing.
- **Run `regress.py` before and after editing any prompt** — unit tests only assert a prompt *contains* a clause, never that the model still *obeys* it.
- **Restart the worker after changing any module it imports**, or the edit has no effect and the fix looks failed. Mid-activity it costs that run's attempt, within `_HEARTBEAT` (3min).
- **This repo IS the live service's cwd.** Temporal re-imports *workflow* code fresh per task; *activities* keep running whatever loaded at worker startup — so editing `workflows.py` mid-run can crash on a version mismatch before you restart anything.
- **Global pause**: `data/ESTOP` (`estop.py`, `POST /api/estop`) stops every ingress starting new work, never kills in-flight — nothing re-checks it mid-activity.

## Architecture (each file ≈ one layer)

Read the layer's rules file before editing it — each carries the invariants that layer's bugs came from.

| Layer | Files | Rules |
| --- | --- | --- |
| Ingress — web, schedules, webhooks, board, Slack | `server.py`, `scheduler.py`, `runbooks.py`, `events.py`, `board.py`, `slack.py`, `slack_state.py` | `.claude/rules/ingress.md` |
| Run pipeline — plan gate, ladder, verify, supervisor | `workflows.py`, `worker.py`, `activities.py`, `judging.py`, `plans.py`, `conventions.py`, `supervisor.py` | `.claude/rules/run-pipeline.md` |
| Routing & capabilities | `routing.py`, `registry.py`, `intents.py`, `capabilities/` | `.claude/rules/routing-capabilities.md` |
| Repo work — clones, PRs, review/QA, resuming a repo run, terminal state | `workspace.py`, `chats.py`, `audit.py` | `.claude/rules/repo-work.md` |
| Cost, privacy, memory, report shaping | `privacy.py`, `memory.py`, `knowledge.py`, `contracts.py`, `delivery.py` | `.claude/rules/memory-privacy.md` |
| Gateway, backends, MCP, tool guardrails | `gateway.py`, `local_runtime.py`, `mcp_client.py`, `policy.py`, `claude_cli.py` | `.claude/rules/gateway-backends.md` |
| Engine facade + audit store | `engine.py`, `audit.py`, and the modules split out of them | `.claude/rules/engine-core.md` |
| UI | `web/index.html` | `.claude/rules/ui.md` |

Two cross-layer invariants that don't live in any one of them:

- **The verify→retry→escalate loop is written ONCE, in `engine._ladder_core`** (adapters `_run_ladder`, `execute`). `OttoWorkflow._verify_ladder` is a deliberate third mirror — workflow code is deterministic, so it can't merge in. Change one, mirror the other (`test_core.LadderJudgeContextTests`).
- **Five ingresses normalize into one `OttoWorkflow`.** The split that matters everywhere: **interactive** (clarify, wait for approval) vs **unattended** (deliver to `reply_to`).

## Conventions & gotchas

These bind any edit, in any layer.

- **Auth: Claude subscription via `claude -p` — never require an API key.** `ANTHROPIC_API_KEY` only auto-discovers the cloud model list (`ResidentRuleGuardTests`).
- **Runtime settings store** (`config._SETTING_SPECS`, `data/settings.json`, Admin → Runtime settings) — knobs meant to change without a restart. Read via `config.setting(name)`, precedence env > store > code default. `save_settings` stores only the diff.
- **Workflow code must never call `config.setting()`** — the store is mutable, so a replay could branch differently than history recorded. `OttoWorkflow` takes ONE snapshot via `snapshot_settings` and reads it through `self._setting(...)`. Every in-test Temporal `Worker` must register `snapshot_settings` or it silently falls back to import-time defaults (`ResidentRuleGuardTests`).
- **Every secret resolves through `config.secret(name)`** — env > the `OTTO_SECRET_COMMAND` helper > unset. A bare `os.environ` read is invisible to the helper: the secret stays in the vault and the feature it gates is silently off (`test_core.SecretProviderTests`).
- **`OTTO_SECRET_COMMAND` is env-only, never in `_SETTING_SPECS`** — a shell command settable over this unauthenticated API leaves `_csrf_ok` as the only thing between a page the user visits and code execution as them (`ResidentRuleGuardTests`).
- **Run ids never collide across processes** — Temporal uses the real workflow id for audit+transcripts+board correlation; anything without one mints `wf-<6hex>-NNNN` via `engine._next_wid()`. Never mint an id any other way.
- **`data/` is gitignored runtime state** — never commit anything under it except `.gitkeep`. The audit trail is immutable, never cleared by "clear memory" (`ResidentRuleGuardTests`).
- **JSON state writes go through `storage.mutate_json`** — `server.py`/`worker.py` mutate the same files from separate processes; `fcntl` lock + atomic replace. Never raw `open()`/`json.dump` a store; keep the mutator fast, no network (`JsonStoreConcurrencyTests`).
- **SQLite stores go through `storage.sqlite_connect`+`storage.tx`** (all tables in `data/otto.db` since #103). Open+close a connection per operation — connections aren't thread-safe and `server.py` is threaded. **Any read-modify-write must use `storage.tx` (`BEGIN IMMEDIATE`)**; `BEGIN DEFERRED` lets concurrent writers upgrade-fail and silently lose writes. Keep `gateway.embed` outside the transaction. Embeddings are packed float32 BLOBs (`knowledge._pack`/`_unpack`) (`ResidentRuleGuardTests`).
- **Never call a `tc.run`-based sync wrapper from inside a coroutine already on tc's background loop** — it deadlocks the loop and every later Temporal call in the server. `await` the async sibling instead (`test_integration.NeedsYouLoopSafetyTests`).

## Where to change things

**Every `OTTO_*` env var and its default lives in `config.py`, or in the module owning that feature (`slack.py`, `workspace.py`, `events.py`, `routing.py`) — read it there; this file does not mirror the list.**

Anything a `grep` answers in two seconds is deliberately absent. For the non-obvious homes, the layer table above is the index: Admin writes + conventions UI → `ui.md`; stock caps + risk model → `routing-capabilities.md`; scorecard + retry/accept → `memory-privacy.md`, `repo-work.md`; the `engine.X` facade and audit store → `engine-core.md`.
