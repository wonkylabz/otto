"""Router #1 and the swarm planner: which capability, and is this really N sub-tasks.

Extracted from engine.py (which re-exports everything here — callers and tests keep using
engine.X, same facade contract as the other extracted layers). Owns the keyword shortlist +
LLM route call, swarm decomposition (`decompose`/`merge`), and the `plan()` route-only web
entry point. A wrong route is usually retrieval (the shortlist), not the model — diagnose
with `registry.rank()` before touching the prompt.
"""
import os
import re

import config
import gateway
import registry
from contracts import CONVERSATION_AUDIENCE, _DIRECT_REPLY_FORMAT
from ui import trace


def _eng():
    """The engine facade — tests monkeypatch attributes there, so patch-sensitive values and
    cross-calls resolve through it at call time, never bind at import. Same contract as the
    other extracted layers' _eng."""
    import engine
    return engine


# --- Router #1: which capability? -----------------------------------------

# Routing tunables. Descriptions carry the discriminating "use when / triggers on" text,
# usually past the first 160 chars — show more so the router can tell a specific skill from
# a general one. With a large catalogue (e.g. many plugin skills), shortlist by keyword
# overlap first so the long, clearly-irrelevant tail doesn't dilute the choice.
ROUTE_DESC_CHARS = 600        # enough to include skills' "Triggers on …" lists, the gold signal
ROUTE_SHORTLIST = 25


def _repo_eligible(caps, project_root):
    """Drop repo-scoped PROJECT caps that don't belong to the run's repo context. A project cap
    (`cap.source == "project"`, `cwd` = its repo) only resolves when `claude -p` runs from that
    repo, so it must NOT be an auto-routing candidate for a request targeting a DIFFERENT repo —
    or no repo at all. That keyword collision is how an inference/platform ticket mis-routed to
    an unrelated service's agent (its data-exporter's "Prometheus metrics / monitoring" matched).
    Project caps stay reachable by PINNING (a slash command skips routing) or by selecting their
    repo. Global, plugin and custom caps are repo-agnostic and always eligible."""
    root = os.path.abspath(os.path.expanduser(project_root)) if project_root else None
    out = []
    for c in caps:
        if getattr(c, "source", None) == "project":
            if root and os.path.abspath(c.cwd or "") == root:
                out.append(c)
        else:
            out.append(c)
    return out


def _shortlist(request, caps):
    """The candidate set Router #1 (and the swarm planner) choose from: the best `ROUTE_SHORTLIST`
    caps by lexical rank, but only when the catalogue is large AND there's signal to rank by —
    with no signal at all the whole catalogue goes, since weak signal must never drop the right
    capability. This is retrieval, and it bounds the router absolutely: whatever it drops, the
    model cannot pick."""
    # `route_hidden` caps are MODES the user opts into explicitly (today: `brainstorm`), never
    # route destinations. Brainstorm skips the verify ladder, so a router that could land there
    # on its own would silently strip a real task's only quality check — and its description
    # ("thinks an idea through", "weighs options") is exactly the vocabulary a genuine question
    # uses, so it would win those ties against `assistant`. Filtered here rather than at the
    # call sites because this is the documented candidate set for BOTH Router #1 and the swarm
    # planner. Pinning (`/brainstorm`) bypasses routing entirely and is unaffected.
    caps = [c for c in caps if not getattr(c, "route_hidden", False)]
    score = registry.rank(request, caps)                  # names are unique (registry de-dupes)
    # Ties are the norm at this scale, so break them by name — an ARBITRARY tie order silently
    # decides which caps survive a top-N cut, which made routing irreproducible run to run.
    ranked = sorted(caps, key=lambda c: (-score[c.name], c.name))
    if len(caps) > ROUTE_SHORTLIST and score[ranked[0].name] > 0:
        # Always fill the shortlist. It used to be cut to only the POSITIVE-scoring caps, which
        # collapsed to a handful whenever the request shared little vocabulary with any description
        # ("why is the webapp pod crashlooping" → 7 candidates, none of them the diagnostic cap) —
        # and a cap the router never sees is a cap it can never choose. A zero-scoring candidate
        # costs one line of prompt; an absent one costs the whole route.
        short = ranked[:ROUTE_SHORTLIST]
        # The general fallbacks (assistant, worker) score 0 on topic keywords (intentionally
        # topic-neutral), so keyword pruning would drop them — but they're the only place an
        # informational request (assistant) or an unmatched task (worker) can land. Always keep
        # them in the candidate set so the router LLM can choose them.
        for c in caps:
            if getattr(c, "general", False) and c not in short:
                short.append(c)
        return short
    return caps


def _confirm_route(chosen, sample):
    """Re-sample the router when its pick is a WRITE capability, and return the majority of
    `route_confirmations` samples (ties keep the first — i.e. today's behaviour).

    `claude -p` exposes no temperature, top-p or seed, so a single routing sample is a coin
    flip weighted by the prompt — the same instability `judging.confirm_adverse` exists for.
    Measured on "what's open on the board right now" against this operator's catalogue: 3 of 4
    live runs routed to the read `assistant` and answered in seconds on the local model, while
    the 4th picked `product-manager` (its description manages "a Projects v2 board", so the
    topic word matches hard) and a purely informational question paid a write route: the
    approval gate, a 15-minute Opus plan preview at $0.29, and a human decision — to then read
    the board. A follow-up 10-sample run went 10/10 assistant, so the flip is a low-rate tail,
    which is exactly why it reads as "the same task behaves differently every time".

    Only a WRITE pick is confirmed, because only a WRITE pick is expensive: it is what arms the
    plan gate and the preview tier. A read misroute is ungated, cheap and read-only, so it keeps
    costing one call.

    Deliberately a MAJORITY, unlike `confirm_adverse`'s asymmetric "the adverse verdict must
    reproduce". There, both outcomes are the same verdict on one axis and only one direction is
    costly. Here a flip changes WHICH WORK HAPPENS, and the reverse error is just as expensive in
    the other direction: letting one dissenting read sample win would send a genuine task to the
    `assistant`, which only answers and never acts — the silent under-delivery
    `assistant_write_redirect` was built to undo. A majority has no such bias: it can only pick
    what a single sample would have picked anyway, so the worst case is the status quo."""
    try:
        tries = max(1, int(config.setting("route_confirmations")))
    except (TypeError, ValueError):
        tries = 1
    if tries < 2 or getattr(chosen, "risk", None) != "write":
        return chosen
    picks = [chosen]
    for _ in range(tries - 1):
        again = sample()
        if again is not None:
            picks.append(again)
    # Count by name (Capability isn't hashable-by-identity across samples); ties keep the
    # first sample, so an unstable 3-way split is no worse than not confirming at all.
    best = max(picks, key=lambda c: (sum(1 for p in picks if p.name == c.name),
                                     -picks.index(c)))
    if best.name != chosen.name:
        trace("ROUTER", f"write route [{chosen.name}] did not hold over {len(picks)} samples "
                        f"-> [{best.kind}] {best.name}")
    return best


def route(request, caps, project_root=None):
    caps = [c for c in caps if getattr(c, "enabled", True)]
    caps = _repo_eligible(caps, project_root)   # repo-scoped project caps need matching repo ctx
    if not caps:
        return None
    score = registry.rank(request, caps)                  # names are unique (registry de-dupes)
    shortlist = _shortlist(request, caps)

    # Numbered from 1: a 0-indexed listing invites an off-by-one, since a model asked to pick from
    # a list answers with the ordinal it would use in prose. `stock` marks Otto's own bundled
    # generics — the user's purpose-built cap must win a tie against one (a bundled `qa-tester` was
    # beating the user's real `sre-qa` agent, which is strictly more capable at the same job).
    listing = "\n".join(
        f"{i}. [{c.kind}]{' [generic]' if getattr(c, 'source', None) == 'stock' else ''} "
        f"{c.name}: {c.description[:ROUTE_DESC_CHARS]}{'…' if len(c.description) > ROUTE_DESC_CHARS else ''}"
        for i, c in enumerate(shortlist, 1))
    prompt = (
        "You are a strict router for an SRE automation platform. Pick the SINGLE best "
        "capability for the user's request.\n"
        "A request often contains BACKGROUND CONTEXT (the problem, the systems involved) and "
        "pasted reference URLs. Do not let the topic or the systems mentioned dominate your "
        "choice. Identify the user's PRIMARY intended ACTION — the concrete deliverable they "
        "want produced, usually an imperative verb (create, open, post, apply, deploy, renew) — "
        "and route to the capability that PERFORMS that action, not one that merely reports on "
        "or matches the topic. (E.g. 'X is broken, <details>… create a ticket on the board' is "
        "a ticket-creation request, not an investigation of X.)\n"
        "Prefer the MOST SPECIFIC capability that matches the intent: a purpose-built skill "
        "beats a general CLI/tool or catch-all, which is a last resort only when nothing "
        "specific fits.\n"
        "EXCEPTION: if the request is purely INFORMATIONAL — a question seeking an answer or "
        "explanation with no concrete deliverable to produce (no ticket, PR, deploy, doc, or "
        "state change) — route to the general 'assistant' capability, NOT to a specialized "
        "agent/skill that merely matches the topic. A specialized capability performs an action; "
        "it is the wrong choice when the user just wants to KNOW something. A request to work "
        "on, fix, implement, or resolve an issue/ticket IS task-shaped (there is a deliverable) "
        "— never route it to 'assistant'. That includes asking to PICK/CHOOSE a ticket TO WORK "
        "ON ('pick a good candidate to work on from the issues'): the selection is a sub-step of "
        "implementing it, not the deliverable — route it to an implementer.\n"
        "DIAGNOSTIC EXCEPTION: if the request asks WHY something is broken/failing/regressed or "
        "WHAT caused a failure (e.g. 'why did this PR break the build', 'what is the issue', "
        "'diagnose this failure', 'what's wrong with X'), it is an INVESTIGATION that must gather "
        "fresh evidence. Route to a read-only investigation/diagnostic capability, NOT to one that "
        "merely reviews, reports on, or restates the referenced artifact (e.g. a PR-review cap is "
        "the WRONG choice for 'why did this PR break CI' — reviewing the diff is not diagnosing the "
        "failure), and NOT to 'assistant' (which only answers from prior context and cannot "
        "investigate). A pasted PR/build/alert/commit URL in such a request is a REFERENCE TO "
        "INVESTIGATE, not a request to review it.\n"
        "MANAGEMENT EXCEPTION: a capability that MANAGES work items (creates/refines/labels/"
        "organizes tickets, epics, or boards) is the WRONG choice for a request to IMPLEMENT "
        "what a ticket describes — 'work on issue #N', 'fix the bug in that ticket', 'pick a "
        "ticket and do it' asks for the CHANGE, not for ticket management. Route those to a "
        "capability that implements code/config changes (or the 'worker' fallback below).\n"
        "FALLBACK: if the request IS task-shaped — a concrete change or deliverable to produce — "
        "but NO capability specifically performs that action, route to the general 'worker' "
        "capability (a generic implementer), NOT to a specialized capability that merely matches "
        "the topic, and NOT to 'assistant' (which only answers, never acts).\n"
        "TIE-BREAK: a capability marked [generic] is one of Otto's own bundled stand-ins. When a "
        "non-generic capability performs the same action, prefer it — it is the user's own "
        "purpose-built one and knows their systems.\n"
        "Reply with ONLY the number, nothing else.\n\n"
        f"Request: {request}\n\nCapabilities:\n{listing}"
    )
    def sample():
        """One router sample. Returns the chosen Capability, or None when the reply doesn't
        name a listed option (the caller falls back to the keyword score)."""
        text = gateway.complete("routing", prompt) or ""
        # Take the LAST number in the reply, not the first: a reasoning-heavy model prefixes its
        # answer with prose that mentions other option numbers, and grabbing the first silently
        # routes to whichever capability that prose happened to name first.
        nums = re.findall(r"\d+", text)
        idx = int(nums[-1]) - 1 if nums else -1           # listing is numbered from 1
        if 0 <= idx < len(shortlist):
            return shortlist[idx]
        trace("ROUTER", f"unparseable reply {text[:60]!r}")
        return None

    chosen = sample()
    if chosen is None:
        best = max(shortlist, key=lambda c: score[c.name])
        trace("ROUTER", f"no listed option chosen -> fallback keyword score [{best.kind}] {best.name}")
        return best
    chosen = _confirm_route(chosen, sample)
    trace("ROUTER", f"Claude chose [{chosen.kind}] {chosen.name}  (from {len(shortlist)}/{len(caps)})")
    return chosen


# --- Planner: decompose into a parallel swarm -----------------------------

# How many sub-tasks a single request may fan out into. Bounded so a runaway plan can't
# spawn an unbounded swarm of child workflows.
MAX_SWARM = int(os.environ.get("OTTO_MAX_SWARM", "5"))


def _parse_plan(text, n_caps):
    """Parse the planner's reply into a list of {index, subtask}. PURE (no LLM) so it's
    unit-testable. Each fan-out line is 'N: <sub-task>', N indexing the capability listing.
    'SINGLE' (or empty / unparseable / out-of-range) yields [] — meaning don't fan out, take
    the single-capability path. Lines that don't parse are ignored, not fatal."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        m = re.match(r"^\[?\s*(\d+)\s*[\]:.)\-]\s*(.+)$", line)
        if not m:
            continue
        idx, sub = int(m.group(1)), m.group(2).strip()
        if 0 <= idx < n_caps and sub:
            out.append({"index": idx, "subtask": sub})
    return out


def decompose(request, caps, project_root=None):
    """Swarm planner: decide whether a request is really SEVERAL independent sub-tasks that
    can run in parallel, each handled by a different capability. Returns a list of
    {cap, request}; an EMPTY list means 'one cohesive task' — the caller falls back to the
    single-capability route() path (so single requests take the simple path, no regression).

    Runs on its own 'plan' model tier (configurable in the Admin tab — local-capable like
    routing, but separable so the fan-out decision can use a stronger model than Router #1).
    Conservative by design: it only splits when the deliverables are genuinely independent, so
    the common single-task case keeps costing just one extra (cheap) planning call."""
    caps = [c for c in caps if getattr(c, "enabled", True)]
    caps = _repo_eligible(caps, project_root)   # repo-scoped project caps need matching repo ctx
    if len(caps) < 2:
        return []
    shortlist = _shortlist(request, caps)
    listing = "\n".join(
        f"{i}. [{c.kind}] {c.name}: {c.description[:ROUTE_DESC_CHARS]}" for i, c in enumerate(shortlist))
    prompt = (
        "You are a task PLANNER for an SRE automation platform that runs small agents in "
        "parallel. Decide whether the user's request is really SEVERAL INDEPENDENT sub-tasks "
        "that could each be handled by a different capability and run concurrently.\n"
        "Only split when the request clearly contains MULTIPLE separate deliverables that do "
        "NOT depend on each other's output (e.g. 'check the failing build AND open a ticket AND "
        "post an update to Slack'). If it is one cohesive task — even a multi-step one that a "
        "single capability handles end to end — reply with exactly: SINGLE.\n"
        f"When you DO split, output one line per sub-task as 'N: <imperative sub-task>' (at most "
        f"{MAX_SWARM} lines), where N is the capability number from the list and the sub-task is "
        "fully self-contained (it runs on its own, with no shared context).\n\n"
        f"Request: {request}\n\nCapabilities:\n{listing}"
    )
    text = gateway.complete("plan", prompt)
    plan = _parse_plan(text, len(shortlist))
    if len(plan) < 2:
        trace("PLANNER", "single cohesive task -> no fan-out")
        return []
    seen, tasks = set(), []
    for p in plan[:MAX_SWARM]:
        cap = shortlist[p["index"]]
        key = (cap.name, p["subtask"].lower())
        if key in seen:                       # drop duplicate (cap, sub-task) lines
            continue
        seen.add(key)
        tasks.append({"cap": cap, "request": p["subtask"]})
    if len(tasks) < 2:
        return []
    trace("PLANNER", f"fanned out into {len(tasks)} sub-tasks: "
          + ", ".join(t["cap"].name for t in tasks))
    return tasks


def merge(request, parts, audience=None):
    """Synthesize the results of a swarm's sub-tasks into ONE coherent answer for the user.
    `parts` is a list of {cap, request, result}. Runs on the 'verify' tier — reasoning over
    already-produced outputs, the same shape verify does, and Claude by default. Synthesises
    rather than concatenates, but preserves each sub-task's concrete outcomes.

    THE MERGE IS WHAT GETS DELIVERED, so `audience` shapes it here (from `delivery.audience_for`,
    threaded through `_run_swarm`). The swarm's CHILDREN deliberately don't get an audience: nothing
    reads a child's output but this function, so a report is the right shape for them — the text a
    person actually receives is the one below. Only the conversational audience adds a contract; the
    prose above already IS the report contract, and layering `_REPORT_FORMAT` on it would be
    redundant."""
    parts = parts or []
    if not parts:
        return "(no sub-task results)"
    if len(parts) == 1:
        return parts[0].get("result") or "(no output)"
    blocks = "\n\n".join(
        f"### Sub-task {i + 1} — {p.get('cap')}\nAsked: {p.get('request')}\n"
        f"Result:\n{(p.get('result') or '(no output)')[:3000]}"
        for i, p in enumerate(parts))
    trace("MERGE", f"synthesizing {len(parts)} sub-task results into one response")
    shape = ("\n\n" + _DIRECT_REPLY_FORMAT) if audience == CONVERSATION_AUDIENCE else ""
    text = gateway.complete(
        "verify",
        "Several agents worked in PARALLEL on different parts of one request. Combine their "
        "results into a SINGLE, coherent answer for the user — synthesise, don't just "
        "concatenate. Preserve every concrete outcome (IDs, links, statuses, numbers). If a "
        "sub-task failed, note it briefly rather than hiding it."
        + shape + "\n\n"
        f"Original request: {request}\n\n{blocks}",
    ).strip()
    return text or "(merge produced no output)"


def plan(request, caps, project_root=None):
    """Web entry point: route only (no execution). Returns the chosen Capability."""
    return route(request, caps, project_root)
