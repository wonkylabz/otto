# Slack

Otto listens on Slack under **two identities**, independently switchable, both running the
same unattended workflow with the same guarantees. Needs Temporal.

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
| `chat:write` | post the introduction + the result |
| `reactions:write` | acknowledge a message with 👀 (optional — without it, Otto posts an ack instead) |

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
- `ack_template` (the introduction posted on first contact under your own account — every other
  acknowledgement is a 👀 reaction on the message), `greeting_template` (the reply to a message with no
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
| `chat:write` | post the result |
| `reactions:write` | acknowledge a message with 👀 (optional — without it, Otto posts an ack instead) |
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

### 3. Configure it (Events tab → Slack → **Bot user**)

- `bot_enabled: true`
- `bot_allow_channels` — channel IDs the bot may answer in. **This list is the bound**: the bot
  reads only the channels named here *and* that it's been invited to. A channel it's in but not
  listed is ignored; a channel listed but not invited to yields nothing.
- `bot_allow_users` — who may **DM** the bot. Both lists empty means nobody, same safe default.
- `bot_approvers` — who may clear an approval gate by replying in the thread (see below).
  **Empty by default, and separate from the two lists above on purpose.**
- `bot_watch_dms` / `bot_watch_mentions`, `bot_ack_template`, `bot_greeting_template`

Poll interval, write approval and the pinned capability are shared with auto-answer.

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

### 5. Approving a write from Slack (optional)

Reads answer immediately. A **write** pauses for approval, and Otto now says so in the thread:

> That needs [you]'s approval before I can do it — I've put it in front of them. I'll reply here
> as soon as it's cleared.

By default you clear it on the **Needs-you board** (or the ntfy Approve/Deny buttons). Add your
own Slack ID to `bot_approvers` and you can also clear it by replying in the thread:

- `approve` / `yes` / `go ahead` / `lgtm` / `ship it` / 👍 → runs it
- `no` / `deny` / `cancel` / `stop` / 👎 → declines; nothing runs

Three things worth knowing, because they are what makes this safe to switch on:

- **`bot_approvers` is not `bot_allow_users`.** Being allowed to ask Otto for something is not
  being allowed to authorise it — otherwise a colleague's request approves its own write. Only
  IDs on this list count, and an empty list means nobody.
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

## Channel administration (optional — archiving)

Everything above is Slack *inbound*: Otto reads and replies. Nothing in it can archive a
channel, and neither can the claude.ai Slack connector or any off-the-shelf Slack MCP server —
they are all read/post. `slack_mcp.py` is the write half, registered as an ordinary MCP server
so a capability picks it up like any other tool.

It exposes three tools: `list_channels` (id, name, creation date, archive state — enough to
answer "older than N days" without opening each one), `archive_channel` and
`unarchive_channel`.

### 1. Add the scope

Archiving needs scopes none of the listener scopes imply. Add them to the *same* app, under
**OAuth & Permissions → Scopes**:

| Token | Public channel | Private channel |
|---|---|---|
| User (`xoxp-…`) | `channels:read`, `channels:write` | `groups:read`, `groups:write` |
| Bot (`xoxb-…`) | `channels:read`, `channels:manage` | `groups:read`, `groups:write` |

The `:read` scopes are for `conversations.list`, which `list_channels` and every name lookup
call — without them the server loads, but every call fails `missing_scope`.

`channels:manage` exists only as a *Bot* scope — on the User list the equivalent is
`channels:write`. Reinstall the app afterwards and replace the token in `.env`: a scope added
without a reinstall is not on the token you already hold.

Two things the scope does **not** override:

- The token's identity must be **in** the channel (a bot must be invited).
- **Settings → Permissions → Channel Management** can reserve archiving for workspace admins.
  Slack reports that as `restricted_action`, which reads like a bug and is a policy.

### 2. Register the server

Admin → MCP servers → add, then **activate** it (registering a server and running it are two
acts). The def:

```json
{
  "command": "python3",
  "args": ["/path/to/otto/slack_mcp.py"],
  "env": {"SLACK_TOKEN": "${OTTO_SLACK_USER_TOKEN}"}
}
```

The `${...}` is resolved by Otto on the way to the `--mcp-config` file, which is `0600` and
read-denied to every run — the token reaches this server and nothing else. (`OTTO_SECRET_COMMAND`
works here too: a bare `MY_SLACK_TOKEN` is looked up in the vault rather than the environment.)

Give the capability that needs it the server in Admin → per-cap `mcp`, or name it in the cap's
`tools:` frontmatter. Archiving is a write, so the run gates for approval like any other.

### 3. Check it

```
python3 slack_mcp.py <<< '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Should list the three tools. A call that fails reports Slack's error code **and** what to do
about it — `missing_scope` names the scope to add, rather than sending you to the API docs.

## Both identities

### Threads: the other person can carry the conversation on

Otto's ack and its answer are posted **in a thread** under the message that triggered them. Every
thread Otto has replied in is then **watched**: a new reply in it *continues the same conversation*
rather than starting a cold one — the follow-up resumes that run's Claude session, so "and the other
one?" or "no, I meant staging" work without repeating the context.

- Only the thread is continuable. A new *top-level* message (in the channel or DM, outside the
  thread) is a new task, with a new session — that's the deliberate signal for "different subject".
- A follow-up is acknowledged with a 👀 **reaction** on your message, not a post. A posted ack is a
  promise made before Otto knows whether there is anything to say, and a turn that legitimately
  says nothing then leaves "On it — let me check…" as the thread's last word. The introduction
  (your own account, first contact) is still a post; a token without `reactions:write` falls back
  to posting the old ack.
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
