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

## Slack

Otto listens on Slack under **two identities**, independently switchable, both running the same
unattended workflow with the same guarantees. Needs Temporal.

| | **Auto-answer** (user token) | **Bot user** (bot token) |
|---|---|---|
| Token | `OTTO_SLACK_USER_TOKEN`, `xoxp-…` | `OTTO_SLACK_BOT_TOKEN`, `xoxb-…` |
| Reads | **your** DMs, @-mentions of **you** | DMs sent **to the bot**, @-mentions of the bot |
| Replies as | **you** | **the bot**, under its own name |
| Voice | your assistant, while you're unavailable | Otto, speaking for itself |
| Allowlist | `allow_users` / `allow_channels` | `bot_allow_users` / `bot_allow_channels` |

They run on one poll and one set of options (`poll_seconds`, `approval_default`, `cap`), but
nothing else is shared: separate tokens, separate allowlists, separate read cursors, separate
sessions. Turning one off leaves the other listening. Run both, either, or neither.

If you only want one: **auto-answer** is the one that can read your own DMs (a bot token never
can); **bot user** is the one that can post under a name other than yours.

## Auto-answer setup (answering as you)

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

No Event Subscriptions, no Socket Mode, no app-distribution review — inbound is a Web-API poll on
a Temporal Schedule (a user token has no event stream). Add a bot user to the *same* app if you
also want the bot identity, below.

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

## Bot-user setup (answering as Otto)

Same Slack app, second token. Skip this if you only want auto-answer.

### 1. Add a bot user and its scopes

Under **OAuth & Permissions → Scopes**, add these as *Bot* Token Scopes:

| Scope | Why |
|---|---|
| `app_mentions:read` | see `@Otto` in a channel |
| `channels:read` | **list** the public channels it's in — without this it finds none |
| `channels:history` | **read** those channels |
| `im:read` | **list** DMs sent to the bot |
| `im:history` | **read** those DMs |
| `chat:write` | post the ack + the result |
| `users:read` | resolve who is talking |
| `groups:read` + `groups:history` | private channels (optional) |
| `mpim:read` + `mpim:history` | group DMs (optional) |

Slack splits *listing* a conversation from *reading* it, so the `:read` and `:history` halves are
both needed and neither works alone. Miss one and the bot polls, answers nobody, and reports
nothing — Otto checks the granted scopes and says which are missing on the Events tab, but it can
only do that once the token is in place.

Under **App Home**, give the bot a display name and turn on **Messages Tab** →
*Allow users to send Slash Commands and messages* — without it, nobody can DM it.

There is no `search:read` for bots, so channel mentions are found by reading the channels the bot
is **a member of** rather than by search. That makes it more reliable than the user path's fuzzy
search, at the cost of one extra step: **invite the bot to each channel** (`/invite @Otto`).

### 2. Wire up the token

- **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`, *not* the `xoxp-…` one)
- Add it to `.env`: `OTTO_SLACK_BOT_TOKEN=xoxb-…`
- **Restart the service** — tokens are read at import time
- Verify identity: `curl -s -H "Authorization: Bearer $OTTO_SLACK_BOT_TOKEN" https://slack.com/api/auth.test`
  → the **bot's** `user_id`, which is not yours
- Verify scopes: `curl -si -XPOST -H "Authorization: Bearer $OTTO_SLACK_BOT_TOKEN" https://slack.com/api/auth.test | grep -i x-oauth-scopes`
  → compare against the table above. **Adding a scope requires reinstalling the app** — the token
  you already hold does not gain it, and this is the single most common reason a fresh bot is
  silent. Otto shows the same check on the Events tab.

### 4. Instant replies (optional — Socket Mode)

Without this the bot answers on the poll, so up to `poll_seconds` late. Socket Mode is an outbound
WebSocket the app holds open — no public URL, no tunnel, works behind NAT — so replies start
immediately. Only the bot can do this: a user token has no event stream at all, which is why the
poll exists in the first place.

- Slack app → **Basic Information → App-Level Tokens → Generate Token and Scopes**, add scope
  `connections:write`, copy the `xapp-…` token
- **Settings → Socket Mode** → toggle **Enable Socket Mode** on
- **Event Subscriptions** → *Subscribe to bot events*: add **all four**

  | Event | Covers |
  |---|---|
  | `app_mention` | `@Otto …` in a channel — starts a conversation |
  | `message.channels` | **replies in that thread**, which don't re-mention the bot |
  | `message.groups` | the same in a private channel |
  | `message.im` | DMs sent to the bot |

  `app_mention` alone is the tempting subset and it is the wrong one: it makes the *first*
  message instant and every follow-up poll-speed, which reads as "sometimes instant, mostly
  not". A thread reply is an ordinary `message.channels` event — Otto being in the thread
  doesn't change that. Subscribing to `message.channels` means the bot is *sent* every message
  in channels it's in; it still only *answers* what the allowlist permits, and events that
  can't produce work are dropped before anything is started.
- `.env`: `OTTO_SLACK_APP_TOKEN=xapp-…`, then restart

**It is a wake-up signal, not a second inbox.** An event never carries a message into Otto — it
only tells the existing poll to run now instead of in a minute. That is deliberate:

- The poll's per-message logic (allowlist, cursor, backlog rule, pleasantry short-circuit,
  resume-vs-handoff, one-turn-at-a-time) stays the single path. A second copy of those decisions
  is the bug, not the feature.
- **Socket Mode is lossy** — it delivers only while connected, and Slack never replays what it
  sent while you were disconnected, asleep or restarting. Because the poll still reads everything
  past the cursor, a dropped event costs a minute of latency, never a message.

So there is nothing to recover if it drops, and nothing to reconfigure if you remove the token —
the bot just goes back to poll speed. The Events tab says which mode it's in.

Leave `poll_seconds` at 60. It's the backstop that makes the socket safe to lose, and raising it
past `OTTO_SLACK_DOWNTIME_S` (300) would make every poll look like a resume-after-downtime and
start burning backlog.

- **The whole message must be the decision.** "yes, but change the title first" approves nothing,
  and neither does "I'd approve this once the leak is fixed". Anything else is treated as an
  ordinary message, which leaves the gate shut — the safe direction.
- **A non-approver's "yes" is silently just a message.** Otto doesn't tell them a gate exists;
  their message is answered normally once the gate resolves.

While a run is parked, other messages in that thread wait rather than starting a second run —
same one-turn-at-a-time rule as everywhere else. Auto-answer (the user token) has no equivalent:
Otto posts as *you* there, so it never reads your own messages and there is nobody it could
recognise as an approver. That path keeps the board.

### What's different about the bot

- **It answers under its own name and doesn't pretend otherwise.** The request framing tells it it
  is a bot in your workspace, not your stand-in — so it won't claim to relay anything on your
  behalf. That's a different prompt, not a different wording of the same one.
- **In a channel it only answers when @-mentioned.** A bot that replies to everything said in a
  channel it happens to be in is a bot people mute. A bare `@Otto` with nothing after it is
  treated as a greeting, not as a task.
- **It cannot see your DMs.** That's Slack's boundary, not Otto's — a bot token has no access to a
  conversation it isn't a party to.
- **Turning it on doesn't replay history.** Its cursors start at "now" like any newly-watched
  channel, so inviting it to a busy channel won't answer a week of backlog.
- **A thread it has spoken in is watched**, including one it only said hello in — so the real
  question after a greeting continues the same conversation instead of falling into a gap.
- **The two never cross.** If a channel is allowlisted for both, one message becomes two runs with
  two answers — one from you, one from the bot. That's usually not what you want: pick one per
  channel.

## Both identities

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

- **Auto-answer replies post as you, not as "Otto".** `chat.postMessage` on a user token always
  renders as the token owner; the `username`/`icon_emoji` overrides are classic-bot-only and
  ignored. The ack text (`ack_template`) is what does the attribution. The **bot** identity is the
  way to get a reply under a different name — but it can never reach a DM between you and someone
  else, so the two are complements, not alternatives.
- Slack text is **untrusted input**; it's framed as task data and the write gate stays the real
  guard. Keep the allowlists tight — both sets.
- Auto-answer's channel @-mention detection goes through Slack search, which is fuzzy — DMs are the
  robust path. The bot's does not (it reads the channels it's in), so it doesn't have this problem.

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
