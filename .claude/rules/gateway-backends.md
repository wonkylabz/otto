# Gateway, backends, MCP, tool guardrails

## Model gateway / Router #2

`gateway.py` — maps each task tier to a model. Claude backend runs `claude -p`; LOCAL backend runs `local_runtime.py`'s tool-calling loop. Tool-free read caps first-attempt as a local completion and escalate to Claude on verify fail.

- **A local endpoint is configured once and every model on it inherits the URL + key + extra headers** — the entry stores `endpoint`, `load()` hydrates `base_url`/`api_key_env`/`headers` onto it and `save()` strips them, so one endpoint edit moves every model on that server. Legacy per-entry connections auto-adopt on load; an endpoint holds connection facts only (`max_turns` measures the model, not the server) (`ModelEndpointTests`).
- **A credential is masked at the API edge; a masked value posted BACK means keep-stored** (`gateway.masked`/`unmask`) — `api_key_env` and header values hold literal keys, so a client-facing body leaks them and defeats the `models.json` read deny (`ModelSecretMaskingTests`).
- **Every local-endpoint request takes its headers from `gateway.request_headers`** — a hand-rolled dict silently drops the extra headers. Values resolve like `api_key`: env > secret helper > literal.
- **`discover_models` groups by `root`, not one row per id** — vLLM lists a served alias and its canonical repo path as separate entries sharing a root, so a raw id list multiplies the model count. The short served name is the offer, the rest ride as `aliases` (so an already-added model is recognized under either), and the UI states how many were folded in.
- **`provider` is TRANSPORT; `gateway.model_kind` is CLASS** — `kind` is stored on the endpoint (`local`/`hosted`, guessed if unset). Latches and failure copy read the class; dispatch and `_default_claude` repoints are transport (`ModelKindTests`).
- **The approval preview has its OWN tier (`preview`), not execution's** — `exec_model_id` made it a side effect of another setting, and a LOCAL execution model fell through to `_default_claude`, the CHEAPEST tier, so a human approved a Haiku plan (`PreviewModelTierTests`).
- **`preview` dispatches on TRANSPORT** (`preview_model_entry`; `preview_model_id` is the Claude half) — Claude-only made a local pick silently run sonnet under a radio naming qwen, so `_normalize` repointed it. Otto has plan mode now: the pick stands (`PreviewModelTierTests`).
- **No fallback lands on haiku** — `_default_claude` is sonnet-first. 'First entry' meant surprise opus; 'cheapest' made haiku the silent answer wherever local can't serve, incl. the preview. `downshift_model_id` keeps cheapest: an opted-in spend lever, not a fallback.
- **Model health is real-outcome-first** (`gateway.record_health`/`unhealthy_models`, `/api/health`) — every call records ok/failed so a model nothing retries still warns; last-write-wins. Only "cannot serve any run" failures count (unreachable, mis-configured, tool-calls-rejected) — a bad-but-served answer doesn't, or the badge stops meaning anything. Probing only runs for configured-but-idle models, and Claude tiers only on `force=True`.

## Backend selection

**Execution backend follows the execution model** — Claude model → `claude -p`; LOCAL model → `local_runtime.py`. Plus tool-free: a read cap with a `cap_local_exec` model tries one local completion, escalating to Claude on verify fail.

- **This is ONE Admin control, not three** — `cap_exec`/`cap_local_exec`/policy `tool_free` interact invisibly: tool-free is checked *first*, so setting both meant attempt 1 had no tools *and* every later rung was pinned local, defeating the Claude backstop. One `Execution` select (`web/index.html` `execSelect`), one writer (`setCapBackend`).
- **A resume follows the SESSION's backend, never the phase model** (`local_runtime.resume_entry`, one impl) — `claude -p --resume local-…` is rejected outright, so the plan preview came back is_error and the write gate rendered with NO plan on it.
- **A WALLED local preview re-previews on Claude** (`plans._local_wall_reason`) — the ladder re-dispatches and the preview did not, so an endpoint refusing tools handed the human an approval card with NO plan. Walls only, never on a resume (`PlanPreviewLocalSessionTests`).
- **A recovery must not erase what it recovered from** — both writers open the plan transcript `w`, so the Claude re-preview overwrote the wall's own evidence. Kept at `-walled-<reason>`; the canonical path stays the pass the human approves (`PlanPreviewLocalSessionTests`).
- **A local resume saves IN PLACE** — `claude -p`'s plan mode forks the session for free; `plan_preview` must `fork_session`/`drop_session`, or the plan instruction lands in the history the approved run resumes.
- **Claude fallback is a flag** (`config.LOCAL_FALLBACK`, default on) — a failing local model is covered by Claude; `=0` (strict) makes that illegal, stops the run, delivers `config.strict_stop_message`. `verify` is exempt (the judge must catch a bad local execution, not go down with it). Strict-stop is terminal in every ladder.
- **A DETERMINISTIC local wall latches the ladder off local; a budget death does not.** Tool-calls-rejected and endpoint-unreachable (`local_runtime`'s `tools_unsupported`/`unavailable` → `engine`'s `local_wall` → `local_incapable`) fail identically every attempt, so the run re-dispatches to Claude. A turn-budget death is the model *working and not finishing* — a retry with the critique can do better, so it stays local (`…redispatches_instead_of_burning_the_ladder`/`…does_NOT_latch_the_ladder`).
- **Three `claude -p` failures are WALLS, not harness deaths** (`error_classifier.claude_wall` -> `auth_stop` + a reason): dead login, spent usage limit, unservable model. Each names its own remedy — "re-authenticate" is wrong for a limit resetting in hours (`ClaudeAuthWall`).
- **Which failures are walls is `error_classifier.classify`, not an if-chain at the call site** — 401/403/402 and a 429/500 that outlives its backoff are deterministic, so they latch and re-dispatch instead of burning two more attempts on the same endpoint.
- **A cap failing on a local model is latched off it ACROSS runs** (`gateway.cap_local_latched`) — the ladder self-corrects within a run, so each new run re-paid the doomed attempt. Keyed on (cap, MODEL), 3 consecutive JUDGED fails, expiring into one probation (`CapLocalLatch`).
- **Every `/chat/completions` body comes from `gateway.chat_body`** — the parameters are the ENDPOINT's, not ours (OpenAI's newer models refuse three Otto always sent), learned from its own 400 and remembered on the entry (`OpenAiParamDialectTests`).
- **A body adaptation removes, renames or ADVANCES ONE STEP, and one nothing can fix WALLS** — `resolve_quirk` reads the BODY: two endpoints refuse `reasoning_effort`+tools with the SAME message and need opposite fixes, so drop → `'none'` → wall (`OpenAiParamDialectTests`).
- **A quirk that DOWNGRADES rather than drops must be reportable** (`gateway.effective_effort`) — `tool_reasoning_none` pins the weakest reasoning on every tool turn, the approval plan included, while the operator's pick still reads as honoured (`OpenAiParamDialectTests`).
- **A wall crosses to the engine as `wall_reason` (a plain string), never a new boolean** — `engine.run_attempt` turns it into `local_wall` via `wall_message`, which degrades on a reason it doesn't know rather than raising.
- **One wall clock for both backends** — `config.LOCAL_RUN_TIMEOUT_S`, never a literal in `run_json`; a read timeout near the deadline is that clock, not a sick endpoint (`RunDeadline`).
- **A context compaction must reach the caller or it is re-paid every turn** (`_chat_step` returns `(response, compacted)`) — pruning the wire body alone left the loop rebuilding the full history and the session growing, so every later turn and every resume re-hit the same 400.
- **Which 400 is an overflow is `error_classifier.classify`, not `_context_fit`** — only vLLM names both numbers, so a numbers-gated prune dead-ended every other endpoint into an anonymous 400 the ladder retries with the identical over-long prompt.
- **Measure the harness before blaming the model.** A verify critique starting `prior attempt errored or timed out` is a HARNESS death, not a judgement. Exclude them before any model-quality claim.

## MCP on the LOCAL backend

`mcp_client.py` — stdio JSON-RPC only, stdlib.

- **Registering a server and RUNNING it are two acts** (`policy.add_mcp_def`; `McpActivationTests`) — a stored `command`+`args` is spawned as the operator, so the gate is on the DEF and BOTH doors read it (`active_mcp_config`, `mcp_client.servable`). Audited with the argv.
- **Servable/unservable is the whole design**: a stdio server (`command`+`args`) is a subprocess we spawn. A claude.ai *connector* (Gmail/Calendar/Slack/Notion) is remote OAuth inside Claude Code's own session — nothing to spawn, no token to present. `servable()` refuses anything not stdio.
- **A cap needing a connector must not run locally** — `mcp_client.unservable(cap)` keeps it on Claude (strict mode stops instead). The Admin Execution dropdown disables local options for such a cap.
- **A connector blocks a local run by REQUEST, not only by declaration** (`mcp_client.connectors_named`) — the generalists declare no `tools:`, so the cap-side test is silent for the caps handed anything, and the model hunts credentials to hand-roll it (`LocalConnectorGapTests`).
- **What the local backend CANNOT reach is declared in-context** (`mcp_client.connector_note`) — outside the stdio branch: a connector is absent either way. 4/4 without it the model plans to read `~/.netrc`; 4/4 with it, it reports the blocker (`LocalConnectorGapTests`).
- **Two bounds, not one**: which servers = a cap's `tools:` frontmatter ∩ servable ∩ risk allowlist; how many tools = `Pool` ranks against the request, keeping at most `LOCAL_MCP_MAX_TOOLS` (25) — the full fleet is schema resent every turn, fatal on a small context window. An undeclared cap draws only the request-relevant few.
- Tool catalogue is cached (`data/mcp-tools.json`, keyed on server-def hash) so ranking needn't spawn every server. A server that can't start is negatively cached (`LOCAL_MCP_PROBE_TTL_S`) so one dead server doesn't force a cold-cache "spawn everything" fallback, and is declared in-context (not fatal). Kill switch `OTTO_LOCAL_MCP=0`; pool closed in `run_json`'s `finally`.
- **Still missing** (env, not code): the worker has no `AWS_*`/`aws-vault`, so `aws-mcp`/EKS auth as nobody on either backend.

## Tools + guardrails

`claude_cli.run_json` runs `claude -p`; per-risk allowlists in `config.py`; MCP via `policy.py`+`--mcp-config`.

- **`--allowedTools` grants permission but unloads nothing** — the whole built-in set plus every MCP server, skill and agent stays in every turn's system prompt. `--disallowedTools` on the complement (`config.DISALLOWED_TOOLS`, synced with `KEEP_TOOLS` by `ContextTrimTests`) is what removes them. **Never disallow `ToolSearch`** — deferred MCP schemas load through it.
- **`--setting-sources user` for any run with no cwd of its own** (`engine._setting_sources`) — an unanchored run inherits the worker's directory, i.e. Otto's own checkout, loading Otto's CLAUDE.md into every unrelated run. A cwd IS set for repo-mode/project caps, which must keep their repo's CLAUDE.md.
- **A cheap tier loads NO CLAUDE.md: `setting_sources=""`, which is not the same as omitting it** (that defaults to user+project+local). `"user"` pulled in the OPERATOR's global file, and "never restate something already known" then answered NONE (`CheapTierContextTests`).
- **`--strict-mcp-config` only on tool-free calls** (`gateway._claude_complete`) — `data/mcp-servers.json` is normally `{}`, so every server a cap uses is INHERITED; strict mode on an execution run strips them all.
- **`--effort` only WARNS on a bad value** — an unvalidated level runs at the DEFAULT effort while every layer reports the pick honoured, so BOTH backends normalize through `config.effort_level`; local's `reasoning_effort` is advisory, accepted and ignored (`EffortLevelTests`).
- `--tools` is NOT the lever — it loaded 0 tools and *doubled* context.
- **A path deny rule has exactly one working spelling: `Edit(//abs/**)` in `permissions.deny` via `--settings`** (`file_safety._rule`). `Write(...)`, a single-slash path, and the same rule on `--disallowedTools` each parse, raise nothing and block nothing.
- **A deny glob and the path a run writes are two names for one file** — `file_safety` resolves BOTH ends and emits both spellings, or a rule crossing a symlink (macOS `/tmp`, `/var`) matches nothing on the LOCAL backend while `claude -p` still enforces it (`FileSafetySymlinkTests`).
- **A deny rule covers `rm` through Bash, not just writes** (measured against a control) — which is why `data/ESTOP` is on the list: deleting it releases the global pause, handing a run the operator's only stop lever.
- **`file_safety` is the write guard that needs no human** — the approval gate judges a plan, but `READ_TOOLS` has unscoped `Bash`. A matching deny beats an explicit allow and covers Bash redirection (`FileSafetyTests`).
- **Otto's own runtime state is READ-denied** (`file_safety.read_denied_globs`) — `otto.db`, `data/*.json` (plaintext keys), `transcripts/`. Exempt: cwd IS Otto's checkout. `data/workspaces/**` stays readable or repo-mode dies (`ReadDenyTests`).
- **The credential stores are READ-denied for EVERY run, Otto-cwd included** (`file_safety._secret_store_globs`) — editor history caches too: a copy of every file ever edited. `~/.aws/credentials` stays readable by decision (`ReadDenyTests`).
- **A `Read(//path/**)` deny covers `cat` through Bash** (measured) — but local Bash is not, so `local_runtime` guards Read and filters Grep's OUTPUT, never its root: the denied set is files under `data/`, never `data/` itself.
- **The LOCAL backend bypasses `claude -p`'s permission system entirely**, so `local_runtime._deny_guard` re-enforces the same list on its own Write/Edit. Its `Bash` is NOT covered — parsing a shell to catch `tee`/`sed -i` is protection theatre.
- **The local PLAN pass reproduces plan mode in TWO layers** — a `bwrap` read-only root with a scratch tmpfs on /tmp (shell stays WHOLE), else `bash_refusal`'s argv allowlist. Measured: 78% of a Claude planner's Bash is composed, so argv-only is far poorer (`LocalPlanModeTests`).
- **The sandbox is PROBED, never assumed from PATH** — `bwrap` installs fine where userns is off; the probe must RUN a command and see a write refused, or a broken sandbox is trusted. `--tmpfs /tmp` masks a cwd under /tmp: re-bind it read-only after (`LocalPlanModeTests`).
- **The sandbox enforces the READ deny-set too** (`_read_deny_mounts`) — it confines writes by construction, so `Read data/models.json` was refused while `cat` returned the API keys, to a THIRD-PARTY endpoint. Honours the cwd-is-Otto exemption (`LocalPlanModeTests`).
- **Neither layer bounds the NETWORK** — reading the ticket is why the planner has Bash, and plan mode permits read-only network too. `gh api -X POST` is refused by the allowlist, NOT by the sandbox; the approval gate still stands there (`LocalPlanModeTests`).
- **Every REGISTERED repo's live checkout is write-denied by default** (`file_safety.denied_globs`, issue #59) — a cap with no cwd of its own could otherwise reach sideways into any of them and edit in place, which the in-place-edit guard only ever detected after the fact. `allow_cwd` (threaded from `claude_cli.run_json`'s/`local_runtime`'s own `cwd`) is the one exemption — a project capability's OWN repo, never a sibling's.

## Execution transcripts

**`system_context` is recorded beside `prompt` in the meta line** — the approved plan, the mismatch note, the output contract and recalled memory all travel that argument, so without it a transcript cannot say what the model was told (`SystemContextTranscriptTests`).

- **Every transcript line is scrubbed as it is written** (`claude_cli.transcript_line`, the ONE writer for both backends) — a run handling a credential otherwise leaves it in plaintext for the whole TTL; forensics needs the shape, never the bytes (`RedactTests`).

Both backends append `data/transcripts/<wid>-a<attempt>.jsonl` (TTL `TRANSCRIPT_TTL_H`). Live: `/api/progress`; full: `/api/run/detail`.
