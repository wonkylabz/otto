"""The Temporal-schedule layer over runbooks — Temporal owns the firing, runbooks.py owns the
definition.

A runbook WITH a cron gets a Temporal Schedule: "on this cron, start an OttoWorkflow with this
runbook's rendered args, marked scheduled (unattended)." Temporal is the single durable
scheduler — survives restarts, no duplicate fires, visible in the Temporal UI. `data/runbooks.json`
fires nothing; it's the definition, so it can't reintroduce the old multiple-scheduler
duplicate-fire bug. Run history lives in the Audit tab.

A runbook WITHOUT a cron has NO Temporal Schedule object at all. That's deliberate: an on-demand
run can be given parameter values at the moment it's clicked, and a Schedule's action args are
fixed at creation — so "run now" starts a workflow DIRECTLY (`run_now`) rather than going through
`ScheduleHandle.trigger()`. A cron fire has nobody to prompt, so it renders with defaults only
(which `runbooks.normalize` guarantees exist for any required param on a scheduled runbook).

Requires Temporal for cron firing. Without it, `available()` is False and the server returns 503
for the schedule-touching endpoints; `run_now` degrades to the direct path like any other run.
"""
import os
import uuid

import config
import runbooks
import storage
import temporal_client as tc

if tc.OK:
    from temporalio.client import (
        Schedule, ScheduleActionStartWorkflow, ScheduleOverlapPolicy,
        SchedulePolicy, ScheduleSpec, ScheduleUpdate,
    )
    from workflows import OttoWorkflow

ID_PREFIX = "otto-"
# Schedule ids created before the Mosaic->Otto rename. Existing schedules KEEP their old ids (they
# are migrated into the runbook store under the SAME id, and _reconcile re-points them at
# OttoWorkflow and the current task queue) — this prefix only keeps the orphan GC able to sweep a
# stale pre-rename schedule the store no longer tracks.
_LEGACY_ID_PREFIXES = ("mosaic-",)
_LEGACY_STORE = os.path.join(config.DATA_DIR, "schedules.json")


def cron_valid(expr):
    return runbooks.cron_valid(expr)


def local_tz_name():
    """IANA timezone for schedule cron expressions. Without this, Temporal fires crons in
    UTC — so a user in (say) UTC+12 sets "0 9 * * *" and it fires at 9pm local, looking like
    "cron doesn't run". We default to the server's local zone so cron means local time.
    Override with OTTO_SCHEDULE_TZ; returns None (UTC) only if nothing is detectable."""
    tz = os.environ.get("OTTO_SCHEDULE_TZ") or os.environ.get("TZ")
    if tz:
        return tz
    try:
        with open("/etc/timezone") as f:
            name = f.read().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        link = os.readlink("/etc/localtime")        # .../zoneinfo/Pacific/Auckland
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return None


def available():
    return tc.connected()


def migrate_legacy():
    """Import pre-runbook `data/schedules.json` rows into the runbook store, keyed by their
    ORIGINAL schedule id so the Temporal schedule that already exists keeps firing the same
    definition. Idempotent (runbooks.migrate_schedules skips ids already present), so it runs on
    every startup; the legacy file is left on disk untouched as a forensic copy."""
    legacy = storage.read_json(_LEGACY_STORE, {})
    return runbooks.migrate_schedules(legacy) if legacy else 0


# --- workflow args ---------------------------------------------------------

def _args(rid, rb, values=None, unattended=True):
    """The OttoWorkflow input for one runbook run.

    `cap` PINS the capability so Router #1 is skipped — the fix for a request that resolves to a
    project skill (repo-scoped, hence NOT a routing candidate for a no-repo run) and would
    otherwise silently fall back to the general assistant. A pinned project cap runs from its own
    cwd (activities._cap reconstructs it), so no repo/clone is needed. Per-STEP caps are resolved
    the same way inside execute_plan.

    `chat_key` ties every firing of this runbook to ONE Chat sidebar thread (created on the first
    run, appended thereafter)."""
    r = runbooks.render(rb, values)
    args = {"request": r["request"], "scheduled": unattended,
            "unattended": unattended,
            "auto_approve": bool(rb.get("auto_approve")),
            "chat_key": f"chat-{rid}", "chat_title": (rb.get("name") or r["request"])[:80],
            # One label, not both: "scheduled-job" is what every existing scheduled chat is
            # already tagged with and what the sidebar filters on — re-tagging them all as
            # runbooks would churn history to say something the cron field already says.
            "chat_labels": ["scheduled-job"] if rb.get("cron") else ["runbook"]}
    pinned = runbooks.resolve_cap(r["cap"])
    if pinned:
        args["cap"] = pinned          # trusted {name,kind,risk}; a name we can't resolve auto-routes
    if r["steps"]:
        args["steps"] = r["steps"]
    if r["doc"]:
        args["doc"] = r["doc"]
    return args


def _schedule(rid, rb):
    return Schedule(
        action=ScheduleActionStartWorkflow(
            OttoWorkflow.run,
            args=[_args(rid, rb)],       # cron fire: defaults only, nobody is here to be prompted
            id=f"sched-{rid}",
            task_queue=tc.TASK_QUEUE,
        ),
        spec=ScheduleSpec(cron_expressions=[rb["cron"]], time_zone_name=local_tz_name()),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


# --- Temporal schedule ops -------------------------------------------------

async def _create(rid, rb):
    await (await tc.client()).create_schedule(rid, _schedule(rid, rb))


async def _delete(rid):
    try:
        await (await tc.client()).get_schedule_handle(rid).delete()
    except Exception:  # noqa: BLE001
        pass


async def _set_paused(rid, paused):
    h = (await tc.client()).get_schedule_handle(rid)
    await (h.pause() if paused else h.unpause())


class AlreadyRunning(Exception):
    """This runbook already has a run in flight — surfaced to the caller instead of stacking."""


async def _in_flight(c, rid):
    """Workflow ids for this runbook that are currently RUNNING — a cron fire (`sched-<rid>-…`,
    Temporal suffixes the scheduled time) or an earlier on-demand start (`runbook-<rid>-…`)."""
    out = []
    async for wf in c.list_workflows(
            f'WorkflowType = "OttoWorkflow" AND ExecutionStatus = "Running"'):
        if wf.id.startswith((f"sched-{rid}", f"runbook-{rid}-")):
            out.append(wf.id)
    return out


async def _run_now(rid, rb, values, unattended):
    """Start ONE workflow for this runbook right now, with the parameter values the caller
    supplied. Deliberately NOT ScheduleHandle.trigger(): a schedule's action args are frozen at
    creation, so triggering it would silently run with the DEFAULTS while the operator watched a
    form they had just filled in. It also means an on-demand runbook needs no Schedule object.

    A manual trigger used to inherit ScheduleOverlapPolicy.SKIP, which is what stopped "run now"
    from starting a SECOND concurrent run alongside one already in flight (the "it ran multiple
    times" symptom). Starting workflows directly loses that for free, so the same intent is
    enforced here explicitly. The workflow id stays unique per run rather than being made
    collision-prone on purpose: audit rows and transcripts are keyed on it, so reusing one id
    across runs would merge their histories."""
    c = await tc.client()
    busy = await _in_flight(c, rid)
    if busy:
        raise AlreadyRunning(f"already running ({busy[0]})")
    wid = f"runbook-{rid}-{uuid.uuid4().hex[:6]}"
    await c.start_workflow(OttoWorkflow.run, _args(rid, rb, values, unattended),
                           id=wid, task_queue=tc.TASK_QUEUE)
    return wid


async def _sync(rid, rb):
    """Make Temporal match ONE runbook: create/update its schedule when it has a cron, delete it
    when the cron was removed (the runbook stays, as an on-demand one)."""
    c = await tc.client()
    if not rb.get("cron"):
        await _delete(rid)
        return
    fresh = _schedule(rid, rb)
    try:
        await c.get_schedule_handle(rid).describe()
    except Exception:  # noqa: BLE001 - missing in Temporal -> create
        await c.create_schedule(rid, fresh)
        return

    def _apply(inp):
        s = inp.description.schedule
        s.spec, s.action, s.policy = fresh.spec, fresh.action, fresh.policy
        return ScheduleUpdate(schedule=s)
    await c.get_schedule_handle(rid).update(_apply)


async def _reconcile(store):
    """Make Temporal match `data/runbooks.json` (the source of truth) on startup.

    We rebuild each scheduled runbook's full definition (spec + action) from the store, so it picks
    up every field the current `_schedule()` emits. This fixes drifts from schedules created by an
    earlier version and never migrated:
      * **Wrong timezone** — schedules created before `time_zone_name` was wired in fire in
        UTC (a "30 17 * * *" job runs 12h off).
      * **No chat thread** — schedules created before chat-recording landed lack the
        `chat_key`/`chat_labels` action args, so their runs only hit the audit trail and never
        open a Chat sidebar thread.
      * **Orphan duplicate fires** — an `otto-*` schedule left in Temporal but absent from the
        store (e.g. a half-deleted job, or a dev-era id scheme) keeps firing while being
        invisible in the UI, so the same request appears to "run multiple times". We delete any
        `otto-*` (or pre-rename `mosaic-*`) schedule the store doesn't know about — and now also
        any whose runbook has since had its cron removed.

    The rebuild is applied *in place* via `update()`, preserving the schedule's pause state and
    run history. It also self-heals after an in-memory Temporal restart: schedules missing from
    Temporal but present in the store are recreated."""
    c = await tc.client()
    scheduled = set()
    for rid, rb in store.items():
        if not rb.get("cron") or not cron_valid(rb["cron"]):
            continue
        scheduled.add(rid)
        await _sync(rid, rb)
    # GC orphans: our schedules that no longer correspond to a SCHEDULED runbook.
    async for s in await c.list_schedules():
        if s.id.startswith((ID_PREFIX,) + _LEGACY_ID_PREFIXES) and s.id not in scheduled:
            try:
                await c.get_schedule_handle(s.id).delete()
            except Exception:  # noqa: BLE001
                pass


async def _enrich(store):
    """Merge each runbook with live Temporal state (next run, paused). A runbook with no cron has
    no Schedule object, so its live fields come from the store alone — it is always 'enabled'
    (there is nothing to pause) and simply has no next run."""
    c = await tc.client()
    out = []
    for rid, rb in store.items():
        row = {
            "id": rid,
            "name": rb.get("name", ""),
            "request": rb.get("request", ""),
            "cron": rb.get("cron", ""),
            "on_demand": not rb.get("cron"),
            "auto_approve": rb.get("auto_approve", False),
            "cap": rb.get("cap") or None,          # pinned capability name, or None (auto-route)
            "params": rb.get("params") or [],
            "steps": rb.get("steps") or [],
            "has_doc": bool(rb.get("doc")),
            "enabled": True,
            "next_run": None,
            "last_run": None,
            "running": False,
        }
        if rb.get("cron"):
            try:
                d = await c.get_schedule_handle(rid).describe()
            except Exception:  # noqa: BLE001 - gone from Temporal; report from the store alone
                out.append(row)
                continue
            nxt = d.info.next_action_times
            recent = d.info.recent_actions
            row["enabled"] = not d.schedule.state.paused
            # Temporal returns UTC; show local time so it matches the cron's (local) meaning.
            row["next_run"] = nxt[0].astimezone().isoformat(timespec="minutes") if nxt else None
            row["last_run"] = (recent[-1].scheduled_at.astimezone().isoformat(timespec="seconds")
                               if recent else None)
            row["running"] = len(getattr(d.info, "running_actions", None) or []) > 0
        out.append(row)
    return out


# --- sync API for the HTTP server ------------------------------------------

def add(rb):
    """Create a runbook (validated by runbooks.normalize — raises ValueError with a message meant
    for the author) and, if it has a cron, its Temporal schedule."""
    rid, clean = runbooks.add(rb)
    if clean["cron"]:
        tc.run(_sync(rid, clean))
    return rid


def update(rid, rb):
    clean = runbooks.update(rid, rb)
    # Sync unconditionally when Temporal is up: this is also what DELETES the schedule when a
    # cron was removed. With Temporal down, an on-demand runbook still saves — reconcile()'s
    # orphan GC catches up the schedule side on the next startup.
    if clean["cron"] or available():
        tc.run(_sync(rid, clean))
    return clean


def remove(rid):
    if available():
        tc.run(_delete(rid))
    runbooks.remove(rid)


def set_paused(rid, paused):
    tc.run(_set_paused(rid, paused))


def run_now(rid, values=None, unattended=False):
    """Start this runbook immediately with `values`. Raises ValueError (via runbooks.render) when
    a required parameter is missing, BEFORE anything is started. Interactive by default — a human
    clicked it, so they are present to answer the approval gate."""
    import estop
    rb = runbooks.get(rid)
    if not rb:
        raise KeyError(rid)
    if estop.blocked("run-now"):
        raise RuntimeError("Otto is paused — release the stop to run a runbook")
    runbooks.render(rb, values)   # validate now; a bad value must not reach a started workflow
    return tc.run(_run_now(rid, rb, values, unattended))


def _rows_without_temporal(store):
    """Store-only rows: everything except the live cron fields. An on-demand runbook needs no
    Temporal Schedule, so it must stay listable (and editable) when Temporal is down — returning
    [] there would make the whole tab look empty rather than degraded."""
    return [{"id": rid, "name": rb.get("name", ""), "request": rb.get("request", ""),
             "cron": rb.get("cron", ""), "on_demand": not rb.get("cron"),
             "auto_approve": rb.get("auto_approve", False), "cap": rb.get("cap") or None,
             "params": rb.get("params") or [], "steps": rb.get("steps") or [],
             "has_doc": bool(rb.get("doc")), "enabled": True, "next_run": None,
             "last_run": None, "running": False}
            for rid, rb in store.items()]


def list():  # noqa: A001 - matches the endpoint vocabulary
    store = runbooks.load()
    try:
        return tc.run(_enrich(store))
    except Exception:  # noqa: BLE001 - Temporal unreachable
        return _rows_without_temporal(store)


def reconcile():
    """Sync wrapper: migrate any legacy schedules, then align Temporal with the store on startup.
    Best-effort — a failure here must never stop the server from coming up, so swallow if Temporal
    is unreachable."""
    try:
        migrate_legacy()
    except Exception:  # noqa: BLE001 - a bad legacy file must not block startup
        pass
    try:
        tc.run(_reconcile(runbooks.load()))
        return True
    except Exception:  # noqa: BLE001 - Temporal unreachable / transient
        return False
