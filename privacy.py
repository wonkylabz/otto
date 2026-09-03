"""Egress privacy: what Otto is allowed to say OUTSIDE the machine it runs on.

Everything Otto handles is private by default — the request text, the transcript, the memory
store, a colleague's Slack DM. Most of it never leaves the box: the chat UI, the audit db and
`data/transcripts/` are all local, and the run-detail drawer is a localhost page. Three paths
do leave, and they are the ones this module guards:

  * **ntfy push** (`delivery.notify`) — a THIRD-PARTY broker (ntfy.sh by default) where the
    topic name is the only credential. Anyone who knows or guesses it reads every push, and
    ntfy caches messages server-side. Treat it as a public bulletin board.
  * **Slack reply** (`delivery._slack` / `slack.post`) — read by whoever wrote in, who is
    routinely NOT the owner.
  * **GitHub comment / webhook** (`delivery._github_issue` / `_webhook`) — a durable record
    other people read.

Two separate controls, because they fail differently:

  1. `redact()` — deterministic scrub of credential-shaped substrings. Applies to EVERY egress
     unconditionally. This is the guard that holds when a prompt-level rule doesn't: the
     capability decides what to say, and no instruction survives contact with every model on
     every run. Targeted (secret-keyword k/v + known token shapes) so it doesn't mangle benign
     ids; the on-disk transcript keeps full fidelity for forensics.
  2. Content minimization for the push path (`delivery.notify`'s `detail` gate) — a
     notification says WHAT happened and WHERE to look, not what the request said. Redaction
     only catches credential SHAPES; a request body is private whether or not it contains a
     token, and ntfy.sh is the wrong place for it.

`redact` lives here rather than in `supervisor` (its first caller) so there is exactly ONE
implementation behind every boundary. A second copy is how one of them silently falls behind.
"""
import re

REDACTED = "[redacted]"

_SECRET_PATTERNS = (
    # Opaque token shapes that carry the secret in the body. These run FIRST so a value like
    # `Authorization: Bearer eyJ…` is scrubbed as a token before the k/v rule below sees it.
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),   # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    # Anthropic (sk-ant-api03-…) and OpenAI project (sk-proj-…) keys carry hyphens INSIDE the
    # body, so an alnum-only body stops dead at the one in `sk-ant` — which left every Anthropic
    # key, the one vendor's credential this tool actually handles, walking out of all four
    # egresses. Kept as its own pattern rather than widening the charset below: `sk-` plus a
    # 16-char hyphenated body also matches ordinary names like `sk-cluster-prod-eu-west-1`.
    re.compile(r"\bsk-(?:ant|proj)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9]{16,}"),                             # OpenAI legacy
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}"),                             # GitHub token
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),                           # Slack
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                            # AWS access key id
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),                                # Google API key
    # PEM private key block — collapse the whole body, not just the header line.
    re.compile(r"(?is)-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----"),
    # Credentials embedded in a URL: https://user:pass@host -> https://[redacted]@host. Keeping
    # the host is deliberate — the reader still learns WHERE, which is the actionable half.
    # The `@` is matched by LOOKAHEAD so it survives the substitution: consuming it rendered
    # `https://[redacted]db.internal`, which reads as though the host itself were scrubbed.
    re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+(?=@)"),
    # Secret-NAMED key/value: "api_key": "…", token=…, password: …. Last, and the only pattern
    # that keeps its group-1 prefix, so the reader still sees WHICH key was scrubbed.
    re.compile(
        r"(?i)((?:api[_-]?key|secret|token|passw(?:or)?d|access[_-]?key|"
        r"private[_-]?key|client[_-]?secret|authorization|credential)"
        r'["\']?\s*[:=]\s*["\']?)[^\s"\',}]+'
    ),
)

# Patterns whose replacement keeps a captured prefix. Indexed rather than flagged inline so the
# tuple above stays readable; asserted in test_core to stay in sync with the tuple's length.
_KEEP_PREFIX = {len(_SECRET_PATTERNS) - 2, len(_SECRET_PATTERNS) - 1}


def redact(text):
    """Scrub credential-shaped substrings. Pure, defensive, and IDEMPOTENT — never raises, and
    re-running it over already-redacted text is a no-op, so a value can safely pass more than
    one egress choke point (delivery.deliver AND slack.post both call it)."""
    if not text:
        return text
    try:
        for i, pat in enumerate(_SECRET_PATTERNS):
            repl = (r"\1" + REDACTED) if i in _KEEP_PREFIX else REDACTED
            text = pat.sub(repl, text)
    except Exception:  # noqa: BLE001 - a redaction crash must never become an unredacted egress
        return REDACTED
    return text


# --- notification content minimization -------------------------------------
# A push notification's job is to get the owner to the run, not to reproduce it. These build the
# non-content metadata lines that are ALWAYS safe to send; the request text itself rides only on
# `delivery.notify(detail=…)`, which is dropped unless OTTO_NTFY_DETAIL opts in.

def source_line(reply_to, unattended=False):
    """Where the run came from, named coarsely enough to be safe on a public topic. Returns None
    when there's nothing useful to say.

    A Slack source is deliberately NOT identified by channel or user id: the id is a direct
    pointer into a private DM, and "Slack" plus the run id already gets the owner to the right
    place in the UI. A GitHub issue IS named (repo#number) — it's the actionable half of the
    notification, and an internal repo name is a far smaller exposure than a ticket body. A
    webhook's URL is never included; it routinely carries a token in the path or query."""
    kind = (reply_to or {}).get("kind") if isinstance(reply_to, dict) else None
    if kind == "slack_thread":
        return "source: Slack message"
    if kind == "github_issue":
        repo, number = (reply_to.get("repo") or "?"), reply_to.get("number")
        return f"source: GitHub issue {repo}#{number}" if number else f"source: GitHub {repo}"
    if kind == "github_pr":
        repo, number = (reply_to.get("repo") or "?"), reply_to.get("number")
        return f"source: GitHub PR {repo}#{number}" if number else f"source: GitHub {repo}"
    if kind == "webhook":
        return "source: webhook"
    if unattended:
        return "source: scheduled run"
    return None


def context_lines(cap=None, repo=None, reply_to=None, unattended=False, extra=None):
    """The always-sent body of a push: capability, risk, target repo, source. All of it is Otto's
    own vocabulary or a name the owner chose — none of it is request, ticket or message CONTENT.
    Redacted anyway, since `extra` is caller-supplied."""
    lines = []
    if isinstance(cap, dict) and cap.get("name"):
        risk = cap.get("risk")
        lines.append(f"{cap['name']}" + (f" · {risk}" if risk else ""))
    if repo:
        lines.append(f"repo: {repo}")
    src = source_line(reply_to, unattended=unattended)
    if src:
        lines.append(src)
    for x in (extra or []):
        if x:
            lines.append(str(x))
    return [redact(x) for x in lines]
