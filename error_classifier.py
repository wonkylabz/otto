"""Why a call failed, and what the ladder should do about it.

Otto already draws the one distinction that matters most — **a deterministic wall latches the
ladder off local, a budget death does not** — but it drew it over exactly two conditions:
tool-calls-rejected and endpoint-unreachable. Everything else fell into one anonymous
`RuntimeError("HTTP <code>: …")` that the verify ladder treats as a model-quality failure and
retries, on the same endpoint, twice more.

Three of those are deterministic walls wearing a quality failure's clothes:

- **401/403** — the key is wrong, missing, or unresolved. Fails identically every attempt. Otto's
  own notes already record a vLLM endpoint key "silently 401-ing every call"; before this it cost
  three attempts and a needs-human banner to say so.
- **402** — credit exhausted. No retry produces credit.
- **429 that outlives every backoff** — the server is serving somebody, just never us.

And one was misfiled the other way: **500** was treated as permanent while 502/503/504 backed
off, though a bare internal error is at least as likely to be transient as a bad gateway.

Classification is a pure function of (status, detail) so it can be tested without a server —
which matters, because the wrong verdict here is expensive in both directions. Too eager to call
a wall and a recoverable blip permanently re-dispatches the run to Claude; too shy and a
misconfigured endpoint burns the whole ladder before anyone learns the key is wrong.

Shaped after hermes-agent's `agent/error_classifier.py` (MIT), minus what Otto has no use for:
Hermes rotates among pooled credentials and falls back across providers, so its taxonomy splits
`auth` from `auth_permanent` and `rate_limit` from `upstream_rate_limit`. A local Otto endpoint
has one key and one URL, so those collapse — the only recovery is Claude, and `local_fallback`
already decides whether that is allowed.

Everything above is about a LOCAL endpoint. The Claude backend has exactly one failure with the
same property — it cannot authenticate — and it lives at the bottom of this file for the same
reason the rest is here: which failures are walls is one decision, not an if-chain at each call
site.
"""
import enum
import re


class Reason(str, enum.Enum):
    """Why it failed. String-valued so it survives the activity result boundary as plain JSON."""
    auth = "auth"                            # 401/403 — credential wrong, missing or unresolved
    quota = "quota"                          # 402 — no credit
    rate_limit = "rate_limit"                # 429 — serving, but not us
    overloaded = "overloaded"                # 502/503/504 — momentarily not serving
    server_error = "server_error"            # 500 — internal error, often transient
    tools_unsupported = "tools_unsupported"  # rejects the tools param outright
    context_overflow = "context_overflow"    # prompt longer than the window
    unsupported_param = "unsupported_param"  # the body carries a parameter this model refuses
    unknown = "unknown"


class Action(str, enum.Enum):
    retry_in_place = "retry_in_place"  # back off against the SAME endpoint; may still recover
    prune = "prune"                    # shrink the prompt and try again
    adapt = "adapt"                    # rewrite the body for this endpoint's parameter dialect
    wall = "wall"                      # cannot serve any run: latch off local, re-dispatch
    fail = "fail"                      # nothing structured to do — the normal ladder applies


# A wall is not "this failed", it is "this will fail the same way every time". Getting a reason
# onto this list is what spends the ladder differently, so each entry needs that property to
# actually hold — `unknown` is deliberately absent, since an unrecognised error might well be a
# blip and the safe default is to let the ladder retry.
_WALL = {Reason.auth, Reason.quota, Reason.tools_unsupported}

# Reasons a backoff can legitimately outlive. Past the retry budget they become walls, which is
# `escalate()` below rather than a second table.
_TRANSIENT = {Reason.rate_limit, Reason.overloaded, Reason.server_error}

_MESSAGE = {
    # Every line names THE ENDPOINT, never "local": the same wall is hit by a vendor API with a
    # bad key or spent quota, and "fix the local server" points the operator at the wrong thing.
    Reason.auth: ("the model endpoint rejected our credentials (HTTP {code}) — check the "
                  "endpoint's api_key_env and that OTTO_SECRET_COMMAND resolves it"),
    Reason.quota: "the model endpoint reports no remaining credit (HTTP 402)",
    Reason.rate_limit: ("the model endpoint rate-limited every attempt (HTTP 429) — it is "
                        "serving other traffic"),
    Reason.overloaded: ("the model endpoint is unreachable (it stayed down through "
                        "every retry/backoff)"),
    Reason.server_error: ("the model endpoint failed internally on every attempt (HTTP 500)"),
    Reason.tools_unsupported: ("the model endpoint rejects tool calls (on vLLM: missing "
                               "--enable-auto-tool-choice / --tool-call-parser)"),
    Reason.context_overflow: "the prompt exceeds the model's context window",
    Reason.unsupported_param: ("the model refuses a parameter in the request body "
                               "({quirk}) — retried in this endpoint's dialect"),
    Reason.unknown: "the model endpoint failed (HTTP {code})",
}


class Verdict:
    __slots__ = ("reason", "action", "message", "quirk")

    def __init__(self, reason, action, message, quirk=None):
        self.reason, self.action, self.message = reason, action, message
        # Only ever set on an `adapt` verdict: WHICH parameter dialect the server just told us
        # it speaks. The caller rewrites the body for it and retries — see `param_quirk`.
        self.quirk = quirk

    @property
    def is_wall(self):
        return self.action is Action.wall

    @property
    def counts_as_unhealthy(self):
        """Should this light the model-health badge? Only "cannot serve any run" conditions —
        the standing rule is that a bad-but-served answer must never light it, or the badge
        stops meaning anything.

        Being a wall IS that condition, so there is no second clause. A context overflow is our
        prompt's fault rather than the endpoint's and must not light it — it doesn't, because it
        classifies as `prune`, not `wall`. An explicit exclusion for it was written here first
        and was dead code: worth stating, since it reads like load-bearing defence."""
        return self.action is Action.wall

    def __repr__(self):
        return f"<Verdict {self.reason.value}/{self.action.value}>"


def classify(status=None, detail="", transport_error=False, adaptable=True):
    """Map one failed call to a Verdict.

    `status` is the HTTP status (None when the request never got one), `detail` the response
    body, and `transport_error` marks a connection-level failure — refused/reset/DNS — which has
    no status but means the same thing a 503 does. `adaptable=False` means the caller has
    already applied every body rewrite it knows and the server said the same thing again, so
    an `adapt` verdict would be a loop rather than a recovery."""
    detail = detail or ""
    if transport_error or status is None:
        return _v(Reason.overloaded, Action.retry_in_place)
    if status == 400:
        # Ordered: a rewritable body comes first (it costs one round trip and may fix the run),
        # then the permanent tool-call wall, then a context overflow, which is recoverable by
        # pruning. All three arrive as a 400, so only the body separates them.
        #
        # `adaptable=False` is the caller saying it has already rewritten everything it can and
        # the complaint stands. That distinction is load-bearing: gpt-6-astra's refusal names
        # `reasoning_effort`, so it reads as adaptable, and only once dropping it changes
        # nothing is the real verdict visible — this model cannot run function tools on
        # /v1/chat/completions at all, which is the tool-call wall.
        if adaptable:
            quirk = param_quirk(detail)
            if quirk:
                return Verdict(Reason.unsupported_param, Action.adapt,
                               _MESSAGE[Reason.unsupported_param].format(quirk=quirk), quirk)
        if _tools_refused(detail):
            return _v(Reason.tools_unsupported, Action.wall)
        if _looks_like_context_overflow(detail):
            return _v(Reason.context_overflow, Action.prune)
        return _v(Reason.unknown, Action.fail, status)
    if status in (401, 403):
        return _v(Reason.auth, Action.wall, status)
    if status == 402:
        return _v(Reason.quota, Action.wall, status)
    if status == 429:
        return _v(Reason.rate_limit, Action.retry_in_place)
    if status == 500:
        return _v(Reason.server_error, Action.retry_in_place)
    if status in (502, 503, 504):
        return _v(Reason.overloaded, Action.retry_in_place)
    return _v(Reason.unknown, Action.fail, status)


def escalate(verdict):
    """What a `retry_in_place` becomes once the backoff budget is spent: a wall. The endpoint had
    its chances and never served, so retrying it on the next ladder rung reaches the same place —
    which is the whole reason `local_wall` exists."""
    if verdict.action is not Action.retry_in_place:
        return verdict
    return Verdict(verdict.reason, Action.wall, verdict.message)


def wall_message(reason_value):
    """The operator-facing sentence for a wall reason that crossed a process boundary as a plain
    string (the engine reads it off the runtime's result dict). Unknown values degrade to a
    generic line rather than raising — a run must never die because a newer worker sent a reason
    this one has not heard of."""
    try:
        reason = Reason(reason_value)
    except ValueError:
        return f"the model endpoint hit a wall ({reason_value})"
    return _MESSAGE[reason].format(code="") if "{code}" in _MESSAGE[reason] else _MESSAGE[reason]


# --- parameter dialects (issue #10) ----------------------------------------------------
# Every OpenAI-compatible body Otto sends carries `max_tokens` and `temperature: 0`. OpenAI's
# newer models reject BOTH — `max_tokens` was renamed `max_completion_tokens`, and only the
# default temperature is accepted — and each refusal is a 400 that names the offending
# parameter and, for the rename, its replacement.
#
# Detected from the SERVER'S OWN WORDS rather than guessed from the model id, because the id is
# exactly what we cannot predict: the reporting operator's model was called "chatgpt 6 astra".
# A name/date table would have to be edited for every future OpenAI generation and would still
# be wrong the day one ships; a 400 that says `Use 'max_completion_tokens' instead` is right by
# construction, costs one round trip once, and works on any endpoint that speaks this dialect.
#
# Deliberately NOT a wall: unlike a bad key, this failure has a recovery the caller can perform
# by itself, so it must not latch the ladder off local or light the health badge.
QUIRK_MAX_COMPLETION_TOKENS = "max_completion_tokens"
QUIRK_DEFAULT_TEMPERATURE = "default_temperature"
QUIRK_NO_REASONING_EFFORT = "no_reasoning_effort"
QUIRK_NO_TOOL_REASONING = "no_tool_reasoning"
# The THIRD way one parameter is refused, and the only one that needs a value rather than a
# deletion: gpt-5.6-terra applies a reasoning_effort SERVER-SIDE, so omitting the field is not
# the same as disabling it and the identical "Function tools with reasoning_effort are not
# supported" 400 comes back against a body that no longer carries it. Its own message names the
# fix ("or set reasoning_effort to 'none'"), which gpt-6-astra rejects — hence a separate quirk
# reached only AFTER dropping was tried, never a re-reading of the same message.
QUIRK_TOOL_REASONING_NONE = "tool_reasoning_none"

QUIRKS = (QUIRK_MAX_COMPLETION_TOKENS, QUIRK_DEFAULT_TEMPERATURE, QUIRK_NO_REASONING_EFFORT,
          QUIRK_NO_TOOL_REASONING, QUIRK_TOOL_REASONING_NONE)


def param_quirk(detail):
    """Which parameter dialect this 400 is asking for, or None. Pure, so the two live bodies
    below are the whole test corpus.

    The token-limit test requires BOTH names: the server has to be naming `max_tokens` as the
    problem AND `max_completion_tokens` as the fix, or a context-overflow message that merely
    mentions max_completion_tokens would rename a parameter that was never the complaint."""
    d = (detail or "").lower()
    if "max_completion_tokens" in d and "max_tokens" in d:
        return QUIRK_MAX_COMPLETION_TOKENS
    # "Unsupported value: 'temperature' does not support 0 with this model. Only the default
    # (1) is supported." Anchored on the quoted param name so a model whose ANSWER discusses
    # temperature can never match — this only ever reads an error body, but the cost of a false
    # positive is silently sampling at temperature 1 for the rest of the process.
    if "'temperature'" in d and ("does not support" in d or "unsupported" in d):
        return QUIRK_DEFAULT_TEMPERATURE
    # `reasoning_effort` is refused two DIFFERENT ways, and they need different adaptations —
    # one endpoint, one parameter, two facts:
    #
    #   "Function tools with reasoning_effort are not supported for gpt-5.5 in
    #    /v1/chat/completions." — only the PAIR is refused. The model reasons fine on its own,
    #    so the effort is dropped for tool-carrying bodies and kept for the rest.
    #
    #   "Unrecognized request argument supplied: reasoning_effort" — gpt-4o/gpt-4.1 and every
    #    other non-reasoning model. The parameter does not exist for them, tools or no tools,
    #    so it is dropped outright. Collapsing this into the pair-only quirk left a tool-free
    #    turn re-sending it and re-paying the 400 on every single turn.
    if "reasoning_effort" in d:
        if "function tools" in d:
            return QUIRK_NO_TOOL_REASONING
        if ("unrecognized" in d or "not supported" in d or "unsupported" in d
                or "does not support" in d):
            return QUIRK_NO_REASONING_EFFORT
    return None


def _tools_refused(detail):
    """The server will not run function tools for this model, full stop — a permanent config
    wall, and the run must re-dispatch to Claude rather than spend the ladder proving it.

    Two very different servers say it. vLLM names the flags it was started without. OpenAI says
    the model cannot take tools on /chat/completions and points at /v1/responses — an endpoint
    Otto does not speak, so for our purposes the model simply cannot drive the agentic loop.
    Only reachable with `adaptable=False`, i.e. after dropping `reasoning_effort` changed
    nothing, so a run that merely had to lose that parameter is never walled by this."""
    d = (detail or "").lower()
    return ("--enable-auto-tool-choice" in d or "--tool-call-parser" in d
            or ("function tools" in d and "not supported" in d))


# What to DO about a tool refusal, which is not one instruction: vLLM is a local process the
# operator can restart with the right flags, while a hosted model that cannot take tools on
# /chat/completions has no flag to add — the only move is to run that capability somewhere else.
# Same rule as the Claude walls below: a wall that names the wrong remedy is worse than a
# generic one, because the operator acts on it.
_VLLM_TOOL_REMEDY = ("the local model server rejects tool calls — start vLLM with "
                     "--enable-auto-tool-choice and --tool-call-parser <parser>")


def tools_refused_message(detail=""):
    """The operator-facing sentence for a tool-call wall, chosen from the server's own words."""
    d = (detail or "").lower()
    if "--enable-auto-tool-choice" in d or "--tool-call-parser" in d or not d:
        return _VLLM_TOOL_REMEDY
    said = " ".join((detail or "").split())[:220]
    return ("this model cannot run function tools on /chat/completions, so it cannot drive the "
            "local agent runtime — assign the capability a different execution model "
            f"(Admin → LLM models). The endpoint said: {said}")


_OUTPUT_CAP = re.compile(r"supports at most (\d+) completion tokens", re.IGNORECASE)


def output_cap(detail):
    """The model's own maximum output budget, when a 400 names it — else None.

    A DIFFERENT failure from a context overflow, though both are about tokens: the prompt fits
    fine, we simply asked for more completion than this model can produce ("max_tokens is too
    large: 32768. This model supports at most 16384 completion tokens"). Otto's
    LOCAL_EXEC_MAX_TOKENS is one number for every model, so any model with a smaller ceiling —
    gpt-4o, gpt-4, gpt-3.5-turbo — rejected every call.

    Recovering by halving (what a real overflow does) works but pays extra round trips for a
    number the server already told us, so the caller clamps to it directly."""
    m = _OUTPUT_CAP.search(detail or "")
    return int(m.group(1)) if m else None


def _looks_like_context_overflow(detail):
    """Includes the output-budget rejection above: it is not literally a context overflow, but
    it wants the same `prune` verdict — the caller's recovery for both is "ask for fewer
    tokens", and it clamps to `output_cap` when the server named one."""
    if output_cap(detail):
        return True
    return _prompt_too_long(detail)


def _prompt_too_long(detail):
    """Every phrasing an OpenAI-shaped endpoint has been seen to use. vLLM and OpenAI both
    open with "maximum context length"; an Anthropic-compatible proxy says "prompt is too
    long" and names no limit at all, so a numbers-first test misses it."""
    d = detail.lower()
    return ("maximum context length" in d or "context_length_exceeded" in d
            or "reduce the length" in d or "prompt is too long" in d)


def _v(reason, action, code=""):
    msg = _MESSAGE[reason].format(code=code) if "{code}" in _MESSAGE[reason] else _MESSAGE[reason]
    return Verdict(reason, action, msg)


# ---------------------------------------------------------------------------
# The CLAUDE backend's deterministic walls.
#
# Everything above classifies a LOCAL endpoint's HTTP failures. The Claude backend has its own
# failures with the same property — they fail identically on every rung — and they used to be
# invisible: the CLI dies before emitting a result event, so the attempt came back as a generic
# harness death, drew on `max_harness_retries`, and re-ran the identical doomed call two more
# times before the run surfaced as "harness_exhausted (timeout or worker crash)". That banner
# names the wrong culprit and hides a one-line fix.
#
# Three conditions qualify, all measured on the audit trail (2026-07-06..2026-08-25):
#
#   auth         — the subscription session expired. 6 attempts across the trail.
#   usage_limit  — "You've hit your session limit · resets 7pm (Pacific/Auckland)". 6 attempts.
#                  The ladder's rungs are minutes apart and the limit resets in HOURS, so every
#                  retry reaches the same refusal; worse, it reads as a model-quality failure,
#                  so the final rung ESCALATES the model and spends the most expensive tier on
#                  a call that never runs.
#   bad_model    — "There's an issue with the selected model (qwen38-27b). It may not exist or
#                  you may not have access to it." A models.json entry naming a model this
#                  subscription cannot serve. Deterministic by construction.
#
# Matching is deliberately narrow AND gated by the caller on `is_error` — i.e. the CLI produced
# no result event at all, so this text is the process's own dying words, never model output. A
# capability whose REPORT mentions an expired OAuth token therefore cannot latch this.
_CLAUDE_AUTH_MARKERS = (
    "failed to authenticate",
    "oauth session expired",
    "oauth token has expired",
    "oauth token expired",
    "invalid api key",
    "authentication_error",
    "please run /login",
    "please run `claude /login`",
    "run `claude login`",
)

# Anthropic phrases the subscription cap several ways; each is the CLI's own refusal line.
_CLAUDE_LIMIT_MARKERS = (
    "hit your session limit",
    "hit your usage limit",
    "usage limit reached",
    "session limit reached",
    "claude usage limit reached",
)

# `claude -p --model <name>` with a name the subscription cannot serve.
_CLAUDE_MODEL_MARKERS = (
    "may not exist or you may not have access to it",
    "there's an issue with the selected model",
)

# reason -> markers. Order matters only in that the first match wins; the three sets are
# disjoint in practice.
_CLAUDE_WALLS = (
    ("auth", _CLAUDE_AUTH_MARKERS),
    ("usage_limit", _CLAUDE_LIMIT_MARKERS),
    ("bad_model", _CLAUDE_MODEL_MARKERS),
)


def claude_wall(detail):
    """Which deterministic Claude-backend wall this attempt died on, or None.

    Returns a plain string reason so it can cross the activity-result boundary as JSON — the
    same shape a LOCAL wall uses (`wall_reason`), and for the same reason: which failures are
    walls is one decision, not an if-chain at each call site."""
    d = (detail or "").lower()
    for reason, markers in _CLAUDE_WALLS:
        if any(m in d for m in markers):
            return reason
    return None


def claude_auth_expired(detail):
    """Did this `claude -p` attempt die because Claude rejected our credentials? Pure text match
    over the CLI's stderr/last line — see the note above for why that is safe. Kept as its own
    predicate because "re-authenticate" is a different instruction from "wait for the reset"."""
    return claude_wall(detail) == "auth"


# What the operator is told, per reason. Each ends in the ONE action that clears it — the whole
# point of separating these from `harness_exhausted` is that they have known remedies.
_CLAUDE_WALL_REMEDY = {
    "auth": ("Claude rejected our credentials",
             "Re-authenticate on the machine running the worker (`claude /login`, or fix "
             "`ANTHROPIC_API_KEY`), then retry this run."),
    "usage_limit": ("Claude's usage limit is spent",
                    "Wait for the reset named above, then retry this run. Retrying sooner "
                    "reaches the same refusal."),
    "bad_model": ("Claude cannot serve the configured model",
                  "Fix the model name in Admin \u2192 Models (`data/models.json`), then retry "
                  "this run."),
}

_CLAUDE_WALL_REASON = {
    "auth": "claude_auth_expired",
    "usage_limit": "claude_usage_limit",
    "bad_model": "claude_model_unavailable",
}


def claude_wall_reason(reason):
    """The needs-human `reason` for a Claude wall — what the Needs-you dashboard and the audit
    row are labelled with.

    Falls back to the AUTH reason, not a generic one: `auth_stop` is an older boolean than this
    string, so an attempt dict carrying the flag but no reason (an in-flight run mid-deploy, or
    any caller that only mirrors the boolean) is by construction the wall that flag used to be
    the only name for. Degrades rather than raising on a reason a newer worker sent."""
    return _CLAUDE_WALL_REASON.get(reason, _CLAUDE_WALL_REASON["auth"])


def claude_wall_message(detail="", reason=None):
    """The operator-facing body for a Claude-backend wall. Carries the CLI's own words (so the
    trail keeps the raw evidence — a usage limit's line names the reset time, which is the one
    fact the operator needs) plus the remedy for this particular wall."""
    reason = reason or claude_wall(detail) or "auth"
    said = (detail or "").strip().splitlines()
    said = said[-1].strip() if said else ""
    what, remedy = _CLAUDE_WALL_REMEDY.get(reason, _CLAUDE_WALL_REMEDY["auth"])
    return ("\u26d4 **Stopped \u2014 nothing ran.** " + what
            + (f": {said[:300]}" if said else ".")
            + "\n\n" + remedy
            + " No further attempts were made \u2014 every one of them would have failed the "
              "same way.")


def claude_auth_message(detail=""):
    """Back-compat alias: the auth wall's body. Callers that have a reason should use
    `claude_wall_message(detail, reason)` so a usage limit is not reported as a bad login."""
    return claude_wall_message(detail, claude_wall(detail) or "auth")
