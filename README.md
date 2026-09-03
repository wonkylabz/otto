<img src="docs/otto-mark.svg" width="72" align="right" alt="">

# Otto

A local agentic orchestrator. You ask in a chat UI; Otto routes your request to one
of **your real Claude Code subagents/skills**, asks a clarifying question if something's
missing, pauses for your approval before anything that writes, runs it via `claude -p`,
**verifies the result and retries (escalating the model) if it falls short**, and records
every attempt. Execution is durable, via **Temporal**.

```
  you ─▶ INGRESS ─▶ ROUTER ─▶ CLARIFY ─▶ GATE ─▶ RUN ⇄ VERIFY ─▶ AUDIT
   (chat UI)   (pick agent)  (ask if    (approve (claude -p) (judge+   (memory +
                              unclear)   writes)             retry×N)  audit log)
```

The **RUN ⇄ VERIFY** loop is what lets Otto catch the executor's own mistakes: after
each run Claude judges whether the request was actually fulfilled, feeds a critique back
into the next attempt, and on the final attempt escalates to the strongest model before
giving up (bounded by `OTTO_MAX_ATTEMPTS`, default 3).

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

## Quick start — default (Temporal)

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

## Using it

- Type a request. **Reads auto-run; writes pause for approval; ambiguous requests ask first.**
- **Multi-turn**: after an agent runs, the conversation stays in its session — your next
  message continues the *same* agent (e.g. answering a question it asked), instead of
  re-routing. Click **New task** to start fresh. (Temporal mode only.)
- Tabs:
  - **Chat** — the front door.
  - **Memory** — distilled facts Otto *learned* from past runs, injected as context into
    the next run (so it actually remembers). Distinct from Audit; clearable.
  - **Audit** — immutable record of every action, including declined writes.
  - **Schedules** — run a request on a cron schedule.
  - **Admin** — capabilities, MCP servers, and the LLM model per phase.

## How it works (file ≈ layer)

| Layer | File(s) | Notes |
|---|---|---|
| Ingress | `web/index.html` + `server.py` | chat UI + HTTP / Temporal client |
| Async work queue | `board.py` | a GitHub Projects "Ready" column, polled → unattended runs → result commented back |
| Orchestration | `workflows.py`, `worker.py` | the durable Temporal workflow (the only run path) |
| Router #1 (which agent) | `engine.route` | an LLM call via the gateway |
| Agents / skills | `registry.py` | discovered from `~/.claude/{agents,skills}`; custom ones in `data/capabilities.json` |
| Model gateway (Router #2) | `gateway.py` | routing/clarify can use a local model; execution follows the picked model — Claude runs `claude -p`, a local model runs the local agent runtime |
| Local agent runtime | `local_runtime.py` | OpenAI tool-calling loop for local execution models: real tools, inlined skill/agent instructions, no Claude at all (no MCP; local-only retries) |
| Run supervisor (shadow) | `supervisor.py` | cheap mid-run checkpoints over the live stream; records what it *would* do, never touches the run |
| Tools + guardrails | `claude_cli.py`, `config.py`, `policy.py` | `claude -p` with per-risk allowed tools + MCP servers |
| Memory + audit | `engine.py` → `data/` | `memory.json` (clearable) and `otto.db`'s `audit` table (immutable) |
| Scheduler | `scheduler.py` | 5-field cron, fires through the server |

## Safety

- Every capability is classified **read** or **write**. Reads run on their own; **writes
  require explicit approval** (the human-in-the-loop, a real Temporal signal).
- **Scheduled** writes are skipped unless a job opts into auto-approve — nothing mutating
  runs unattended by accident.
- The **audit log is immutable** — "clear memory" never touches it.

## Tests

```bash
./.venv/bin/python -m unittest -v      # after ./install.sh
```

Covers the pure logic (cron, risk classification, routing-invocation, gateway resolution) plus
the Temporal workflow path — no Claude calls or network. Use the venv interpreter: under a bare
`python3` the Temporal tests self-skip and the suite still reports `OK`.

## Configuration

All editable from the **Admin** tab and persisted under `data/` (which is git-ignored —
it's local runtime state):

- capability risk + enable/disable + custom capabilities,
- which MCP servers runs may use,
- which model handles routing / clarification / execution,
- the **GitHub board queue** (`data/board.json`): point Otto at a GitHub Projects board and it
  picks up issues parked in the **Ready** column, runs them, and comments the result back (moving
  the card to Review/Done). Moving a card to Ready is the approval; needs Temporal + `gh`.

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

## Slack auto-answer

Otto can answer Slack DMs and @-mentions on your behalf: it polls Slack **as you** (a user
OAuth token), acks in-thread, runs the request as an unattended workflow, and posts the result
back to the same thread. Needs Temporal. Replies are sent **as you** — a user token can't
render under a different name (see the caveat below).

### 1. Create the Slack app

- https://api.slack.com/apps → **Create New App** → **From scratch**
- Name it (e.g. `Otto`) and pick the target workspace — the app is bound to it
- If the workspace requires admin approval for apps, request it before installing

### 2. Add **User Token Scopes**

Under **OAuth & Permissions → Scopes**, add these as *User* Token Scopes, **not** Bot Token
Scopes — Otto reads your own DMs, which a bot token can never see:

| Scope | Why |
|---|---|
| `im:read` | list your DM conversations |
| `im:history` | read DM messages |
| `mpim:history` | group DMs |
| `channels:history` | public channels you're in |
| `groups:history` | private channels you're in |
| `search:read` | `search.messages`, how @-mentions are found |
| `chat:write` | post the ack + the result |

No bot user, no Event Subscriptions, no Socket Mode, no app-distribution review — inbound is a
Web-API poll on a Temporal Schedule (a user token has no event stream).

### 3. Install and wire up the token

- **Install to Workspace** → authorize as yourself → copy the **User OAuth Token** (`xoxp-…`)
- Add it to `.env`: `OTTO_SLACK_USER_TOKEN=xoxp-…` — never to anything under `data/` (the web
  UI has no auth)
- **Restart the service** (`systemctl --user restart otto`): the token is read at import time,
  so a running worker keeps the old one
- Verify: `curl -s -H "Authorization: Bearer $OTTO_SLACK_USER_TOKEN" https://slack.com/api/auth.test`
  → your `user_id` + the expected `team`

### 4. Configure it (Events tab → `data/slack.json`)

- `enabled: true`
- `allow_users` — Slack member IDs (profile → **⋮** → *Copy member ID*)
- `allow_channels` — channel IDs (channel → *Copy link*, the `C…` part). Otto must be a member
  of the channel to read its history
- IDs are opaque, so **either list may be labelled**: `U01ABCDE2FG  #alex`, one per line. The
  label is stored as typed and stripped wherever the ID is compared; a `#comment`-only line is
  ignored (and doesn't count as an entry for the "enabled needs an allowlist" check)
- **The allowlists are the gate, and they're OR'd**: a message qualifies if its *author* is
  listed **or** its *channel* is — so allowlisting a channel allows everyone in it.
  **Both empty means nobody**, which is the safe default, not "everyone".
- `allow_self` — test mode: implicitly allows the token owner, so a solo self-DM triggers a run
  without listing your own ID. Turn it **off** on a real workspace.
- `approval_default: "ask"` keeps writes pausing on the Needs-you board; reads auto-answer
- `ack_template` (posted when a run starts), `greeting_template` (the reply to a message with no
  request in it — "hi", "thanks" — which never starts a run), `watch_dms`, `watch_mentions`,
  `poll_seconds`, `max_per_poll` to taste

### Threads: the other person can carry the conversation on

Otto's ack and its answer are posted **in a thread** under the message that triggered them. Every
thread Otto has replied in is then **watched**: a new reply in it *continues the same conversation*
rather than starting a cold one — the follow-up resumes that run's Claude session, so "and the other
one?" or "no, I meant staging" work without repeating the context.

- Only the thread is continuable. A new *top-level* message (in the channel or DM, outside the
  thread) is a new task, with a new session — that's the deliberate signal for "different subject".
- The follow-up ack is short (`On it — let me check…`): the introduction only happens once.
- A `thanks!` in a thread is answered with silence, not with the greeting again.
- A follow-up that turns the conversation into a **write** ("just restart it then") is re-classified
  and pauses on the Needs-you board for you, even though nobody is watching — someone else's words
  never get auto-approved.
- One turn at a time: a reply that lands while the previous run is still working waits for it, then
  runs. If that run dies without answering, the thread frees itself after 30 minutes.
- A thread goes cold after `OTTO_SLACK_THREAD_TTL_H` (default 336 = 14 days) of no activity; a reply
  after that starts fresh. At most 200 threads are tracked.
- Threads Otto answered *before* this feature existed aren't watched — they were never recorded.

### Caveats

- **Replies post as you, not as "Otto".** `chat.postMessage` on a user token always renders as
  the token owner; the `username`/`icon_emoji` overrides are classic-bot-only and ignored. A bot
  token could post under its own identity in *channels*, but never inside a DM between you and
  someone else — so the ack text (`ack_template`) is what does the attribution.
- Slack text is **untrusted input**; it's framed as task data and the write gate stays the real
  guard. Keep the allowlists tight.
- Channel @-mention detection goes through Slack search, which is fuzzy — DMs are the robust path.

### What the other person can and can't see

A colleague's message starts a normal run, so the capability answering them has your memory, your
knowledge base and real tools. Three things bound what comes back out:

- The reply contract tells it to **answer the question and nothing more** — use the injected
  context to ground the answer, not to recite it or volunteer what else it noticed — and never to
  hand over a credential, whatever the reason given. It can say *where* a secret lives
  ("it's in AWS Secrets Manager under `registry/prod`"), never what it is.
- The verifier fails a reply that discloses more than the question needed, so the retry ladder
  gets a chance to tighten it before anything is posted.
- Both of those are instructions to a model. The actual guard is `privacy.py`: every outbound
  Slack message is scrubbed of credential-shaped strings (tokens, keys, private-key blocks,
  `user:pass@host` URLs, `password=…`) on the way out, whatever the model decided to write. The
  same scrub covers GitHub comments and webhook deliveries.

This is a backstop, not an authorization system — anyone on the allowlist can ask Otto to do
things with your access. Keep the list to people you'd hand your terminal to.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — setup, the venv-interpreter test rule, the
regression corpus, and the two ratchets that will fail your first PR if nobody warns you.

## License

[MIT](LICENSE).
