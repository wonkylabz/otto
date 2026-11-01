"""Integrations — who owns each reply-target `kind`.

An integration is a vendor-specific ingress/egress pair (Slack, GitHub, a webhook) declared
ONCE, so the core dispatches on `reply_to["kind"]` through `for_kind` instead of branching on
vendor names in `delivery`, `privacy`, `activities` and `workflows`. A new integration is a module
here plus a card in the UI, never an edit to the pipeline (`ReplyKindDispatchRatchetTests`).

Not "plugin" (a Claude Code plugin's skills/agents, `registry.py`) and not "connector" (a
claude.ai OAuth connector, `mcp_client.py`) — both words are taken.

PURE at import: no I/O, no clock, no env. `audience_for` is called from workflow code through
`delivery`, which is passed through the Temporal sandbox, so this module must stay deterministic
and import nothing that is not."""
from dataclasses import dataclass
import importlib

# Audiences — the ONE split delivery, contracts and the judge all read (delivery.AUDIENCE owns
# the values today; these mirror it by value, deliberately: importing delivery here is a cycle).
CONVERSATION = "conversation"   # a person in a live exchange, who replies
REPORT = "report"               # the operator, or a durable record read later
NOTICE = "notice"               # a one-way post nobody asked for


@dataclass(frozen=True)
class Integration:
    """One vendor's declaration. Every callable takes the SAME arguments the core's old branch
    did, so moving a body here is a cut-and-paste, not a redesign.

    `deliver(reply_to, result, cap, run_id) -> str` — the push; returns a status for the trace.
        It receives text `delivery.deliver` has ALREADY redacted and may raise: that caller owns
        the scrub and the never-raises wrapper, so nothing else may call it with raw text.
    `source_line(reply_to) -> str | None` — the privacy-safe origin for a push notification.
    `interim(reply_to, text) -> (bool, str, ts | None)` — a mid-run note; None = no such channel.
    `on_gate_notice(reply_to, wid, notice_ts) -> None` — after a gate notice LANDED: arm the
        conversation so the reply that clears it is matched to this run.
    `on_delivered(reply_to, payload) -> None` — bookkeeping after `deliver` (session, busy flag).
    `on_needs_human(reply_to) -> dict` — card/label moves when a run parks; {} = nothing to move.
    """
    name: str
    kinds: tuple
    audiences: dict
    deliver: object
    source_line: object
    interim: object = None
    on_gate_notice: object = None
    on_delivered: object = None
    on_needs_human: object = None


BUNDLED = ("slack", "github", "webhook")
_LOADED = None


def all():  # noqa: A001 - mirrors registry.all / scheduler.all
    """Every registered integration, bundled order. Loaded once; the modules are pure."""
    global _LOADED
    if _LOADED is None:
        found = tuple(importlib.import_module(f"integrations.{n}").INTEGRATION for n in BUNDLED)
        seen = {}
        for i in found:
            for k in i.kinds:
                if k in seen:
                    raise RuntimeError(f"reply kind {k!r} is owned by both {seen[k]} and {i.name}")
                seen[k] = i.name
        _LOADED = found
    return _LOADED


def for_kind(kind):
    """The integration owning a reply-target kind, or None — the caller decides the default."""
    for i in all():
        if kind in i.kinds:
            return i
    return None


def audiences():
    """kind -> audience across every integration; what `delivery.AUDIENCE` is built from."""
    out = {}
    for i in all():
        out.update(i.audiences)
    return out
