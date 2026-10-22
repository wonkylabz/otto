---
name: otto-setup
description: Set Otto up for someone conversationally — install, models, repos, MCP servers, jobs, events, Slack, the GitHub board and cost profile — by asking what they want and configuring it through Otto's own API. Use when the user says "set up Otto", "configure Otto for me", "help me get started", "add a job/event/MCP/repo", or has just cloned this repo and doesn't want to read the docs.
---

# Set up Otto

You are configuring a person's Otto install for them. They chose an AI assistant over the docs, so
do the reading yourself: ask short questions, decide the obvious defaults, configure through
Otto's API, and confirm what you did in plain words. Never make them edit a JSON file.

Run this from a Claude Code session in the Otto checkout, as the operator. It does not work from
inside an Otto run: every run is denied the API token by design (`api_auth.py`).

## Three rules that override convenience

1. **Configure through the API, never by writing `data/`.** The server and the worker mutate the
   same stores from two processes behind a lock (`storage.mutate_json`), and the handlers carry
   the validation: a hand-written `data/*.json` races them and skips the checks. If the server is
   down, start it (step 1) rather than editing files.
2. **Secrets never pass through this conversation.** Do not ask the user to paste a token, key or
   webhook secret. Send them to a prompt that reads it without echoing:
   `./.venv/bin/python setup_wizard.py` (it fills `.env`, never overwrites, never prints a
   secret), or to the Admin screen that owns it. Ask them to run it in their OWN terminal: it
   reads with `getpass` and skips itself without a TTY, which Claude Code's `!` prefix doesn't
   give it. Then confirm it landed with `doctor.py`, which
   reports a secret as set or missing, never its value.
3. **Approval steps stay human.** Registering an MCP server and RUNNING it are two acts: a stored
   command is spawned as the operator, so `/api/mcp/activate` is the human's confirmation of the
   exact argv. Show the command line and ask before you activate. Likewise, never set
   `auto_approve` on a job that writes without the user saying so in those words.

## Calling the API

```bash
TOKEN=$(cat data/.api/token)                     # created on first server start
PORT=${PORT:-8765}
api() { curl -sS -H "X-Otto-Token: $TOKEN" -H 'Content-Type: application/json' \
          "http://localhost:$PORT$1" ${2:+-d "$2"}; }
api /api/health                                  # GET
api /api/settings '{"settings":{"max_attempts":3}}'   # POST when a body is given
```

Before you POST to a route for the first time, read its handler in `server.py` (`_post_<route>`,
routed from `_POST_ROUTES`) for the exact body. Several routes **replace a whole list**: GET it,
change your item, POST the full list back. `/api/event-rules` and `/api/slack-triggers` are two.

## The conversation

Keep each turn to one or two questions. Skip any area the user says they don't need. After each
area, say in one line what is now set up and what they can try.

### 0. Where are we?

```bash
./.venv/bin/python doctor.py          # every gap, ok/warn/fail, with a hint each
```

No `.venv`? Run `./install.sh` (idempotent; `--guided` asks for secrets itself). The doctor's
output is your checklist: work its FAILs first, then the WARNs that matter for what they want.

### 1. Running

`./run.sh` starts Temporal, the worker and the server; `docs/operating.md` has the service
install (launchd/systemd) if they want it always on. Then `./run.sh login` prints a one-time
browser login link — give them that link, not the token.

### 2. What is it for?

Ask what they want Otto to do — personal errands, work code, both — and how cost-sensitive they
are. That is the onboarding wizard's question set, so apply it through the same API, which shows
the diff before it writes:

```bash
api /api/onboarding                                       # current answers + profiles
api /api/onboarding/preview '{"answers":{...}}'           # diff + integration checklist
api /api/onboarding/apply '{"answers":{...},"expect":"<fingerprint from preview>"}'
```

Read `onboarding.py` for the answer keys. Show them the preview's diff in words before applying.
The checklist it returns names the integrations their answers need; that is your agenda for the
next steps.

### 3. Models

Otto runs on their Claude subscription through `claude -p`; no API key is required, so don't ask
for one. Ask only whether they also want a local or hosted model (vLLM, Ollama, an OpenAI-
compatible endpoint, Codex):

- Endpoint URL from them; any key through rule 2.
- `api /api/models/discover '{"endpoint":"<name>"}'` lists what it serves; `/api/models/probe`
  checks a model before it is saved, and a refusal is not a save.
- Which phases use which model is Admin → Models; explain the phase table rather than guessing
  assignments. Default advice: keep execution on Claude until a local model has earned it on the
  Audit tab's per-capability pass rates.

### 4. Repos

Ask which repositories Otto should work on. Register each by URL; a local checkout path is
optional and wins if given:

```bash
api /api/project/add '{"url":"https://github.com/org/repo"}'
```

`gh auth status` must be logged in for PRs and the board; if not, have them run `gh auth login` in their terminal (it is interactive).

### 5. MCP servers

Ask which tools they want Otto to reach (Jira, Linear, a database, Kubernetes…). Two kinds:

- **claude.ai connectors** (OAuth inside Claude Code) need nothing from Otto; they just won't run
  on a local model. `claude mcp list` shows what is connected.
- **stdio servers**: `api /api/mcp/add '{"name":…,"command":…,"args":[…],"env":{…}}'` stores it
  inactive. Show the user the exact command and args, ask, then `api /api/mcp/activate
  '{"name":…}'`. Credentials never go in literally: an `env` value may be a NAME
  (`"JIRA_API_TOKEN"` or `"${JIRA_API_TOKEN}"`), resolved at spawn from the environment or the
  `OTTO_SECRET_COMMAND` vault (`mcp_client.resolve_env_map`). Post the name, and have the user put
  the value in `.env` or their vault themselves; `.env` is read at startup, so restart Otto after.

### 6. Jobs (scheduled or on-demand runbooks)

Ask what they want done on a schedule ("every weekday at 9, summarise my PRs"). Turn it into a
runbook: a request, an optional cron in the server's timezone (`OTTO_SCHEDULE_TZ`), an optional
pinned capability, optional Slack destination. Read `runbooks.normalize` for the fields, then:

```bash
api /api/runbooks/add '{"name":…,"request":…,"cron":"0 9 * * 1-5"}'
api /api/runbooks/run '{"id":"<id>"}'          # fire it once now so they see it work
```

Each firing opens its own chat thread. A runbook that writes stays gated unless they explicitly
asked for `auto_approve`.

The weekly health check is NOT a runbook: a run is read-denied Otto's own `data/` (the trail
included), and its reply is delivered even in a quiet week. Suggest the operator's own cron
instead — `./.venv/bin/python insights.py --since 7d --changes-only` prints nothing when nothing
moved — or the Audit tab's Health section.

### 7. Events (webhooks and Slack triggers)

- **Webhooks**: each rule maps a `source` + match to a request template (`events.py`). The
  sender signs with the shared secret (`OTTO_EVENT_SECRET`, rule 2) and must send
  `X-Otto-Timestamp` and `X-Otto-Signature`; the Events tab's guides show the curl.
- **Slack**: user token and/or bot token via rule 2; then allowlists in Admin → Slack.
  `docs/slack.md` has the app manifest and scopes. Slack triggers (`/api/slack-triggers`) start
  runs from posts in watched channels.

### 8. GitHub board / PR reviews (optional)

`/api/board-config` points Otto at a Projects v2 board (moving a card to Ready approves it);
`/api/pr-review-config` makes it review PRs where they are a requested reviewer. Ask before
enabling either: both act on GitHub in their name.

## Finish

Run `./.venv/bin/python doctor.py` again and walk the remaining WARNs with them, or say plainly
that they can wait. Close with: what is configured, one thing to try now (a first chat message, or
"Run now" on the job you made), and where to look when something goes wrong (the Audit tab's
Health section, or ask for the `audit-runs` skill).
