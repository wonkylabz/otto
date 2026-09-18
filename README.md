<img src="docs/otto-mark.svg" width="72" align="right" alt="">

# Otto

A local agentic orchestrator. You ask; Otto routes your request to one of **your real Claude
Code subagents/skills**, asks a clarifying question if something's missing, shows you a plan
and pauses for approval before anything that writes, runs it, **verifies the result and
retries (escalating the model) if it falls short**, and records every attempt. Execution is
durable, via **Temporal**.

```
  you ─▶ INGRESS ─▶ ROUTER ─▶ CLARIFY ─▶ PLAN ─▶ GATE ─▶ RUN ⇄ VERIFY ─▶ AUDIT
   (chat, Slack,  (pick agent)  (ask if   (preview  (approve  (judge +   (memory +
    cron, board,                unclear)   the work)  writes)   retry×N)   audit log)
    webhook, PR)
```

The **RUN ⇄ VERIFY** loop is what lets Otto catch the executor's own mistakes: after each run
a judge decides whether the request was actually fulfilled, feeds a critique back into the
next attempt, and on the final attempt escalates to the strongest model before giving up
(bounded by `OTTO_MAX_ATTEMPTS`, default 3). Nothing fails silently — an exhausted ladder,
a blown budget or a stuck run lands on the **Needs-you** board rather than vanishing.

> [!WARNING]
> **Otto runs as you, and its web UI has no authentication.** It executes an LLM's decisions
> on your machine with your Claude subscription, your `~/.claude` config and your tools —
> and `server.py` has no login, no token and no session. Anything that can reach the port
> can start a run and approve its own write gate. It is built for one operator on their own
> workstation: keep it on `localhost`, don't reverse-proxy it, don't run it on a shared box.
> [SECURITY.md](SECURITY.md) has the full threat model. Read it before you start.

## Prerequisites

- **Claude Code**, logged in. Otto runs on your Claude **subscription** via `claude -p` —
  **no API key required**. (`ANTHROPIC_API_KEY` is optional; only used to auto-discover the
  cloud model list.)
- **Python 3.12+**.
- For the default (Temporal) mode: the **Temporal CLI** and a **venv** with `temporalio`.
- Optional, per backend: a local OpenAI-compatible endpoint (Ollama, vLLM, LM Studio) for the
  local runtime; `codex` + `bwrap` for the Codex backend. Both are opt-in — see
  [Backends](#backends).

## Quick start

Unattended install (venv + deps + Temporal CLI + `.env` + smoke tests, plus a
background service so Otto runs unattended — a systemd `--user` unit on Linux, a
launchd LaunchAgent on macOS; both run as you, not root):

```bash
./install.sh                # add --no-service to skip the background service
./install.sh --guided       # …and then walk the setup interactively
```

Re-running it is safe (idempotent) — e.g. after `git pull`, or to recover a `.venv`
broken by a system Python upgrade. See `./install.sh --help` for details.

`--guided` runs `setup_wizard.py`, which walks what `python3 doctor.py` reports as
unconfigured and offers a fix for each: register a project repo, generate the event-ingress
key and the ntfy topic, paste a Slack token, `gh auth login`. It never overwrites a value
you already set, never echoes a secret, and no-ops without a terminal — so piping the
installer stays unattended. Run it any time: `./.venv/bin/python setup_wizard.py`.

Without the installer, the equivalent manual one-time setup is:

```bash
sudo apt install -y python3-venv                  # venv support (Debian/Ubuntu; macOS ships it)
curl -sSf https://temporal.download/cli.sh | sh -s -- --version 1.8.0   # Temporal CLI -> ~/.temporalio/bin
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Otto runs on Linux and macOS. To (re)install just the background service later:
`systemd/install.sh` on Linux, `launchd/install.sh` on macOS.

Run:

```bash
./run.sh        # starts the Temporal dev server + worker + web UI
```

Open the URL it prints (default http://localhost:8765). Runs go through real Temporal
workflows — durable and replayable, with approval + clarification as real Temporal
**signals**. Watch them live in the Temporal UI at http://localhost:8233.

Temporal is required — `server.py` refuses to start without it (the old non-durable
"direct" path was removed; every run now gets the same durability, gates, and verification).

`docs/operating.md` covers the service, restart discipline and the global pause.

## Using it

- Type a request. **Reads auto-run; writes preview a plan and pause for approval; ambiguous
  requests ask first.** `/<capability> …` pins one and skips routing.
- **Multi-turn**: after an agent runs, the conversation stays in its session — your next
  message continues the *same* agent (e.g. answering a question it asked), instead of
  re-routing. A follow-up that hands over a genuinely new task re-enters the full pipeline.
  Click **New task** to start fresh.
- **Code changes run in an isolated clone**, never your live checkout — see
  [Working on repos](#working-on-repos).
- Tabs:
  - **Chat** — the front door, and the Needs-you board for anything parked.
  - **Jobs** — runbooks: a saved request, on a cron schedule or on demand.
  - **Events** — the webhook ingress, Slack, and GitHub PR reviews.
  - **Board** — live and recent runs, with the stage, model and cost of each.
  - **Memory** — facts Otto *learned* from past runs, injected as context into the next one.
    Distinct from Audit; clearable, and individually forgettable.
  - **Knowledge** — documents you add, chunked and embedded for retrieval.
  - **Audit** — immutable record of every action, including declined writes.
  - **Admin** — capabilities, backends and per-phase models, MCP servers, project repos,
    runtime settings, memory GC.

### Gotchas

- **Pin a capability on any runbook or schedule that is one deliverable.** With nothing
  pinned, a fresh request goes through the swarm planner first, which may fan it out into
  parallel sub-tasks. On a weak planner model that split can go wrong in a way nothing
  downstream catches: it reads the *capability list* back as its plan, and because it
  renumbers its own lines, each child ends up running a **different** capability from the
  work it was handed. The tell is children whose reported outcome has nothing to do with
  your request. A pinned capability skips the planner entirely.
- **A runbook's `auto_approve` means its writes never pause.** Combined with the above, a
  mis-split runbook can write in three places you didn't ask about, so pin the cap before
  you tick it.

## Ingress — five ways in

All five normalize into the same workflow, with the same gates, verification and audit. The
split that matters is **interactive** (can clarify, waits for your approval) vs **unattended**
(delivers the result to wherever it came from).

| | What it is |
|---|---|
| **Web chat** | the front door; the only interactive one |
| **Jobs** | runbooks on a Temporal Schedule — a saved request, optionally with prose that *is* its approved plan, or a human-authored dependency graph |
| **Webhooks** | `POST /api/events/<cap>`, HMAC-signed over body **and** timestamp, replay-protected |
| **GitHub board** | a Projects v2 board as a queue — moving a card to **Ready** is the approval; the result is commented back |
| **Slack** | DMs and @-mentions, under your own account, a bot user, or both — see [docs/slack.md](docs/slack.md) |

GitHub also has a **pull** half: `pr_review.py` polls PRs where you're a requested reviewer,
runs the stock read-only reviewer, and parks the review for you to post — or posts it itself
if you switch `auto_post` on.

## Backends

Which runtime executes a run follows the **execution model** you pick per capability (Admin →
Execution). There are three, and they are not interchangeable:

| | Runs | Auth | MCP | Notes |
|---|---|---|---|---|
| **Claude** (default) | `claude -p` | your Claude subscription | yes, incl. claude.ai connectors | the reference path; everything works |
| **Local** | `local_runtime.py`, an OpenAI-style tool-calling loop | your endpoint's key, if any | stdio servers only, ranked and capped | any OpenAI-compatible endpoint; no connectors |
| **Codex** | `codex exec` | `codex login`, or an endpoint | none | needs `bwrap`; walls rather than run unguarded |

- **Claude fallback is on by default** (`OTTO_LOCAL_FALLBACK`): a local or Codex model that
  can't serve the run is covered by Claude rather than failing it. Set it to `0` for strict
  mode, where that substitution is illegal and the run stops instead.
- **Judging is exempt from strict mode** — the verifier has to be able to catch a bad local
  execution rather than go down with it.
- **Codex needs a working `bwrap`** (`npm i -g @openai/codex`; set `OTTO_CODEX_BIN` to an
  absolute path if you use asdf/mise/nvm — a shim resolves from the current directory, and
  every run has a cwd of its own). Codex's own sandbox cannot deny a *read*, so without
  `bwrap` the backend refuses to run rather than expose Otto's key store. `python3 doctor.py`
  checks both, and stays silent if nothing selects Codex.
- Routing, clarification, planning, judging and memory GC each have their **own** model tier,
  set independently in Admin — a cheap local model can do the plumbing while execution stays
  on Claude.

## Working on repos

A request that changes code runs against an **isolated shallow clone**, never your live
checkout. Otto pushes a branch, opens a draft PR, and tears the clone down after.

- **Code review is on by default** for every repo-mode PR (a PR is a PR whoever wrote it) —
  the stock reviewer's must-fix findings are folded into a fix on the same branch and
  re-reviewed, bounded by `OTTO_MAX_REVIEW_ROUNDS` (3).
- **QA is opt-in** and runs after review — a failed round re-provisions, re-runs and re-QAs,
  bounded by `OTTO_MAX_QA_ROUNDS` (2).
- **The approved plan is posted to the PR as a comment**, so the reviewer sees what was
  approved and not just the diff.
- A request naming one of **your own** open PRs branches off that PR's head. A colleague's PR
  is never a target.
- Registered checkouts are refreshed with `git fetch`, never `git pull` — that tree is yours
  and is routinely dirty.

## How it works (file ≈ layer)

| Layer | File(s) | Notes |
|---|---|---|
| Ingress | `server.py`, `web/`, `slack*.py`, `board.py`, `pr_review.py`, `events.py`, `runbooks.py`, `scheduler.py` | the five doors, normalizing into one workflow |
| Orchestration | `workflows.py` + `wf_*.py`, `worker.py`, `activities.py` | the durable Temporal workflow (the only run path) |
| Router #1 (which agent) | `routing.py`, `registry.py`, `intents.py` | shortlist by retrieval, then an LLM call |
| Agents / skills | `registry.py`, `capabilities/` | discovered from `~/.claude/{agents,skills}`, plugins, and other repos' `.claude/` |
| Router #2 (which model) | `gateway.py` | tier → model → backend; health, cost and walls |
| Backends | `claude_cli.py`, `local_runtime.py`, `codex_cli.py` | `claude -p`, the local tool-calling loop, `codex exec` |
| Plan gate | `plans.py`, `conventions.py` | a read-only preview pass, critiqued, then approved or revised |
| Verify / judge | `judging.py`, `contracts.py` | pass/fail + critique, re-sampled before anything adverse is acted on |
| Run supervisor | `supervisor.py` | cheap mid-run checkpoints over the live stream; shadow by default, can steer or kill |
| Repo work | `workspace.py`, `repos.py`, `chats.py` | isolated clones, branches, draft PRs, review/QA loops |
| Guardrails | `policy.py`, `file_safety.py`, `mcp_client.py`, `privacy.py` | per-risk tool grants, path deny rules, MCP gating, egress scrubbing |
| Memory + audit | `memory.py`, `knowledge.py`, `audit.py` → `data/otto.db` | facts, solutions, behaviors, embedded docs, and an immutable audit trail |

`CLAUDE.md` and `.claude/rules/*.md` are the maintainer's guide to editing each layer.

## Safety

- Every capability is classified **read** or **write**. Reads run on their own; **writes
  preview a plan and require explicit approval** (a real Temporal signal). The gate wait is
  bounded — expiry *declines*, it never approves.
- **Scheduled** writes are skipped unless a job opts into auto-approve — nothing mutating
  runs unattended by accident.
- **Path deny rules** are enforced independently of the gate: Otto's own runtime state, your
  credential stores and every registered checkout are write- or read-denied, through Bash as
  well as through the edit tools.
- **A global pause** (`data/ESTOP`, or the header control) stops every ingress starting new
  work. It never kills what's already running.
- **Per-run cost budgets** (`OTTO_BUDGET_SOFT_*` / `OTTO_BUDGET_HARD_*`, 0 = off) downshift
  the model, then stop the run and ask for a human.
- **Everything leaving the box is scrubbed** of credential-shaped strings — Slack, ntfy,
  GitHub comments, webhooks.
- The **audit log is immutable** — "clear memory" never touches it.

## Tests

```bash
./.venv/bin/python -m unittest -v      # after ./install.sh
```

Covers the pure logic (cron, risk classification, routing-invocation, gateway resolution) plus
the Temporal workflow path — no Claude calls or network. Use the venv interpreter: under a bare
`python3` the Temporal tests self-skip and the suite still reports `OK`. `docs/testing.md` has
the regression corpus and how to write a guard test.

## Configuration

All editable from the **Admin** tab and persisted under `data/` (which is git-ignored —
it's local runtime state):

- capability risk + enable/disable + custom capabilities,
- the execution backend and per-phase model,
- which MCP servers runs may use,
- project repos (by URL or by path) and their conventions,
- runtime settings — knobs that take effect without a restart; env still wins,
- the **GitHub board queue** (`data/board.json`): point Otto at a GitHub Projects board and it
  picks up issues parked in the **Ready** column, runs them, and comments the result back (moving
  the card to Review/Done). Moving a card to Ready is the approval; needs Temporal + `gh`.

`python3 profile.py export` packs your capabilities and settings for another machine.

### Secrets

Otto's secrets — the Slack user token, the event-ingress HMAC key, the ntfy topic, any local
endpoint key — default to plaintext in `.env` (mode 600). To keep them in a password manager
instead, set one helper command and Otto resolves each name through it:

```bash
OTTO_SECRET_COMMAND='pass show otto/{name}'     # or: op read, bw get, keepassxc-cli, gpg -d
```

Resolution is env → helper → unset, so a value in `.env` still wins. This one is env-only and
never settable from the UI: the web API is unauthenticated by design, and an arbitrary command
writable over HTTP would be an arbitrary command an attacker can write. `python3 doctor.py`
reports whether the helper actually resolves anything — every way it can fail reads as "unset".

## Documentation

- [docs/operating.md](docs/operating.md) — setup, the background service, restart discipline,
  the global pause.
- [docs/slack.md](docs/slack.md) — both Slack identities, scope by scope.
- [docs/testing.md](docs/testing.md) — the suite, the regression corpus, guard tests.
- [SECURITY.md](SECURITY.md) — the threat model. Read it before exposing anything.
- `CLAUDE.md` + `.claude/rules/` — the maintainer's guide, one file per layer.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — setup, the venv-interpreter test rule, the
regression corpus, and the two ratchets that will fail your first PR if nobody warns you.

## License

[MIT](LICENSE).
