# Security

## Reporting a vulnerability

Open a [private security advisory](../../security/advisories/new) on this repository.
Please don't open a public issue for anything exploitable.

There is no SLA — Otto is a personal project maintained in spare time.

## Threat model — read this before running Otto

Otto executes an LLM's decisions on your machine, as you. It is designed for a single
operator running it on their own workstation. **It is not multi-tenant, not hardened, and
not safe to expose to a network.** Several properties below are deliberate design choices,
not bugs — but you should know about all of them before you start it.

### The web API has one credential, and it is yours

`server.py` binds `localhost`, and every `/api/*` route needs the per-install token in
`data/.api/token` (`api_auth.py`, #217) — as an `X-Otto-Token` header, or, for a browser, as a revocable
session cookie (HttpOnly, SameSite=Strict, `Secure` over HTTPS) that `./run.sh login` or the login
screen mints. The cookie never holds the token itself, so another localhost app that receives it
(cookies are not port-scoped) gets one session you can revoke in Admin → Browser sessions. There are no users and no roles: **holding the token is
holding your full authority** — start runs, approve write gates, release the pause, read every
transcript. The token file and your browser profiles are masked by the kernel (`bwrap`) around every run, which is
what stops a run's own shell from approving its own gate. **Without a usable `bwrap`, a
`claude -p` run can still read the token** (see below). The mask is a mount namespace, not a
different user: a run that hands work to a same-user process OUTSIDE it — `systemd-run --user`,
a running tmux session, `docker` if you are in that group — reads the token (measured with
`systemd-run`). It stops a run that reaches for the file, not one engineering an escape.

Two routes carry their own credential instead: webhooks (`/api/events/`, HMAC) and the ntfy gate
buttons (`/api/gate/<token>`, single-use). Mutating requests are also `Origin`-checked
(`server.Handler._csrf_ok`) against cross-site pages. Do not port-forward it, do not put it
behind a naive reverse proxy, and do not run it on a shared host: anything running as your user
can read the token file.

### Runs have real tools and your real credentials

- Execution is `claude -p` under **your** Claude subscription and **your** `~/.claude`
  configuration — every MCP server, connector and credential you have logged in to.
- The read-risk tool allowlist (`config.READ_TOOLS`) includes unscoped `Bash`. A capability
  classified "read" therefore *can* mutate external state. The approval gate, not the
  toolset, is the actual guard.
- The real write guard that needs no human is `file_safety.py`: deny rules that cover Edit,
  Write, and `rm` through Bash, applied to every registered repo's live checkout and to
  Otto's own runtime state.
- Those `claude -p` deny rules match the command TEXT: `cat <file>` is refused, but
  `python3 -c "open(<file>)"` reads it. Only the API credentials are masked by the kernel
  around `claude -p`; the rest of the deny set is a guard against accidents, not a determined run.
- The **local** execution backend bypasses `claude -p`'s permission system entirely.
  `local_runtime._deny_guard` re-implements the deny list for its own Write/Edit, but its
  `Bash` is **not** covered — parsing a shell to catch `tee`/`sed -i` would be theatre.

- Registering an MCP server (`/api/mcp/add`) stores a command line Otto will **spawn on your
  machine**. It is stored inactive: nothing reaches `--mcp-config` or the local backend until
  you press Activate in Admin, next to the exact argv. Adding, activating and removing are all
  written to the audit trail. This bounds what one request through the API can
  do; it is not a substitute for the section above.

### Untrusted input reaches the model

Slack messages, GitHub issue bodies and webhook payloads are all attacker-influencable text
that ends up in a prompt. Classifiers that interpolate it fence it (`engine._fenced`), but
that is advisory: the real controls are the capability's static read/write risk, the
fail-to-WRITE default on an unparseable verdict, and the human approval gate. **Keep the
Slack allowlists to people you would hand your terminal to** — anyone on them can ask Otto
to act with your access.

### Secrets

- Secrets resolve through `config.secret()`: env → the `OTTO_SECRET_COMMAND` helper → unset.
  By default they sit in plaintext in `.env` (mode 600).
- `OTTO_SECRET_COMMAND` is **env-only and never settable over the API** by design: it is a
  shell command, and the API's token is a single all-powerful credential.
- Egress is scrubbed by `privacy.py` (`redact`) on all four outbound paths — ntfy, Slack,
  GitHub comments, webhooks. It is deterministic and fails closed, but it is a backstop
  against a model quoting a credential, not an authorization system.
- Transcripts (`data/transcripts/`) and the audit trail (`audit`, `audit_content` in
  `data/otto.db`) pass every value through the same `redact` as it is written — the trail is
  immutable, so an unscrubbed secret there is permanent. The cost is deliberate: a transcript
  cannot prove the exact bytes the model saw, and retrying a run whose request held a pasted
  secret re-runs it with `[REDACTED]` in its place.
- Chat history, memory and knowledge in `data/otto.db` are still stored in the clear. All of
  `data/` is read-denied to runs, but it is plaintext on disk.

### ntfy push

If you enable ntfy, the topic name is the only credential — anyone who knows it can read
your notifications, and gate-approval action buttons ride on it (single-use per-run tokens,
`delivery.mint_action_token`). Request content is only ever included when you opt in with
`OTTO_NTFY_DETAIL`.

### Stopping everything

`data/ESTOP` (or `POST /api/estop`, or the header control in the UI) blocks every ingress
from starting new work. It does **not** kill in-flight runs — nothing re-checks it
mid-activity.
