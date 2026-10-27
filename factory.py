"""Factory mode — a seeded, self-driving build loop over ONE repo and the board.

The operator hands Otto a registered repo and a seed: the idea for an empty repo, or what to
finish or add for one that already has code (`_seed_goals` picks the GOALS.md shape). Then:

    sprint (PM, auto) ──▶ tickets in Ready ──board poll──▶ worker (repo-mode, auto)
        ──▶ draft PR ──▶ review + QA loops ──both PASS──▶ auto-merge ──▶ card Done
        ──queue drained──▶ next sprint … until GOALS.md is met, the budget is spent, or the
        line stalls

Everything here is a COMPOSITION of machinery that already exists — the board ingress, the post-PR
loops, the stock `product-manager` cap, an `approval: "auto"` run. What this module owns is the one
switch that sets all of it up and the three limits that make an unattended loop safe to leave:

  * **One switch, idempotent, reversible** (`enable` / `disable`). Enabling snapshots the board
    config it is about to change (`prev_board`) and disabling restores exactly that — it never
    touches the operator's own runbooks, and never kills a run in flight (same rule as ESTOP).
  * **Auto-approval is the operator's act, scoped to the factory repo.** Only the board poll
    ACTIVITY stamps a ticket with `auto_merge`/`factory` (`apply`), and only for the factory repo;
    nothing reads either from a client body. Every other ingress stays Manual.
  * **Every stop is loud.** Budget, stall, DONE and a manual stop each notify and land in the
    state's `history` — an unattended loop that just goes quiet reads as "Otto broke".
  * **Spend is fail-closed** (`spent` returns None when it can't measure): an unmeasurable budget
    starts no new sprint.

State lives in `data/factory.json` (RUNTIME in `snapshot.py`: an imported install must never
inherit an auto-approving loop). The sprint cadence is the `factory-sprint` Temporal Schedule.
"""
import base64
import datetime
import json
import os
import re
import sqlite3

import config
import storage
from ui import trace

SCHED_ID = "factory-sprint"
SPRINT_WF_ID = "factory-sprint-run"
PM_CAP = "product-manager"
GOALS_FILE = "GOALS.md"
SPEC_FILE = "SPEC.md"
_HISTORY = 20

_DEFAULTS = {
    "enabled": False,
    "repo": "",              # registered repo NAME (workspace.resolve)
    "slug": "",              # owner/name on GitHub, resolved at enable
    "seed": "",
    "budget_usd": 0.0,       # whole-factory ceiling; 0 refuses to enable
    "wip": 1,                # max factory cards In Progress at once
    "sprint_every_s": 1800,  # how often the sprint gate is checked
    "max_idle_sprints": 3,   # consecutive sprints with no merged PR -> stop
    "max_tickets": 5,        # new tickets a single sprint may ready
    "started_at": "",        # this factory's identity; tags every run it starts
    "sprint_n": 0,
    "idle_sprints": 0,
    "merged": 0,
    "merged_seen": 0,        # `merged` at the last sprint's end — the idle-sprint test
    "wids": [],              # every workflow this factory started (spend is summed over them)
    "status": "manual",
    "prev_board": None,
    "history": [],
    "paused": None,          # {reason, since, wids}: a Claude wall hit; sprints skip, not idle
    "held": None,            # {since}: the OPERATOR's hold — configured, starting nothing
    "walls_seen_at": "",     # audit watermark: walls at or before this are already handled
    "blind_ticks": 0,        # consecutive ticks that could not read spend or the board
    "merged_prs": [],        # PR urls already counted in `merged` (a retried merge counts once)
    "mcp_allow": [],         # MCP servers the factory's runs may use; every other one is denied
    "playtest_merged": -1,   # `merged` when the product was last playtested (-1 = never)
    "playtest_verdict": "",  # "clean" | "defects" | "" (unparseable or never)
}

_PLAYTEST_RE = re.compile(r"^[\s*_`>#-]*PLAYTEST:\s*(CLEAN|DEFECTS)\b", re.I)
_VERDICT_RE = re.compile(r"^[\s*_`>#-]*FACTORY:\s*(DONE|CONTINUE)\b(.*)$", re.I)

SPRINT_BRIEF = """\
You are running one SPRINT of an autonomous build loop ("factory mode") for the GitHub repo
`{slug}`, tracked on the GitHub Projects board `{project}`. Nobody is watching: do the sprint
end to end, never ask a question back.

1. Read `{goals}` at the root of `{slug}` (e.g. `gh api repos/{slug}/contents/{goals} --jq .content
   | base64 -d`). It holds the product vision and its Definition of Done — the only source of
   what "finished" means. Read the repo's current tree and the PRs merged so far
   (`gh pr list --repo {slug} --state merged`).
   The test and lint commands every ticket must pass are the ones under Commands in `{goals}`
   (no Commands section: `make test` and `make lint`).
   **Foundation first.** If the Product section of `{goals}` still says it is to be expanded by
   the foundation ticket, the FIRST ticket you ready must be the foundation: scaffold the project
   (manifest, `Makefile` with `run`/`test`/`lint`, a GitHub Actions workflow running lint +
   tests, a README skeleton) AND rewrite the Product section of `{goals}` into a concrete feature
   list and interface for the seed. Every other ticket this sprint depends on it, so give it the
   lowest number.
   **Assessment first.** If it says instead that it is to be written by the assessment ticket,
   the repo already had code: the FIRST ticket is the assessment, and it changes ONLY `{goals}`.
   It reads the code, README, manifest and CI and rewrites Product as what exists (marked done)
   plus what the Seed still asks for; fills Commands with the repo's OWN run/test/lint commands
   (a missing one is written "none yet"); and ticks every Definition-of-Done item already met.
   Never replace the repo's toolchain, layout or framework. Every other ticket waits for it. Check
   the tree YOURSELF now: if it has no test runner or linter, ready the ticket adding them right
   after the assessment, this same sprint — until it merges, a ticket's criteria name the tests
   it adds and how to run them instead.
   **Spec before features.** If `{spec}` does not exist yet, the next ticket (right after the LAST
   of the foundation, assessment and missing-runner tickets that applies — its tests need a
   runner — or the FIRST once those are merged) writes it, with two numbered
   sections: **Rules** (R1, R2…) — every domain rule the product must obey, taken from an
   authoritative source the ticket must find and cite (official rules, a standard, an RFC),
   stating explicitly what is ILLEGAL and when, never paraphrased from memory; and **UX**
   (U1, U2…) — what a first-time user must be able to tell at every moment without reading the
   code: whose turn or what state it is, what just happened and its outcome, who won each step,
   what actions are legal now. That ticket also adds a test per rule, legal AND illegal cases,
   written from the rule's text. Every later ticket cites the R/U ids it implements, and its
   acceptance criteria include a test for each cited rule.
2. Look at the board columns "{done}", "{blocked}" and "{review}" for issues of `{slug}`.
   Nothing new is picked up while ANY card sits in "{blocked}" or "{review}" — unblocking comes
   first. You cannot merge, and no run is coming back for a card in "{review}": its run ended
   before the merge (often a usage limit after the PR opened). Treat it exactly like a card in
   "{blocked}" — never wait for its PR to merge.
   A card labelled `{hold}` is a HUMAN's: never close its PR, re-ready, rewrite or close it.
   A re-readied ticket is rebuilt from scratch on a fresh branch off the default branch, so when
   you move one back to "{ready}", CLOSE its open PR with a one-line comment, and never point
   the issue text at an old PR or tell it how to branch or push — the platform owns git.
   For each card in "{blocked}" or "{review}": read its last comment (why it failed). If its PR is open and only
   an unobservable or wrong criterion failed (e.g. "passes in CI"), fix that criterion in the
   issue text and move it back to "{ready}". Otherwise EITHER rewrite the
   issue so it is smaller and unambiguous and move it back to "{ready}", adding the label
   `factory-retry-1` (or bump an existing `factory-retry-N`), OR — if it already carries
   `factory-retry-2` — close it with a one-line comment saying why it was dropped.
3. Write at most {max_tickets} NEW issues in `{slug}` for the next most valuable work toward the
   Definition of Done and the Product section. Each must be one coherent PR, mergeable on top of
   the current default branch, and state its own acceptance criteria — including the tests it adds
   and that the test and lint commands under Commands pass LOCALLY. Every criterion must be checkable from a
   clone of the PR branch: never "passes in CI" or anything only GitHub can observe — CI is a
   Definition-of-Done item YOU check (`gh run list --repo {slug}`), not a ticket's. Aim for a real product: a feature is done when a user
   can reach it from the interface, not when a function exists. Never duplicate an
   open or done issue. Add each to the board and move it to "{ready}" in the order it must be
   built — earliest dependency first (the line builds the lowest issue number first).
4. If a PLAYTEST REPORT is appended below, it is a product tester's account of using the
   current default branch as a real user. Every BLOCKER or MAJOR defect in it becomes a ticket
   (or is folded into an open one) BEFORE any new feature this sprint, citing the defect and the
   `{spec}` id it violates — a product that works but is wrong or unusable is not done.
5. Do NOT write code, open PRs, or touch the repo's files yourself — implementation is the
   worker's job once a card is in "{ready}".

Your reply MUST end with exactly one of these as its LAST line:
FACTORY: CONTINUE
FACTORY: DONE — <one line of evidence that every Definition-of-Done item is met>
Say DONE only when every Definition-of-Done item AND every Product feature is already merged —
check the tree, not the issue titles; never when work remains. DONE is refused while the latest
playtest still reports defects.
"""

PLAYTEST_BRIEF = """You are the product tester for `{slug}`, an autonomous build loop's only real user. Your
working directory is a fresh, throwaway clone of its default branch. Nobody is watching: never
ask a question back, never commit, push, or open a PR.

1. Read `{goals}` (the product and its Definition of Done) and `{spec}` if it exists (numbered
   Rules R* and UX U*). Without `{spec}`, hold the product to the authoritative rules of its
   domain and to what a first-time user needs to see.
2. Set it up and launch it exactly as its README says (e.g. `make run`, in the background), in
   this directory only. Use it the way a real user would, END TO END, more than once: for a
   game, play complete matches to the end; for an app, every main flow. A web UI is driven with
   a headless browser (Playwright — install it into a scratch venv or via `npx` if missing):
   screenshot every meaningful step and LOOK at each screenshot with the Read tool. A terminal
   UI is driven through its input and output. Stop everything you started when done.
3. Judge it as its user, not its author:
   - **Rules** — every move or action either side made or was offered: legal under {spec}/the
     domain's rules? Name any illegal one with the exact sequence that produced it.
   - **UX** — at every step, could a newcomer tell what state it is in, whose turn it is, what
     just happened, and who won each step? Is the outcome of each step SHOWN, not just logged?
   - **Quality** — layout, legibility, broken or unstyled elements, errors in the console or
     server log, anything a user would call unfinished.
4. Report a numbered list of DEFECTS. Each: what you did, what you saw (quote it, name the
   screenshot), the {spec} id or {goals} item it violates, and a severity — BLOCKER (wrong
   rules, a crash, a flow that cannot be completed), MAJOR (a user cannot tell what happened,
   or it looks broken), MINOR (polish). Be concrete enough that each can become a ticket.

Your reply MUST end with exactly one of these as its LAST line:
PLAYTEST: CLEAN   (no BLOCKER or MAJOR defect)
PLAYTEST: DEFECTS
"""

_COMMANDS_NEW = """\
## Commands
<!-- What every ticket must pass. The foundation ticket creates them. -->
- run: `make run`
- test: `make test`
- lint: `make lint`
"""

_COMMANDS_EXISTING = """\
## Commands
<!-- The repo's OWN commands, filled in by the assessment ticket. Every ticket must pass them. -->
_To be filled in by the assessment ticket._
"""

_PRODUCT_NEW = """\
<!-- Expanded by the FOUNDATION ticket: who it is for, the core loop, every feature the seed
     implies, and the interface people use it through. Edit it any time to steer the line. -->
_To be expanded by the foundation ticket._"""

_PRODUCT_EXISTING = """\
<!-- The repo already had code when the factory started, so the Seed says what to finish or add.
     Written by the ASSESSMENT ticket: what exists (done), then what the Seed still asks for.
     Edit it any time to steer the line. -->
_To be written by the assessment ticket._"""

GOALS_TEMPLATE = """\
# Goals

## Seed
{seed}

## Product
{product}

{commands}
## Definition of Done
<!-- The factory stops when every item here is merged. -->
- [ ] Runs from a fresh clone with one documented command (the run command under Commands)
- [ ] A real user interface suited to the product (for a game: a playable web or rich terminal UI) — not just a library
- [ ] Every feature listed under Product is implemented and reachable from that interface
- [ ] The test command runs the automated suite: core logic AND the interface's main flows
- [ ] The lint command passes, with a formatter and a linter configured
- [ ] CI (GitHub Actions) runs lint and tests on every pull request
- [ ] A project manifest with pinned dependencies (e.g. `pyproject.toml`, `package.json`)
- [ ] A README: what it is, how to install, run, test and use it
- [ ] `SPEC.md` holds the domain Rules (cited from an authoritative source) and the UX contract; every rule has a test
- [ ] The latest playtest of the default branch reports no BLOCKER or MAJOR defect

## Quality bar (every PR)
- New behaviour ships with tests; the test and lint commands pass
- No placeholders, stubs or TODOs standing in for the feature the ticket asks for
- One coherent, reviewable change
"""


def _path():
    return os.path.join(config.DATA_DIR, "factory.json")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def load():
    """Current factory state, defaults filled in. Never raises."""
    raw = storage.read_json(_path(), {}) or {}
    out = {**_DEFAULTS, **{k: v for k, v in raw.items() if k in _DEFAULTS}}
    out["wids"] = list(out.get("wids") or [])
    out["history"] = list(out.get("history") or [])
    return out


def _mutate(fn):
    def _apply(raw):
        cur = {**_DEFAULTS, **(raw or {})}
        fn(cur)
        return cur
    return storage.mutate_json(_path(), _apply, default={})


def _event(state, kind, detail=""):
    state["history"] = ([{"at": _now(), "event": kind, "detail": detail[:300]}]
                        + list(state.get("history") or []))[:_HISTORY]


def enabled(state=None):
    s = state if state is not None else load()
    return bool(s.get("enabled") and s.get("repo") and s.get("started_at"))


def tag(state):
    """A wid-safe form of `started_at` — names this factory's sprint runs."""
    return re.sub(r"[^0-9]", "", state.get("started_at") or "")[:14]


# --- the board half (called from activities.poll_board, never from a client) --------------

def owns(issue, state=None):
    """Is this board issue one of the factory repo's? Matched on the repo's basename, the same
    key `board.issue_to_request` resolves a repo by."""
    s = state if state is not None else load()
    if not enabled(s):
        return False
    slug = (issue.get("repo") or "").lower()
    if s.get("slug"):
        return slug == s["slug"].lower()
    return slug.split("/")[-1] == (s.get("repo") or "").lower()


def stuck_cards(counts, cfg):
    """Factory cards no run is moving: Blocked, AND the review column. A factory card only lands
    in review when its run ended before the merge — a Claude usage-limit wall after the PR
    opened, or a run outside factory mode — and nothing in the loop ever looks there, so the PM
    waited for a merge nobody would make until the stall limit stopped the factory."""
    cols = cfg.get("columns") or {}
    return sum((counts or {}).get(cols.get(k), 0) for k in ("blocked", "review"))


def line_stopped(counts, cfg):
    """True while any factory card is stuck (`stuck_cards`). Later tickets build on earlier ones,
    so picking the next while one is stuck runs it on a tree missing its prerequisites: the
    worker correctly reports the gap and changes nothing, and the card closes as if it were done."""
    return bool(stuck_cards(counts, cfg))


def apply(params, state):
    """Stamp a factory ticket's run params: always repo-mode + review + QA, never a clarify
    pause (nobody is there to answer), pre-authorized, and merged when both loops PASS.

    Pinned to the worker unless a label already pinned a cap: a factory ticket is code in the
    factory repo by construction, and Router #1 reading a game-rules ticket by its TOPIC sent
    half of them to the non-code `doer`. The name is resolved against the trusted registry by
    the poll activity, like any label pin."""
    if not params.get("cap"):
        params["cap"] = config.WORKER_CAP
    # The board's `hold` label still holds: a human asked to approve that ticket, so it waits at
    # the gate and is never merged unread.
    held = params.get("approval") == "ask"
    params.update({"repo": state["repo"], "repo_hint": None, "qa": True, "clarify": False,
                   "approval": "ask" if held else "auto", "auto_merge": not held,
                   "factory": state["started_at"],
                   # Always off the default branch, never onto an older PR the ticket mentions
                   # (activities.resolve_pr_target).
                   "fresh_branch": True})
    if isinstance(params.get("reply_to"), dict):
        params["reply_to"]["repo_edit"] = True
    return params


def claim(wid):
    """Record a workflow this factory started, so its spend counts against the budget."""
    def _fn(s):
        if wid not in s["wids"]:
            s["wids"] = list(s["wids"]) + [wid]
    _mutate(_fn)


def note_merge(factory_tag, pr_url):
    """A factory PR merged. Ignored when it belongs to an earlier factory (`started_at` moved)."""
    def _fn(s):
        if not (s.get("started_at") and s["started_at"] == factory_tag):
            return
        # Once per PR: a retried merge activity reaches here twice, and an inflated count hides
        # idle sprints and moves the playtest/DONE watermark past a tree nobody tested.
        if pr_url and pr_url in (s.get("merged_prs") or []):
            return
        s["merged_prs"] = list(s.get("merged_prs") or []) + ([pr_url] if pr_url else [])
        s["merged"] = int(s.get("merged") or 0) + 1
        _event(s, "merged", pr_url or "")
    _mutate(_fn)


# --- budget ------------------------------------------------------------------------------

_GEN_RE = re.compile(r"^r\d+(?:-|$)")


def _owned(wid, wids):
    """`wid` is a claimed run or one of its loops (`-rev0`, `-qa1`, `-play`). Never a later
    GENERATION (`-r1`) of a claimed card: the factory claims each generation itself, and on a
    shared board `gh-issue-5-r1` may be another repo's issue #5."""
    for w in wids:
        if wid == w:
            return True
        if wid.startswith(w + "-") and not _GEN_RE.match(wid[len(w) + 1:]):
            return True
    return False


def spent(state=None):
    """USD spent by every run this factory started (attempt rows + tier calls), or None when it
    cannot be measured — the caller treats None as "start nothing new"."""
    s = state if state is not None else load()
    wids = s.get("wids") or []
    if not wids:
        return 0.0
    since = s.get("started_at") or ""
    total = 0.0
    try:
        conn = storage.sqlite_connect(config.DB_PATH)
        try:
            for wf, data in conn.execute(
                    "SELECT workflow, data FROM audit WHERE at >= ?", (since,)).fetchall():
                if _owned(wf or "", wids):
                    total += float((json.loads(data) or {}).get("cost_usd") or 0)
            try:
                rows = conn.execute("SELECT wid, cost_usd FROM tier_calls WHERE at >= ?",
                                    (since,)).fetchall()
            except sqlite3.OperationalError:
                rows = []     # no tier call ever ledgered on this install
            for wid, cost in rows:
                if _owned(wid or "", wids):
                    total += float(cost or 0)
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        trace("FACTORY", f"spend unmeasurable: {str(e)[:120]}")
        return None
    return round(total, 4)


def mcp_allow_for(wid, state=None):
    """The MCP servers a run may use when this factory started it, or None (not a factory run:
    no restriction). Keyed on OWNERSHIP, not on `enabled` — a run still going after Manual was
    switched back on keeps the scope it started under. A factory run is unattended and
    pre-approved, so the operator's personal servers (mail, chat, home) are off unless ticked."""
    s = state if state is not None else load()
    if not wid or not _owned(wid, s.get("wids") or []):
        return None
    return list(s.get("mcp_allow") or [])


# --- a Claude wall pauses the line ---------------------------------------------------------
# A spent usage limit (or an expired login) fails EVERY run the same way until it clears — it
# says nothing about the tickets. Left to the normal machinery, each sprint after it planned
# nothing and counted idle, so the factory stopped itself as "stalled" hours before the limit
# reset, and the PM then rewrote the walled ticket as if it had failed, spending a retry on it.
WALL_REASONS = ("claude_usage_limit", "claude_auth_expired")
_SPRINT_WID_RE = re.compile(r"^factory-(\d+)-s(\d+)(?:-play)?$")


def is_sprint(wid):
    """A factory sprint (or its playtest) run id. Never retried by hand: the next scheduled
    sprint re-reads the board as it is NOW, while a retry replays a stale brief as an ordinary
    run — outside the factory's WIP cap and budget, and working the very tickets the line has
    since re-readied (`web-bcb63055`, a retried sprint #3 closing PR #83 under #78's re-run)."""
    return bool(_SPRINT_WID_RE.match(wid or ""))


def superseded(wid, state=None):
    """A sprint card nobody needs to look at: a LATER sprint of the same factory has run, and
    that sprint re-read the board. Factory mode is meant to run without a human, so a dead
    sprint must not wait in Needs-you for one to dismiss it."""
    m = _SPRINT_WID_RE.match(wid or "")
    if not m:
        return False
    s = state if state is not None else load()
    return m.group(1) == tag(s) and int(s.get("sprint_n") or 0) > int(m.group(2))


_ISSUE_WID_RE = re.compile(r"^gh-issue-(\d+)(?:-r\d+)?(?:-|$)")


def _walls_since(s):
    """[(wid, reason, at)] of factory runs that died on a WALL after the watermark. Never
    raises: an unreadable trail is no evidence of a wall."""
    since = max(s.get("started_at") or "", s.get("walls_seen_at") or "")
    wids = s.get("wids") or []
    out = []
    try:
        conn = storage.sqlite_connect(config.DB_PATH)
        try:
            rows = conn.execute(
                "SELECT workflow, at, json_extract(data, '$.reason') FROM audit WHERE at > ? "
                "AND json_extract(data, '$.outcome') = 'needs_human'", (since,)).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        if "no such table" not in str(e):       # a fresh install has no trail yet
            trace("FACTORY", f"wall check skipped: {str(e)[:120]}")
        return []
    for wf, at, reason in rows:
        if reason in WALL_REASONS and _owned(wf or "", wids):
            out.append((wf, reason, at))
    return out


def pause(reason, wids, seen_at=None):
    """Hold the line on a wall: no sprint, no pickup, no idle count. Notifies once."""
    import delivery
    before = load()
    if before.get("paused"):
        return before
    fresh = []

    def _fn(s):
        if s.get("paused"):          # a concurrent pause won the lock: keep its record
            return
        fresh.append(True)
        s["paused"] = {"reason": reason, "since": _now(), "wids": sorted(set(wids))}
        s["walls_seen_at"] = max(s.get("walls_seen_at") or "", seen_at or _now())
        _event(s, "paused", f"{reason}: {', '.join(sorted(set(wids)))[:200]}")
    state = _mutate(_fn)
    if not fresh:
        return state
    delivery.notify("Otto factory paused: Claude unavailable",
                    lines=[f"repo: {before.get('slug')}", f"reason: {reason}",
                           "resumes by itself once Claude answers again"],
                    tags=["factory"], priority="default")
    return state


def claude_available():
    """(ok, detail) from one tool-free cheap-tier turn — the same wall every run would hit."""
    import claude_cli
    import gateway
    model = gateway.downshift_model_id() or config.ROUTER_MODEL
    out = claude_cli.run_json("Reply with the single word OK.", model=model,
                              timeout=config.CLAUDE_TIER_TIMEOUT_S,
                              disallowed_tools=config.ALL_BUILTIN_TOOLS,
                              setting_sources="", strict_mcp=True)
    if out.get("is_error"):
        return False, str(out.get("result") or "")[:200]
    return True, ""


def _requeue(s, wids):
    """Move each walled ticket's card from Blocked back to Ready — no retry label: nothing was
    wrong with the ticket. A card already elsewhere (the PM moved it, a human did) is left."""
    import board
    nums = {int(m.group(1)) for m in (_ISSUE_WID_RE.match(w) for w in wids) if m}
    if not nums:
        return []
    bcfg = board.load()
    cols = bcfg.get("columns") or {}
    data = board.item_list(bcfg)
    if data is None:
        return None
    meta = board.project_meta(bcfg)
    moved = []
    for stub in board._parse_items_in(data, bcfg, cols.get("blocked")):
        if stub.get("number") in nums and (stub.get("repo") or "").lower() == (s.get("slug") or "").lower():
            if not board.set_status(bcfg, meta, stub.get("item_id"), cols.get("ready")):
                return None
            moved.append(stub["number"])
    return moved


def resume():
    """Claude answers again: re-ready the walled tickets, clear the pause, notify."""
    import delivery
    s = load()
    p = s.get("paused") or {}
    # Runs already in flight when it paused may have walled since; the watermark moves to now
    # below, so they are re-readied here or never.
    wids = list(p.get("wids") or []) + [w for w, _r, _a in _walls_since(s)]
    moved = _requeue(s, wids)
    if moved is None:
        # Clearing the pause now would strand the walled cards in Blocked, where the PM spends a
        # retry on them — the exact thing the pause exists to prevent. Stay paused, try next tick.
        _mutate(lambda st: _event(st, "resume_deferred", "board unreadable"))
        return None

    def _fn(st):
        st["paused"] = None
        st["walls_seen_at"] = _now()
        _event(st, "resumed", "re-readied " + (", ".join(f"#{n}" for n in moved) or "nothing"))
    state = _mutate(_fn)
    delivery.notify("Otto factory resumed", tags=["factory"], priority="default",
                    lines=[f"repo: {s.get('slug')}",
                           f"re-readied: {', '.join(f'#{n}' for n in moved) or 'none'}"])
    return state


def _wall_gate(s):
    """None to carry on, or the gate's skip result while a wall holds."""
    if not s.get("paused"):
        walls = _walls_since(s)
        if not walls:
            return None
        pause(walls[-1][1], [w for w, _r, _a in walls], seen_at=max(a for _w, _r, a in walls))
        return {"go": False, "why": f"paused: {walls[-1][1]}"}
    ok, detail = claude_available()
    if not ok:
        return {"go": False, "why": f"paused: {s['paused'].get('reason')} ({detail[:80]})"}
    if resume() is None:
        return {"go": False, "why": "paused: resume deferred (board unreadable)"}
    return None


# --- enable / disable --------------------------------------------------------------------

class Refused(Exception):
    """A precondition failed; the message says which, in the operator's words."""


def _gh(args, timeout=60):
    import board
    return board._run(["gh"] + args, timeout=timeout)


def _repo_slug(name):
    import workspace
    r = workspace.resolve(name)
    if not r:
        return None
    return workspace.origin_slug(workspace._git_origin(r["path"]) or "")


# Files a fresh GitHub repo can be created with: a root holding only these is still empty.
_SCAFFOLD_ONLY = {GOALS_FILE.lower(), "readme", "readme.md", "readme.rst", "readme.txt", "license",
                  "license.md", "license.txt", "copying", ".gitignore", ".gitattributes"}


def _has_code(slug):
    """True when the repo's root holds anything beyond what `gh repo create` adds. A repo with
    no commits answers 404 "This repository is empty"; any other failure raises Refused —
    guessing "empty" would scaffold a second toolchain over a real project."""
    rc, out, err = _gh(["api", f"repos/{slug}/contents", "--jq", ".[].name"])
    if rc != 0:
        if "empty" in (err or "").lower():
            return False
        raise Refused(f"could not read {slug}'s files: {(err or '').strip()[:160]}")
    return any(n.strip().lower() not in _SCAFFOLD_ONLY for n in out.splitlines() if n.strip())


def goals_text(seed, existing):
    """GOALS.md for a new project (the foundation ticket scaffolds it) or one that already has
    code (the assessment ticket describes it and names its own commands)."""
    return GOALS_TEMPLATE.format(
        seed=seed.strip(), product=_PRODUCT_EXISTING if existing else _PRODUCT_NEW,
        commands=_COMMANDS_EXISTING if existing else _COMMANDS_NEW)


def _seed_goals(slug, seed):
    """Commit GOALS.md through the contents API — it works on a repo with NO commits, which a
    clone-and-push cannot. Skipped when the file is already there (re-enable, or the operator
    wrote their own)."""
    rc, _, _ = _gh(["api", f"repos/{slug}/contents/{GOALS_FILE}", "--jq", ".sha"])
    if rc == 0:
        return "kept"
    existing = _has_code(slug)
    body = base64.b64encode(goals_text(seed, existing).encode()).decode()
    rc, _, err = _gh(["api", "-X", "PUT", f"repos/{slug}/contents/{GOALS_FILE}",
                      "-f", "message=Seed the factory's goals",
                      "-f", f"content={body}"])
    if rc != 0:
        raise Refused(f"could not write {GOALS_FILE} to {slug}: {err[:160]}")
    return "seeded for an existing project" if existing else "seeded"


def _int(v, lo, default):
    try:
        return max(lo, int(v))
    except (TypeError, ValueError):
        return default


def validate(form):
    """Normalize the enable form, or raise Refused naming the first bad field."""
    repo = str(form.get("repo") or "").strip()
    seed = str(form.get("seed") or "").strip()
    try:
        budget = float(form.get("budget_usd") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    if not repo:
        raise Refused("pick the repo the factory builds in")
    if not seed:
        raise Refused("write the seed — the idea the factory builds")
    if budget <= 0:
        raise Refused("set a budget above $0 — a factory with no ceiling never stops spending")
    allow = form.get("mcp_allow") or []
    if not isinstance(allow, list):
        raise Refused("mcp_allow must be a list of MCP server names")
    return {"repo": repo, "seed": seed, "budget_usd": round(budget, 2),
            "mcp_allow": sorted({str(n).strip() for n in allow if str(n).strip()}),
            "wip": _int(form.get("wip"), 1, 1),
            "sprint_every_s": _int(form.get("sprint_every_s"), 300, 1800),
            "max_idle_sprints": _int(form.get("max_idle_sprints"), 1, 3),
            "max_tickets": _int(form.get("max_tickets"), 1, 5)}


def enable(form):
    """Turn Factory mode on: check every precondition, seed GOALS.md, point the board at
    auto-approval and create the sprint schedule — ON HOLD. Nothing runs until `start()`: the
    operator configuring it may still have the GitHub events to wire on another tab, and a save
    that started a sprint gave them no chance. Idempotent — a second call with the factory
    already on just updates the knobs, and never starts or holds the line. Raises Refused."""
    import board
    import estop
    import workspace
    cfg = validate(form)
    if estop.engaged():
        raise Refused("the global pause is on — release it first")
    if not workspace.resolve(cfg["repo"]):
        raise Refused(f"'{cfg['repo']}' is not a registered repo (Admin → Project repos)")
    slug = _repo_slug(cfg["repo"])
    if not slug:
        raise Refused(f"'{cfg['repo']}' has no GitHub origin")
    bcfg = board.load()
    if not board.project_spec(bcfg.get("project")):
        raise Refused("configure the GitHub board first (Events → Board queue)")
    meta = board.project_meta(bcfg)
    if not meta.get("project_id") or not meta.get("status_field_id"):
        err = meta.get("error") or ""
        if "scope" in err:
            raise Refused("gh can't read the board: its login lacks the `project` scope — run "
                          "`gh auth refresh -s project` on this machine, then retry")
        raise Refused(f"the GitHub board is unreachable or has no "
                      f"'{bcfg.get('status_field') or 'Status'}' field"
                      + (f": {err[:160]}" if err else ""))
    # The loop reads and moves cards BY NAME: an "In Progress" that is "In progress" on the board
    # counts zero in flight (no WIP cap at all), and a missing Blocked hides every failure from
    # the PM. Refuse rather than run a line that cannot see itself.
    cols = bcfg.get("columns") or {}
    missing = [f"{k} → '{cols.get(k)}'" for k in ("ready", "active", "review", "done", "blocked")
               if cols.get(k) not in (meta.get("options") or {})]
    if missing:
        raise Refused("the board has no column named " + ", ".join(missing)
                      + f" (it has: {', '.join(sorted(meta.get('options') or {}))}) — fix the "
                      "names in Events → Board queue → configure, or add the column on GitHub")
    goals = _seed_goals(slug, cfg["seed"])

    started = []

    def _fn(s):
        fresh = not enabled(s)
        started.append(fresh)
        s.update(cfg)
        s["slug"] = slug
        if fresh:
            # EVERYTHING a run of the line accumulates resets, or a new factory inherits the old
            # one's clean playtest (and declares DONE untested) or its pause (and re-readies the
            # new repo's cards whose numbers match the old walled tickets).
            s.update({"started_at": _now(), "sprint_n": 0, "idle_sprints": 0, "merged": 0,
                      "merged_seen": 0, "wids": [], "prev_board": bcfg, "paused": None,
                      "walls_seen_at": "", "playtest_merged": -1, "playtest_verdict": "",
                      "blind_ticks": 0, "merged_prs": [], "held": {"since": _now()}})
        s["enabled"], s["status"] = True, "running"
        _event(s, "enabled", f"{slug}, budget ${cfg['budget_usd']}, wip {cfg['wip']}, "
                             f"{GOALS_FILE} {goals}")
    state = _mutate(_fn)
    board.save({**bcfg, "enabled": True, "approval_default": "auto"})
    sched = {"board": board.reconcile_schedule(), "sprint": reconcile_schedule()}
    trace("FACTORY", f"enabled on {slug}: {sched}")
    if not str(sched["sprint"]).startswith(("created", "updated")):
        if started and started[0]:
            # No schedule, no sprint: "running" at sprint 0 forever. Undo and say why.
            disable("schedule_failed", str(sched["sprint"]))
            raise Refused(f"could not create the sprint schedule ({sched['sprint']}) — is "
                          "Temporal up? Nothing was left enabled")
        # An EDIT of a running factory: its existing schedule still fires, so a Temporal blip
        # must not switch the whole line off — the new knobs are saved, the schedule is not.
        raise Refused(f"settings saved, but the sprint schedule could not be updated "
                      f"({sched['sprint']}) — the factory keeps running on its old cadence")
    return {**state, "schedules": sched}


def start():
    """Release the hold and fire a sprint now. Raises Refused when there is nothing to start."""
    import estop
    s = load()
    if not enabled(s):
        raise Refused("Factory mode is off — configure it first")
    if estop.engaged():
        raise Refused("the global pause is on — release it first")

    def _fn(st):
        st["held"] = None
        _event(st, "started", "by the operator")
    state = _mutate(_fn)
    sched = reconcile_schedule(trigger=True)
    trace("FACTORY", f"started on {s.get('slug')}: {sched}")
    if "triggered" not in str(sched):
        # Released anyway: the schedule's own next tick starts the sprint. Say so, not "started".
        return {**state, "schedule": sched,
                "warning": f"no sprint fired now ({sched}) — the next scheduled check starts one"}
    return {**state, "schedule": sched}


def hold():
    """Stop starting work, keep everything configured. Runs in flight finish; their PRs still
    merge. Unlike `disable`, the board stays on auto-approval and the schedule keeps ticking
    (each tick skips), so `start()` resumes the same line, counters and budget intact."""
    s = load()
    if not enabled(s):
        raise Refused("Factory mode is off")

    def _fn(st):
        if not st.get("held"):
            st["held"] = {"since": _now()}
            _event(st, "on_hold", "by the operator")
    return _mutate(_fn)


def disable(reason="manual", detail=""):
    """Turn Factory mode off: delete the sprint schedule and restore the board config exactly as
    it was before enable. Never cancels a run in flight. Idempotent. Notifies on every stop."""
    import board
    import delivery
    before = load()
    if not enabled(before):
        return before
    # The board first: marking the factory off before a failed restore leaves auto-approval on
    # with no factory to switch off again (a second disable returns early above).
    if isinstance(before.get("prev_board"), dict):
        board.save(before["prev_board"])
        board.reconcile_schedule()
    # A LIMIT stop must stop the work too: the factory's tickets still in Ready would otherwise
    # run as ordinary board tickets — no WIP cap, no budget, no MCP allowlist. Manual (and a
    # refused enable) leave them for the operator, who is the one deciding.
    def _fn(s):
        s["enabled"] = False
        s["status"] = f"stopped: {reason}"
        _event(s, f"stopped:{reason}", detail)
    state = _mutate(_fn)
    # After marking it off, so a poll in between cannot start one more factory ticket.
    held = _hold_ready(before) if reason in _LIMIT_STOPS else []
    if held:
        state = _mutate(lambda s: _event(s, "held", f"Ready → Blocked: {held}"))
    sched = reconcile_schedule()
    trace("FACTORY", f"stopped ({reason}): {detail[:160]} — {sched}")
    delivery.notify(f"Otto factory stopped: {reason}",
                    lines=[f"repo: {before.get('slug')}", f"sprints: {before.get('sprint_n')}",
                           f"merged PRs: {before.get('merged')}",
                           f"spent: {_usd(spent(before))} of ${before.get('budget_usd')}"],
                    detail=detail or None, tags=["factory"],
                    priority="default" if reason in ("done", "manual") else "high")
    return state


_LIMIT_STOPS = ("budget", "stalled")


def _usd(v):
    return "unknown" if v is None else f"${v:.2f}"


def _hold_ready(s):
    """Move the factory repo's Ready cards to Blocked; returns their numbers. Best-effort."""
    import board
    try:
        bcfg = board.load()
        cols = bcfg.get("columns") or {}
        data = board.item_list(bcfg)
        if data is None:
            return []
        meta = board.project_meta(bcfg)
        return [st["number"] for st in board._parse_items_in(data, bcfg, cols.get("ready"))
                if (st.get("repo") or "").lower() == (s.get("slug") or "").lower()
                and board.set_status(bcfg, meta, st.get("item_id"), cols.get("blocked"))]
    except Exception as e:  # noqa: BLE001 - a stop must still complete and notify
        trace("FACTORY", f"could not hold the Ready cards: {str(e)[:160]}")
        return []


def pickup_room(state, counts, cfg, meta):
    """(room, why) — how many more factory tickets the board poll may start now. Zero while the
    line is stopped or paused, when the card could not be moved to In progress (incomplete
    metadata: the run would start, the card would stay in Ready, and WIP would read 0 forever),
    and past or unable to measure the budget (the sprint gate checks it only once per tick)."""
    cols = cfg.get("columns") or {}
    if state.get("held"):
        return 0, "on hold"
    if state.get("paused") or line_stopped(counts, cfg):
        return 0, "line stopped"
    meta = meta or {}
    if not (meta.get("project_id") and meta.get("status_field_id")
            and cols.get("active") in (meta.get("options") or {})):
        return 0, "board metadata incomplete"
    cost = spent(state)
    if cost is None or cost >= float(state.get("budget_usd") or 0):
        return 0, "budget"
    active = (counts or {}).get(cols.get("active"), 0)
    return max(0, int(state.get("wip") or 1) - active), ""


def _blind(why):
    """A tick that could not see (spend or board): skipped, but never silently for long — the
    third in a row notifies once, so a lasting fault does not read as a healthy, idle line."""
    import delivery
    st = _mutate(lambda s: s.update(blind_ticks=int(s.get("blind_ticks") or 0) + 1))
    if st["blind_ticks"] == 3:
        _mutate(lambda s: _event(s, "blind", why))
        delivery.notify("Otto factory can't see its state", tags=["factory"], priority="high",
                        lines=[f"repo: {st.get('slug')}", f"cause: {why}",
                               "no sprint starts until this clears"])
    return {"go": False, "why": why}


# --- the sprint gate (activities.factory_gate / factory_after_sprint) -----------------------

def gate():
    """Decide whether this tick starts a sprint. Returns {go, why, ...}; with go=True it also
    carries the child workflow id and params. Stops the factory when a limit is reached."""
    import board
    import estop
    s = load()
    if not enabled(s):
        return {"go": False, "why": "factory off"}
    if estop.blocked("factory"):
        return {"go": False, "why": "paused"}
    # Ahead of the wall gate: on hold, nothing should be probed, counted or re-readied.
    if s.get("held"):
        return {"go": False, "why": "on hold"}
    # Before the stall and budget limits: a wall's skipped sprints must not count against them.
    held = _wall_gate(s)
    if held:
        return held
    s = load()
    cost = spent(s)
    if cost is None:
        return _blind("spend unmeasurable")
    if cost >= float(s["budget_usd"] or 0):
        disable("budget", f"spent ${cost:.2f} of ${s['budget_usd']}")
        return {"go": False, "why": "budget"}
    if int(s["idle_sprints"]) >= int(s["max_idle_sprints"]):
        disable("stalled", f"{s['idle_sprints']} sprints in a row merged nothing")
        return {"go": False, "why": "stalled"}
    bcfg = board.load()
    counts = board.column_counts(bcfg, repo=s.get("slug"), skip_label=bcfg.get("hold_label"))
    if counts is None:
        return _blind("board unreadable")
    if s.get("blind_ticks"):
        _mutate(lambda st: st.update(blind_ticks=0))
    cols = bcfg.get("columns") or {}
    ready, active = (counts.get(cols.get(k), 0) for k in ("ready", "active"))
    if active:
        return {"go": False, "why": f"{active} ticket(s) in progress"}
    # A stuck card stops the line (`line_stopped`), so Ready cannot drain while one sits there:
    # that is exactly when a sprint must run — the PM is the one who unblocks it.
    if ready and not stuck_cards(counts, bcfg):
        return {"go": False, "why": f"{ready} ticket(s) still queued"}

    n = int(s["sprint_n"]) + 1
    wid = f"factory-{tag(s)}-s{n}"

    def _fn(st):
        st["sprint_n"] = n
        if wid not in st["wids"]:
            st["wids"] = list(st["wids"]) + [wid]
        _event(st, "sprint", f"#{n}")
    _mutate(_fn)
    brief = SPRINT_BRIEF.format(
        slug=s["slug"], project=board.project_spec(bcfg.get("project")), goals=GOALS_FILE,
        done=cols.get("done"), blocked=cols.get("blocked"), ready=cols.get("ready"),
        review=cols.get("review"), spec=SPEC_FILE, hold=bcfg.get("hold_label") or "hold",
        max_tickets=s["max_tickets"])
    params = {"request": f"Run factory sprint #{n} for {s['slug']}.",
              "cap_name": PM_CAP, "approval": "auto", "unattended": True, "scheduled": True,
              "doc": brief, "factory": s["started_at"],
              "chat_key": f"factory-{tag(s)}", "chat_title": f"Factory — {s['slug']}"[:80],
              "chat_labels": ["factory"]}
    # The product is playtested once per change to it: never before the first merge, and never
    # twice over the same tree (a playtest costs a whole run).
    merged = int(s.get("merged") or 0)
    playtest = merged > 0 and merged != int(s.get("playtest_merged", -1))
    return {"go": True, "wid": wid, "params": params, "sprint": n, "playtest": playtest}


def playtest_request(state=None):
    s = state if state is not None else load()
    return PLAYTEST_BRIEF.format(slug=s.get("slug"), goals=GOALS_FILE, spec=SPEC_FILE)


def playtest_verdict(result):
    """The playtest's LAST non-blank line: "clean" | "defects" | "" (unparseable)."""
    lines = [l for l in (result or "").splitlines() if l.strip()]
    m = _PLAYTEST_RE.match(lines[-1]) if lines else None
    return m.group(1).lower() if m else ""


def note_playtest(result):
    """Book a playtest against the merge count it tested. Unparseable counts as DEFECTS for the
    DONE gate (`playtest_clean`) — a tester that could not say "clean" has not said it."""
    word = playtest_verdict(result)

    def _fn(st):
        st["playtest_merged"] = int(st.get("merged") or 0)
        st["playtest_verdict"] = word
        _event(st, "playtest", word or "unparseable")
    _mutate(_fn)
    return word


def note_playtest_skipped(why):
    """A playtest that could not run is recorded as such, so DONE's refusal names the cause."""
    _mutate(lambda st: _event(st, "playtest_skipped", why))
    trace("FACTORY", f"playtest skipped: {why}")


def playtest_clean(state):
    """DONE needs the CURRENT tree playtested clean — a stale clean predates the last merge."""
    return (state.get("playtest_verdict") == "clean"
            and int(state.get("playtest_merged", -1)) == int(state.get("merged") or 0))


def playtest_block(result):
    """The report as the PM brief's appendix (factory.SPRINT_BRIEF step 4)."""
    text = (result or "").strip() or "(the playtest produced no report)"
    return "\n\n---\nPLAYTEST REPORT (the current default branch, used as a real user):\n\n" + text


def verdict(result):
    """The sprint's LAST non-blank line: "done" | "continue" | None (unparseable)."""
    lines = [l for l in (result or "").splitlines() if l.strip()]
    if not lines:
        return None, ""
    m = _VERDICT_RE.match(lines[-1])
    if not m:
        return None, ""
    return m.group(1).lower(), m.group(2).strip(" —-:")


def after_sprint(payload):
    """Book a finished sprint: DONE stops the factory; otherwise count it idle when nothing
    merged since the previous one. An unparseable reply counts as CONTINUE — the budget and the
    idle limit bound it, and a false DONE would end the line on a model's slip."""
    word, evidence = verdict(payload.get("result"))
    s = load()
    if not enabled(s):
        return {"state": "off"}
    nh = payload.get("needs_human") or {}
    if nh.get("reason") in WALL_REASONS:
        pause(nh["reason"], [payload.get("wid") or ""])
        return {"state": "paused", "reason": nh["reason"]}
    if word == "done" and playtest_clean(s):
        disable("done", evidence)
        return {"state": "done"}
    if word == "done":
        # The PM's own check reads the tree; only the playtest USED it. Not idle either way:
        # the next sprint turns the defects into tickets.
        _mutate(lambda st: _event(st, "done_refused", "the latest playtest is not clean (verdict: "
                                  f"{st.get('playtest_verdict') or 'none'}, of merge "
                                  f"{st.get('playtest_merged')} / {st.get('merged')})"))

    def _fn(st):
        merged = int(st.get("merged") or 0)
        st["idle_sprints"] = 0 if merged > int(st.get("merged_seen") or 0) else (
            int(st.get("idle_sprints") or 0) + 1)
        st["merged_seen"] = merged
        if payload.get("needs_human"):
            _event(st, "sprint_blocked", str((payload.get("needs_human") or {}).get("reason")))
    st = _mutate(_fn)
    return {"state": "continue", "idle_sprints": st["idle_sprints"], "verdict": word}


# --- the Temporal schedule ---------------------------------------------------------------

def reconcile_schedule(trigger=False):
    """Create/update (or delete when off) the `factory-sprint` Schedule; `trigger` fires one tick
    now. Best-effort — returns a short status, never raises."""
    import temporal_client as tc
    if not tc.OK:
        return "skipped (no temporalio)"
    try:
        return tc.run(_reconcile(load(), trigger))
    except Exception as e:  # noqa: BLE001 - Temporal unreachable / transient
        return f"skipped ({str(e)[:80]})"


async def _reconcile(state, trigger):
    import temporal_client as tc
    from datetime import timedelta
    from temporalio.client import (
        Schedule, ScheduleActionStartWorkflow, ScheduleIntervalSpec, ScheduleOverlapPolicy,
        SchedulePolicy, ScheduleSpec, ScheduleUpdate,
    )
    from workflows import FactorySprintWorkflow
    c = await tc.client()
    h = c.get_schedule_handle(SCHED_ID)
    if not enabled(state):
        try:
            await h.delete()
        except Exception:  # noqa: BLE001 - not there to begin with
            pass
        return "deleted"
    every = timedelta(seconds=max(300, int(state.get("sprint_every_s") or 1800)))
    fresh = Schedule(
        # A sprint holds a whole PM run, so its bound is generous; SKIP never stacks two.
        action=ScheduleActionStartWorkflow(
            FactorySprintWorkflow.run, id=SPRINT_WF_ID, task_queue=tc.TASK_QUEUE,
            execution_timeout=max(every * config.POLL_TIMEOUT_FACTOR, timedelta(hours=4))),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    try:
        await h.describe()
    except Exception:  # noqa: BLE001 - missing -> create
        await c.create_schedule(SCHED_ID, fresh)
        status = "created"
    else:
        def _apply(inp, fresh=fresh):
            sch = inp.description.schedule
            sch.spec, sch.action, sch.policy = fresh.spec, fresh.action, fresh.policy
            return ScheduleUpdate(schedule=sch)
        await h.update(_apply)
        status = "updated"
    if trigger:
        # The schedule's action takes no args, so trigger() runs exactly what a tick would.
        await h.trigger()
        status += ", first sprint triggered"
    return f"{status} (every {int(every.total_seconds())}s)"


def status():
    """State for the Admin card: the stored state minus the board snapshot, plus spend."""
    s = load()
    s.pop("prev_board", None)
    s["spent_usd"] = spent(s)
    return s
