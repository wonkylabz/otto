"""Data health: invariants over Otto's STORES, not its runs.

`insights` reads the audit trail, so it sees only what a run recorded. A store that drifts from
what it is derived from fails no run and writes no row: a workstream grew to 38 refs, and a
guard flagged the operator's own commits, with every run green (2026-11-10). Each check here
RECOMPUTES a stored value from its source and reports the difference, or flags an outlier no
working rule produces. Read-only; every check is isolated, so one broken store can't blank the rest.

A finding the app can repair carries a `fix` label and a `key`. `fix()` takes only that KEY,
re-runs the checks and applies the action THIS module picked, so a client never names an action
or a target, and a finding that has since resolved is a no-op.
"""
import json

# A chat or workstream is about ONE piece of work; more refs than this is a rule counting
# citations, not subjects.
REF_SPRAWL = 10


def _finding(check, subject, detail, fix=None, key=None, apply=None):
    f = {"check": check, "subject": subject, "detail": detail}
    if fix:
        f.update(fix=fix, key=f"{check}:{key}", _apply=apply)
    return f


def _load(v):
    try:
        return set(json.loads(v or "[]"))
    except ValueError:
        return set()


def _workstreams():
    import chats
    import workstreams
    out = []
    with chats._conn() as conn:
        titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM workstreams")}
        derived = {}
        for c in conn.execute("SELECT id, title, refs, workstream_id FROM chats").fetchall():
            now = workstreams.chat_refs(conn, c["id"])
            stored = _load(c["refs"]) if c["refs"] is not None else None
            name, cid = c["title"] or c["id"], c["id"]
            if stored is not None and stored != now:
                out.append(_finding("chat_refs_drift", name,
                                    f"stored {len(stored)} refs, its messages give {len(now)}",
                                    "Re-derive", cid, workstreams.rebuild))
            ws = c["workstream_id"]
            # A detached chat ('') is never grouped, so however many refs it holds, none can
            # spread — a job's old shared thread legitimately holds one PR per run.
            if len(now) > REF_SPRAWL and ws != "":
                out.append(_finding("ref_sprawl", f"chat: {name}", f"{len(now)} refs (> {REF_SPRAWL})",
                                    "Detach chat", cid, lambda cid=cid: workstreams.assign(cid)))
            if ws and ws not in titles:
                out.append(_finding("dangling_membership", name, f"filed under missing workstream {ws}",
                                    "Detach chat", cid, lambda cid=cid: workstreams.assign(cid)))
            if ws:
                derived.setdefault(ws, set()).update(now)
        for r in conn.execute("SELECT id, title, refs FROM workstreams"):
            stored, now = _load(r["refs"]), derived.get(r["id"])
            if now is None:
                out.append(_finding("empty_workstream", r["title"], "no chat belongs to it",
                                    "Delete", r["id"], lambda w=r["id"]: workstreams.delete(w)))
                continue
            if stored != now:
                out.append(_finding("workstream_refs_drift", r["title"],
                                    f"stored {len(stored)} refs, its chats give {len(now)}",
                                    "Re-derive", r["id"], workstreams.rebuild))
            if len(stored) > REF_SPRAWL:
                out.append(_finding("ref_sprawl", f"workstream: {r['title']}",
                                    f"{len(stored)} refs (> {REF_SPRAWL})"))
    return out


_CLEAR = {"cap_exec": "set_cap_exec", "cap_local_exec": "set_cap_local_exec"}


def _models():
    """A pin on a cap that no longer exists is checked FIRST: a renamed plugin leaves its pins
    under the old name, where Admin (which lists only installed caps) can never show or clear
    them, and "model not in the pool" sent the operator to a cap that runs fine."""
    import gateway
    import registry
    cfg = gateway.load()
    caps = {c.name for c in registry.load()}
    pool = {m["name"] for m in cfg.get("pool", [])}
    out = []
    for kind, setter in _CLEAR.items():
        # `load` already pruned a dangling LOCAL pin from the live map; `dangling` still has it.
        pins = {**(cfg.get(kind) or {}), **((cfg.get("dangling") or {}).get(kind) or {})}
        for cap, m in sorted(pins.items()):
            clear = lambda cap=cap, setter=setter: getattr(gateway, setter)(cap, None)
            if cap not in caps:
                out.append(_finding("orphaned_model_pin", cap,
                                    f"{kind} pins '{m}' for a capability that is not installed",
                                    "Remove pin", f"{kind}:{cap}", clear))
            elif m not in pool:
                out.append(_finding("dangling_model_pin", cap,
                                    f"{kind} names '{m}', which is not in the pool, so it runs on the "
                                    "default; re-pick in Admin, or clear it", "Clear pin", f"{kind}:{cap}", clear))
    return out


CHECKS = (("workstreams", _workstreams), ("models", _models))


def _run():
    findings, errors = [], []
    for name, fn in CHECKS:
        try:
            findings += fn()
        except Exception as e:  # noqa: BLE001 - one broken store must not hide the others
            errors.append({"check": name, "error": f"{type(e).__name__}: {e}"[:300]})
    return findings, errors


def report():
    """{findings, errors}: every check runs; one that raises is reported, never fatal."""
    findings, errors = _run()
    return {"findings": [{k: v for k, v in f.items() if k != "_apply"} for f in findings],
            "errors": errors}


def fix(key):
    """Apply the repair for the finding `key` names, as recomputed NOW. False when no current
    finding has that key (already fixed, or never fixable)."""
    f = next((f for f in _run()[0] if key and f.get("key") == key), None)
    if not f:
        return False
    f["_apply"]()
    return True


def render_text(r):
    if not r["findings"] and not r["errors"]:
        return "Data health: no store drift found."
    lines = [f"Data health: {len(r['findings'])} finding(s)"]
    lines += [f"- {f['check']}: {f['subject']} — {f['detail']}" for f in r["findings"]]
    lines += [f"- check {e['check']} could not run: {e['error']}" for e in r["errors"]]
    return "\n".join(lines)
