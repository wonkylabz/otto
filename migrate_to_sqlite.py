"""One-shot backfill: the legacy JSON/JSONL stores -> data/otto.db (issue #103). Run by hand:

    python3 migrate_to_sqlite.py            # migrate everything not yet migrated
    python3 migrate_to_sqlite.py audit      # just one store

Each store is INDEPENDENTLY skipped when its tables already hold rows, so re-running is safe and
a later phase can add its store here without redoing the earlier ones. The legacy files are left
untouched — no code path reads them afterwards, so they simply stop growing and stay as a frozen
forensic copy (issue #103: "keep a JSONL export for forensics").

Stores:
  audit      — data/audit.log + audit-content.log (+ .1/.2/.3 rotations) -> `audit`/`audit_content`
  chats      — data/chats.json                                       -> `chats`/`messages`
  memory     — data/memory.json + data/memory/<ns>.json              -> `memory` (namespace column)
  solutions  — data/solutions.json                                   -> `solutions`
  behaviors  — data/behaviors.json                                   -> `behaviors`
  knowledge  — data/knowledge.json                                   -> `knowledge_docs`/
                                                                        `knowledge_chunks`/
                                                                        `knowledge_settings`
"""
import glob
import json
import os
import sys

import chats
import config
import engine
import knowledge
import storage


def _rotated_paths(path, keep=3):
    """The old rotated segments, OLDEST first (path.3 … path.1, then the live file) — the order
    engine.iter_audit_entries() used to guarantee, so insertion order stays chronological."""
    paths = [f"{path}.{i}" for i in range(keep, 0, -1)] + [path]
    return [p for p in paths if os.path.exists(p)]


def _iter_jsonl(paths):
    for p in paths:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        print(f"  skipping unparseable line in {p}", file=sys.stderr)


def migrate_audit():
    conn = engine._audit_conn()
    try:
        if conn.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"]:
            return "skipped (already has rows)"
        n_audit = n_content = 0
        with storage.tx(conn):
            for e in _iter_jsonl(_rotated_paths(os.path.join(config.DATA_DIR, "audit.log"))):
                verified = e.get("verified")
                conn.execute("INSERT INTO audit (at, workflow, capability, verified, data) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (e["at"], e["workflow"], e.get("capability"),
                              None if verified is None else int(bool(verified)), json.dumps(e)))
                n_audit += 1
            for e in _iter_jsonl(_rotated_paths(
                    os.path.join(config.DATA_DIR, "audit-content.log"))):
                conn.execute("INSERT INTO audit_content (at, workflow, attempt, data) "
                             "VALUES (?, ?, ?, ?)",
                             (e["at"], e["workflow"], e.get("attempt"), json.dumps(e)))
                n_content += 1
    finally:
        conn.close()
    return f"{n_audit} audit rows + {n_content} audit-content rows"


def migrate_chats():
    path = os.path.join(config.DATA_DIR, "chats.json")
    legacy = storage.read_json(path, [])
    with chats._conn() as conn:
        if conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]:
            return "skipped (already has rows)"
        n_chats = n_msgs = 0
        with storage.tx(conn):
            # Oldest first, so `seq` (the ordering tiebreaker) lines up with real chronology.
            for i, c in enumerate(sorted(legacy, key=lambda c: c.get("updated") or "")):
                conn.execute("""INSERT INTO chats
                    (id, seq, title, created, updated, session_id, run_id, repo, git_run_id,
                     pinned, cap, labels, stats)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (c["id"], i + 1, c.get("title"), c.get("created"), c.get("updated"),
                              c.get("session_id"), c.get("run_id"), c.get("repo"),
                              c.get("git_run_id"), int(bool(c.get("pinned"))),
                              json.dumps(c["cap"]) if c.get("cap") is not None else None,
                              json.dumps(c.get("labels") or []),
                              json.dumps(c["stats"]) if c.get("stats") is not None else None))
                n_chats += 1
                for seq, m in enumerate(c.get("messages") or []):
                    conn.execute("INSERT INTO messages (chat_id, seq, role, text, ts, pending) "
                                 "VALUES (?, ?, ?, ?, ?, ?)",
                                 (c["id"], seq, m.get("role"), m.get("text"), m.get("ts"),
                                  int(bool(m.get("pending")))))
                    n_msgs += 1
    return f"{n_chats} chats + {n_msgs} messages"


def migrate_memory():
    """Global memory.json plus every data/memory/<ns>.json into one table. The namespace column is
    NULL for global rows and the filename stem for project rows — the filename WAS the namespace
    (engine._memory_ns / registry.project_namespace produce the same slug)."""
    sources = [(None, os.path.join(config.DATA_DIR, "memory.json"))]
    for p in sorted(glob.glob(os.path.join(config.DATA_DIR, "memory", "*.json"))):
        sources.append((os.path.basename(p)[:-len(".json")], p))
    with engine._conn() as conn:
        if conn.execute("SELECT COUNT(*) AS n FROM memory").fetchone()["n"]:
            return "skipped (already has rows)"
        n, spaces = 0, []
        with storage.tx(conn):
            # Global first, then each namespace — so `id` order matches the read order
            # recent_facts() relies on (global facts before project facts).
            for namespace, path in sources:
                events = storage.read_json(path, [])
                if events:
                    spaces.append(f"{namespace or 'global'}:{len(events)}")
                for e in events:
                    conn.execute(
                        "INSERT INTO memory (namespace, at, capability, data) VALUES (?, ?, ?, ?)",
                        (namespace, e.get("at"), e.get("capability"), json.dumps(e)))
                    n += 1
    return f"{n} events ({', '.join(spaces) or 'none'})"


def migrate_solutions():
    rows = storage.read_json(os.path.join(config.DATA_DIR, "solutions.json"), [])
    with engine._conn() as conn:
        if conn.execute("SELECT COUNT(*) AS n FROM solutions").fetchone()["n"]:
            return "skipped (already has rows)"
        with storage.tx(conn):
            for i, s in enumerate(rows):
                conn.execute("INSERT INTO solutions (id, seq, at, capability, request, approach) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (s.get("id") or f"legacy{i:08d}", i + 1, s.get("at"),
                              s.get("capability"), s.get("request"), s.get("approach")))
    return f"{len(rows)} approaches"


def migrate_behaviors():
    rows = storage.read_json(os.path.join(config.DATA_DIR, "behaviors.json"), [])
    with engine._conn() as conn:
        if conn.execute("SELECT COUNT(*) AS n FROM behaviors").fetchone()["n"]:
            return "skipped (already has rows)"
        with storage.tx(conn):
            for i, b in enumerate(rows):
                conn.execute("INSERT INTO behaviors (id, seq, at, scope, rule) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (b.get("id") or f"legacy{i:08d}", i + 1, b.get("at"),
                              b.get("scope") or "global", b.get("rule")))
    return f"{len(rows)} rules"


def migrate_knowledge():
    """Docs + chunks, with each chunk's embedding re-packed from JSON floats into the float32 BLOB
    the new store uses (see knowledge._VEC for why float32 is safe here)."""
    raw = storage.read_json(os.path.join(config.DATA_DIR, "knowledge.json"), None) or {}
    docs = raw.get("docs") or []
    cfg = raw.get("settings") or {}
    with knowledge._conn() as conn:
        if conn.execute("SELECT COUNT(*) AS n FROM knowledge_docs").fetchone()["n"]:
            return "skipped (already has rows)"
        n_chunks = n_vecs = 0
        with storage.tx(conn):
            if cfg.get("threshold") is not None:
                conn.execute("INSERT OR REPLACE INTO knowledge_settings (key, value) "
                             "VALUES ('threshold', ?)", (repr(float(cfg["threshold"])),))
            if cfg.get("embed_model"):
                conn.execute("INSERT OR REPLACE INTO knowledge_settings (key, value) "
                             "VALUES ('embed_model', ?)", (cfg["embed_model"],))
            for i, d in enumerate(docs):
                conn.execute("INSERT INTO knowledge_docs (id, seq, title, source, at) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (d.get("id") or f"legacy{i:08d}", i + 1, d.get("title"),
                              d.get("source"), d.get("at")))
                for seq, c in enumerate(d.get("chunks") or []):
                    vec = c.get("embedding")
                    conn.execute("INSERT INTO knowledge_chunks (doc_id, seq, text, embedding) "
                                 "VALUES (?, ?, ?, ?)",
                                 (d.get("id") or f"legacy{i:08d}", seq, c.get("text"),
                                  knowledge._pack(vec)))
                    n_chunks += 1
                    n_vecs += 1 if vec else 0
    return f"{len(docs)} docs, {n_chunks} chunks ({n_vecs} embedded)"


STORES = {"audit": migrate_audit, "chats": migrate_chats, "memory": migrate_memory,
          "solutions": migrate_solutions, "behaviors": migrate_behaviors,
          "knowledge": migrate_knowledge}


def main(argv):
    wanted = argv or list(STORES)
    unknown = [s for s in wanted if s not in STORES]
    if unknown:
        print(f"unknown store(s): {', '.join(unknown)} — known: {', '.join(STORES)}",
              file=sys.stderr)
        return 1
    for name in wanted:
        print(f"{name}: {STORES[name]()}")
    print(f"\n-> {config.DB_PATH}")
    print("legacy files left in place, untouched, as a forensic copy — safe to archive or "
          "delete manually once you've spot-checked the migration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
