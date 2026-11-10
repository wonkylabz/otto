"""Data health: invariants over Otto's STORES, not its runs.

`insights` reads the audit trail, so it sees only what a run recorded. A store that drifts from
what it is derived from fails no run and writes no row: a workstream grew to 38 refs, and a
guard flagged the operator's own commits, with every run green (2026-11-10). Each check here
RECOMPUTES a stored value from its source and reports the difference, or flags an outlier no
working rule produces. Read-only; every check is isolated, so one broken store can't blank the rest.
"""
import json

# A chat or workstream is about ONE piece of work; more refs than this is a rule counting
# citations, not subjects.
REF_SPRAWL = 10


def _finding(check, subject, detail):
    return {"check": check, "subject": subject, "detail": detail}


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
            if stored is not None and stored != now:
                out.append(_finding("chat_refs_drift", c["title"] or c["id"],
                                    f"stored {len(stored)} refs, its messages give {len(now)}"))
            if len(now) > REF_SPRAWL:
                out.append(_finding("ref_sprawl", f"chat: {c['title'] or c['id']}",
                                    f"{len(now)} refs (> {REF_SPRAWL})"))
            ws = c["workstream_id"]
            if ws and ws not in titles:
                out.append(_finding("dangling_membership", c["title"] or c["id"],
                                    f"filed under missing workstream {ws}"))
            if ws:
                derived.setdefault(ws, set()).update(now)
        for r in conn.execute("SELECT id, title, refs FROM workstreams"):
            stored, now = _load(r["refs"]), derived.get(r["id"])
            if now is None:
                out.append(_finding("empty_workstream", r["title"], "no chat belongs to it"))
                continue
            if stored != now:
                out.append(_finding("workstream_refs_drift", r["title"],
                                    f"stored {len(stored)} refs, its chats give {len(now)}"))
            if len(stored) > REF_SPRAWL:
                out.append(_finding("ref_sprawl", f"workstream: {r['title']}",
                                    f"{len(stored)} refs (> {REF_SPRAWL})"))
    return out


def _models():
    import gateway
    d = gateway.load().get("dangling") or {}
    return [_finding("dangling_model_pin", cap, f"{kind} names '{m}', which is not in the pool")
            for kind in ("cap_exec", "cap_local_exec") for cap, m in sorted((d.get(kind) or {}).items())]


CHECKS = (("workstreams", _workstreams), ("models", _models))


def report():
    """{findings, errors}: every check runs; one that raises is reported, never fatal."""
    findings, errors = [], []
    for name, fn in CHECKS:
        try:
            findings += fn()
        except Exception as e:  # noqa: BLE001 - one broken store must not hide the others
            errors.append({"check": name, "error": f"{type(e).__name__}: {e}"[:300]})
    return {"findings": findings, "errors": errors}


def render_text(r):
    if not r["findings"] and not r["errors"]:
        return "Data health: no store drift found."
    lines = [f"Data health: {len(r['findings'])} finding(s)"]
    lines += [f"- {f['check']}: {f['subject']} — {f['detail']}" for f in r["findings"]]
    lines += [f"- check {e['check']} could not run: {e['error']}" for e in r["errors"]]
    return "\n".join(lines)
