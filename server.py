#!/usr/bin/env python3
"""Local web ingress for Otto.

Serves the chat UI and drives every run through the Temporal workflow:

    GET  /                 -> the UI
    GET  /api/capabilities -> the discovered agents/skills (+ risk)
    POST /api/submit       -> {request,...}        -> starts an OttoWorkflow, returns its id
    POST /api/continue     -> {session_id,...}     -> resumes a bound session (new workflow)

The human-in-the-loop (clarify answers, plan approval, write gate) lives INSIDE the
workflow — the browser only watches state and sends signals. Temporal is required
(issue #278); main() refuses to start without it.

    ./run.sh               # temporal + worker + this server; open http://localhost:8765
"""
import json
import os
import re
import socketserver
import time
import uuid
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import board
import chats
import claude_cli
import config
import conventions
import delivery
import doctor
import engine
import estop
import events
import gateway
import knowledge
import local_runtime
import mcp_client
import policy
import pr_review
import registry
import repos
import runbooks
import scheduler
import slack
import storage
import supervisor
import workspace

# Temporal is REQUIRED to serve (main() refuses to start without it — issue #278). The import
# stays soft only so the stdlib test run can import this module; TEMPORAL_OK gates the few
# endpoints tests exercise without temporalio into honest 503s instead of tracebacks.
import temporal_client as tc

TEMPORAL_OK = tc.OK
if tc.OK:
    from workflows import OttoWorkflow

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8765"))
_MAX_BODY = int(os.environ.get("OTTO_MAX_BODY_BYTES", "1000000"))   # reject oversized POSTs (DoS)
# Extra browser origins allowed to POST (comma-separated, e.g. a same-origin proxy used to drive
# the UI headlessly). Everything else cross-site is refused — see Handler._csrf_ok.
_ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in
                    os.environ.get("OTTO_ALLOWED_ORIGINS", "").split(",") if o.strip()}
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
_START_TIME = time.time()


class PayloadTooLarge(Exception):
    """A POST body exceeded _MAX_BODY — surfaced as HTTP 413 instead of an unbounded read."""
TASK_QUEUE = tc.TASK_QUEUE
TEMPORAL_UI = os.environ.get("TEMPORAL_UI_ADDR", "http://localhost:8233")


def _temporal_connected():
    return tc.connected()


async def _wf_start(wid, params):
    c = await tc.client()
    await c.start_workflow(OttoWorkflow.run, params, id=wid, task_queue=TASK_QUEUE)


async def _wf_state(wid):
    c = await tc.client()
    h = c.get_workflow_handle(wid)
    try:
        desc = await h.describe()
    except Exception as e:  # noqa: BLE001
        # Two very different failures arrive here. A NOT_FOUND is authoritative — the id is gone
        # (in-memory dev server wiped on restart, history aged out) — so report a terminal state
        # and let a reattaching UI stop polling. ANYTHING else is the transport, not the run: a
        # deadline exceeded, a server restart mid-poll, a dev server too busy to answer. Those
        # must NOT be reported as terminal: the workflow is still executing, and calling it
        # failed paints a dead pipeline over a live run and stops the watch loop for good.
        if not tc.workflow_gone(e):
            return {"state": "unreachable", "result": str(e)[:200] or "Temporal unreachable"}
        return {"state": "failed", "result": "workflow no longer available"}
    name = desc.status.name if desc.status else "RUNNING"
    if name == "COMPLETED":
        res = await h.result()
        if isinstance(res, dict):
            nh = res.get("needs_human")
            return {"state": "done", "result": res.get("result"),
                    "session_id": res.get("session_id"), "cap": res.get("cap"),
                    "swarm": bool(res.get("swarm")),
                    "repo": res.get("repo"), "git_run_id": res.get("git_run_id"),
                    "git_branch": res.get("git_branch"), "cost": res.get("cost", 0),
                    "times": res.get("times") or {},
                    # Surfaced so the chat can visibly flag an unverified / needs-human outcome
                    # instead of rendering it identically to a clean, verified success.
                    "verified": res.get("verified"),
                    "discussion": bool(res.get("discussion")),
                    "needs_human": (nh or {}).get("reason") if nh else None}
        return {"state": "done", "result": res}
    if name in ("FAILED", "TERMINATED", "TIMED_OUT", "CANCELED"):
        # `terminal_status` lets the pipeline diagram tell an audited failure (FAILED — caught by
        # OttoWorkflow's own except-and-finalize; TERMINATED — audited by _wf_terminate above)
        # from one that genuinely has no audit row yet (TIMED_OUT, CANCELED, a dead worker) —
        # without it the client can't honestly render the AUDIT stage either way.
        return {"state": "failed", "result": f"workflow {name.lower()}", "terminal_status": name}
    try:
        st = await h.query(OttoWorkflow.status)
    except Exception:  # noqa: BLE001
        st = {}
    if st.get("awaiting_clarification"):
        return {"state": "awaiting_clarification", "question": st.get("question"), "cap": st.get("cap"),
                "times": st.get("times") or {}}
    if st.get("awaiting_approval"):
        # NOTE: this is a WHITELIST, not a passthrough — a field added to OttoWorkflow.status is
        # invisible to the gate until it is named here too. plan_concerns was silently dropped
        # exactly this way on its first live run: the critic ran, the workflow held the findings,
        # and the browser got a gate with no warning block. Guarded by
        # test_core.GateStateForwardingTests.
        return {"state": "awaiting_approval", "cap": st.get("cap"), "plan": st.get("plan"),
                "plan_concerns": st.get("plan_concerns") or [],
                # WHICH model wrote this plan. Approving one without being told who authored it
                # is how a silent downgrade to the cheapest Claude tier went unnoticed.
                "plan_model": st.get("plan_model"),
                "plan_revisions": st.get("plan_revisions") or 0,
                "replanning": bool(st.get("replanning")),
                "max_plan_revisions": st.get("max_plan_revisions") or 0,
                "repo": st.get("repo"), "risk_reason": st.get("risk_reason"),
                "times": st.get("times") or {}}
    return {"state": "running", "cap": st.get("cap"),
            "swarm": st.get("swarm", False), "children": st.get("children") or [],
            # A follow-up this run re-read as a QUESTION: no gate, no plan preview, read tools
            # only. Named here (same whitelist rule as the gate block above) so the composer can
            # say why the chat is answering straight away instead of showing an approval card —
            # otherwise a user who expected the gate reads its absence as the gate having broken.
            "discussion": bool(st.get("discussion")),
            "times": st.get("times") or {}, "attempt": st.get("attempt")}


async def _wf_origin_chat_key(wid):
    """The chat_key the ORIGINAL run was writing to, recovered straight from its own Temporal
    result/status the same way `_board()` enriches a Swarm row (workflows.py's terminal dict and
    live status both carry `chat_key`; the audit trail never does). Retry used to only try
    `chats.find_reattach`'s text-similarity match, which fails silently (pre-rename `role="mosaic"`
    rows, an edited schedule request, a non-pending stop state) and forks a brand-new chat thread
    — the board's "Chat" link then points at the fresh fork while the Chats tab still shows the
    old, now-stale thread. None here just means "history gone or never had one"; caller falls back
    to find_reattach."""
    try:
        c = await tc.client()
        h = c.get_workflow_handle(wid)
        desc = await h.describe()
        if desc.status and desc.status.name == "COMPLETED":
            res = await h.result()
            return res.get("chat_key") if isinstance(res, dict) else None
        q = await h.query(OttoWorkflow.status)
        return (q or {}).get("chat_key")
    except Exception:  # noqa: BLE001 - workflow id unknown / history aged out of retention
        return None


# What a post-PR round's transcript is, in the reader's words — "<wid>-revfix1" is Otto's
# vocabulary, not a phrase the chat can show.
_PROGRESS_PARTS = {"rev": "review round", "revfix": "review fix", "qa": "QA round",
                   "qafix": "QA fix", "fix": "QA fix"}


def _run_progress(wid):
    """Live execution progress for one run, read from its streaming transcript (issue #97,
    first cut — #89 flushes per line, so tailing the file is safe while it's being written).
    Returns the NEWEST attempt's last activity line, event count, and how stale the file is,
    so the chat can show WHAT a run is doing and whether it looks stuck. File reads only —
    no Temporal, no worker, no LLM — and the wid is pattern-validated because it lands in a
    filesystem path."""
    if not wid or not re.fullmatch(r"[A-Za-z0-9._:@-]+", wid):
        return {"found": False}
    try:
        names = os.listdir(claude_cli.TRANSCRIPTS)
    except OSError:
        return {"found": False}
    # The run's OWN transcripts: the execution attempts, plus every post-PR round (review, QA and
    # their fix runs, "<wid>-rev2-a1"). Those rounds are where a repo-mode run spends its last
    # 20 minutes — reading only "<wid>-aN" there tails a file nothing has written since RUN ended
    # and reports a working run as stuck. A swarm child ("<wid>-sN-aM") stays excluded: it is its
    # own run with its own card.
    best, best_attempt, best_part, best_key = None, 0, None, ()
    for name in names:
        m = re.fullmatch(re.escape(wid) + r"(?:-(revfix|rev|qafix|qa|fix)(\d+))?-a(\d+)\.jsonl", name)
        if not m:
            continue
        kind, rnd, attempt = m.group(1), m.group(2), int(m.group(3))
        path = os.path.join(claude_cli.TRANSCRIPTS, name)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        key = (mtime, int(rnd) if rnd else -1, attempt)
        if key > best_key:
            best, best_attempt, best_key = path, attempt, key
            best_part = f"{_PROGRESS_PARTS[kind]} {int(rnd) + 1}" if kind else None
    if not best:
        return {"found": False}
    try:
        stat = os.stat(best)
        with open(best, "rb") as f:
            events_n = sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))
            f.seek(0)
            meta_line = f.readline()
            f.seek(max(0, stat.st_size - 16384))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {"found": False}
    supervised = False
    try:
        supervised = bool(json.loads(meta_line).get("supervised"))
    except ValueError:
        pass
    last = ""
    for line in reversed(tail):
        try:
            compacted = supervisor.compact_event(json.loads(line))
        except ValueError:
            continue
        if compacted:
            last = compacted.splitlines()[-1][:200]
            break
    # The AI supervisor (issue #143) appends its own "otto-supervisor" checkpoint lines to
    # this same transcript live, mid-attempt (supervisor._append_marker) — surface the newest
    # one so the chat can show "supervisor checked Ns ago" while the run is still going,
    # rather than only after the fact via the audit row.
    supervisor_last = None
    for line in reversed(tail):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "otto-supervisor":
            supervisor_last = {"at_s": event.get("at_s"), "verdict": event.get("verdict"),
                               "critique": event.get("critique")}
            break
    return {"found": True, "attempt": best_attempt, "events": events_n, "part": best_part,
            "idle_s": max(0.0, round(time.time() - stat.st_mtime, 1)), "last": last,
            "supervised": supervised, "supervisor_last": supervisor_last}


_TERMINAL_OUTCOMES = ("needs_human", "workflow_error", "delivery_failed")


def _transcript_events(wid, attempt, cap=250):
    """One attempt's execution transcript compacted to readable lines (tool calls + results +
    assistant text), in order — reusing supervisor.compact_event (which also redacts secrets).
    Returns (lines, truncated). Missing file (e.g. a local run, or swept transcript) -> []."""
    path = os.path.join(claude_cli.TRANSCRIPTS, f"{wid}-a{attempt}.jsonl")
    lines = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                compacted = supervisor.compact_event(ev)
                if compacted:
                    lines.extend(compacted.splitlines())
    except OSError:
        return [], False
    if len(lines) > cap:
        return lines[:cap] + [f"… ({len(lines) - cap} more events truncated)"], True
    return lines, False


def _run_detail(wid):
    """Everything about ONE run for the debug drawer (#96): per-attempt metadata (model,
    backend/fallback, cost, tokens, duration, verify pass/fail + critique), the execution
    transcript compacted to readable tool lines, and the terminal/needs-human reason — assembled
    from the audit + content logs + transcript files. No Temporal/worker/LLM, so it works even
    after the workflow itself is gone (the audit trail outlives it). wid is pattern-validated
    because it lands in a filesystem path."""
    if not wid or not re.fullmatch(r"[A-Za-z0-9._:@-]+", wid):
        return {"found": False}
    meta_rows = [e for e in engine.iter_audit_entries() if e.get("workflow") == wid]
    content_rows = [e for e in engine.iter_content_entries() if e.get("workflow") == wid]
    if not meta_rows and not content_rows:
        return {"found": False}
    # Content (request/result/critique) keyed by attempt; the run's request is the first seen.
    request, content_by_attempt = None, {}
    for c in content_rows:
        if c.get("request") and not request:
            request = c["request"]
        if c.get("attempt") is not None:
            content_by_attempt[c["attempt"]] = c
    cap = risk = repo = None
    needs_human, terminal = None, None
    for e in meta_rows:
        cap = e.get("capability") or cap
        risk = e.get("risk") or risk
        repo = e.get("repo") or repo
        if e.get("needs_human"):
            needs_human = e.get("reason") or "needs_human"
        if e.get("outcome") in _TERMINAL_OUTCOMES:
            # The terminal row's detail lives in the content log (correlated by at).
            det = next((c.get("detail") or c.get("result")
                        for c in content_rows if c.get("at") == e.get("at")), None)
            terminal = {"reason": e.get("reason") or e.get("outcome"), "detail": det}
    attempts, seen = [], set()
    for e in meta_rows:
        a = e.get("attempt")
        if a is None or a in seen:
            continue
        seen.add(a)
        c = content_by_attempt.get(a, {})
        events, truncated = _transcript_events(wid, a)
        attempts.append({
            "attempt": a, "at": e.get("at"), "model": e.get("model"),
            "backend": e.get("backend"), "fallback_from": e.get("fallback_from"),
            "fallback_reason": e.get("fallback_reason"), "cost_usd": e.get("cost_usd"),
            "tokens": e.get("tokens"), "duration_s": e.get("duration_s"),
            "verified": e.get("verified"), "critique": c.get("critique"),
            "result": c.get("result"), "events": events, "events_truncated": truncated})
    attempts.sort(key=lambda x: x["attempt"])
    final = next((a["result"] for a in reversed(attempts) if a.get("result")), None)
    if not final:
        final = next((c.get("result") for c in reversed(content_rows) if c.get("result")), None)
    return {"found": True, "wid": wid, "request": request, "cap": cap, "risk": risk,
            "repo": repo, "needs_human": needs_human, "terminal": terminal,
            "attempts": attempts, "result": final}


def _run_model(wid):
    """(model, is_local_runtime, fallback_from, fallback_reason) of a run's NEWEST execution
    attempt, from its transcript's meta line — written at attempt start, so it's available
    while the attempt is still in flight (the workflow status only learns the model after an
    attempt returns). The fallback fields are stamped by engine.run_attempt when the CHOSEN
    model couldn't run and Claude substituted. Best-effort file read for the board's model
    chip; (None, False, None, None) when there's no transcript."""
    if not wid or not re.fullmatch(r"[A-Za-z0-9._:@-]+", wid):
        return None, False, None, None
    prefix, best, best_attempt = f"{wid}-a", None, 0
    try:
        for name in os.listdir(claude_cli.TRANSCRIPTS):
            if name.startswith(prefix) and name.endswith(".jsonl"):
                try:
                    attempt = int(name[len(prefix):-len(".jsonl")])
                except ValueError:
                    continue
                if attempt > best_attempt:
                    best_attempt, best = attempt, os.path.join(claude_cli.TRANSCRIPTS, name)
        if not best:
            # No execution attempt yet — fall back to the PLAN preview's own transcript, so the
            # board's model chip is populated DURING the plan phase instead of staying blank
            # until the first attempt starts. The preview can run for up to 15 minutes, and an
            # empty chip there reads as "nothing is running".
            plan = claude_cli.plan_transcript_path(wid)
            best = plan if os.path.exists(plan) else None
        if not best:
            return None, False, None, None
        with open(best) as f:
            meta = json.loads(f.readline() or "{}")
        return (meta.get("model"), meta.get("runtime") == "local",
                meta.get("fallback_from"), meta.get("fallback_reason"))
    except (OSError, ValueError):
        return None, False, None, None


async def _wf_signal(wid, sig, value):
    c = await tc.client()
    h = c.get_workflow_handle(wid)
    if sig == "approve":
        await h.signal(OttoWorkflow.approve, bool(value))
    elif sig == "revise_plan":
        await h.signal(OttoWorkflow.revise_plan, str(value))
    else:
        await h.signal(OttoWorkflow.provide_clarification, str(value))


async def _wf_terminate(wid):
    """Hard-stop a run from the board. Temporal TERMINATE (immediate, no cooperative
    cleanup) rather than cancel: OttoWorkflow has no cancellation handling, and the one
    thing a human killing a runaway/stuck task wants is for it to stop NOW. Swarm children
    die with the parent (default parent-close policy); an in-flight `claude -p` activity is
    abandoned — its workspace, if any, is backstopped by workspace.gc's TTL sweep.

    Temporal TERMINATE delivers no exception into the workflow, so `OttoWorkflow.run`'s
    top-level `except Exception` — the thing that normally writes a terminal audit row via
    `finalize_terminal` — never runs. Left alone, a killed run has NO audit trail at all, even
    one that already spent money and produced a real side effect before being killed
    (user-reported: a run that had already posted to Slack showed no audit row whatsoever, only
    its earlier plan_preview accounting). Recording it here, at the one call site that actually
    knows the kill happened, closes that gap — only reached once `.terminate()` itself succeeds,
    so a stale board click against an already-closed workflow doesn't fabricate a record."""
    c = await tc.client()
    await c.get_workflow_handle(wid).terminate(reason="terminated from the Otto board")
    request, capname, repo, _ = _run_origin(wid)
    engine.record_terminal(wid, request, capname, "terminated",
                           detail="Terminated from the Otto board.", repo=repo)


_NEEDS_BANNER_RE = re.compile(r"^⚠️\s*\*\*Needs human review\*\*[^\n]*\n\n")


def _outcome_preview(text, limit=220):
    """A short preview of a run's result for the board card. Strips the needs-human banner
    workflows.py prepends (`_NEEDS_HUMAN_BANNER`) — the card already shows that same warning via
    its own hint text, so repeating it here just pushes the run's actual content out of the
    truncated preview instead of showing something informative."""
    text = _NEEDS_BANNER_RE.sub("", text or "", count=1).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


async def _board(limit=40):
    """Surface recent OttoWorkflow executions for the swarm board — read-only. Enriches
    each with its capability/risk/outcome by querying the live status (running) or fetching
    the result (completed). Best-effort: enrichment failures leave those fields null."""
    c = await tc.client()

    async def _collect(query):
        rows = []
        async for wf in c.list_workflows(query):
            rows.append(wf)
            if len(rows) >= limit:
                break
        return rows

    try:
        rows = await _collect('WorkflowType = "OttoWorkflow" ORDER BY StartTime DESC')
    except Exception:  # noqa: BLE001 - visibility may not support ORDER BY; sort client-side
        rows = await _collect('WorkflowType = "OttoWorkflow"')
        rows.sort(key=lambda w: w.start_time or 0, reverse=True)

    out = []
    for wf in rows:
        status = wf.status.name if wf.status else "RUNNING"
        e = {"id": wf.id, "run_id": wf.run_id, "status": status,
             "scheduled": wf.id.startswith("sched-"),
             "start": wf.start_time.astimezone().isoformat(timespec="minutes") if wf.start_time else None,
             # A closed run is filed by WHEN IT FINISHED, not when it started: a long run started
             # first can close last, so ordering Finished by start time buries the newest result
             # mid-column. Null while RUNNING.
             "end": wf.close_time.astimezone().isoformat(timespec="minutes") if wf.close_time else None,
             "cap": None, "risk": None, "phase": None, "outcome": None, "verified": None,
             "repo": None, "in_place": False, "chat_key": None, "question": None, "pr": None,
             "needs_human": None, "qa": None, "retried_to": None}
        e["model"], e["local"], e["fallback_from"], e["fallback_reason"] = _run_model(wf.id)
        h = c.get_workflow_handle(wf.id, run_id=wf.run_id)
        try:
            if status == "COMPLETED":
                res = await h.result()
                if isinstance(res, dict):
                    cap = res.get("cap") or {}
                    e["cap"], e["risk"] = cap.get("name"), cap.get("risk")
                    e["verified"] = res.get("verified")
                    e["outcome"] = _outcome_preview(res.get("result"))
                    e["repo"] = res.get("repo")
                    e["in_place"] = bool(res.get("in_place"))
                    e["chat_key"] = res.get("chat_key")
                    pr = res.get("pr") or {}
                    e["pr"] = pr.get("pr_url") if isinstance(pr, dict) else None
                    nh = res.get("needs_human")
                    e["needs_human"] = (nh or {}).get("reason") if nh else None
                    e["qa"] = (res.get("qa") or {}).get("state")
                    e["review"] = (res.get("review") or {}).get("state")
            elif status == "RUNNING":
                q = await h.query(OttoWorkflow.status)
                cap = (q or {}).get("cap") or {}
                e["cap"], e["risk"] = cap.get("name"), cap.get("risk")
                e["repo"] = q.get("repo")
                e["chat_key"] = q.get("chat_key")
                nh = q.get("needs_human")
                e["needs_human"] = (nh or {}).get("reason") if nh else None
                if q.get("awaiting_clarification"):
                    e["phase"] = "awaiting clarification"
                    e["question"] = q.get("question")
                elif q.get("awaiting_approval"):
                    e["phase"] = "awaiting approval"
                    e["risk_reason"] = q.get("risk_reason")
                elif q.get("attempt"):
                    e["phase"] = f"running · try {q['attempt']}"
                else:
                    e["phase"] = "running"
                # WHICH stage of the pipeline the run is in right now. `phase` above collapses
                # everything before the first attempt to a bare "running", so a card sat there
                # unchanged through routing, a 15-minute plan preview and the gate — the run
                # looked stalled when it was working. `times` records one span per stage and
                # leaves the OPEN one with dur=None, so the last such entry is where it is now.
                open_stages = [k for k, v in (q.get("times") or {}).items()
                               if isinstance(v, dict) and v.get("dur") is None]
                e["stage"] = open_stages[-1] if open_stages else None
        except Exception:  # noqa: BLE001 - enrichment is best-effort
            pass
        # A run with no chat_key of its own (an interactive web-* run — the browser owns the
        # conversation — or a workflow-opened chat whose terminal status fell outside the
        # COMPLETED/RUNNING branches above) can still be traced back to its chat via the sticky
        # origin_run_id backstop, so the board's Chat link isn't lost just because the run's own
        # result/status never carried one (user-reported: some finished cards had no Chat button).
        if not e["chat_key"]:
            e["chat_key"] = chats.find_by_run_origin(wf.id)
        out.append(e)
    dismissed = _dismissed_ids()
    retries = _retries()
    for e in out:
        e["retried_to"] = retries.get(e["id"])
    return [e for e in out if e["id"] not in dismissed]


def _run_result(wid):
    """The last result text a run recorded — what the human actually read on the card before
    accepting it. Content rows are yielded oldest-first, so the last one wins (the terminal row
    for a needs-human run, else the final attempt)."""
    result = ""
    for e in engine.iter_content_entries():
        if e.get("workflow") == wid and e.get("result"):
            result = e["result"]
    return result


async def _board_full_result(wid):
    """The untruncated result text for one board card's detail modal, fetched only on demand
    (not part of the polled /api/board list, which stays truncated so its payload stays light
    regardless of how large any individual run's result is)."""
    c = await tc.client()
    h = c.get_workflow_handle(wid)
    desc = await h.describe()
    status = desc.status.name if desc.status else "RUNNING"
    if status == "COMPLETED":
        res = await h.result()
        text = (res.get("result") if isinstance(res, dict) else res) or ""
        return {"status": status, "result": _NEEDS_BANNER_RE.sub("", text, count=1).strip()}
    if status == "RUNNING":
        return {"status": status, "result": None}
    return {"status": status, "result": f"workflow {status.lower()}"}


_DISMISSED_PATH = os.path.join(HERE, "data", "dismissed.json")
_RETRIES_PATH = os.path.join(HERE, "data", "retries.json")


def _retries():
    """Needs-you run id -> the new run id started by clicking Retry on it. Clicking Retry starts
    a brand-new, unrelated workflow with no visible link back to the card it came from — without
    this, the original card just sits there and a human has no way to tell whether Retry did
    anything at all. Surfaced on the card as "retried as <new id>"."""
    return storage.read_json(_RETRIES_PATH, {})


def _record_retry(wid, new_id):
    def _set(cur):
        cur = dict(cur or {})
        cur[wid] = new_id
        return cur
    storage.mutate_json(_RETRIES_PATH, _set, {})


def _dismissed_ids():
    """Workflow ids a human has acknowledged on the Needs-you board — hidden from /api/board
    from then on. Not a Temporal or audit mutation: the run's own history is untouched, this
    just stops it rotting visibly in the UI."""
    return set(storage.read_json(_DISMISSED_PATH, []))


def _dismiss(wid):
    def _add(cur):
        cur = cur or []
        return cur if wid in cur else cur + [wid]
    storage.mutate_json(_DISMISSED_PATH, _add, [])


def _slack_reply_target(wid):
    """Last-resort reply target for a Slack-originated run whose Temporal history is gone."""
    try:
        import slack
        return slack.reply_target_from_wid(wid)
    except Exception:  # noqa: BLE001 - never block a retry on this
        return None


def _run_origin(wid):
    """Delegates to engine.run_origin — the one implementation, shared with the reaper."""
    return engine.run_origin(wid)


def _audit_content(wid, at="", attempt=None):
    """On-demand fetch of one audit row's full request/result text — kept OUT of /api/audit's
    payload so that endpoint stays pure operational metadata. Mirrors the /api/board/full
    lazy-load pattern: the Audit tab calls this only when a row is expanded."""
    for e in engine.iter_content_entries():
        if e.get("workflow") != wid:
            continue
        if at and e.get("at") != at:
            continue
        if attempt is not None and e.get("attempt") != attempt:
            continue
        return {"request": e.get("request"), "result": e.get("result"), "detail": e.get("detail")}
    return {"request": None, "result": None}


def _classify(row):
    """Bucket a board row for the Needs-you dashboard. Returns a bucket key or None (nothing to
    surface). Priority: explicit needs-human > awaiting a human > failed > in-flight."""
    status, phase = row.get("status"), row.get("phase")
    if row.get("needs_human"):
        return "needs_human"
    if row.get("status") == "COMPLETED" and row.get("verified") is False and not row.get("pr"):
        return "needs_human"        # delivered-unverified is a needs-human state, UNLESS a PR
                                     # opened — the PR is the deliverable and awaits human review
                                     # on GitHub anyway (mirrors the Board tab's needsYou())
    if phase == "awaiting clarification":
        return "awaiting_clarification"
    if phase == "awaiting approval":
        return "awaiting_approval"
    if status not in ("COMPLETED", "RUNNING"):
        return "failed"             # FAILED / TERMINATED / TIMED_OUT / CANCELED
    if status == "RUNNING":
        return "in_flight"
    return None


async def _needs_you(limit=40):
    """One aggregated view for hands-off operation: what needs a human, what's stuck/failed, and
    what's in flight — plus a health strip (Temporal, board poll, reaper). Read-only, best-effort."""
    buckets = {"needs_human": [], "awaiting_clarification": [], "awaiting_approval": [],
               "failed": [], "in_flight": []}
    try:
        rows = await _board(limit=limit)
    except Exception:  # noqa: BLE001 - Temporal visibility unavailable
        rows = []
    for r in rows:
        b = _classify(r)
        if b:
            buckets[b].append(r)
    # This coroutine RUNS ON tc's background loop (the handler calls it via tc.run), so it must
    # await board's async status helpers directly — the sync wrappers (board.poll_status,
    # board.reaper_status, tc.connected) each re-enter tc.run, which blocks the loop thread on
    # work only that loop can run: the first /api/needs-you hit would deadlock the whole server.
    async def _safe(coro):
        try:
            return await coro
        except Exception:  # noqa: BLE001 - Temporal unreachable / schedule absent
            return {"exists": False}
    poll = await _safe(board._poll_status())
    reaper = await _safe(board._reaper_status())
    try:
        connected = TEMPORAL_OK and bool(await tc.client())
    except Exception:  # noqa: BLE001 - Temporal unreachable
        connected = False
    # "worker_stale": the poll schedule exists but hasn't fired recently — a hint the worker is down.
    health = {"temporal": TEMPORAL_OK, "connected": connected,
              "board_poll": poll, "reaper": reaper,
              "board_enabled": board.enabled()}
    counts = {k: len(v) for k, v in buckets.items()}
    return {"health": health, "buckets": buckets, "counts": counts, "ui": TEMPORAL_UI}


def _board_health():
    """CHEAP health strip for the Board tab — schedule describes + a cost read only, NO per-workflow
    enrichment (that's what /api/board already does). Kept separate so the dashboard's auto-refresh
    doesn't run the expensive _board() pass twice per tick."""
    health = {"temporal": TEMPORAL_OK, "connected": tc.connected() if TEMPORAL_OK else False,
              "board_enabled": board.enabled() if TEMPORAL_OK else False,
              "board_poll": board.poll_status() if TEMPORAL_OK else {"exists": False},
              "reaper": board.reaper_status() if TEMPORAL_OK else {"exists": False}}
    return {"health": health, "costs": _costs()}


def _costs(days=30):
    """Aggregate audit spend by day / capability / model for the cost dashboard. Pure file
    read (no Temporal), across all rotated segments. Best-effort — zeros if the log is absent."""
    by_day, by_cap, by_model = {}, {}, {}
    total = 0.0
    tokens_out = 0
    for e in engine.iter_audit_entries():
        cost = e.get("cost_usd", 0) or 0
        total += cost
        out_tok = (e.get("tokens") or {}).get("output", 0) or 0
        tokens_out += out_tok
        day = (e.get("at") or "")[:10]
        if day:
            by_day[day] = round(by_day.get(day, 0) + cost, 4)
        cap = e.get("capability")
        if cap:
            by_cap[cap] = round(by_cap.get(cap, 0) + cost, 4)
        model = e.get("model")
        if model:
            by_model[model] = round(by_model.get(model, 0) + cost, 4)
    recent = dict(sorted(by_day.items())[-days:])
    top_caps = dict(sorted(by_cap.items(), key=lambda kv: kv[1], reverse=True)[:15])
    return {"total_usd": round(total, 4), "output_tokens": tokens_out,
            "by_day": recent, "by_capability": top_caps, "by_model": by_model,
            "budget": {"soft_tokens": config.setting("budget_soft_tokens"),
                       "hard_tokens": config.setting("budget_hard_tokens"),
                       "soft_usd": config.setting("budget_soft_usd"),
                       "hard_usd": config.setting("budget_hard_usd")}}

def _stats():
    """Per-capability scorecard for the Admin tab (issue #102): reliability + cost aggregates
    from the audit trail (rotation-aware, no Temporal) joined with the gateway's per-tier
    call/fallback counters. Feeds the table beside each cap's exec-model dropdown so a
    downgrade-to-local decision has evidence, not gut feel."""
    return {"caps": engine.scorecard(engine.iter_audit_entries()), "gateway": gateway.stats()}


def _audit_view(wid="", cap="", verified=""):
    """The Audit tab's payload (issue #95): entries across ALL rotated segments, newest first,
    server-side filtered by workflow id / capability / verify verdict. `capabilities` lists the
    distinct cap names over the UNFILTERED trail so the filter dropdown doesn't shrink as you
    narrow. Token totals/segments reflect the filtered view."""
    entries, all_caps = [], set()
    for e in engine.iter_audit_entries():
        if e.get("capability"):
            all_caps.add(e["capability"])
        if wid and (e.get("workflow") or "") != wid:
            continue
        if cap and (e.get("capability") or "") != cap:
            continue
        if verified == "true" and e.get("verified") is not True:
            continue
        if verified == "false" and e.get("verified") is not False:
            continue
        entries.append(e)
    # Token usage is the real resource on a Claude subscription; segment it by the model that
    # ran each attempt so an expensive tier (e.g. everything on Opus) stays visible. cost_usd
    # is kept but is only an API-equivalent estimate.
    by_model, total_tokens = {}, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for e in entries:
        tok = e.get("tokens")
        if not tok:
            continue
        agg = by_model.setdefault(
            e.get("model") or "unknown",
            {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "runs": 0})
        agg["runs"] += 1
        for k in total_tokens:
            agg[k] += tok.get(k, 0) or 0
            total_tokens[k] += tok.get(k, 0) or 0
    return {
        "entries": list(reversed(entries)),
        "count": len(entries),
        "total_cost": round(sum(e.get("cost_usd", 0) or 0 for e in entries), 4),
        "tokens_by_model": by_model,
        "total_tokens": total_tokens,
        "capabilities": sorted(all_caps),
        "filter": {"wid": wid, "cap": cap, "verified": verified},
    }


CAPS = registry.load()
POLICY = policy.load()
registry.apply_policy(CAPS, POLICY)


def rebuild():
    """Re-discover capabilities (after a custom one is added/removed) + re-apply policy."""
    global CAPS
    CAPS = registry.load()
    registry.apply_policy(CAPS, POLICY)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # Local single-user app whose state changes constantly (admin edits, runs, audit).
            # Without this the browser heuristically caches GET JSON, so a re-fetch after an
            # edit (e.g. loadAdmin() after adding a project repo) serves the stale body and the
            # change only shows on a full page reload.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionError):
            pass  # client went away before we finished responding — nothing to do

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > _MAX_BODY:
            raise PayloadTooLarge()
        return json.loads(self.rfile.read(n) or "{}")

    def _csrf_ok(self):
        """Is this POST allowed to mutate state? The API is unauthenticated by design (bound to
        127.0.0.1, single user), so without this ANY page the user visits can drive a write run,
        approve its own gate, or rewrite policy: a cross-origin `fetch` with a text/plain body is
        a "simple request", so it needs no preflight and the attacker never has to read the reply.
        The port is guessable (_bind walks PORT..PORT+40).

        Browsers always send Origin on a POST; a non-browser client (curl, tests, the webhook
        senders) sends none, so absent == allowed. Sec-Fetch-Site is the stronger signal when
        present. /api/events/ is exempt — it carries its own HMAC (see _handle_event)."""
        if self.path.startswith("/api/events/"):
            return True
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if not origin:
            return True
        if origin == "null":
            return False        # sandboxed iframe / file:// — opaque, so never trusted
        if origin in _ALLOWED_ORIGINS:
            return True
        try:
            u = urlparse(origin)
            port = u.port            # .port is what raises on a malformed one, not urlparse()
        except ValueError:
            return False
        return u.hostname in _LOCAL_HOSTS and port == self.server.server_address[1]

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            # `mcp.unhealthy` and `models.broken` both read CACHED health (no slow re-poll, no
            # probe) so any tab can feed the Admin-tab warning badge cheaply on its poll.
            broken = gateway.unhealthy_models()
            self._send(200, json.dumps({"temporal": TEMPORAL_OK, "connected": _temporal_connected(),
                                        "gateway": gateway.stats(),
                                        "plan_mode": config.setting("plan_mode"),
                                        "mcp": {"unhealthy": policy.unhealthy_count(POLICY)},
                                        # Did the last push a human is blocked behind actually
                                        # leave the machine? A silently-failed one is invisible
                                        # otherwise: the run parks at its gate, the phone stays
                                        # quiet, and the gate deadline declines it a day later.
                                        "ntfy": delivery.health(),
                                        "models": {"unhealthy": len(broken), "broken": broken},
                                        # Folded in rather than given its own poller: it is one
                                        # os.stat, and every tab already polls this endpoint on a
                                        # 15s badge tick AND before every turn (refreshHealth), so
                                        # the pause reaches the header with no extra request.
                                        "estop": estop.status(),
                                        "uptime_s": int(time.time() - _START_TIME)}))
        elif self.path == "/api/doctor":
            # Environment doctor (portability): which features are silently degraded on THIS
            # install. Live checks (gh auth, local-model probes) — slow-ish, on demand only.
            checks = doctor.run_checks(caps=CAPS)
            self._send(200, json.dumps({"checks": checks, **doctor.summary(checks)}))
        elif self.path.startswith("/api/wf"):
            wid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            self._send(200, json.dumps(tc.run(_wf_state(wid))))
        elif self.path.startswith("/api/progress"):
            wid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            self._send(200, json.dumps(_run_progress(wid)))
        elif self.path.startswith("/api/run/detail"):
            wid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            self._send(200, json.dumps(_run_detail(wid)))
        elif self.path == "/api/capabilities":
            self._send(200, json.dumps([
                {"name": c.name, "kind": c.kind, "risk": c.risk,
                 "enabled": c.enabled, "description": c.description}
                for c in CAPS
            ]))
        elif self.path == "/api/memory":
            # GLOBAL facts only, newest first — same scope this endpoint always had. Per-project
            # namespaces stay unlisted here (engine.memory_events(every=True) exposes them).
            mem = engine.memory_events()
            facts_total = sum(len(e.get("facts", [])) for e in mem)
            self._send(200, json.dumps({
                "events": list(reversed(mem)),
                "count": len(mem),
                "facts_total": facts_total,
            }))
        elif self.path == "/api/solutions":
            # Solved-task approaches (issue #66) — how a verified run accomplished its task,
            # recalled into similar future runs. A second store beside facts.
            sols = engine.solutions()
            self._send(200, json.dumps({"solutions": sols, "count": len(sols)}))
        elif self.path == "/api/behaviors":
            # Behaviour rules (issue #68) — how the user wants the agent to work, injected as
            # directives. A third store beside facts and solutions.
            rules = engine.behaviors()
            self._send(200, json.dumps({"behaviors": rules, "count": len(rules)}))
        elif self.path == "/api/memory/gc/status":
            # Polled by the Memory tab to reattach to a scan already running server-side — e.g.
            # after a page refresh, since gc/run no longer blocks the request for the scan's
            # whole duration.
            self._send(200, json.dumps(engine.gc_status()))
        elif self.path == "/api/knowledge":
            # Imported reference docs (issue #67) — RAG-retrieved + injected on fresh runs.
            docs = knowledge.documents()
            # only LOCAL models can embed (Claude has no embeddings API) — offer those for the picker
            embed_models = [m["name"] for m in gateway.load().get("pool", [])
                            if m.get("provider") != "claude"]
            self._send(200, json.dumps({"docs": docs, "count": len(docs),
                                        "settings": knowledge.settings(), "embed_models": embed_models}))
        elif self.path.startswith("/api/audit/content"):
            q = parse_qs(urlparse(self.path).query)
            wid = (q.get("wid") or [""])[0]
            at = (q.get("at") or [""])[0]
            attempt_raw = (q.get("attempt") or [""])[0]
            attempt = int(attempt_raw) if attempt_raw.strip().isdigit() else None
            self._send(200, json.dumps(_audit_content(wid, at, attempt)))
        elif self.path.startswith("/api/audit"):
            q = parse_qs(urlparse(self.path).query)
            self._send(200, json.dumps(_audit_view(
                wid=(q.get("wid") or [""])[0], cap=(q.get("cap") or [""])[0],
                verified=(q.get("verified") or [""])[0])))
        elif self.path == "/api/models":
            cfg = gateway.load()
            # The Admin view asking for the models IS the refresh trigger. Safe to keep inline
            # (unlike the MCP health check, which moved off /api/policy): only stale LOCAL entries
            # are probed and a GET /models is milliseconds — a Claude probe is a real `claude -p`
            # turn and waits for the Recheck button below.
            self._send(200, json.dumps({"pool": cfg["pool"], "assign": cfg["assign"],
                                        "endpoints": gateway.endpoints(cfg),
                                        "cap_exec": cfg.get("cap_exec", {}),
                                        "cap_local_exec": cfg.get("cap_local_exec", {}),
                                        "health": gateway.probe_models(cfg=cfg),
                                        "tasks": gateway.TASKS}))
        elif self.path == "/api/settings":
            # UI-editable runtime knobs (config._SETTING_SPECS). Each entry carries value + kind +
            # env var + env_pinned so Admin can render an env-pinned knob as locked instead of
            # offering a control whose clicks env precedence would silently discard.
            # `secrets` rides along READ-ONLY (presence + source per secret, never a value). The
            # helper command is not in _SETTING_SPECS on purpose — see config.SECRET_COMMAND.
            self._send(200, json.dumps({"settings": config.settings_all(),
                                        "secrets": config.secret_status()}))
        elif self.path in ("/api/runbooks", "/api/schedules"):
            # One list: scheduled runbooks (cron) and on-demand ones. `/api/schedules` stays as an
            # alias so an open tab from before the rename keeps working until it reloads.
            self._send(200, json.dumps({"jobs": scheduler.list(), "temporal": TEMPORAL_OK,
                                        "tz": scheduler.local_tz_name() or "UTC",
                                        "caps": [c.name for c in CAPS if c.enabled]}))
        elif self.path.startswith("/api/runbook/"):
            # ONE runbook's full definition (doc + steps), kept out of the list endpoint so the
            # tab's poll doesn't carry every runbook's prose on every refresh.
            rid = self.path.rsplit("/", 1)[-1]
            rb = runbooks.get(rid)
            if not rb:
                self._send(404, json.dumps({"error": "no such runbook"})); return
            self._send(200, json.dumps({"id": rid, "runbook": rb}))
        elif self.path == "/api/board":
            if not TEMPORAL_OK:
                self._send(200, json.dumps({"temporal": False, "items": []})); return
            try:
                items = tc.run(_board())
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"temporal": True, "items": [],
                                            "error": str(e)[:200], "ui": TEMPORAL_UI})); return
            self._send(200, json.dumps({"temporal": True, "items": items, "ui": TEMPORAL_UI}))
        elif self.path.startswith("/api/board/full"):
            # The untruncated result for one card's detail modal — kept OUT of /api/board (polled
            # every few seconds) so that list stays light regardless of how large any run's result is.
            if not TEMPORAL_OK:
                self._send(503, json.dumps({"error": "Temporal not available"})); return
            wid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0].strip()
            if not wid:
                self._send(400, json.dumps({"error": "missing 'id'"})); return
            try:
                self._send(200, json.dumps(tc.run(_board_full_result(wid))))
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"error": str(e)[:200]}))
        elif self.path == "/api/needs-you":
            # Unified "what needs a human / what failed / what's in flight" + health strip.
            if not TEMPORAL_OK:
                self._send(200, json.dumps({"temporal": False, "buckets": {}, "counts": {},
                                            "health": {"temporal": False}})); return
            try:
                data = tc.run(_needs_you())
            except Exception as e:  # noqa: BLE001
                self._send(200, json.dumps({"temporal": True, "buckets": {}, "counts": {},
                                            "health": {"temporal": True}, "error": str(e)[:200]})); return
            self._send(200, json.dumps({"temporal": True, **data}))
        elif self.path == "/api/estop":
            # Global pause state. Cheap (one os.stat) — safe for the header to poll.
            self._send(200, json.dumps(estop.status()))
        elif self.path == "/api/costs":
            # Spend observability, aggregated from the audit log (no Temporal needed).
            self._send(200, json.dumps(_costs()))
        elif self.path == "/api/stats":
            # Per-capability reliability scorecard (issue #102), aggregated from the audit log
            # (no Temporal needed) plus the gateway's per-tier fallback counters.
            self._send(200, json.dumps(_stats()))
        elif self.path == "/api/board-health":
            # Cheap health strip for the Board tab (schedules + cost only — no per-workflow queries).
            self._send(200, json.dumps(_board_health()))
        elif self.path == "/api/bundle/export":
            self._send(200, json.dumps(policy.export_bundle(), indent=2))
        elif self.path == "/api/profile/export":
            # Full portable profile (portability) — the bundle plus policy/models/behaviors/
            # knowledge/project-instructions, secret-free. See profile.py for the CLI twin.
            self._send(200, json.dumps(policy.export_profile(), indent=2))
        elif self.path == "/api/event-rules":
            self._send(200, json.dumps({
                "rules": events.load_rules(),
                "enabled": events.enabled(),         # OTTO_EVENT_SECRET set?
                "caps": [c.name for c in CAPS if c.enabled],  # for the rule form's capability picker
            }))
        elif self.path == "/api/board-config":
            # GitHub Projects board used as the async work queue (distinct from /api/board, the
            # on-screen swarm/approval Board tab).
            bcfg = board.load()
            self._send(200, json.dumps({
                "config": bcfg,
                "url": board.project_url(bcfg),      # link the operator back to the board
                "temporal": TEMPORAL_OK,             # the poller needs Temporal
                "poll": board.poll_status() if TEMPORAL_OK else {"exists": False},
                "caps": [c.name for c in CAPS if c.enabled],  # for the label->cap picker
            }))
        elif self.path == "/api/pr-review-config":
            # Auto-review of PRs assigned to the operator (the GitHub ingress's pull half).
            # `pending` reads STATE ONLY — no gh call — so the Events tab's load time never
            # depends on GitHub being reachable.
            pcfg = pr_review.load()
            self._send(200, json.dumps({
                "config": pcfg,
                "temporal": TEMPORAL_OK,             # the poller needs Temporal
                "viewer": pr_review.viewer(),        # who `gh` is authenticated as
                "poll": pr_review.poll_status() if TEMPORAL_OK else {"exists": False},
                "reviews": pr_review.pending(pcfg),
                "cap": pr_review.review_cap(pcfg),
                # The registered project repos, so the config form ticks boxes instead of
                # asking the operator to retype slugs Otto already holds.
                "known_repos": pr_review.known_repos(),
                "caps": [c.name for c in CAPS if c.enabled],
            }))
        elif self.path.startswith("/api/pr-review/chat"):
            # Does this chat thread hold a PR review, and what would the button do? Asked by
            # the Chat view when a thread is opened — the actions live beside the review the
            # operator is reading, not in the Events tab.
            cid = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
            row = pr_review.for_chat(cid)
            pcfg2 = pr_review.load()
            self._send(200, json.dumps({
                "review": row,
                "approve_on_pass": bool(pcfg2.get("approve_on_pass")),
                "auto_post": bool(pcfg2.get("auto_post")),
            }))
        elif self.path == "/api/slack-config":
            # Slack auto-answer listener (a pull ingress; polls Slack as the user).
            self._send(200, json.dumps({
                "config": slack.load(),
                "temporal": TEMPORAL_OK,             # the poller needs Temporal
                "token_set": slack.token_set(),      # OTTO_SLACK_USER_TOKEN present?
                "self": slack.whoami() if slack.token_set() else None,
                "poll": slack.poll_status() if TEMPORAL_OK else {"exists": False},
                "caps": [c.name for c in CAPS if c.enabled],  # for the optional pinned-cap picker
            }))
        elif self.path == "/api/repos":
            # Allowlisted git repos a task can be run against in an isolated workspace (#57).
            self._send(200, json.dumps({"repos": [
                {"name": r["name"], "origin": r["origin"]} for r in workspace.git_repos()]}))
        elif self.path == "/api/chats":
            self._send(200, json.dumps({"chats": chats.list_summaries()}))
        elif self.path.startswith("/api/chats/get"):
            cid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            self._send(200, json.dumps(chats.get(cid) or {}))
        elif self.path == "/api/policy":
            # `local_blockers`: MCP servers this cap declares that the LOCAL backend can't
            # serve (claude.ai connectors), so the Execution dropdown can refuse the local
            # options with a reason instead of letting the pick fail three attempts later.
            # `local_latched`: the models this cap has PROVED it cannot work on (three
            # consecutive judged failures). Same column, opposite provenance — one is declared
            # up front, the other is earned — and a latch nobody can see is a latch nobody can
            # undo, which is why it ships with its own clear button (/api/cap-latch/clear).
            latched = {}
            for (cap_name, model_name), e in gateway.cap_local_latches().items():
                if not e.get("expired"):
                    latched.setdefault(cap_name, []).append(model_name)
            self._send(200, json.dumps({
                "capabilities": [
                    {"name": c.name, "kind": c.kind, "risk": c.risk,
                     "enabled": c.enabled, "source": c.source, "description": c.description,
                     "prompt": c.prompt or "", "plugin": getattr(c, "plugin", None),
                     "tool_free": getattr(c, "tool_free", False),
                     "local_blockers": mcp_client.unservable(c, POLICY),
                     "local_latched": latched.get(c.name, []),
                     "tier": getattr(c, "tier", None),
                     "stock_kind": getattr(c, "stock_kind", None)}
                    for c in CAPS
                ],
                # Cache only. This used to refresh, which put an ~8s `claude mcp list` (it health-
                # checks every server, with network timeouts) inside the request the Admin panel's
                # spinner waits on — once per TTL, i.e. exactly on the first open. The client now
                # renders from cache and refreshes health in the background via /api/mcp/recheck.
                "mcps": policy.all_mcps(POLICY),
                "projects": registry.projects(),
                # per-project namespace + standing instructions (issue #69), keyed by path
                "project_meta": {p: registry.project_meta(p) for p in registry.projects()},
                "readTools": config.READ_TOOLS,
                "writeTools": config.WRITE_TOOLS,
            }))
        elif self.path == "/api/conventions":
            # Cache-only (conventions.status never derives) — this is one of the fetches the
            # Admin spinner awaits, and deriving here would be N model calls per page load.
            self._send(200, json.dumps({
                "repos": [conventions.status(p) for p in registry.projects()]}))
        else:
            self._send(404, "not found", "text/plain")

    def _handle_event(self):
        """Event/webhook ingress: verify signature on the RAW body, normalize via a rule, and
        start an UNATTENDED workflow (skip clarify; writes gated on the rule's auto_approve)."""
        if not events.enabled():
            self._send(503, json.dumps({"error": "event ingress disabled — set OTTO_EVENT_SECRET"})); return
        if not TEMPORAL_OK:
            self._send(503, json.dumps({"error": "event ingress needs Temporal — run via ./run.sh"})); return
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > _MAX_BODY:
            self._send(413, json.dumps({"error": "payload too large"})); return
        raw = self.rfile.read(n) if n else b""
        sig = self.headers.get("X-Otto-Signature")
        if not events.verify_sig(raw, sig):
            self._send(401, json.dumps({"error": "bad or missing signature"})); return
        # Replay protection: reject a stale timestamp (if the caller sends one) and any exact
        # re-send of an already-processed signature within the replay window.
        if not events.timestamp_fresh(self.headers.get("X-Otto-Timestamp")):
            self._send(401, json.dumps({"error": "stale timestamp"})); return
        # Paused: refuse BEFORE is_replay, which BURNS the signature. Rejecting after it would
        # make the sender's retry-after-release look like a replay and drop the event for good.
        # 503, not 409 — a webhook sender should treat this as "try again later".
        if estop.blocked("events"):
            self._send(503, json.dumps({"error": "Otto is paused", "paused": True})); return
        if events.is_replay(sig):
            self._send(409, json.dumps({"error": "duplicate event (replay)"})); return
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            self._send(400, json.dumps({"error": "invalid JSON"})); return
        source = self.path[len("/api/events/"):].strip("/")
        norm = events.to_request(source, payload)
        if not norm:
            self._send(200, json.dumps({"ignored": True})); return   # no rule matched -> no-op
        cap = None
        if norm.get("cap"):
            # Resolve a pinned capability from the trusted registry (don't take risk from a rule).
            c = next((c for c in CAPS if c.name == norm["cap"]), None)
            if c is None:
                self._send(400, json.dumps({"error": f"rule pins unknown capability '{norm['cap']}'"})); return
            cap = {"name": c.name, "kind": c.kind, "risk": c.risk}
        wid = "evt-" + uuid.uuid4().hex[:8]
        tc.run(_wf_start(wid, {"request": norm["request"], "cap": cap, "unattended": True,
                               "approval": norm["approval"], "reply_to": norm.get("reply_to")}))
        self._send(202, json.dumps({"id": wid, "request": norm["request"]}))

    def do_POST(self):
        if not self._csrf_ok():
            self._send(403, json.dumps({"error": "cross-site request refused"})); return
        try:
            if self.path.startswith("/api/events/"):
                return self._handle_event()        # reads the raw body itself (signature check)
            body = self._json_body()
            if self.path == "/api/estop":
                # Engage/release the global pause. Origin-checked like every other mutating POST
                # (_csrf_ok above) — releasing the pause is exactly the kind of thing a page the
                # operator happens to visit must not be able to do on their behalf.
                if body.get("engaged"):
                    estop.engage(body.get("reason") or "")
                else:
                    estop.release()
                self._send(200, json.dumps(estop.status()))
                return
            if estop.blocked("web") and self.path in ("/api/submit", "/api/continue"):
                self._send(409, json.dumps({"error": "Otto is paused — release the stop to start new work",
                                            "paused": True})); return
            handler = _POST_ROUTES.get(self.path)
            if handler is None:
                for prefix, h in _POST_PREFIXES:
                    if self.path.startswith(prefix):
                        handler = h
                        break
            if handler is None:
                self._send(404, "not found", "text/plain")
                return
            return handler(self, body)
        except PayloadTooLarge:
            self._send(413, json.dumps({"error": "payload too large"}))
        except Exception as e:  # noqa: BLE001 - return the error to the UI
            self._send(500, json.dumps({"error": str(e)}))

    def _post_submit(self, body):
        """POST /api/submit"""
        if not TEMPORAL_OK:
            self._send(503, json.dumps({"error": "Temporal not available — run the server with ./.venv/bin/python server.py and start worker.py"})); return
        req = (body.get("request") or "").strip()
        name = (body.get("cap") or "").strip()
        # Explicit slash-command pick — resolve from the trusted registry (risk never
        # comes from the client) and pin it, skipping Router #1.
        cap = next((c for c in CAPS if c.name == name), None) if name else None
        if name and cap is None:
            self._send(400, json.dumps({"error": f"unknown capability '{name}'"})); return
        if not req:
            # A bare pinned capability (e.g. a self-describing skill like
            # /slack-qna-harvest) is a valid run with no free-text args — synthesize a
            # request so verify/audit/memory have coherent text. An UNPINNED empty request
            # has nothing to route, so it stays an error.
            if cap is None:
                self._send(400, json.dumps({"error": "missing 'request'"})); return
            req = f"Run the {cap.name} {cap.kind}."
        params = {"request": req}
        if cap:
            params["cap"] = {"name": cap.name, "kind": cap.kind, "risk": cap.risk}
        # Per-chat memory opt-out (default on) and model override (default: admin config).
        # Explicit-key check, not truthiness — `body.get("memory_enabled")` would silently
        # collapse an intentional `False` back to the True default.
        if "memory_enabled" in body:
            params["memory_enabled"] = bool(body["memory_enabled"])
        # Composer's "Auto approve" toggle — the human at the keyboard pre-authorizes
        # this chat's writes, so the gate and its plan preview are bypassed. The client
        # sends a BOOLEAN and the server maps it; `approval` itself is never taken from a
        # body, so a browser still cannot reach the unattended "ask"/"skip" modes. What
        # keeps a page the operator merely VISITS from setting it on their behalf is
        # `_csrf_ok` above — this is the toggle that makes that check load-bearing.
        if body.get("auto_approve"):
            params["approval"] = "auto"
        model_override = (body.get("model_override") or "").strip()
        if model_override:
            if not gateway.resolve_model(model_override):
                self._send(400, json.dumps({"error": f"unknown model '{model_override}'"})); return
            params["model_override"] = model_override
        # How hard the model thinks (config.EFFORT_LEVELS). Validated here rather than passed
        # through, for the same reason model_override is: the CLI only WARNS on an unknown
        # --effort value, so an unchecked string yields a run at the default effort that every
        # layer above reports as having honoured the pick.
        effort = config.effort_level(body.get("effort"))
        if effort:
            params["effort"] = effort
        elif (body.get("effort") or "").strip().lower() not in ("", "default"):
            self._send(400, json.dumps({"error": f"unknown effort '{body.get('effort')}'"})); return
        repo = (body.get("repo") or "").strip()
        if repo:
            # Validate against the allowlist HERE (trusted), so the workflow can trust it.
            if not workspace.resolve(repo):
                self._send(400, json.dumps({"error": f"unknown or non-git repo '{repo}'"})); return
            params["repo"] = repo
            # Opt-in post-PR QA loop only makes sense in repo-mode (it validates the PR).
            # Setting it pre-authorizes the QA cap to run through the loop.
            if body.get("qa"):
                params["qa"] = True
            # Opt-in post-PR code-review loop (default-on for the worker cap; this flag
            # enables it for other caps too). Repo-mode only — it reviews the opened PR.
            if body.get("review"):
                params["review"] = True
        # Opt-in plan-then-execute: a strong model decomposes the task into atomic steps a
        # local executor runs one at a time. Only takes effect when OTTO_PLAN_MODE != off
        # (the workflow gates on it) and, in v1, for non-repo runs (repo-mode plans are a
        # follow-up — the workflow ignores it under repo-mode).
        if body.get("plan_mode"):
            params["plan_mode"] = True
        wid = "web-" + uuid.uuid4().hex[:8]
        tc.run(_wf_start(wid, params))
        self._send(200, json.dumps({"id": wid}))

    def _post_continue(self, body):
        """POST /api/continue"""
        name = (body.get("cap") or {}).get("name")
        cap = next((c for c in CAPS if c.name == name), None)
        if cap is None:
            self._send(400, json.dumps({"error": "unknown capability"})); return
        # Follow-up handoff: a follow-up that DELEGATES a new task (e.g. accepting
        # a ticket the cap OFFERED) must start a FRESH routed run — resume keeps
        # the session's cap for life and never engages repo-mode/verify/review (the
        # PM-implements-the-code failure, PR #194). Return the extracted standalone
        # task; the client re-submits it through the normal fresh path. Needs the
        # previous reply (`prev`) for reference resolution — a client that doesn't
        # send it keeps plain resume semantics.
        prev = (body.get("prev") or "").strip()
        if prev and not body.get("no_handoff"):
            task = engine.followup_handoff(body["message"], prev, cap)
            if task:
                self._send(200, json.dumps({"handoff": {"request": task}}))
                return
        trusted_cap = {"name": cap.name, "kind": cap.kind, "risk": cap.risk}
        params = {"request": body["message"], "resume": body["session_id"],
                  "cap": trusted_cap}
        # Same composer toggle as /api/submit: a follow-up can be re-classified from
        # read to write (classify_followup) and hit the gate, so pre-authorization has
        # to travel with the follow-up too or the toggle reads as broken on turn 2.
        if body.get("auto_approve"):
            params["approval"] = "auto"
        # The composer's model picker applies to a follow-up too — same validation as
        # /api/submit.
        model_override = (body.get("model_override") or "").strip()
        if model_override:
            entry = gateway.resolve_model(model_override)
            if not entry:
                self._send(400, json.dumps({"error": f"unknown model '{model_override}'"})); return
            params["model_override"] = model_override
            # A session is BOUND to the backend that minted its id — `claude -p --resume`
            # cannot read a local session's history, nor the local runtime a Claude one —
            # so an override on the other backend is unhonourable in-session. The explicit
            # pick wins over the binding: hand the follow-up back for a fresh run on the
            # chosen model rather than silently resuming on the OLD one (user-reported:
            # "it carries on using that model rather than using the override"). The client
            # re-submits with the conversation carried as context, and says so on screen.
            if (entry.get("provider") != "claude") != local_runtime.is_local_session(
                    body["session_id"]):
                self._send(200, json.dumps({"rebind": {"request": body["message"],
                                                       "model": model_override}}))
                return
        # Effort applies to a follow-up too — same validation as /api/submit. A resume is one
        # more turn of the same conversation, so the composer's current pick governs THIS turn
        # rather than whatever the first turn ran at.
        effort = config.effort_level(body.get("effort"))
        if effort:
            params["effort"] = effort
        elif (body.get("effort") or "").strip().lower() not in ("", "default"):
            self._send(400, json.dumps({"error": f"unknown effort '{body.get('effort')}'"})); return
        # A repo-mode session's isolated clone was torn down after the original run —
        # re-provisioning it needs the repo + the ORIGINAL run's id (its git identity)
        # so the workflow can rebuild the exact same workspace/branch. Re-validate the
        # repo against the allowlist here (trusted), same as /api/submit.
        repo = (body.get("repo") or "").strip()
        git_run_id = body.get("git_run_id")
        # Fall back to the CHAT's stored identity when the client didn't send one. The
        # browser's copy lives in memory from when the chat was opened, so a tab held
        # open across a change to that row keeps sending `undefined` — and an omitted
        # repo doesn't degrade, it makes the follow-up un-resumable (no workspace, so
        # the resume runs from the wrong cwd and returns `(no output)`). The store is
        # Otto's own state and the better authority; it's still allowlist-checked below.
        if not repo:
            ident = chats.git_identity(body["session_id"])
            repo, git_run_id = ident.get("repo", ""), ident.get("git_run_id")
        if repo and workspace.resolve(repo):
            params["repo"] = repo
            params["git_run_id"] = git_run_id
            # Validate here too (trusted boundary) — this reaches a git argv inside
            # the workflow, and the client body isn't trusted input.
            branch = (body.get("git_branch") or "").strip()
            params["git_branch"] = branch if workspace.valid_branch(branch) else None
        wid = "web-" + uuid.uuid4().hex[:8]
        tc.run(_wf_start(wid, params))
        self._send(200, json.dumps({"id": wid}))

    def _post_wf_signal(self, body):
        """POST /api/wf/signal"""
        tc.run(_wf_signal(body["id"], body["signal"], body.get("value")))
        self._send(200, json.dumps({"ok": True}))

    def _post_wf_terminate(self, body):
        """POST /api/wf/terminate"""
        if not TEMPORAL_OK:
            self._send(503, json.dumps({"error": "Temporal not available"})); return
        wid = (body.get("id") or "").strip()
        if not wid:
            self._send(400, json.dumps({"error": "missing 'id'"})); return
        try:
            tc.run(_wf_terminate(wid))
        except Exception as e:  # noqa: BLE001 - already-closed workflow, unknown id
            self._send(200, json.dumps({"error": str(e)[:200]})); return
        _dismiss(wid)
        self._send(200, json.dumps({"ok": True}))

    def _post_needs_you_retry(self, body):
        """POST /api/needs-you/retry"""
        if not TEMPORAL_OK:
            self._send(503, json.dumps({"error": "Temporal not available"})); return
        wid = (body.get("id") or "").strip()
        request, capname, repo, reached_run = _run_origin(wid)
        if not request:
            self._send(404, json.dumps({"error": "no audit history found for that run"})); return
        params = {"request": request}
        if capname and ":" in capname:
            cap = next((c for c in CAPS if c.name == capname.split(":", 1)[1]), None)
            if cap:
                params["cap"] = {"name": cap.name, "kind": cap.kind, "risk": cap.risk}
        if repo and workspace.resolve(repo):
            params["repo"] = repo
        # Carry the ORIGINAL run's reply target across, so a retry returns to the thread
        # that asked (a Slack DM, a GitHub issue, a webhook) instead of only landing on the
        # board. The audit trail never stored `reply_to`, so recover it from the dead run's
        # own Temporal history; if that's aged out, rebuild a Slack target from the run id.
        origin = tc.workflow_input(wid) or {}
        reply_to = origin.get("reply_to") or _slack_reply_target(wid)
        if reply_to:
            params["reply_to"] = reply_to
        # An unattended run stays unattended on retry — nobody is watching to approve, so
        # it must keep its original approval mode instead of blocking on a screen. Recovered
        # INDEPENDENTLY of reply_to: a SCHEDULE-origin run delivers to the chat sidebar and
        # has no reply target, so folding this into the `if reply_to:` block above silently
        # downgraded every scheduled retry to interactive — its `auto_approve` was dropped
        # and a write cap hit the gate the schedule exists to skip.
        unattended = bool(reply_to or origin.get("unattended") or origin.get("scheduled"))
        if unattended:
            params["unattended"] = True
            # `auto_approve=True` is the scheduler's spelling of approval "auto"; mirror
            # the workflow's own mapping so a schedule retry stays pre-authorized.
            params["approval"] = (origin.get("approval")
                                  or ("auto" if origin.get("auto_approve") else "skip"))
        # Overrides whatever the block above decided (including a plain interactive
        # origin, where `unattended` above is False and nothing sets `approval` at all):
        # a run that already reached RUN passed its gate once already (or never needed
        # one), and re-authorizing that same write is exactly what clicking retry means.
        # `unattended=True` here also makes the workflow skip CLARIFY (gated on `not
        # unattended`) — pinning `cap` already skips DECOMPOSE/ROUTER — so this lands
        # straight back in the run/verify ladder instead of redoing everything upstream
        # of the failure.
        if reached_run:
            params["unattended"] = True
            params["approval"] = "auto"
        new_id = "web-" + uuid.uuid4().hex[:8]
        # Record the retry into a Chat thread so its result lands in a conversation, not
        # just on the board: an interactive run records CLIENT-side, so retrying it from
        # the board previously left the result board-only. Three tiers, most precise
        # first: (1) the ORIGINAL run's own chat_key straight from Temporal (exact, no
        # ambiguity); (2) the sticky origin_run_id backstop — catches an interactive
        # web-* run that never had a chat_key at all but did get a browser-tracked
        # run_id; (3) the text-similarity `find_reattach` guess, last resort. Getting all
        # three wrong forks a brand-new chat that holds the real result while the old,
        # familiar thread the Chats tab shows stays frozen (user-reported). chat_key also
        # gives the new card an "Open conversation" link + makes delete-in-flight able to
        # terminate it.
        try:
            reattach = tc.run(_wf_origin_chat_key(wid))
        except Exception:  # noqa: BLE001 - background loop/client unreachable
            reattach = None
        reattach = reattach or chats.find_by_run_origin(wid) or chats.find_reattach(request)
        params["chat_key"] = reattach or new_id
        if not reattach:
            params["chat_labels"] = ["retry"]
        tc.run(_wf_start(new_id, params))
        _record_retry(wid, new_id)
        # Retrying IS acknowledging the card: dismiss it so it leaves "Needs review"
        # immediately — the retry shows up under Running as its own card. Leaving the
        # old card in place read as "did my click even work?" (user-reported).
        _dismiss(wid)
        self._send(200, json.dumps({"ok": True, "id": new_id}))

    def _post_needs_you_accept(self, body):
        """POST /api/needs-you/accept"""
        wid = (body.get("id") or "").strip()
        if not wid:
            self._send(400, json.dumps({"error": "missing 'id'"})); return
        request, capname, repo, _reached_run = _run_origin(wid)
        if not request:
            self._send(404, json.dumps({"error": "no audit history found for that run"})); return
        engine.accept_run(wid, request, capname, _run_result(wid), repo=repo)
        _dismiss(wid)
        self._send(200, json.dumps({"ok": True}))

    def _post_needs_you_dismiss(self, body):
        """POST /api/needs-you/dismiss"""
        wid = (body.get("id") or "").strip()
        if not wid:
            self._send(400, json.dumps({"error": "missing 'id'"})); return
        _dismiss(wid)
        self._send(200, json.dumps({"ok": True}))

    def _post_conventions_refresh(self, body):
        """POST /api/conventions/refresh"""
        path = (body.get("path") or "").strip()
        # Only a registered project — deriving reads a caller-named path off disk and
        # spends model calls, so the path comes from the trusted registry, not the client.
        if path not in registry.projects():
            self._send(400, json.dumps({"error": "not a registered project repo"})); return
        self._send(200, json.dumps(conventions.refresh(path)))

    def _post_policy(self, body):
        """POST /api/policy"""
        POLICY["capabilities"] = body.get("capabilities", {})
        # The panel POSTs the WHOLE policy on any toggle and only tracks `enabled`, so the
        # stored MCP notes are re-attached server-side rather than trusted from the client —
        # otherwise flipping one switch (or a stale tab doing it) erases every note.
        POLICY["mcps"] = policy.keep_notes(policy.load().get("mcps", {}), body.get("mcps", {}))
        policy.save(POLICY)
        registry.apply_policy(CAPS, POLICY)   # take effect immediately
        self._send(200, json.dumps({"ok": True}))

    def _post_capability_add(self, body):
        """POST /api/capability/add"""
        name = (body.get("name") or "").strip()
        if not name:
            self._send(400, json.dumps({"error": "name is required"})); return
        if any(c.name == name for c in CAPS):
            self._send(400, json.dumps({"error": "a capability with that name already exists"})); return
        lst = policy.custom_caps()
        lst.append({"name": name, "description": body.get("description", ""),
                    "risk": body.get("risk", "write"), "prompt": body.get("prompt", "")})
        policy.save_custom_caps(lst); rebuild()
        self._send(200, json.dumps({"ok": True}))

    def _post_capability_edit(self, body):
        """POST /api/capability/edit"""
        name = (body.get("name") or "").strip()
        lst = policy.custom_caps()
        cap = next((c for c in lst if c["name"] == name), None)
        if cap is None:
            self._send(400, json.dumps({"error": "only custom capabilities can be edited"})); return
        cap["description"] = body.get("description", cap.get("description", ""))
        cap["prompt"] = body.get("prompt", cap.get("prompt", ""))
        cap["risk"] = body.get("risk", cap.get("risk", "write"))
        policy.save_custom_caps(lst)
        # Risk for custom caps is resolved by apply_policy (override > classify), so
        # persist the chosen risk as a policy override too, or the edit won't stick.
        POLICY.setdefault("capabilities", {}).setdefault(name, {})["risk"] = cap["risk"]
        policy.save(POLICY)
        rebuild()
        self._send(200, json.dumps({"ok": True}))

    def _post_capability_remove(self, body):
        """POST /api/capability/remove"""
        lst = [c for c in policy.custom_caps() if c["name"] != body.get("name")]
        policy.save_custom_caps(lst)
        gateway.set_cap_exec(body.get("name"), None)   # drop any stale exec override
        gateway.set_cap_local_exec(body.get("name"), None)
        rebuild()
        self._send(200, json.dumps({"ok": True}))

    def _post_mcp_add(self, body):
        """POST /api/mcp/add"""
        name = (body.get("name") or "").strip()
        cmd = (body.get("command") or "").strip()
        if not name or not cmd:
            self._send(400, json.dumps({"error": "name and command are required"})); return
        defs = policy.mcp_defs()
        entry = {"command": cmd, "args": body.get("args", [])}
        if body.get("env"):
            entry["env"] = body["env"]
        defs[name] = entry
        policy.save_mcp_defs(defs)
        self._send(200, json.dumps({"ok": True}))

    def _post_mcp_note(self, body):
        """POST /api/mcp/note — operator usage guidance for one MCP server (empty text clears
        it). Written in place via policy.set_mcp_note, then folded back into the live POLICY so
        a later /api/policy save doesn't overwrite what we just stored."""
        name = (body.get("name") or "").strip()
        if not name or not any(m["name"] == name for m in policy.all_mcps(POLICY)):
            self._send(400, json.dumps({"error": "unknown MCP server"})); return
        saved = policy.set_mcp_note(name, body.get("notes", ""))
        POLICY.clear(); POLICY.update(saved)
        self._send(200, json.dumps({"ok": True,
                                    "notes": (saved.get("mcps", {}).get(name) or {}).get("notes", "")}))

    def _post_mcp_remove(self, body):
        """POST /api/mcp/remove"""
        defs = policy.mcp_defs(); defs.pop(body.get("name"), None)
        policy.save_mcp_defs(defs)
        self._send(200, json.dumps({"ok": True}))

    def _post_mcp_recheck(self, body):
        """POST /api/mcp/recheck"""
        self._send(200, json.dumps({"ok": True,
            "mcps": policy.all_mcps(POLICY, allow_refresh=True,
                                    force=bool(body.get("force", True)))}))

    def _post_cap_latch_clear(self, body):
        """POST /api/cap-latch/clear"""
        gateway.clear_cap_local((body.get("name") or "").strip() or None,
                                (body.get("model") or "").strip() or None)
        self._send(200, json.dumps({"ok": True}))

    def _post_mcp_reconnect(self, body):
        """POST /api/mcp/reconnect"""
        self._send(200, json.dumps(policy.reconnect_mcp((body.get("name") or "").strip(), POLICY)))

    def _post_project_add(self, body):
        """POST /api/project/add — a repo is registered by its remote URL; a local checkout is
        an optional accelerator (`workspace` clones from it and `file_safety` write-denies it).

        The clone runs INLINE, because a registration that returns ok and then resolves to an
        empty directory reads as "Otto imported nothing from my repo" with nothing to click.
        A shallow clone of a 200MB history takes seconds; the form's own spinner covers it."""
        url = (body.get("url") or "").strip()
        path = (body.get("path") or "").strip()
        if not url and not path:
            self._send(400, json.dumps({"error": "a repo URL is required"})); return
        p = os.path.abspath(os.path.expanduser(path)) if path else ""
        if p and not os.path.isdir(p):
            self._send(400, json.dumps({"error": "no such directory: " + p})); return
        if url and not repos.parse(url):
            self._send(400, json.dumps({"error": "not a git remote URL (expected e.g. "
                                                 "https://github.com/owner/repo)"})); return
        if url and not p:
            _, err = repos.ensure(url)
            if err:
                self._send(400, json.dumps({"error": "clone failed: " + err})); return
        url = (repos.parse(url) or {}).get("url", "") if url else ""   # store/echo one spelling
        root = registry.add_project(p, url); rebuild()
        caps = sum(1 for c in registry.project_skills() if c[4] == root)
        self._send(200, json.dumps({"ok": True, "path": root, "url": url, "capabilities": caps,
                                    "warning": "" if os.path.isdir(os.path.join(root, ".claude"))
                                    else "no .claude/ directory in this repo — it is registered "
                                         "for repo-mode and conventions, but imports no "
                                         "capabilities"}))

    def _post_project_remove(self, body):
        """POST /api/project/remove"""
        registry.remove_project(body.get("path", "")); rebuild()
        self._send(200, json.dumps({"ok": True}))

    def _post_project_instructions(self, body):
        """POST /api/project/instructions"""
        registry.set_project_instructions(body.get("path", ""), body.get("instructions", ""))
        self._send(200, json.dumps({"ok": True, "meta": registry.project_meta(body.get("path", ""))}))

    def _post_event_rules(self, body):
        """POST /api/event-rules"""
        self._send(200, json.dumps({"ok": True, "rules": events.save_rules(body.get("rules", []))}))

    def _post_board_config(self, body):
        """POST /api/board-config"""
        saved = board.save(body.get("config", body))
        # Reconcile the poll schedule so enable/disable + interval changes take effect now.
        status = board.reconcile_schedule() if TEMPORAL_OK else "temporal unavailable"
        self._send(200, json.dumps({"ok": True, "config": saved, "schedule": status}))

    def _post_pr_review_config(self, body):
        """POST /api/pr-review-config"""
        saved = pr_review.save(body.get("config", body))
        status = pr_review.reconcile_schedule() if TEMPORAL_OK else "temporal unavailable"
        self._send(200, json.dumps({"ok": True, "config": saved, "schedule": status}))

    def _post_pr_review_post(self, body):
        """POST /api/pr-review/post — write Otto's review to the PR, as the operator.

        Thin on purpose: `pr_review.publish` is the ONE path a review reaches GitHub, shared
        with the unattended auto-post sweep, so the approve decision cannot differ between a
        button press and a poll. Nothing about WHAT is posted comes from the request — the
        API is unauthenticated, so a client-supplied body or approve flag would let any page
        the operator visits write to a colleague's PR under their name."""
        key = str(body.get("key") or "")
        ok, detail, approved = pr_review.publish(key)
        code = 200 if ok else (404 if detail == "unknown PR"
                               else 409 if detail.startswith("no review yet") else 502)
        self._send(code, json.dumps({"ok": ok, "detail": detail, "approved": approved}))

    def _post_pr_review_dismiss(self, body):
        """POST /api/pr-review/dismiss — hide a review from the panel without posting it.

        UI-only, and deliberately NOT a re-review trigger: the request is still pending on
        GitHub, so `decide()` keeps this round marked done and only a genuine re-request
        brings it back."""
        key = str(body.get("key") or "")
        if not (pr_review.state().get("prs") or {}).get(key):
            self._send(404, json.dumps({"ok": False, "error": "unknown PR"}))
            return
        pr_review.update_entry(key, {"dismissed": bool(body.get("dismissed", True))})
        self._send(200, json.dumps({"ok": True}))

    def _post_slack_config(self, body):
        """POST /api/slack-config"""
        saved = slack.save(body.get("config", body))
        # Reconcile the Slack poll schedule so the toggle takes effect immediately.
        status = slack.reconcile_schedule() if TEMPORAL_OK else "temporal unavailable"
        self._send(200, json.dumps({"ok": True, "config": saved, "schedule": status}))

    def _post_chats_save(self, body):
        """POST /api/chats/save"""
        self._send(200, json.dumps({"id": chats.save(body)}))

    def _post_chats_pin(self, body):
        """POST /api/chats/pin"""
        self._send(200, json.dumps({"pinned": chats.set_pinned(body.get("id"), body.get("pinned"))}))

    def _post_chats_delete(self, body):
        """POST /api/chats/delete"""
        cid = body.get("id")
        # If this chat has an in-flight workflow (paused awaiting approval/clarification,
        # or still executing), deleting the chat abandons the task — so hard-stop the
        # workflow too, else its board card lingers as a zombie awaiting a human who's
        # gone. Mirrors the board Terminate button: TERMINATE + dismiss the card.
        # Best-effort; a finished run has cleared its run_id (clearRun) so nothing to kill,
        # and terminating an already-closed workflow just raises and is swallowed.
        wid = (chats.get(cid) or {}).get("run_id")
        if wid and TEMPORAL_OK:
            try:
                tc.run(_wf_terminate(wid))
                _dismiss(wid)
            except Exception:  # noqa: BLE001 - already-closed workflow / unknown id
                pass
        chats.delete(cid)
        self._send(200, json.dumps({"ok": True}))

    def _post_bundle_import(self, body):
        """POST /api/bundle/import"""
        try:
            summary = policy.import_bundle(
                body,
                existing_caps=[c.name for c in CAPS],
                existing_mcps=[m["name"] for m in policy.all_mcps(POLICY)])
        except ValueError as e:
            self._send(400, json.dumps({"error": str(e)})); return
        rebuild()
        self._send(200, json.dumps({"ok": True, **summary}))

    def _post_profile_import(self, body):
        """POST /api/profile/import"""
        try:
            summary = policy.import_profile(
                body,
                existing_caps=[c.name for c in CAPS],
                existing_mcps=[m["name"] for m in policy.all_mcps(POLICY)])
        except ValueError as e:
            self._send(400, json.dumps({"error": str(e)})); return
        rebuild()
        self._send(200, json.dumps({"ok": True, **summary}))

    def _post_memory_delete(self, body):
        """POST /api/memory/delete"""
        ok = engine.delete_fact(body.get("id"), body.get("fact", ""))
        self._send(200 if ok else 404, json.dumps({"ok": ok}))

    def _post_memory_clear(self, body):
        """POST /api/memory/clear"""
        engine.clear_memory()
        self._send(200, json.dumps({"ok": True}))

    def _post_solutions_delete(self, body):
        """POST /api/solutions/delete"""
        engine.delete_solution(body.get("id"))
        self._send(200, json.dumps({"ok": True}))

    def _post_solutions_clear(self, body):
        """POST /api/solutions/clear"""
        engine.clear_solutions()
        self._send(200, json.dumps({"ok": True}))

    def _post_behaviors_add(self, body):
        """POST /api/behaviors/add"""
        rule = engine.add_behavior(body.get("rule", ""), body.get("scope", "global"))
        if not rule:
            self._send(400, json.dumps({"error": "rule text is required"})); return
        self._send(200, json.dumps({"ok": True, "behavior": rule}))

    def _post_behaviors_update(self, body):
        """POST /api/behaviors/update"""
        engine.update_behavior(body.get("id"), body.get("rule", ""))
        self._send(200, json.dumps({"ok": True}))

    def _post_behaviors_delete(self, body):
        """POST /api/behaviors/delete"""
        engine.delete_behavior(body.get("id"))
        self._send(200, json.dumps({"ok": True}))

    def _post_behaviors_suggest(self, body):
        """POST /api/behaviors/suggest"""
        self._send(200, json.dumps(
            engine.suggest_behavior_rule(body.get("message", ""), body.get("cap"))))

    def _post_memory_gc_run(self, body):
        """POST /api/memory/gc/run"""
        self._send(200, json.dumps(engine.gc_start()))

    def _post_memory_gc_evict(self, body):
        """POST /api/memory/gc/evict"""
        n = engine.gc_evict(body.get("candidates") or [])
        self._send(200, json.dumps({"ok": True, "evicted": n}))

    def _post_knowledge_add(self, body):
        """POST /api/knowledge/add"""
        doc = knowledge.add_document(body.get("title", ""), body.get("text", ""),
                                     body.get("source", "paste"))
        if not doc:
            self._send(400, json.dumps({"error": "title and non-empty text are required"})); return
        self._send(200, json.dumps({"ok": True, "doc": doc}))

    def _post_knowledge_delete(self, body):
        """POST /api/knowledge/delete"""
        knowledge.delete_document(body.get("id"))
        self._send(200, json.dumps({"ok": True}))

    def _post_knowledge_clear(self, body):
        """POST /api/knowledge/clear"""
        knowledge.clear()
        self._send(200, json.dumps({"ok": True}))

    def _post_knowledge_settings(self, body):
        """POST /api/knowledge/settings"""
        self._send(200, json.dumps({"ok": True, "settings": knowledge.set_settings(
            threshold=body.get("threshold"), embed_model=body.get("embed_model"))}))

    def _post_knowledge_reembed(self, body):
        """POST /api/knowledge/reembed"""
        self._send(200, json.dumps({"ok": True, "result": knowledge.reembed_all()}))

    def _post_knowledge_preview(self, body):
        """POST /api/knowledge/preview"""
        self._send(200, json.dumps({"hits": knowledge.recall_knowledge(body.get("query", ""))}))

    def _post_models(self, body):
        """POST /api/models"""
        cfg = gateway.load()
        cfg["pool"] = body.get("pool", cfg.get("pool", []))
        cfg["assign"] = body.get("assign", cfg.get("assign", {}))
        cfg["endpoints"] = body.get("endpoints", cfg.get("endpoints", []))
        gateway.save(cfg)
        self._send(200, json.dumps({"ok": True, "endpoints": gateway.endpoints()}))

    def _post_models_discover(self, body):
        """POST /api/models/discover"""
        ep = next((e for e in gateway.endpoints() if e["name"] == body.get("endpoint")),
                  None) if body.get("endpoint") else None
        url = (ep or body).get("base_url") or ""
        if not url:
            self._send(200, json.dumps({"ok": False, "detail": "no endpoint / base URL"}))
        else:
            try:
                found = gateway.discover_models(
                    url, (ep or body).get("api_key_env") or "",
                    headers=(ep or body).get("headers") or {})
                known = {m.get("model") for m in gateway.load().get("pool", [])
                         if m.get("endpoint") == (ep or {}).get("name")}
                # `served` is every id before grouping — a dedupe the operator can't see
                # reads as "that was the whole catalogue".
                self._send(200, json.dumps(
                    {"ok": True, "models": found,
                     "served": sum(1 + len(e["aliases"]) for e in found),
                     "known": sorted(x for x in known if x)}))
            except Exception as e:  # noqa: BLE001 - a dead endpoint is a UI verdict
                self._send(200, json.dumps({"ok": False, "detail": str(e)[:180]}))

    def _post_models_recheck(self, body):
        """POST /api/models/recheck"""
        self._send(200, json.dumps({"ok": True, "health": gateway.probe_models(force=True)}))

    def _post_models_capexec(self, body):
        """POST /api/models/capexec"""
        overrides = gateway.set_cap_exec(body.get("name"), body.get("model") or None)
        self._send(200, json.dumps({"ok": True, "cap_exec": overrides}))

    def _post_models_caplocal(self, body):
        """POST /api/models/caplocal"""
        overrides = gateway.set_cap_local_exec(body.get("name"), body.get("model") or None)
        self._send(200, json.dumps({"ok": True, "cap_local_exec": overrides}))

    def _post_settings(self, body):
        """POST /api/settings"""
        self._send(200, json.dumps(
            {"ok": True, "settings": config.save_settings(body.get("settings") or {})}))

    def _post_models_test(self, body):
        """POST /api/models/test"""
        self._send(200, json.dumps(gateway.test_model(body.get("name"))))

    def _post_prefix_runbooks(self, body):
        """POST /api/runbooks/*"""
        self._runbooks(self.path.rsplit("/", 1)[-1], body)

    def _post_prefix_gate(self, body):
        """POST /api/gate/<token> — approve or deny a parked run from a notification action
        button. `{"approve": bool}`.

        The token IS the authorization, and it is the only endpoint here that has one: everything
        else is protected by binding to 127.0.0.1 plus the Origin check, neither of which a phone
        on the far side of a tunnel can satisfy. So the grant is deliberately the narrowest thing
        that still works — one run, one use, expiring with the gate (delivery.mint_action_token) —
        and the run id is never in the URL, so a leaked topic cannot be replayed against any other
        run or any other endpoint. Unknown/expired/spent token: 403 and nothing happens."""
        token = self.path.rsplit("/", 1)[-1]
        wid = delivery.redeem_action_token(token)
        if not wid:
            self._send(403, json.dumps({"error": "expired or already used"})); return
        approve = bool(body.get("approve"))
        try:
            tc.run(_wf_signal(wid, "approve", approve))
        except Exception as e:  # noqa: BLE001 - the run may have moved on (gate expired, denied)
            self._send(409, json.dumps({"error": str(e)[:200]})); return
        self._send(200, json.dumps({"ok": True, "id": wid,
                                    "decision": "approved" if approve else "denied"}))

    def _resolve_pin(self, name):
        """Resolve an optional pinned-capability name against the TRUSTED registry (never trust
        risk from the client, same as /api/submit). Returns (cap_dict|None, error):
        empty name -> (None, None) = auto-route; unknown name -> (None, message)."""
        name = (name or "").strip()
        if not name:
            return None, None
        c = next((c for c in CAPS if c.name == name), None)
        if c is None:
            return None, f"unknown capability '{name}'"
        return {"name": c.name, "kind": c.kind, "risk": c.risk}, None

    def _check_caps(self, body):
        """Every capability a runbook names — its own and each step's — must exist NOW. Caught at
        save time so the author fixes a typo in the form, rather than at fire time on a schedule
        nobody is watching (engine._plan_step_caps is the second, unavoidable check for a cap
        deleted between saving and running)."""
        names = [body.get("cap")] + [s.get("cap") for s in (body.get("steps") or [])
                                     if isinstance(s, dict)]
        for n in names:
            _, err = self._resolve_pin(n)
            if err:
                return err
        return None

    def _runbooks(self, action, body):
        """CRUD + run for runbooks. Every validation failure is a 400 carrying runbooks.py's own
        message, which is written to be shown to the author verbatim."""
        # Only the actions that actually touch Temporal need it. DEFINING an on-demand runbook is
        # a plain store write, so it must keep working under `python3 server.py` (no Temporal) —
        # 503-ing the whole tab there would make the feature look broken rather than degraded.
        cron = (body.get("cron") or "").strip()
        needs_temporal = action in ("toggle", "run") or (action in ("add", "edit") and cron)
        if needs_temporal and not TEMPORAL_OK:
            self._send(503, json.dumps(
                {"error": "a cron schedule needs Temporal — run via ./run.sh"
                          if cron else "running a runbook needs Temporal — run via ./run.sh"})); return
        try:
            if action in ("add", "edit"):
                err = self._check_caps(body)
                if err:
                    self._send(400, json.dumps({"error": err})); return
                if action == "add":
                    self._send(200, json.dumps({"id": scheduler.add(body)}))
                else:
                    scheduler.update(body["id"], body)
                    self._send(200, json.dumps({"ok": True}))
            elif action == "remove":
                scheduler.remove(body.get("id"))
                self._send(200, json.dumps({"ok": True}))
            elif action == "toggle":
                scheduler.set_paused(body.get("id"), not bool(body.get("enabled")))
                self._send(200, json.dumps({"ok": True}))
            elif action == "run":
                wid = scheduler.run_now(body.get("id"), body.get("values"),
                                        unattended=bool(body.get("unattended")))
                self._send(200, json.dumps({"ok": True, "status": "started", "id": wid}))
            else:
                self._send(404, "not found", "text/plain")
        except scheduler.AlreadyRunning as e:
            self._send(409, json.dumps({"error": str(e)}))
        except ValueError as e:            # validation — the message IS the user-facing text
            self._send(400, json.dumps({"error": str(e)}))
        except KeyError:
            self._send(404, json.dumps({"error": "no such runbook"}))

    def log_message(self, *a):
        pass  # engine.trace() already prints a readable trace


# POST dispatch. Built from Handler's methods rather than a 52-branch `elif` chain:
# the chain was 193 branches in one function, and a route's position in it was load-
# bearing (an exact match had to precede every prefix match). Exact wins, then prefix.
_POST_ROUTES = {
    "/api/behaviors/add": Handler._post_behaviors_add,
    "/api/behaviors/delete": Handler._post_behaviors_delete,
    "/api/behaviors/suggest": Handler._post_behaviors_suggest,
    "/api/behaviors/update": Handler._post_behaviors_update,
    "/api/board-config": Handler._post_board_config,
    "/api/bundle/import": Handler._post_bundle_import,
    "/api/cap-latch/clear": Handler._post_cap_latch_clear,
    "/api/capability/add": Handler._post_capability_add,
    "/api/capability/edit": Handler._post_capability_edit,
    "/api/capability/remove": Handler._post_capability_remove,
    "/api/chats/delete": Handler._post_chats_delete,
    "/api/chats/pin": Handler._post_chats_pin,
    "/api/chats/save": Handler._post_chats_save,
    "/api/continue": Handler._post_continue,
    "/api/conventions/refresh": Handler._post_conventions_refresh,
    "/api/event-rules": Handler._post_event_rules,
    "/api/knowledge/add": Handler._post_knowledge_add,
    "/api/knowledge/clear": Handler._post_knowledge_clear,
    "/api/knowledge/delete": Handler._post_knowledge_delete,
    "/api/knowledge/preview": Handler._post_knowledge_preview,
    "/api/knowledge/reembed": Handler._post_knowledge_reembed,
    "/api/knowledge/settings": Handler._post_knowledge_settings,
    "/api/mcp/add": Handler._post_mcp_add,
    "/api/mcp/note": Handler._post_mcp_note,
    "/api/mcp/recheck": Handler._post_mcp_recheck,
    "/api/mcp/reconnect": Handler._post_mcp_reconnect,
    "/api/mcp/remove": Handler._post_mcp_remove,
    "/api/memory/clear": Handler._post_memory_clear,
    "/api/memory/delete": Handler._post_memory_delete,
    "/api/memory/gc/evict": Handler._post_memory_gc_evict,
    "/api/memory/gc/run": Handler._post_memory_gc_run,
    "/api/models": Handler._post_models,
    "/api/models/capexec": Handler._post_models_capexec,
    "/api/models/caplocal": Handler._post_models_caplocal,
    "/api/models/discover": Handler._post_models_discover,
    "/api/models/recheck": Handler._post_models_recheck,
    "/api/models/test": Handler._post_models_test,
    "/api/needs-you/accept": Handler._post_needs_you_accept,
    "/api/needs-you/dismiss": Handler._post_needs_you_dismiss,
    "/api/needs-you/retry": Handler._post_needs_you_retry,
    "/api/policy": Handler._post_policy,
    "/api/profile/import": Handler._post_profile_import,
    "/api/project/add": Handler._post_project_add,
    "/api/project/instructions": Handler._post_project_instructions,
    "/api/project/remove": Handler._post_project_remove,
    "/api/settings": Handler._post_settings,
    "/api/pr-review-config": Handler._post_pr_review_config,
    "/api/pr-review/dismiss": Handler._post_pr_review_dismiss,
    "/api/pr-review/post": Handler._post_pr_review_post,
    "/api/slack-config": Handler._post_slack_config,
    "/api/solutions/clear": Handler._post_solutions_clear,
    "/api/solutions/delete": Handler._post_solutions_delete,
    "/api/submit": Handler._post_submit,
    "/api/wf/signal": Handler._post_wf_signal,
    "/api/wf/terminate": Handler._post_wf_terminate,
}

_POST_PREFIXES = [
    ("/api/runbooks/", Handler._post_prefix_runbooks),
    ("/api/gate/", Handler._post_prefix_gate),
]


def _bind(start):
    """Bind the first free port from `start` upward (skips ports already in use)."""
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    for port in range(start, start + 40):
        try:
            return socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler), port
        except OSError:
            continue
    raise SystemExit(f"No free port in {start}-{start + 39}. Set PORT to a free one.")


def main():
    # A repo registered before URLs existed carries only a path. Derive its remote once, so the
    # Admin row shows what the operator actually registered and a re-registration from another
    # machine resolves the SAME entry instead of forking one.
    #
    # In main(), NOT at import: this WRITES data/projects.json, and module import is what the
    # test suite and every tool does before `setUpModule` re-points PROJECTS_FILE at a temp dir.
    # At import scope it migrated the live store as a side effect of `import server`.
    registry.backfill_project_urls()
    # Temporal is REQUIRED to serve (the direct run path was removed — issue #278). Module
    # import stays soft so the stdlib-only test run can still import handlers.
    if not TEMPORAL_OK:
        raise SystemExit(
            "Otto requires Temporal: temporalio isn't importable from this interpreter.\n"
            "Run ./install.sh once, then start via ./run.sh "
            "(or ./.venv/bin/python server.py with the worker running).")
    httpd, port = _bind(PORT)
    if port != PORT:
        print(f"port {PORT} was busy — using {port} instead")
    print(f"Otto web ingress  ->  http://localhost:{port}   (Ctrl-C to stop)", flush=True)
    print(f"Discovered {len(CAPS)} capabilities "
          f"({sum(c.risk=='read' for c in CAPS)} read / {sum(c.risk=='write' for c in CAPS)} write)", flush=True)
    # Align Temporal with data/schedules.json: fix drifted timezones, GC orphaned
    # schedules that cause duplicate fires, and recreate any wiped by a restart.
    ok = scheduler.reconcile()
    print(f"Schedules: {'reconciled with Temporal' if ok else 'reconcile skipped (Temporal unreachable)'}", flush=True)
    # Board poll schedule — reconcile AFTER scheduler.reconcile (whose orphan-GC only touches
    # "otto-*" ids; board's "board-poll" id is deliberately outside that namespace).
    print(f"Board queue: {board.reconcile_schedule()}", flush=True)
    # Reaper schedule — sweeps for stuck/dead workflows across all ingresses. Same
    # out-of-"otto-*"-namespace reasoning as the board poll id.
    print(f"Reaper: {board.reconcile_reaper_schedule()}", flush=True)
    # Slack auto-answer poll schedule — same out-of-"otto-*"-namespace reasoning.
    print(f"Slack listener: {slack.reconcile_schedule()}", flush=True)
    # PR-review poll schedule — same out-of-"otto-*"-namespace reasoning.
    print(f"PR reviews: {pr_review.reconcile_schedule()}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
