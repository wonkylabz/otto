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

### The web API is unauthenticated

`server.py` binds `localhost` and has **no login, no session, no API token**. Anything that
can reach the port can start runs, approve write gates, and read every transcript.

The only cross-site defence is an `Origin` check on mutating requests
(`server.Handler._csrf_ok`), and a request with no `Origin` header at all is allowed — so
`curl` and webhooks work. Do not port-forward it, do not put it behind a naive reverse
proxy, and do not run it on a shared host.

### Runs have real tools and your real credentials

- Execution is `claude -p` under **your** Claude subscription and **your** `~/.claude`
  configuration — every MCP server, connector and credential you have logged in to.
- The read-risk tool allowlist (`config.READ_TOOLS`) includes unscoped `Bash`. A capability
  classified "read" therefore *can* mutate external state. The approval gate, not the
  toolset, is the actual guard.
- The real write guard that needs no human is `file_safety.py`: deny rules that cover Edit,
  Write, and `rm` through Bash, applied to every registered repo's live checkout and to
  Otto's own runtime state.
- The **local** execution backend bypasses `claude -p`'s permission system entirely.
  `local_runtime._deny_guard` re-implements the deny list for its own Write/Edit, but its
  `Bash` is **not** covered — parsing a shell to catch `tee`/`sed -i` would be theatre.

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
  shell command, and the API is unauthenticated.
- Egress is scrubbed by `privacy.py` (`redact`) on all four outbound paths — ntfy, Slack,
  GitHub comments, webhooks. It is deterministic and fails closed, but it is a backstop
  against a model quoting a credential, not an authorization system.
- Prompts, results and tool calls are written to `data/transcripts/` and `data/otto.db` in
  the clear. Those paths are read-denied to runs, but they are plaintext on disk.

### ntfy push

If you enable ntfy, the topic name is the only credential — anyone who knows it can read
your notifications, and gate-approval action buttons ride on it (single-use per-run tokens,
`delivery.mint_action_token`). Request content is only ever included when you opt in with
`OTTO_NTFY_DETAIL`.

### Stopping everything

`data/ESTOP` (or `POST /api/estop`, or the header control in the UI) blocks every ingress
from starting new work. It does **not** kill in-flight runs — nothing re-checks it
mid-activity.
