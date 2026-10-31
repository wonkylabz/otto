# Using Otto

The long-form half of the README: how the pieces behave once it is running. `operating.md`
covers the service itself; `CLAUDE.md` is the guide for editing the code.

## Using it

- Type a request. **Reads auto-run; writes preview a plan and pause for approval; ambiguous
  requests ask first.** `/<capability> …` pins one and skips routing.
- **Multi-turn**: after an agent runs, the conversation stays in its session — your next
  message continues the *same* agent (e.g. answering a question it asked), instead of
  re-routing. A follow-up that hands over a genuinely new task re-enters the full pipeline.
  Click **New task** to start fresh.
- **Code changes run in an isolated clone**, never your live checkout — see
  [Working on repos](#working-on-repos).

Runs go through real Temporal workflows — durable and replayable, with approval and
clarification as Temporal **signals**. Watch them live in the Temporal UI at
http://localhost:8233. Temporal is required: `server.py` refuses to start without it.

### Logging in

`./run.sh login` prints a single-use link (default port 8765) — open it once and the browser
keeps a cookie for a year. A browser that can't open it (a phone over a tunnel) can paste
`data/.api/token` into the login screen instead.

### Setup help

`./install.sh --guided` runs `setup_wizard.py`, which walks what `python3 doctor.py` reports
as unconfigured and offers a fix for each: register a project repo, generate the event-ingress
key and the ntfy topic, paste a Slack token, `gh auth login`. It never overwrites a value you
already set, never echoes a secret, and no-ops without a terminal. Run it any time:
`./.venv/bin/python setup_wizard.py`.

The `otto-setup` skill (`.claude/skills/otto-setup/`) does the same from inside Claude Code:
open it in the checkout and say *"set up Otto"*.

## From your phone

The web UI works on a phone: below 680px wide the tabs become a bottom bar (Chat, Board,
Jobs, Audit, and **More** for the rest), and plan approvals open as a bottom sheet. Otto
only listens on `127.0.0.1`, so reach it through a private tunnel. With
[Tailscale](https://tailscale.com):

1. On the Otto machine: `sudo tailscale serve --bg <port>` (`8765` by default). On first
   use it prints a link to enable Serve (and HTTPS) on your tailnet — open it, then re-run.
2. Add the URL it prints to `.env`, then restart Otto. Without it every button in the UI
   is refused as a cross-site request:

   ```bash
   OTTO_ALLOWED_ORIGINS=https://<machine>.<tailnet>.ts.net
   OTTO_CLICK_URL=https://<machine>.<tailnet>.ts.net   # push notifications open here too
   ```

3. On the phone: install Tailscale, sign in to the same tailnet, open that URL, and paste
   `data/.api/token` into the login screen.

The tunnel stays private to your tailnet. Keep the Otto machine from sleeping while you're
away — runs stop with it.

## Gotchas

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

## Ingress details

| | What it is |
|---|---|
| **Web chat** | the front door; the only interactive one |
| **Jobs** | runbooks on a Temporal Schedule — a saved request, optionally with prose that *is* its approved plan, or a human-authored dependency graph; a runbook with a Slack destination posts its result there (a channel id, `#name`, or a user id to DM) |
| **Webhooks** | `POST /api/events/<cap>`, HMAC-signed over body **and** timestamp, replay-protected |
| **GitHub board** | a Projects v2 board as a queue — moving a card to **Ready** is the approval; the result is commented back, and a card moved back to Ready runs again; [Factory mode](#factory-mode) drives it on its own |
| **Slack** | DMs and @-mentions, under your own account, a bot user, or both — see [slack.md](slack.md) |

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
  on Claude. Adding a hosted endpoint: see [openai-models.md](openai-models.md).

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

## Factory mode

Admin → Mode → **Start factory** points one registered repo and the GitHub board at a
self-driving build loop, seeded with an idea — or, for a repo that already has code, with what
to finish or add:

```
PM sprint ─▶ tickets in Ready ─▶ worker builds ─▶ draft PR ─▶ review + QA ─▶ auto-merge ─▶ Done
    ▲                                                                                      │
    └──────────── playtest of main ◀──────────── queue drained ◀──────────────────────────┘
```

- **It is unsafe by design, and the switch is the approval.** Tickets on that repo run without
  a gate, and a PR that passes review **and** QA merges itself. Every other ingress stays
  Manual, the header shows a FACTORY chip on every tab, and the global Pause still stops it.
- **The seed becomes `GOALS.md`** (product, commands, Definition of Done). An empty repo's
  first ticket scaffolds the project; a repo with code gets an assessment ticket instead, which
  describes what exists and adopts the repo's own run/test/lint commands — never a second
  toolchain. The PM writes a `SPEC.md` first — domain rules cited from an authoritative source,
  legal and illegal cases, and a UX contract — and every later ticket cites the rules it
  implements, with a test per rule.
- **Each sprint after a merge starts with a playtest**: QA uses `main` as a real user (headless
  browser, screenshots) and its defects become the next tickets. The factory only declares
  itself done once the current `main` playtests clean.
- **One ticket at a time** by default (WIP), so each builds on the last merge; a Blocked or
  stranded card stops the line until the next sprint's PM rewrites or drops it.
- **It stops itself, loudly**: on its budget, after N sprints merging nothing, or on DONE — each
  with a push notification. A spent Claude usage limit *pauses* it instead, and it resumes on
  its own once Claude answers again.
- **MCP servers are an allowlist**: factory runs get only the servers you tick in its form, so
  your mail, chat and home servers stay out of an unattended, pre-approved loop.
- Back to **Manual** restores the board settings Factory changed; runs already going finish.

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

`python3 profile.py export` (or Admin → Share extensions) snapshots this install's whole config
for another machine, secret-free; `import` previews every change first, and ingresses and crons
arrive switched off.

### Secrets

Otto's secrets — the Slack user token, the event-ingress HMAC key, the ntfy topic, any local
endpoint key — default to plaintext in `.env` (mode 600). To keep them in a password manager
instead, set one helper command and Otto resolves each name through it:

```bash
OTTO_SECRET_COMMAND='pass show otto/{name}'     # or: op read, bw get, keepassxc-cli, gpg -d
```

Resolution is env → helper → unset, so a value in `.env` still wins. This one is env-only and
never settable from the UI: the web API has one all-powerful credential, and an arbitrary
command writable over HTTP would turn that token into code execution. `python3 doctor.py`
reports whether the helper actually resolves anything — every way it can fail reads as "unset".

## Tests

```bash
./.venv/bin/python -m unittest -v      # after ./install.sh
```

Covers the pure logic (cron, risk classification, routing-invocation, gateway resolution) plus
the Temporal workflow path — no Claude calls or network. Use the venv interpreter: under a bare
`python3` the Temporal tests self-skip and the suite still reports `OK`. [testing.md](testing.md)
has the regression corpus and how to write a guard test.
