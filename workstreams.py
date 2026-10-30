"""Workstreams — every chat (and so every run) about one topic, grouped.

A ticket or a PR routinely spans several chats: the first run, a retry, a review thread, a
follow-up opened days later. A workstream groups them so their history reads as ONE piece of
work. Display-only by design: nothing that RUNS reads it — not routing, memory, the gate or
workflow code — so a wrong grouping costs a misfiled card, never a misdirected run.

Membership lives on `chats.workstream_id`, not on runs: the Swarm board forgets a finished card
after `board_retention_h`, and a workstream outlives that. NULL = eligible for auto-attach, `''` =
the user took the chat out (never re-attached automatically), anything else = a workstream id.

Auto-attach keys on REFS — canonical ids of the issue/PR/ticket URLs a chat names
(`o/r#12`, `jira:ABC-12`). A user message contributes every ref it links; an Otto reply only
the PRs it links (it opened them), since a reply citing related work is not that work. A bare
`#12` is a mention, never a ref — the same reading `workspace.closing_line` gives it.
"""
import datetime
import json
import re
import uuid

import storage

_GH = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)", re.I)
_JIRA = re.compile(r"https?://[\w.-]+\.atlassian\.net/browse/([A-Z][A-Z0-9]+-\d+)")


def _conn():
    import chats   # chats imports this module; its connection carries the schema and the DB alias
    return chats._conn()


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def refs_of(text, *, prs_only=False):
    """Canonical refs linked in `text`. An issue and a PR share one number space per repo, so
    both spell `owner/repo#N` — a run that opened PR #40 for issue #12 is joined by either."""
    out = set()
    for owner, repo, kind, n in _GH.findall(text or ""):
        if prs_only and kind.lower() != "pull":
            continue
        out.add(f"{owner.lower()}/{repo.lower()}#{n}")
    if not prs_only:
        out.update("jira:" + k for k in _JIRA.findall(text or ""))
    return out


def chat_refs(conn, cid):
    refs = set()
    for r in conn.execute("SELECT role, text FROM messages WHERE chat_id = ? AND text LIKE '%http%'",
                          (cid,)):
        refs |= refs_of(r["text"], prs_only=r["role"] != "user")
    return refs


def _load(value):
    try:
        return set(json.loads(value or "[]"))
    except ValueError:
        return set()


def _merge_refs(conn, wsid, refs):
    row = conn.execute("SELECT refs FROM workstreams WHERE id = ?", (wsid,)).fetchone()
    if row is None:
        return
    merged = _load(row["refs"]) | refs
    conn.execute("UPDATE workstreams SET refs = ?, updated = ? WHERE id = ?",
                 (json.dumps(sorted(merged)), _now(), wsid))


def _create(conn, title, refs=()):
    wsid = "ws-" + uuid.uuid4().hex[:8]
    conn.execute("INSERT INTO workstreams (id, title, refs, created, updated) VALUES (?, ?, ?, ?, ?)",
                 (wsid, (title or "Untitled").strip()[:80] or "Untitled",
                  json.dumps(sorted(refs)), _now(), _now()))
    return wsid


def attach(conn, cid):
    """Re-read `cid`'s refs and file it, under the caller's open transaction. Pure SQL, no
    network: it runs inside every chat write's `storage.tx` critical section."""
    row = conn.execute("SELECT workstream_id FROM chats WHERE id = ?", (cid,)).fetchone()
    if row is None:
        return
    refs = chat_refs(conn, cid)
    conn.execute("UPDATE chats SET refs = ? WHERE id = ?", (json.dumps(sorted(refs)), cid))
    current = row["workstream_id"]
    if current == "" or not refs:
        return
    if current:
        _merge_refs(conn, current, refs)
        return
    hits = [r["id"] for r in conn.execute("SELECT id, refs FROM workstreams")
            if _load(r["refs"]) & refs]
    if len(hits) > 1:
        return                      # ambiguous: two workstreams claim it — a human decides
    if hits:
        conn.execute("UPDATE chats SET workstream_id = ? WHERE id = ?", (hits[0], cid))
        _merge_refs(conn, hits[0], refs)
        return
    peers = [r for r in conn.execute("SELECT id, title, refs FROM chats WHERE id != ? AND "
                                     "workstream_id IS NULL ORDER BY created, seq", (cid,))
             if _load(r["refs"]) & refs]
    if not peers:
        return
    wsid = _create(conn, peers[0]["title"], refs.union(*(_load(p["refs"]) for p in peers)))
    conn.executemany("UPDATE chats SET workstream_id = ? WHERE id = ?",
                     [(wsid, c) for c in [cid] + [p["id"] for p in peers]])


def gc(conn):
    """Drop workstreams no chat belongs to any more (a trim, a delete or a detach emptied it)."""
    conn.execute("DELETE FROM workstreams WHERE id NOT IN "
                 "(SELECT workstream_id FROM chats WHERE workstream_id IS NOT NULL)")


def _summary(r):
    return {"id": r["id"], "title": r["title"], "refs": sorted(_load(r["refs"])),
            "created": r["created"], "updated": r["updated"],
            "chats": r["n"], "running": r["live"]}


def list_all():
    with _conn() as conn:
        rows = conn.execute("""
            SELECT w.*, COUNT(c.id) AS n, SUM(c.run_id IS NOT NULL) AS live
            FROM workstreams w JOIN chats c ON c.workstream_id = w.id
            GROUP BY w.id ORDER BY w.updated DESC""").fetchall()
    return [_summary(r) for r in rows]


def by_chat():
    """{chat_id: {id, title}} for every filed chat — one query, for the board's per-card chip."""
    with _conn() as conn:
        rows = conn.execute("SELECT c.id AS cid, w.id, w.title FROM chats c "
                            "JOIN workstreams w ON w.id = c.workstream_id").fetchall()
    return {r["cid"]: {"id": r["id"], "title": r["title"]} for r in rows}


def get(wsid):
    """The workstream with its chats oldest-first: the timeline of what was done about it."""
    with _conn() as conn:
        ws = conn.execute("""SELECT w.*, COUNT(c.id) AS n, SUM(c.run_id IS NOT NULL) AS live
                             FROM workstreams w LEFT JOIN chats c ON c.workstream_id = w.id
                             WHERE w.id = ? GROUP BY w.id""", (wsid,)).fetchone()
        if ws is None:
            return None
        rows = conn.execute("SELECT * FROM chats WHERE workstream_id = ? ORDER BY created, seq",
                            (wsid,)).fetchall()
        items = []
        for c in rows:
            last = conn.execute("SELECT text FROM messages WHERE chat_id = ? AND role = 'otto' "
                                "AND pending = 0 ORDER BY seq DESC LIMIT 1", (c["id"],)).fetchone()
            first = conn.execute("SELECT text FROM messages WHERE chat_id = ? AND role = 'user' "
                                 "ORDER BY seq LIMIT 1", (c["id"],)).fetchone()
            prs = set()
            for m in conn.execute("SELECT text FROM messages WHERE chat_id = ? AND role = 'otto' "
                                  "AND text LIKE '%/pull/%'", (c["id"],)):
                prs |= {u for u in re.findall(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+", m["text"])}
            stats = json.loads(c["stats"]) if c["stats"] else {}
            items.append({"id": c["id"], "title": c["title"], "created": c["created"],
                          "updated": c["updated"], "cap": (json.loads(c["cap"]) or {}).get("name")
                          if c["cap"] else None,
                          "labels": json.loads(c["labels"] or "[]"), "running": bool(c["run_id"]),
                          "run_id": c["run_id"] or c["origin_run_id"], "repo": c["repo"],
                          "cost": stats.get("cost"), "prs": sorted(prs),
                          "request": (first["text"] if first else "")[:300],
                          "last": (last["text"] if last else "")[:600]})
    return dict(_summary(ws), items=items)


def assign(cid, wsid=None, title=None):
    """File `cid` under `wsid`, or under a NEW workstream named `title`, or (neither) take it out.
    Taking it out stores `''`, so auto-attach never puts it straight back. Returns the chat's
    workstream id ('' when detached), or None when either id is unknown."""
    with _conn() as conn, storage.tx(conn):
        if conn.execute("SELECT 1 FROM chats WHERE id = ?", (cid,)).fetchone() is None:
            return None
        if title:
            wsid = _create(conn, title, chat_refs(conn, cid))
        elif wsid:
            if conn.execute("SELECT 1 FROM workstreams WHERE id = ?", (wsid,)).fetchone() is None:
                return None
            _merge_refs(conn, wsid, chat_refs(conn, cid))
        conn.execute("UPDATE chats SET workstream_id = ? WHERE id = ?", (wsid or "", cid))
        gc(conn)
    return wsid or ""


def rename(wsid, title):
    title = (title or "").strip()[:80]
    if not title:
        return False
    with _conn() as conn:
        return bool(conn.execute("UPDATE workstreams SET title = ?, updated = ? WHERE id = ?",
                                 (title, _now(), wsid)).rowcount)


def delete(wsid):
    """Ungroup: the chats stay, each marked detached so the refs that grouped them don't
    regroup them on their next write."""
    with _conn() as conn, storage.tx(conn):
        conn.execute("UPDATE chats SET workstream_id = '' WHERE workstream_id = ?", (wsid,))
        return bool(conn.execute("DELETE FROM workstreams WHERE id = ?", (wsid,)).rowcount)
