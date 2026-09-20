# Tool grants, MCP, file safety, sandbox

## MCP on the LOCAL backend

`mcp_client.py` — stdio JSON-RPC only, stdlib.

- **Registering a server and RUNNING it are two acts** (`policy.add_mcp_def`; `McpActivationTests`) — a stored `command`+`args` is spawned as the operator, so the gate is on the DEF and BOTH doors read it (`active_mcp_config`, `mcp_client.servable`). Audited with the argv.
- **`~/.claude.json`'s defs spawn UNGATED, by decision; the FILE is write-denied instead** (`file_safety`) — `claude -p` spawns them anyway, so a local gate leaves a server inert on one backend and live on the other; a RUN appending one was the risk (`McpUserScopeSpawnTests`).
- **Every `claude` subprocess gets `claude_cli.child_env()`, not `os.environ`** — the CLI spawns MCP servers itself, so what it inherits third-party code does too. Exempt: a name an ACTIVATED def references as `${VAR}`, which Claude Code expands (`ClaudeCliEnvStripTests`).
- **A spawned server inherits the operator's env MINUS Otto's credentials** (`mcp_client._inherited_env`) — `run.sh` exports `.env` into the worker, so third-party code got the Slack tokens and `OTTO_SECRET_COMMAND`, the key to every other secret (`McpUserScopeSpawnTests`).
- **Servable/unservable is the whole design**: a stdio server (`command`+`args`) is a subprocess we spawn. A claude.ai *connector* (Gmail/Slack/Notion) is remote OAuth inside Claude Code's own session: nothing to spawn, no token. `servable()` refuses anything not stdio.
- **A cap needing a connector must not run locally** — `mcp_client.unservable(cap)` keeps it on Claude (strict mode stops instead). The Admin Execution dropdown disables local options for such a cap.
- **A connector blocks a local run by REQUEST, not only by declaration** (`mcp_client.connectors_named`) — the generalists declare no `tools:`, so the cap-side test is silent for the caps handed anything, and the model hunts credentials to hand-roll it (`LocalConnectorGapTests`).
- **What the local backend CANNOT reach is declared in-context** (`mcp_client.connector_note`) — outside the stdio branch: a connector is absent either way. 4/4 without it the model plans to read `~/.netrc`; 4/4 with it, it reports the blocker (`LocalConnectorGapTests`).
- **A Codex run is TOLD it has no MCP** (`mcp_client.no_mcp_note`, unconditional) — the routing guard keeps a DECLARED cap off it; this is the backstop for the request whose words missed. Wider gap: no MCP at all, but a shell and the network (`CodexWiringTests`).
- **Two bounds, not one**: which servers = `declared_servers` ∩ servable ∩ risk allowlist; how many tools = `Pool` ranks against the request, keeping at most `LOCAL_MCP_MAX_TOOLS` (25) — the full fleet is schema resent every turn, fatal on a small context window.
- **A declaration is `tools:` frontmatter ∪ Admin's per-cap `mcp`** (`POST /api/cap-mcp`) — frontmatter alone can't say it: it is the cap's COMPLETE grant on the Claude path, so naming a server there revokes Bash/Edit (`LocalMcpRegistryTests`).
- **UNDECLARED = the REQUEST picks the fleet, and a pointer can't** — "work on this ticket \<url\>" matched one stopword, so a New Relic task got 2 k8s tools and 0 of 38 NR ones. Hence `_STOP` + `lexicon.tokens` (`LocalMcpToolBudgetTests`).
- **`_rank(owner=)` takes from each server in TURN** — nothing scoring ties every tool, so a flat top-N cut drained them in order and the last server got none, invisibly (`LocalMcpToolBudgetTests`).
- Tool catalogue is cached (`data/mcp-tools.json`, keyed on a sha256 of the def — never builtin `hash()`, which is salted per process, so the cache reads back empty in every other one) so ranking needn't spawn every server.
- A server that can't start is negatively cached (`LOCAL_MCP_PROBE_TTL_S`) so one dead server doesn't force a cold-cache "spawn everything" fallback, and is declared in-context (not fatal). Kill switch `OTTO_LOCAL_MCP=0`; pool closed in `run_json`'s `finally`.
- **Still missing** (env, not code): the worker has no `AWS_*`/`aws-vault`, so `aws-mcp`/EKS auth as nobody on either backend.

## Tools + guardrails

`claude_cli.run_json` runs `claude -p`; per-risk allowlists in `config.py`; MCP via `policy.py`+`--mcp-config`.

- **`--allowedTools` grants permission but unloads nothing** — the whole built-in set plus every MCP server, skill and agent stays in the system prompt. `--disallowedTools` on the complement removes them (`config.DISALLOWED_TOOLS`, synced with `KEEP_TOOLS` by `ContextTrimTests`).
- **Never disallow `ToolSearch`** — deferred MCP schemas load through it (`ContextTrimTests`).
- **`--setting-sources user` for any run with no cwd of its own** (`engine._setting_sources`) — an unanchored run inherits the worker's cwd — Otto's own checkout — loading Otto's CLAUDE.md into unrelated runs. Repo-mode/project caps DO set a cwd and keep their repo's.
- **A cheap tier loads NO CLAUDE.md: `setting_sources=""`, which is not the same as omitting it** (that defaults to user+project+local). `"user"` pulled in the OPERATOR's global file, and "never restate something already known" then answered NONE (`CheapTierContextTests`).
- **`--strict-mcp-config` only on tool-free calls** (`gateway._claude_complete`) — `data/mcp-servers.json` is normally `{}`, so every server a cap uses is INHERITED; strict mode on an execution run strips them all.
- **`--effort` only WARNS on a bad value** — an unvalidated level runs at the DEFAULT effort while every layer reports the pick honoured, so BOTH backends normalize through `config.effort_level`; local's `reasoning_effort` is advisory, accepted and ignored (`EffortLevelTests`).
- `--tools` is NOT the lever — it loaded 0 tools and *doubled* context.
- **A path deny rule has exactly one working spelling: `Edit(//abs/**)` in `permissions.deny` via `--settings`** (`file_safety._rule`). `Write(...)`, a single-slash path, and the same rule on `--disallowedTools` each parse, raise nothing and block nothing.
- **A deny glob and the path a run writes are two names for one file** — `file_safety` resolves BOTH ends and emits both spellings, or a rule crossing a symlink (macOS `/tmp`) matches nothing on the LOCAL backend while `claude -p` still enforces it (`FileSafetySymlinkTests`).
- **A deny rule covers `rm` through Bash, not just writes** (measured against a control) — which is why `data/ESTOP` is on the list: deleting it releases the global pause, handing a run the operator's only stop lever.
- **`file_safety` is the write guard that needs no human** — the approval gate judges a plan, but `READ_TOOLS` has unscoped `Bash`. A matching deny beats an explicit allow and covers Bash redirection (`FileSafetyTests`).
- **Otto's own runtime state is READ-denied** (`file_safety.read_denied_globs`) — `otto.db`, `data/*.json` (plaintext keys), `transcripts/`. Exempt: cwd IS Otto's checkout. `data/workspaces/**` stays readable or repo-mode dies (`ReadDenyTests`).
- **The credential stores are READ-denied for EVERY run, Otto-cwd included** (`file_safety._secret_store_globs`) — editor history caches too: a copy of every file ever edited. `~/.aws/credentials` stays readable by decision (`ReadDenyTests`).
- **A `Read(//path/**)` deny covers `cat` through Bash** (measured) — but local Bash is not, so `local_runtime` guards Read and filters Grep's OUTPUT, never its root: the denied set is files under `data/`, never `data/` itself.
- **The LOCAL backend bypasses `claude -p`'s permission system entirely**, so `local_runtime._deny_guard` re-enforces the same list on its own Write/Edit. Its `Bash` is NOT covered — parsing a shell to catch `tee`/`sed -i` is protection theatre.
- **The local PLAN pass reproduces plan mode in TWO layers** — a `bwrap` read-only root with a scratch tmpfs on /tmp (shell stays WHOLE), else `bash_refusal`'s argv allowlist. Measured: 78% of a Claude planner's Bash is composed, so argv-only is far poorer (`LocalPlanModeTests`).
- **The sandbox is PROBED, never assumed from PATH** — `bwrap` installs fine where userns is off; the probe must RUN a command and see a write refused, or a broken sandbox is trusted. `--tmpfs /tmp` masks a cwd under /tmp: re-bind it read-only after (`LocalPlanModeTests`).
- **The sandbox enforces the READ deny-set too** (`_read_deny_mounts`) — it confines writes by construction, so `Read data/models.json` was refused while `cat` returned the API keys, to a THIRD-PARTY endpoint. Honours the cwd-is-Otto exemption (`LocalPlanModeTests`).
- **The deny-set mounts and the bwrap probe live in `file_safety`** (`read_deny_mounts`, `sandbox_available`) — both runtimes confine with them; a second copy drifts from the globs it derives from, invisibly, until something reads a key (`CodexWriteGuardTests`).
- **Codex's own sandbox is a WRITE allowlist with NO read deny** — `sandbox_read_only.*`/`sandbox_deny_read` are rejected as unknown fields, and a run under `-s read-only` read `data/models.json` off disk. `bwrap` is the guard instead (`CodexWriteGuardTests`).
- **The two sandboxes are ALTERNATIVES, never layers** — inside `bwrap` Codex's confinement cannot initialise and EVERY shell command fails, so it runs `--dangerously-bypass-approvals-and-sandbox` there and nowhere else (`CodexWriteGuardTests`).
- **Without a usable `bwrap` the Codex backend WALLS, it does not degrade** — measured with a control: a canary in a read-denied `data/*.json` leaked 0 times under `bwrap`, twice without. Hatch `OTTO_CODEX_ALLOW_UNGUARDED`, env-only (`CodexWriteGuardTests`).
- **Neither layer bounds the NETWORK** — reading the ticket is why the planner has Bash, and plan mode permits read-only network too. `gh api -X POST` is refused by the allowlist, NOT by the sandbox; the approval gate still stands there (`LocalPlanModeTests`).
- **Every REGISTERED repo's live checkout is write-denied by default** (`file_safety.denied_globs`) — a cap with no cwd of its own could otherwise reach sideways into any of them and edit in place, which the in-place-edit guard only ever detected after the fact.
- `allow_cwd` is the one exemption, threaded from `claude_cli.run_json`'s/`local_runtime`'s own `cwd`: a project capability's OWN repo, never a sibling's.

## Execution transcripts

**`system_context` is recorded beside `prompt` in the meta line** — the approved plan, the mismatch note, the output contract and recalled memory all travel that argument, so without it a transcript cannot say what the model was told (`SystemContextTranscriptTests`).

- **Every transcript line is scrubbed as it is written** (`claude_cli.transcript_line`, the ONE writer for both backends) — a run handling a credential otherwise leaves it in plaintext for the whole TTL; forensics needs the shape, never the bytes (`RedactTests`).

Both backends append `data/transcripts/<wid>-a<attempt>.jsonl` (TTL `TRANSCRIPT_TTL_H`). Live: `/api/progress`; full: `/api/run/detail`.

## The trace log

`ui.trace` writes the console AND `data/logs/<stream>-<date>.log` (TTL `TRACE_LOG_TTL_H`) — the richest debug stream Otto has, and the only one that outlives a restart.

- **It is a durable sink, so it is SCRUBBED and both READ- and WRITE-denied like a transcript** — `data/*.log` reaches no subdirectory, and read-deny alone let a run `rm` the only record of itself (`TraceLogTests`, `ReadDenyTests`).
- **Appended, never truncated, and rolled on a BYTE COUNTER** — `run.sh`'s `>` wiped it on every restart, and a day stamp bounds nothing on a service that is never restarted (`TraceLogTests`).
- **A rolled name must be unique** — `os.replace` DELETES its target, and two rolls in one second left 2 files of 5 (`TraceLogTests`).
- **The wid comes free from `activity.info()`** (measured inside a real sync activity); `ui.set_run` is for everything that is not one (`TraceLogTests`).
- **Anything Otto SPAWNS must carry the context over** (`contextvars.copy_context`, per submit) — no ContextVar crosses a thread, ours or Temporal's, so the supervisor watcher and every plan wave step traced anonymously (`TraceRunAttributionTests`).
- **`run.sh`'s `>>` removed the only bound the raw stdio logs had** — it rolls them itself before opening each redirect; an open fd cannot be rotated from a shell (`TraceLogWiringTests`).
