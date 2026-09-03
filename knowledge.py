"""LAYER 6b - Knowledge base (issue #67): imported reference docs, RAG-retrieved into runs.

A THIRD store beside memory (`data/memory.json`, what Otto *learned*) and audit
(`data/otto.db`'s `audit` table, what *happened*): knowledge is what we told it *up front* — org runbooks,
naming conventions, "how we do X" notes that live in no single repo. On a fresh run the most
relevant chunks are retrieved and injected as system context (the same `--append-system-prompt`
seam as facts), grounding capabilities without a per-run paste.

Embeddings are OPTIONAL. When an embedding model is configured (Admin -> Knowledge) chunks are
ranked by cosine similarity; otherwise retrieval degrades to keyword overlap, so the KB is useful
out of the box and the test suite stays install-free. Cosine is pure-Python (no numpy/FAISS
required); FAISS would only be a scale optimisation, not a dependency. Execution stays Claude-only
— embeddings go through `gateway.embed` to a LOCAL endpoint, never Claude.
"""
import array
import contextlib
import datetime
import math
import re
import uuid

import config
import gateway
import storage
from ui import trace

# Stored in the shared SQLite db (issue #103): `knowledge_docs` + `knowledge_chunks` +
# `knowledge_settings`. Tests monkeypatch THIS alias to a temp file.
_DB = config.DB_PATH
_MAX_INJECT_CHARS = 1500     # hard cap on injected knowledge so a big KB can't blow the prompt
_CHUNK_CHARS = 800           # target chunk size
_CHUNK_OVERLAP = 120         # carry-over between adjacent chunks so a fact split across a boundary survives
_DEFAULT_THRESHOLD = 0.18    # min normalized similarity (0..1) for a chunk to be injected
_TOP_K = 3
# Deliberately NOT "authoritative background" (the wording until 2026-07-30): a loaded doc is a
# snapshot of its subject on the day it was written, and granting it authority is how a 2026-06
# harvested Q&A produced a confident "there is no production vLLM deployment". The run still gets
# the content — it just doesn't get a licence to skip checking live state.
_KB_HEADER = ("Reference material the user has loaded (background — use if relevant, ignore if "
              "not). Written at a point in time and possibly stale: verify any claim about the "
              "CURRENT state of a system against the live source with your tools, and let that "
              "override this text:")
# Embeddings are stored as a packed float32 BLOB (stdlib `array`), not JSON numbers. Measured on a
# real 35-chunk / 4096-dim store: 3.4x smaller than the JSON text, and float32 changed the
# retrieval RANKING on 0/10 queries with a max cosine delta of 9.2e-10 — nine orders of magnitude
# below the 0.18 threshold, so the precision loss cannot move a chunk across it.
_VEC = "f"


# --- store ---------------------------------------------------------------------------------

def _schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_docs (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        title TEXT,
        source TEXT,
        at TEXT
    )""")
    # `seq` is the chunk's position in its doc — load-bearing: policy.export_profile rebuilds a
    # doc's text by joining chunks in this order, so an unordered read would scramble it.
    conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_chunks (
        doc_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        text TEXT,
        embedding BLOB,
        PRIMARY KEY (doc_id, seq)
    )""")
    conn.execute("CREATE TABLE IF NOT EXISTS knowledge_settings (key TEXT PRIMARY KEY, value TEXT)")


@contextlib.contextmanager
def _conn():
    conn = storage.sqlite_connect(_DB)
    try:
        _schema(conn)
        yield conn
    finally:
        conn.close()


def _pack(vec):
    return array.array(_VEC, [float(x) for x in vec]).tobytes() if vec else None


def _unpack(blob):
    return list(array.array(_VEC, blob)) if blob else None


def _settings(conn):
    stored = {r["key"]: r["value"] for r in conn.execute(
        "SELECT key, value FROM knowledge_settings")}
    try:
        threshold = float(stored["threshold"])
    except (KeyError, TypeError, ValueError):
        threshold = _DEFAULT_THRESHOLD
    return {"threshold": threshold, "embed_model": stored.get("embed_model") or None}


def settings():
    with _conn() as conn:
        return _settings(conn)


def set_settings(threshold=None, embed_model=None):
    """Update the retrieval threshold and/or the embedding model. embed_model="" clears it
    (back to keyword matching)."""
    with _conn() as conn, storage.tx(conn):
        if threshold is not None:
            try:
                clamped = max(0.0, min(1.0, float(threshold)))
            except (TypeError, ValueError):
                pass
            else:
                conn.execute("INSERT OR REPLACE INTO knowledge_settings (key, value) "
                             "VALUES ('threshold', ?)", (repr(clamped),))
        if embed_model is not None:
            conn.execute("INSERT OR REPLACE INTO knowledge_settings (key, value) "
                         "VALUES ('embed_model', ?)", (embed_model or None,))
        return _settings(conn)


# --- chunking + similarity ------------------------------------------------------------------

def _chunk(text):
    """Split text into ~_CHUNK_CHARS windows on paragraph boundaries, with a small overlap so a
    fact spanning a boundary isn't lost. Pure + deterministic (unit-tested)."""
    text = (text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > _CHUNK_CHARS:
            chunks.append(buf)
            tail = buf[-_CHUNK_OVERLAP:] if len(buf) > _CHUNK_OVERLAP else buf
            buf = (tail + "\n\n" + p).strip()
        else:
            buf = (buf + "\n\n" + p).strip() if buf else p
        # a single huge paragraph: hard-split it
        while len(buf) > _CHUNK_CHARS:
            chunks.append(buf[:_CHUNK_CHARS])
            buf = buf[_CHUNK_CHARS - _CHUNK_OVERLAP:]
    if buf:
        chunks.append(buf)
    return chunks


def _keywords(text):
    """Significant tokens (>3 chars, URLs stripped) — mirrors registry.Capability.score /
    engine._keywords so keyword fallback ranks like routing does."""
    text = re.sub(r"https?://\S+", " ", (text or "").lower())
    return {w for w in re.findall(r"[a-z0-9]+", text) if len(w) > 3}


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _keyword_sim(query_kw, text):
    """Normalized keyword overlap in 0..1 (shared / query terms) so a single threshold works for
    both the embedding and the fallback path."""
    if not query_kw:
        return 0.0
    return len(query_kw & _keywords(text)) / len(query_kw)


# --- ingest --------------------------------------------------------------------------------

def add_document(title, text, source="paste"):
    """Chunk + (optionally) embed a document and persist it. Returns a summary dict. Embedding
    uses the configured model via gateway.embed; if none/fails, chunks store embedding=None and
    retrieval falls back to keyword overlap."""
    title = " ".join((title or "").split()).strip() or "untitled"
    chunks = _chunk(text)
    if not chunks:
        return None
    # embed OUTSIDE the write transaction — it's a network call and must not hold the write lock
    vecs = gateway.embed(chunks, settings().get("embed_model"))
    doc = {
        "id": uuid.uuid4().hex[:12],
        "title": title[:160],
        "source": source,
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "chunks": [{"text": c, "embedding": (vecs[i] if vecs and i < len(vecs) else None)}
                   for i, c in enumerate(chunks)],
    }
    with _conn() as conn, storage.tx(conn):
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM knowledge_docs").fetchone()[0]
        conn.execute("INSERT INTO knowledge_docs (id, seq, title, source, at) VALUES (?,?,?,?,?)",
                     (doc["id"], seq, doc["title"], doc["source"], doc["at"]))
        conn.executemany(
            "INSERT INTO knowledge_chunks (doc_id, seq, text, embedding) VALUES (?,?,?,?)",
            [(doc["id"], i, c["text"], _pack(c["embedding"]))
             for i, c in enumerate(doc["chunks"])])
    embedded = sum(1 for c in doc["chunks"] if c["embedding"])
    trace("KNOWLEDGE", f"added '{doc['title']}' — {len(chunks)} chunk(s), {embedded} embedded")
    return _summary(doc)


def _summary(doc):
    return {"id": doc["id"], "title": doc["title"], "source": doc.get("source", ""),
            "at": doc.get("at", ""), "chunks": len(doc.get("chunks", [])),
            "embedded": sum(1 for c in doc.get("chunks", []) if c.get("embedding"))}


def reembed_all(model=None):
    """Recompute embeddings for every stored chunk using the configured (or given) embedding
    model. Embeddings are otherwise only computed at add-time (`add_document`), so a doc added
    while the model was misconfigured or unreachable stays keyword-only forever — this is how it
    gets vectors after the config is fixed, WITHOUT re-pasting. Embeds per-doc OUTSIDE the store
    lock (network calls), then applies inside it (matched by doc id, so a concurrent add/delete is
    tolerated). Returns {docs, chunks, embedded, model} — embedded<chunks means the model is still
    unreachable for some/all docs. No-op (embedded=0) when no embedding model is set."""
    model = model or settings().get("embed_model")
    # Only ids + chunk TEXT here — no vectors, since every one is about to be replaced.
    with _conn() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM knowledge_docs ORDER BY seq")]
        texts_by_doc = {
            i: [r["text"] for r in conn.execute(
                "SELECT text FROM knowledge_chunks WHERE doc_id = ? ORDER BY seq", (i,))]
            for i in ids}
    total = sum(len(t) for t in texts_by_doc.values())
    if not model:
        return {"docs": len(ids), "chunks": total, "embedded": 0, "model": None}
    vecs_by_doc, embedded = {}, 0
    for doc_id in ids:
        texts = texts_by_doc[doc_id]
        vecs = gateway.embed(texts, model) if texts else []
        if vecs:
            vecs_by_doc[doc_id] = vecs
            embedded += len(vecs)
    # Apply inside the write transaction, matched by doc id + chunk seq, so a concurrent
    # add/delete is tolerated (a doc that vanished simply updates no rows).
    with _conn() as conn, storage.tx(conn):
        for doc_id, vecs in vecs_by_doc.items():
            conn.executemany(
                "UPDATE knowledge_chunks SET embedding = ? WHERE doc_id = ? AND seq = ?",
                [(_pack(v), doc_id, i) for i, v in enumerate(vecs)])
    trace("KNOWLEDGE", f"re-embedded {embedded}/{total} chunk(s) via {model}")
    return {"docs": len(ids), "chunks": total, "embedded": embedded, "model": model}


def documents():
    """Doc summaries (no chunk text / vectors) for the Admin UI, newest first. Counts come from
    SQL aggregates, so this never materializes a chunk body or a vector."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT d.id, d.title, d.source, d.at,
                   (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.doc_id = d.id) AS chunks,
                   (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.doc_id = d.id
                      AND c.embedding IS NOT NULL) AS embedded
            FROM knowledge_docs d ORDER BY d.seq DESC
        """).fetchall()
    return [{"id": r["id"], "title": r["title"], "source": r["source"] or "",
             "at": r["at"] or "", "chunks": r["chunks"], "embedded": r["embedded"]} for r in rows]


def export_docs():
    """Every doc as {title, source, text} with its chunks re-joined in order — the portable-profile
    shape (policy.export_profile). Embeddings are deliberately excluded: the importing machine
    re-computes them against its own local model."""
    with _conn() as conn:
        rows = conn.execute("SELECT id, title, source FROM knowledge_docs ORDER BY seq").fetchall()
        return [{"title": r["title"], "source": r["source"] or "",
                 "text": "\n\n".join(
                     c["text"] or "" for c in conn.execute(
                         "SELECT text FROM knowledge_chunks WHERE doc_id = ? ORDER BY seq",
                         (r["id"],)))}
                for r in rows]


def delete_document(doc_id):
    with _conn() as conn, storage.tx(conn):
        conn.execute("DELETE FROM knowledge_chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM knowledge_docs WHERE id = ?", (doc_id,))


def clear():
    """Drop every doc. Settings (threshold / embed_model) deliberately survive, as before."""
    with _conn() as conn, storage.tx(conn):
        conn.execute("DELETE FROM knowledge_chunks")
        conn.execute("DELETE FROM knowledge_docs")


# --- retrieve + inject ----------------------------------------------------------------------

def recall_knowledge(request, k=_TOP_K, threshold=None):
    """Top knowledge chunks most relevant to `request`, above the similarity threshold. Returns
    [{title, text, score}]. Cosine when the query embeds AND chunks have vectors; otherwise
    normalized keyword overlap. Returns [] when nothing clears the threshold — so an unrelated
    request injects nothing."""
    if not (request or "").strip():
        return []
    with _conn() as conn:
        cfg = _settings(conn)
        # One pass over the chunks joined to their doc title, in doc-then-chunk order (the same
        # order the flattened `docs` list produced).
        flat = conn.execute("""
            SELECT d.title, c.text, c.embedding
            FROM knowledge_chunks c JOIN knowledge_docs d ON d.id = c.doc_id
            ORDER BY d.seq, c.seq
        """).fetchall()
        # Store-WIDE check (not per-doc): one embedded chunk anywhere means we embed the query.
        have_vectors = conn.execute(
            "SELECT 1 FROM knowledge_chunks WHERE embedding IS NOT NULL LIMIT 1").fetchone()
    if not flat:
        return []
    thr = cfg.get("threshold", _DEFAULT_THRESHOLD) if threshold is None else threshold

    qvec = None
    if have_vectors:
        q = gateway.embed([request], cfg.get("embed_model"))
        qvec = q[0] if q else None

    query_kw = _keywords(request)
    scored = []
    for row in flat:
        title, text = row["title"] or "", row["text"] or ""
        vec = _unpack(row["embedding"])
        if qvec is not None and vec:
            score = _cosine(qvec, vec)
        else:
            score = _keyword_sim(query_kw, text)   # fallback (or per-chunk gap)
        if score >= thr:
            scored.append((score, title, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"title": t, "text": txt, "score": round(s, 3)} for s, t, txt in scored[:k]]


def context_block(request):
    """The Reference-knowledge block to inject for a fresh run, or None. Char-bounded so a large
    KB can't blow the prompt."""
    hits = recall_knowledge(request)
    if not hits:
        return None
    out, used = [], 0
    for h in hits:
        snippet = h["text"].strip()
        if used + len(snippet) > _MAX_INJECT_CHARS:
            snippet = snippet[: max(0, _MAX_INJECT_CHARS - used)].rstrip()
        if not snippet:
            break
        out.append(f"[{h['title']}]\n{snippet}")
        used += len(snippet)
        if used >= _MAX_INJECT_CHARS:
            break
    trace("KNOWLEDGE", f"injecting {len(out)} reference snippet(s) ({used} chars)")
    return _KB_HEADER + "\n" + "\n\n".join(out)
