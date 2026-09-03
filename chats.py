"""Persisted chat history — past conversations the user can reopen and continue.

Stored in `data/otto.db` (issue #103) as two tables: `chats` (one row per thread) and
`messages` (one row per turn, ordered by `seq`). This is UI-convenience state (a chat app's
sidebar), distinct from **audit** (the forensic trail) and **memory** (distilled facts). It
records what was said on screen so you can scroll back to and resume an earlier thread
(`session_id` + `cap` drive the `/api/continue` resume).

Why tables and not one JSON blob: this is the largest store, and it used to be rewritten
WHOLESALE on every save — so rendering the sidebar (`list_summaries`, which needs only a
message COUNT) loaded every message body of every thread, and one appended turn rewrote the
entire file. Now the count is a SQL aggregate and a turn touches one row.

The record shape callers see is unchanged: `{id, title, created, updated, session_id, run_id,
cap, repo, git_run_id, labels, pinned, stats, messages:[{role, text, ts, pending?}]}`.
"""
import contextlib
import datetime
import json

import config
import storage

_DB = config.DB_PATH
MAX_CHATS = 100        # keep the most-recent N; older ones are dropped
MAX_MESSAGES = 400     # per-chat safety cap


def _schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        title TEXT,
        created TEXT,
        updated TEXT,
        session_id TEXT,
        run_id TEXT,
        origin_run_id TEXT,
        repo TEXT,
        git_run_id TEXT,
        pinned INTEGER NOT NULL DEFAULT 0,
        cap TEXT,
        labels TEXT,
        stats TEXT
    )""")
    # Lightweight migration for a DB created before origin_run_id existed: CREATE TABLE IF NOT
    # EXISTS above is a no-op on an already-live table, so a fresh column needs its own ADD COLUMN.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chats)")}
    if "origin_run_id" not in cols:
        conn.execute("ALTER TABLE chats ADD COLUMN origin_run_id TEXT")
    # The sidebar's sort key, and the trim's "which are the oldest" question.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chats_order ON chats(pinned, updated, seq)")
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        chat_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        role TEXT,
        text TEXT,
        ts TEXT,
        pending INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, seq)
    )""")


@contextlib.contextmanager
def _conn():
    conn = storage.sqlite_connect(_DB)
    try:
        _schema(conn)
        yield conn
    finally:
        conn.close()


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _decode(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _message(row):
    """Rebuild one message dict. `ts`/`pending` are omitted when unset rather than emitted as
    null/false, so a round-tripped message is byte-identical to what the writers produced."""
    m = {"role": row["role"], "text": row["text"]}
    if row["ts"] is not None:
        m["ts"] = row["ts"]
    if row["pending"]:
        m["pending"] = True
    return m


def _messages(conn, cid):
    rows = conn.execute(
        "SELECT role, text, ts, pending FROM messages WHERE chat_id = ? ORDER BY seq", (cid,))
    return [_message(r) for r in rows]


def _chat(row, messages):
    return {
        "id": row["id"],
        "title": row["title"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "cap": _decode(row["cap"]),
        "repo": row["repo"],
        "git_run_id": row["git_run_id"],
        "labels": _decode(row["labels"]),
        "pinned": bool(row["pinned"]),
        "stats": _decode(row["stats"]),
        "messages": messages,
        "created": row["created"],
        "updated": row["updated"],
    }


def list_summaries():
    """Lightweight list for the sidebar — no message bodies, newest first. The message count is
    a SQL aggregate, so this never reads a single message body."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT c.id, c.title, c.updated, c.cap, c.labels, c.run_id, c.pinned,
                   (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.id) AS n
            FROM chats c
            -- Pinned chats float to the top; within each group, newest first (`seq` breaks the
            -- tie when two saves land in the same second).
            ORDER BY c.pinned DESC, c.updated DESC, c.seq DESC
        """).fetchall()
    return [{"id": r["id"], "title": r["title"] or "", "updated": r["updated"],
             "messages": r["n"], "cap": (_decode(r["cap"]) or {}).get("name"),
             "labels": _decode(r["labels"]) or [], "run_id": r["run_id"],
             "pinned": bool(r["pinned"])}
            for r in rows]


def get(cid):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM chats WHERE id = ?", (cid,)).fetchone()
        if not row:
            return None
        return _chat(row, _messages(conn, cid))


def save(chat):
    """Upsert a chat by id; returns the stored id (or None if no id given). Preserves the
    original `created`, stamps `updated`, trims messages, and caps total stored chats.

    Runs as ONE serialized transaction (storage.tx): the prior row is read and the new one
    written under the same write lock, so a concurrent save from the other process can't
    interleave and lose the fields this merge preserves."""
    cid = chat.get("id")
    if not cid:
        return None
    messages = (chat.get("messages") or [])[-MAX_MESSAGES:]
    with _conn() as conn, storage.tx(conn):
        prior = conn.execute("SELECT * FROM chats WHERE id = ?", (cid,)).fetchone()

        def kept(field):
            """A field the caller may omit to mean "leave as-is" (None = not supplied)."""
            supplied = chat.get(field)
            return supplied if supplied is not None else (prior[field] if prior else None)

        record = {
            "id": cid,
            "title": (chat.get("title") or "").strip()[:80] or "Untitled",
            "session_id": chat.get("session_id"),
            # In-flight Temporal workflow id for this chat (web runs only). Set while a turn is
            # running so a page reload / chat switch can reattach to the still-running workflow;
            # cleared when the turn finishes. Unattached chats and unattended runs leave it null.
            "run_id": chat.get("run_id"),
            # The LAST non-null run_id this chat ever had — unlike run_id, never cleared. A
            # finished interactive web-* run has no server-side chat_key (the browser owns the
            # conversation), so once run_id resets to null at completion there was previously NO
            # way left to trace that workflow back to its chat — the Swarm board's Chat link
            # depends on find_by_run_origin() finding this after the fact (user-reported: some
            # finished board cards had no Chat button at all).
            "origin_run_id": chat.get("run_id") or kept("origin_run_id"),
            # Repo-mode git identity (issue #57 follow-up): `repo` is the registered repo this
            # chat's task targets; `git_run_id` is the ORIGINAL run's workflow id, whose workspace
            # path/branch name a later follow-up re-provisions on resume. Preserved across upserts
            # like `labels` — a chat turn that isn't repo-mode (or a direct-path resume, which
            # doesn't send them) must not clobber what an earlier turn established.
            "repo": kept("repo"),
            "git_run_id": kept("git_run_id"),
            # Labels (e.g. ["scheduled-job"]) tag a chat's provenance; preserved across upserts
            # unless the caller passes a new list.
            "labels": json.dumps(chat.get("labels") if chat.get("labels") is not None
                                 else (_decode(prior["labels"]) if prior else None) or []),
            # Sidebar pin — preserved across upserts unless the caller passes it explicitly.
            "pinned": int(bool(chat.get("pinned")) if chat.get("pinned") is not None
                          else bool(prior["pinned"] if prior else False)),
            # Per-chat pipeline tallies (requests run / approvals / session cost) shown in the
            # workflow-pipeline footer. Persisted so they survive a chat switch or page reload;
            # preserved across upserts unless the caller passes a fresh object.
            "stats": json.dumps(chat["stats"]) if chat.get("stats") is not None
            else (prior["stats"] if prior else None),
            "cap": json.dumps(chat["cap"]) if chat.get("cap") is not None else None,
            "created": (prior["created"] if prior else None) or _now(),
            "updated": _now(),
            # Monotonic per-save counter: the deterministic tiebreaker for ordering and trimming
            # when several saves share one whole-second `updated` stamp.
            "seq": (conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM chats").fetchone()[0]),
        }
        cols = ", ".join(record)
        conn.execute(f"INSERT OR REPLACE INTO chats ({cols}) VALUES "
                     f"({', '.join(':' + c for c in record)})", record)

        # Messages are replaced wholesale (the caller always sends the full list, and `save`
        # rebuilding the record is exactly why set_pinned() exists as a separate path).
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (cid,))
        conn.executemany(
            "INSERT INTO messages (chat_id, seq, role, text, ts, pending) VALUES (?, ?, ?, ?, ?, ?)",
            [(cid, i, m.get("role"), m.get("text"), m.get("ts"), int(bool(m.get("pending"))))
             for i, m in enumerate(messages)])

        # Cap the store: drop everything ranked past MAX_CHATS by the sidebar's own order.
        stale = [r["id"] for r in conn.execute(
            "SELECT id FROM chats ORDER BY updated DESC, seq DESC LIMIT -1 OFFSET ?", (MAX_CHATS,))]
        if stale:
            marks = ", ".join("?" * len(stale))
            conn.execute(f"DELETE FROM messages WHERE chat_id IN ({marks})", stale)
            conn.execute(f"DELETE FROM chats WHERE id IN ({marks})", stale)
    return cid


def set_pinned(cid, pinned):
    """Flip a chat's sidebar pin in place without touching its title/messages (unlike save,
    which rebuilds the whole record). Returns the new pinned state, or None if no such chat."""
    if not cid:
        return None
    with _conn() as conn:
        cur = conn.execute("UPDATE chats SET pinned = ? WHERE id = ?",
                           (int(bool(pinned)), cid))
        return bool(pinned) if cur.rowcount else None


_PENDING = "_⏳ Otto is working on this…_"


def start_run(cid, request, title=None, labels=None, cap=None, run_id=None):
    """Open a chat thread at the START of an unattended run so it's visible in the sidebar while
    the run is still in flight (the board/schedule/event run has no browser to record itself).
    Appends the user's request plus a *pending* placeholder reply that finish_run() later fills
    in. A re-firing schedule (existing chat) appends a fresh turn the same way. `run_id` is the
    live Temporal workflow id: storing it lets reopening this chat reattach to the still-running
    workflow (to answer a clarification or approve a write); finish_run() clears it at the end."""
    if not cid:
        return None
    prior = get(cid)
    messages = list((prior or {}).get("messages") or [])
    messages.append({"role": "user", "text": str(request or ""), "ts": _now()})
    messages.append({"role": "otto", "text": _PENDING, "pending": True, "ts": _now()})
    return save({
        "id": cid,
        "title": (prior or {}).get("title") or title or str(request or "")[:80],
        "session_id": (prior or {}).get("session_id"),
        "cap": cap or (prior or {}).get("cap"),
        "labels": labels,
        "run_id": run_id,
        "messages": messages,
    })


def finish_run(cid, result, session_id=None, cap=None, repo=None, git_run_id=None):
    """Finalize the in-flight turn opened by start_run(): replace the trailing pending
    placeholder with the real result and record session_id/cap so the thread is resumable.
    Falls back to appending a reply if no placeholder is present (start_run was skipped).

    `repo`/`git_run_id` are the run's repo-mode git identity, and recording them is what makes
    the thread resumable AT ALL for a repo-mode run: a follow-up needs them to re-provision the
    torn-down clone at the SAME path, or `claude -p --resume` runs from the wrong directory, finds
    no session history and returns nothing. A browser-driven chat gets them client-side, but a
    WORKFLOW-opened chat (`chat_key` — a needs-you retry, or any unattended run) has no browser,
    so without threading them here the follow-up dead-ends with an empty reply."""
    if not cid:
        return None
    prior = get(cid)
    if not prior:
        return append_run(cid, "", result, session_id=session_id, cap=cap,
                          repo=repo, git_run_id=git_run_id)
    messages = list(prior.get("messages") or [])
    if messages and messages[-1].get("role") == "otto" and messages[-1].get("pending"):
        messages[-1] = {"role": "otto", "text": str(result or ""), "ts": _now()}
    else:
        messages.append({"role": "otto", "text": str(result or ""), "ts": _now()})
    return save({
        "id": cid,
        "title": prior.get("title"),
        "session_id": session_id,
        "cap": cap,
        "labels": prior.get("labels"),
        "repo": repo,
        "git_run_id": git_run_id,
        "messages": messages,
    })


def append_run(cid, request, result, title=None, session_id=None, cap=None, labels=None,
               repo=None, git_run_id=None):
    """Append one unattended run (a user request + Otto's result) to an existing chat,
    creating it on the first call. Used by scheduled/event runs that have no browser to
    record their own turns: the first run opens a chat keyed by `cid` (e.g. the schedule id),
    later runs append to it. `title`/`labels` are set on creation and preserved afterward."""
    if not cid:
        return None
    prior = get(cid)
    messages = list((prior or {}).get("messages") or [])
    messages.append({"role": "user", "text": str(request or ""), "ts": _now()})
    messages.append({"role": "otto", "text": str(result or ""), "ts": _now()})
    return save({
        "id": cid,
        # Keep the chat's original title once created; otherwise seed from `title`/request.
        "title": (prior or {}).get("title") or title or str(request or "")[:80],
        "session_id": session_id,
        "cap": cap,
        "labels": labels,
        "repo": repo,
        "git_run_id": git_run_id,
        "messages": messages,
    })


def git_identity(session_id):
    """The repo-mode git identity (`{repo, git_run_id}`) stored on the chat bound to `session_id`,
    or `{}`. The SERVER-side backstop for a resume: the browser normally sends these on
    /api/continue out of its in-memory session, but that copy is populated when the chat is opened
    and goes stale (a tab held open across a chat-row change sends `repo: undefined` forever), and
    a client that never had them cannot invent them. Missing them silently makes the follow-up
    un-resumable — no workspace is provisioned, so `claude -p --resume` runs from the wrong cwd and
    returns `(no output)` — so the store, not the client, is the authority.

    Ambiguity resolves to `{}`: a session id is bound to one chat in practice, but two rows
    claiming it means we cannot tell which clone to rebuild, and guessing risks pointing a
    follow-up at another task's workspace."""
    if not session_id:
        return {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT repo, git_run_id FROM chats WHERE session_id = ?", (session_id,)).fetchall()
    if len(rows) != 1 or not rows[0]["repo"]:
        return {}
    return {"repo": rows[0]["repo"], "git_run_id": rows[0]["git_run_id"]}


def find_by_run_origin(wid):
    """The chat id whose `origin_run_id` matches this Temporal workflow id, or None. The board's
    Chat link is normally driven by the workflow's own recorded `chat_key`, but an interactive
    web-* run and a needs-you retry's originating run never had one — this is the sticky,
    never-cleared backstop that lets a chat be found by the run it belongs to even after that
    run finished (unlike `run_id`, which finish_run()/clearRun() null out on purpose to stop the
    sidebar spinner). Ambiguity resolves to None, same as find_reattach/git_identity."""
    if not wid:
        return None
    with _conn() as conn:
        rows = conn.execute("SELECT id FROM chats WHERE origin_run_id = ?", (wid,)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def find_reattach(request):
    """Best-effort: the interactive chat a board-retry should append its result to, so a retry
    isn't board-only (an interactive run records CLIENT-side, so retrying from the board lost the
    result from the conversation — issue: 'the chat didn't get the final result'). Matches a chat
    whose OPENING user message equals `request` and whose LATEST reply is still an unresolved
    outcome (a needs-human banner or a pending placeholder). Returns an id only on an UNAMBIGUOUS
    single match — anything else falls back to a fresh thread rather than risk appending to the
    wrong conversation."""
    req = (request or "").strip()
    if not req:
        return None
    with _conn() as conn:
        # Only the three messages the predicate needs, per chat — not the transcripts.
        rows = conn.execute("""
            SELECT c.id,
              (SELECT m.text FROM messages m WHERE m.chat_id = c.id AND m.role = 'user'
                 ORDER BY m.seq LIMIT 1)                                  AS first_user,
              (SELECT m.role    FROM messages m WHERE m.chat_id = c.id ORDER BY m.seq DESC LIMIT 1) AS last_role,
              (SELECT m.text    FROM messages m WHERE m.chat_id = c.id ORDER BY m.seq DESC LIMIT 1) AS last_text,
              (SELECT m.pending FROM messages m WHERE m.chat_id = c.id ORDER BY m.seq DESC LIMIT 1) AS last_pending
            FROM chats c
        """).fetchall()
    hits = []
    for r in rows:
        if r["first_user"] is None or (r["first_user"] or "").strip() != req:
            continue
        awaiting = r["last_role"] == "otto" and (
            bool(r["last_pending"])
            or str(r["last_text"] or "").startswith("⚠️ **Needs human review**"))
        if awaiting:
            hits.append(r["id"])
    return hits[0] if len(hits) == 1 else None


def delete(cid):
    with _conn() as conn, storage.tx(conn):
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (cid,))
        conn.execute("DELETE FROM chats WHERE id = ?", (cid,))
