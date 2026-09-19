"""The verify -> retry -> escalate ladder's CONTROL FLOW, as a pure state machine.

The loop runs in two places and always will: `engine._ladder_core` calls `run_attempt`/`verify`/
`record_attempt` as functions, while `OttoWorkflow._verify_ladder` must reach the same three
things as Temporal ACTIVITIES, because workflow code is replay-deterministic and may not do I/O.
That is a hard process boundary and no refactor removes it.

But the boundary is around the SIDE EFFECTS, not around the decisions. These were re-derived
independently in both copies, and every one of them is arithmetic over counters:

  - `judged` vs `attempt` — a harness death must not spend a judged rung, because no judge read
    it. `attempt` stays the PHYSICAL index (transcript filename, audit row) so two attempts never
    collide on it; `judged` is what drives escalation and exhaustion.
  - the `spare` budget a harness death draws on instead.
  - `final = judged == n - 1`, which escalates the model on the last rung.
  - arming the supervisor's kill switch: never on the final rung (a kill's only value is the
    critique it hands the NEXT attempt) and never past `max_supervisor_kills`.
  - latching `local_disabled` when a WRITE cap fails verify on the local backend — but NOT on a
    harness death, which is not evidence about the model.

`engine._ladder_core`'s own docstring records what it cost to learn that a duplicated loop does
not stay duplicated: the two copies inside `engine.py` had already drifted at the verify call,
one passing `local=` without `project=` and the other the reverse. The workflow mirror was then
left as a permanent third copy, kept in step by prose ("change one, mirror the other") and by no
test at all. This module is that prose turned into code.

PURE by construction: no I/O, no clock, no `config` import. Limits are passed IN — which is also
what lets the workflow use it, since a workflow may never read the mutable settings store.
"""
from dataclasses import dataclass, replace

# Why a run stopped. `PASSED` is the only good one; the rest all end in needs-human except where
# the caller downgrades them (repo-mode with an open PR is advisory-only).
PASSED = "passed"
VERIFY_EXHAUSTED = "verify_exhausted"
HARNESS_EXHAUSTED = "harness_exhausted"


@dataclass(frozen=True)
class Limits:
    """The bounds one run is held to. Read ONCE by the caller — from `config.setting` on the sync
    path, from the workflow's settings SNAPSHOT on the durable one, which is why they arrive as
    plain numbers rather than being looked up here."""
    max_attempts: int = 1
    max_harness_retries: int = 0
    max_supervisor_kills: int = 0
    local_fallback: bool = True
    write_local_escalate_reason: str = ""

    @classmethod
    def of(cls, max_attempts, max_harness_retries, max_supervisor_kills,
           local_fallback=True, write_local_escalate_reason=""):
        """Clamp as both copies did: at least one attempt, never a negative budget."""
        return cls(max(1, int(max_attempts)), max(0, int(max_harness_retries)),
                   max(0, int(max_supervisor_kills)), bool(local_fallback),
                   write_local_escalate_reason)


@dataclass(frozen=True)
class State:
    """Where the ladder has got to. Frozen: every transition returns a new one, so a caller cannot
    half-update it and leave the counters disagreeing."""
    attempt: int = 0          # PHYSICAL attempt index — transcripts and audit rows key on it
    judged: int = 0           # rungs a judge actually spent a verdict on
    spare: int = 0            # harness-death budget remaining
    kills: int = 0            # supervisor kills spent
    local_disabled: bool = False
    local_disabled_reason: str = None
    critique: str = None


@dataclass(frozen=True)
class Attempt:
    """What the next attempt is to be run with."""
    attempt: int
    final: bool               # last judged rung: escalate the model
    supervise_enforce: bool


@dataclass(frozen=True)
class Step:
    """What to do after a verdict."""
    stop: bool
    reason: str
    state: State


def start(limits):
    """The state before the first attempt."""
    return State(spare=limits.max_harness_retries)


def plan_attempt(state, limits):
    """The next attempt's index and the two flags derived from the counters.

    `final` is keyed on `judged`, never on `attempt` — that is the whole point of separating them.
    The kill switch is disarmed on the final rung because there is no next attempt for the
    supervisor's critique to steer; the run would just end holding an aborted partial."""
    attempt = state.attempt + 1
    final = state.judged == limits.max_attempts - 1
    return Attempt(attempt=attempt, final=final,
                   supervise_enforce=(not final and state.kills < limits.max_supervisor_kills))


def record_attempt(state, attempt, *, killed=False, local_incapable=False):
    """Fold one finished attempt's facts into the state. `local_disabled` LATCHES — a backend that
    proved it cannot serve this cap fails the same way on every later rung."""
    return replace(state,
                   attempt=attempt.attempt if isinstance(attempt, Attempt) else attempt,
                   kills=state.kills + (1 if killed else 0),
                   local_disabled=state.local_disabled or bool(local_incapable))


def next_step(state, limits, verdict, *, write_local=False):
    """Apply a verdict and say whether the ladder continues.

    The two rules worth stating, because both copies got them wrong at some point:

    A HARNESS DEATH IS NOT A VERDICT. It draws on `spare` rather than a judged rung (no judge read
    the work, so it is not evidence about the capability), and for the same reason it must not
    trigger the local write-escalation — a local model that merely ran out of output tokens would
    otherwise be banished from the rest of the run, which measured at three attempts ending on
    Opus for a ceiling an env var raises (`web-a056884d`).

    A SUPERVISOR KILL DOES spend a judged rung: enforce-mode is a deliberate intervention that
    `max_attempts` is documented to bound."""
    if verdict and verdict.get("passed"):
        return Step(True, PASSED, state)
    harness = bool(verdict) and verdict.get("source") == "harness"
    if (limits.local_fallback and not state.local_disabled and write_local and not harness):
        state = replace(state, local_disabled=True,
                        local_disabled_reason=limits.write_local_escalate_reason)
    state = replace(state, critique=(verdict or {}).get("critique"))
    if harness:
        state = replace(state, spare=state.spare - 1)
        if state.spare < 0:
            return Step(True, HARNESS_EXHAUSTED, state)
    else:
        state = replace(state, judged=state.judged + 1)
        if state.judged >= limits.max_attempts:
            return Step(True, VERIFY_EXHAUSTED, state)
    return Step(False, None, state)


def error_verdict(result):
    """A synthetic FAILED verdict for an attempt that errored, timed out or was killed.

    Lives here, beside the rung accounting that reads its `source`, because it is the one thing
    both runtimes must agree on to classify a rung: a `(timed out)` string is never handed to the
    verifier as real output — the attempt counts as a failure and the ladder takes its next shot.
    A supervisor-killed attempt gets the supervisor's own critique as steering; that IS the
    enforce-mode mechanism: kill, then restart with the course-correction folded in.

    Both carry `source`, so neither is mistaken for a judgement downstream: measured over the
    trail, 98 of 291 recorded verify failures were one of these two, and `scorecard` was pricing
    all of them as the capability's fault. `judging.error_verdict` re-exports this; the workflow
    used to re-implement it inline, string parse and all."""
    text = str(result)
    if "(aborted by supervisor:" in text:
        reason = text.split("(aborted by supervisor:", 1)[1].strip().rstrip(")").strip()
        return {"passed": False, "source": "supervisor",
                "critique": ("the mid-run supervisor stopped the previous attempt because it was "
                             f"off-course: {reason} — take a different approach this time.")}
    return {"passed": False, "source": "harness",
            "critique": "prior attempt errored or timed out: " + text[:200]}
