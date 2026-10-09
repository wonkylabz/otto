"""Factory mode — a seeded, self-driving build loop over ONE repo and the board.

The operator hands Otto an empty registered repo and a seed (the idea). From then on:

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
}

_VERDICT_RE = re.compile(r"^[\s*_`>#-]*FACTORY:\s*(DONE|CONTINUE)\b(.*)$", re.I)

SPRINT_BRIEF = """\
You are running one SPRINT of an autonomous build loop ("factory mode") for the GitHub repo
`{slug}`, tracked on the GitHub Projects board `{project}`. Nobody is watching: do the sprint
end to end, never ask a question back.

1. Read `{goals}` at the root of `{slug}` (e.g. `gh api repos/{slug}/contents/{goals} --jq .content
   | base64 -d`). It holds the product vision and its Definition of Done — the only source of
   what "finished" means. Read the repo's current tree and the PRs merged so far
   (`gh pr list --repo {slug} --state merged`).
2. Look at the board columns "{done}" and "{blocked}" for issues of `{slug}`.
   For each card in "{blocked}": read its last comment (why it failed), then EITHER rewrite the
   issue so it is smaller and unambiguous and move it back to "{ready}", adding the label
   `factory-retry-1` (or bump an existing `factory-retry-N`), OR — if it already carries
   `factory-retry-2` — close it with a one-line comment saying why it was dropped.
3. Write at most {max_tickets} NEW issues in `{slug}` for the next most valuable work toward the
   Definition of Done. Each must be small (one PR), independently mergeable on top of the
   current default branch, testable, and state its own acceptance criteria. Never duplicate an
   open or done issue. Add each to the board and move it to "{ready}" in the order it must be
   built — earliest dependency first (the line builds the lowest issue number first).
4. Do NOT write code, open PRs, or touch the repo's files yourself — implementation is the
   worker's job once a card is in "{ready}".

Your reply MUST end with exactly one of these as its LAST line:
FACTORY: CONTINUE
FACTORY: DONE — <one line of evidence that every Definition-of-Done item is met>
Say DONE only when every Definition-of-Done item is already merged; never when work remains.
"""

GOALS_TEMPLATE = """\
# Goals

## Seed
{seed}

## Definition of Done
<!-- The factory's PM refines this list on its first sprint; edit it any time to steer the line.
     The factory stops when every item here is merged. -->
- [ ] A first playable / usable version of the seed above, runnable from a fresh clone
- [ ] Automated tests that exercise its core behaviour, runnable with one command
- [ ] A README explaining how to run it
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


def apply(params, state):
    """Stamp a factory ticket's run params: always repo-mode + review + QA, never a clarify
    pause (nobody is there to answer), pre-authorized, and merged when both loops PASS."""
    params.update({"repo": state["repo"], "repo_hint": None, "qa": True, "clarify": False,
                   "approval": "auto", "auto_merge": True, "factory": state["started_at"]})
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
        if s.get("started_at") and s["started_at"] == factory_tag:
            s["merged"] = int(s.get("merged") or 0) + 1
            _event(s, "merged", pr_url or "")
    _mutate(_fn)


# --- budget ------------------------------------------------------------------------------

def _owned(wid, wids):
    return any(wid == w or wid.startswith(w + "-") for w in wids)


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


def _seed_goals(slug, seed):
    """Commit GOALS.md through the contents API — it works on a repo with NO commits, which a
    clone-and-push cannot. Skipped when the file is already there (re-enable, or the operator
    wrote their own)."""
    rc, _, _ = _gh(["api", f"repos/{slug}/contents/{GOALS_FILE}", "--jq", ".sha"])
    if rc == 0:
        return "kept"
    body = base64.b64encode(GOALS_TEMPLATE.format(seed=seed.strip()).encode()).decode()
    rc, _, err = _gh(["api", "-X", "PUT", f"repos/{slug}/contents/{GOALS_FILE}",
                      "-f", "message=Seed the factory's goals",
                      "-f", f"content={body}"])
    if rc != 0:
        raise Refused(f"could not write {GOALS_FILE} to {slug}: {err[:160]}")
    return "seeded"


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
    return {"repo": repo, "seed": seed, "budget_usd": round(budget, 2),
            "wip": _int(form.get("wip"), 1, 1),
            "sprint_every_s": _int(form.get("sprint_every_s"), 300, 1800),
            "max_idle_sprints": _int(form.get("max_idle_sprints"), 1, 3),
            "max_tickets": _int(form.get("max_tickets"), 1, 5)}


def enable(form):
    """Turn Factory mode on: check every precondition, seed GOALS.md, point the board at
    auto-approval, create the sprint schedule and kick the first sprint. Idempotent — a second
    call with the factory already on just updates the knobs. Raises Refused."""
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

    def _fn(s):
        fresh = not enabled(s)
        s.update(cfg)
        s["slug"] = slug
        if fresh:
            s.update({"started_at": _now(), "sprint_n": 0, "idle_sprints": 0, "merged": 0,
                      "merged_seen": 0, "wids": [], "prev_board": bcfg})
        s["enabled"], s["status"] = True, "running"
        _event(s, "enabled", f"{slug}, budget ${cfg['budget_usd']}, wip {cfg['wip']}, "
                             f"{GOALS_FILE} {goals}")
    state = _mutate(_fn)
    board.save({**bcfg, "enabled": True, "approval_default": "auto"})
    sched = {"board": board.reconcile_schedule(), "sprint": reconcile_schedule(trigger=True)}
    trace("FACTORY", f"enabled on {slug}: {sched}")
    return {**state, "schedules": sched}


def disable(reason="manual", detail=""):
    """Turn Factory mode off: delete the sprint schedule and restore the board config exactly as
    it was before enable. Never cancels a run in flight. Idempotent. Notifies on every stop."""
    import board
    import delivery
    before = load()
    if not enabled(before):
        return before

    def _fn(s):
        s["enabled"] = False
        s["status"] = f"stopped: {reason}"
        _event(s, f"stopped:{reason}", detail)
    state = _mutate(_fn)
    if isinstance(before.get("prev_board"), dict):
        board.save(before["prev_board"])
        board.reconcile_schedule()
    sched = reconcile_schedule()
    trace("FACTORY", f"stopped ({reason}): {detail[:160]} — {sched}")
    delivery.notify(f"Otto factory stopped: {reason}",
                    lines=[f"repo: {before.get('slug')}", f"sprints: {before.get('sprint_n')}",
                           f"merged PRs: {before.get('merged')}",
                           f"spent: ${spent(before) or 0:.2f} of ${before.get('budget_usd')}"],
                    detail=detail or None, tags=["factory"],
                    priority="default" if reason in ("done", "manual") else "high")
    return state


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
    cost = spent(s)
    if cost is None:
        return {"go": False, "why": "spend unmeasurable"}
    if cost >= float(s["budget_usd"] or 0):
        disable("budget", f"spent ${cost:.2f} of ${s['budget_usd']}")
        return {"go": False, "why": "budget"}
    if int(s["idle_sprints"]) >= int(s["max_idle_sprints"]):
        disable("stalled", f"{s['idle_sprints']} sprints in a row merged nothing")
        return {"go": False, "why": "stalled"}
    bcfg = board.load()
    counts = board.column_counts(bcfg, repo=s.get("slug"))
    if counts is None:
        return {"go": False, "why": "board unreadable"}
    cols = bcfg.get("columns") or {}
    queued = counts.get(cols.get("ready"), 0) + counts.get(cols.get("active"), 0)
    if queued:
        return {"go": False, "why": f"{queued} ticket(s) still queued"}

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
        max_tickets=s["max_tickets"])
    params = {"request": f"Run factory sprint #{n} for {s['slug']}.",
              "cap_name": PM_CAP, "approval": "auto", "unattended": True, "scheduled": True,
              "doc": brief, "factory": s["started_at"],
              "chat_key": f"factory-{tag(s)}", "chat_title": f"Factory — {s['slug']}"[:80],
              "chat_labels": ["factory"]}
    return {"go": True, "wid": wid, "params": params, "sprint": n}


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
    if word == "done":
        disable("done", evidence)
        return {"state": "done"}

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
