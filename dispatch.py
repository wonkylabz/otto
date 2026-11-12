"""Which runtime serves an attempt — the DECISION, as a pure function.

`engine._resolve_backend` gathers the facts (the resolved pool entry, what the cap declares, the
session being resumed, the latch) and hands them here; this module turns them into one
`Dispatch`. Same split as `ladder.py`: the side effects stay with the caller, the rules live
once and are table-tested (`DispatchDecisionTests`).

The rules, in the order they apply — a later one sees what an earlier one decided:

  1. A cap carrying its own `.mcp.json` stays on Claude: only `claude -p` reads one.
  2. A subprocess backend cannot reach a claude.ai connector, and Codex grants no MCP at all.
  3. A cap latched off a weak local model stays off it. A per-run pick is exempt.
  4. A resume follows the SESSION's backend, and its model with it.
  5. A local wall proven earlier this run keeps the rest of the ladder on Claude.
  6. A fast-lane run never lands on Codex: a shell, and none of the MCP it was granted.
  7. A per-run pick that is not a Claude model is dropped once the run lands on Claude.

Every move OFF the picked backend honours the fallback contract: fallback on substitutes
Claude and records why, strict mode stops. A resume is never moved — its history lives in the
runtime that minted it.

PURE by construction: no I/O, no clock, no `config`/`gateway` import.
"""
from dataclasses import dataclass

CLAUDE, LOCAL, CODEX = "claude", "local", "codex"

# Why an attempt cannot run at all. STRICT: fallback is off and the pick cannot serve it.
# REFUSED: nothing to fall back TO (Claude cannot resume a Codex session).
STRICT, REFUSED = "strict", "refused"


@dataclass(frozen=True)
class Dispatch:
    backend: str                  # CLAUDE | LOCAL | CODEX — where this attempt runs
    exec_entry: dict              # the pool entry; kept on a fallback so the badge names it
    pick: str = CLAUDE            # the backend the entry asked for, before any rule
    override_entry: dict = None   # the per-run pick, if it survived
    resume_bound: dict = None     # the session's own entry, when the resolved one mismatched
    fallback: dict = None         # {"fallback_from", "fallback_reason"} on a move to Claude
    stop: str = None              # STRICT | REFUSED | None
    why: str = ""                 # the stop's reason
    notes: tuple = ()             # trace lines, in decision order


def blocker_reason(where, connectors, stdio):
    """Two lists because they are two facts: a connector is unreachable from EVERY subprocess
    backend, a stdio server is withheld from Codex only."""
    parts = []
    if connectors:
        parts.append("needs claude.ai connectors (" + ", ".join(connectors)
                     + "), which are OAuth'd inside Claude Code and cannot be served by "
                       "any other backend")
    if stdio:
        parts.append("declares MCP servers (" + ", ".join(stdio)
                     + ") that the codex backend does not grant yet — their credentials "
                       "would have to travel on the command line (issue #121); the local "
                       "backend serves them normally")
    return f"cannot run on the {where} backend: " + "; ".join(parts)


FAST_LANE_CODEX = ("a fast-lane run is granted MCP tools only, and the codex backend has a "
                   "shell and no MCP")


def decide(*, cap_name, exec_entry, pick, override_entry=None, override_is_claude=True,
           own_mcp=False, session_backend=None, resume_candidate=None, connectors=(),
           declared=(), kind=LOCAL, latched=None, latch_fails=0, local_disabled=False,
           fallback=True, fast_lane=False):
    """One `Dispatch` from plain facts.

    `session_backend` is None for a fresh attempt; `resume_candidate` is the pool entry that can
    serve the session. `connectors` are the claude.ai connectors the cap or the request needs,
    `declared` the stdio servers the cap declares. `kind` is the entry's CLASS
    (`gateway.model_kind`), which is what the latch is evidence about. `latched` is a zero-argument
    callable, asked only once every other condition of rule 3 holds: it is a store read."""
    resuming = session_backend is not None
    backend = pick if (pick in (LOCAL, CODEX) and not own_mcp) else CLAUDE
    name = exec_entry["name"]
    forced, notes = None, []

    def result(**kw):
        return Dispatch(backend=backend, exec_entry=exec_entry, pick=pick,
                        override_entry=override_entry, fallback=forced, notes=tuple(notes), **kw)

    def stopped(reason, why):
        return result(stop=reason, why=why)

    if backend != CLAUDE and not resuming:
        connectors = list(connectors)
        stdio = [n for n in declared if n not in connectors] if backend == CODEX else []
        if connectors or stdio:
            why = blocker_reason(backend, connectors, stdio)
            if not fallback:
                return stopped(STRICT, why)
            backend, forced = CLAUDE, {"fallback_from": name, "fallback_reason": why}
            notes.append(f"{cap_name} {why} — running on Claude")

    if (backend == LOCAL and not resuming and not override_entry and kind == LOCAL
            and latched is not None and latched()):
        why = (f"{cap_name} has failed verification on {name} {latch_fails} times in a row — "
               f"latched off the local backend until it is re-tested")
        if not fallback:
            return stopped(STRICT, why)
        backend, forced = CLAUDE, {"fallback_from": name, "fallback_reason": why}
        notes.append(f"{why} — running on Claude")

    resume_bound = None
    if resuming:
        backend = session_backend
        if pick != session_backend and resume_candidate:
            notes.append(f"{name} is on the wrong backend for this resumed session — "
                         f"continuing on {resume_candidate['name']}")
            override_entry, exec_entry, resume_bound = None, resume_candidate, resume_candidate
    elif local_disabled and fallback:
        backend = CLAUDE

    if fast_lane and backend == CODEX:
        if resuming:
            return stopped(REFUSED, FAST_LANE_CODEX)
        if not fallback:
            return stopped(STRICT, FAST_LANE_CODEX)
        backend = CLAUDE
        forced = {"fallback_from": exec_entry["name"], "fallback_reason": FAST_LANE_CODEX}
        notes.append(f"{FAST_LANE_CODEX} — running on Claude")

    # A non-Claude id handed to `claude -p --model` is rejected, and that refusal is the answer.
    if override_entry and backend == CLAUDE and not override_is_claude:
        notes.append(f"{override_entry['name']} is pinned but this run is on the Claude "
                     f"backend — using the Claude execution model instead")
        override_entry = None

    return result(resume_bound=resume_bound)
