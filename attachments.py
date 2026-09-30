"""Chat attachments (#161) — files and images a user hands a web-chat run.

The browser uploads each file on its own (`POST /api/attachments`, raw bytes) and gets back an
id; a submit/continue then names ids, never paths. The server resolves them here, so the only
paths a run is ever told about are ones Otto wrote itself.

Stored as `data/uploads/<id>/<name>`, swept on a TTL. The directory is resolved at CALL time from
`config.DATA_DIR`, like `file_safety`'s globs, so the test suite's redirect covers it for free.
"""
import base64
import mimetypes
import os
import re
import secrets
import shutil
import time

import config

_ID = re.compile(r"^att-[0-9a-f]{16}$")
# Served inline by `GET /api/attachments/<id>`. SVG is deliberately absent: it is a document
# that can carry script, and this origin is the unauthenticated API's.
INLINE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def upload_dir():
    return os.path.join(config.DATA_DIR, "uploads")


def valid_id(aid):
    return isinstance(aid, str) and bool(_ID.match(aid))


def safe_name(name):
    """A filename that cannot leave its directory or smuggle a control character into a prompt."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"[^\w.\- ()]+", "_", base).strip(" .")[:120]
    return base or "file"


def ctype_of(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _meta(aid, path):
    name = os.path.basename(path)
    return {"id": aid, "name": name, "type": ctype_of(name),
            "size": os.path.getsize(path), "path": path}


def store(name, data):
    """Write one upload; returns its meta. Raises ValueError on an empty or oversized body."""
    if not data:
        raise ValueError("empty file")
    limit = int(config.setting("attachment_max_mb")) * 1024 * 1024
    if len(data) > limit:
        raise ValueError(f"file exceeds {config.setting('attachment_max_mb')} MB")
    sweep()
    aid = "att-" + secrets.token_hex(8)
    d = os.path.join(upload_dir(), aid)
    os.makedirs(d, mode=0o700)
    path = os.path.join(d, safe_name(name))
    with open(path, "wb") as f:
        f.write(data)
    return _meta(aid, path)


def get(aid):
    """Meta for one stored upload, or None (bad id, expired, never existed)."""
    if not valid_id(aid):
        return None
    d = os.path.join(upload_dir(), aid)
    try:
        files = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]
    except OSError:
        return None
    return _meta(aid, os.path.join(d, files[0])) if len(files) == 1 else None


def resolve(ids):
    """Trusted metas for client-named ids, in order. Returns (metas, missing_ids)."""
    metas, missing = [], []
    for aid in dict.fromkeys(ids or []):
        m = get(aid)
        (metas.append(m) if m else missing.append(aid))
    return metas, missing


def public(meta):
    """What the browser gets back: never the on-disk path."""
    return {k: meta[k] for k in ("id", "name", "type", "size")}


def sweep(ttl_h=None):
    """Drop uploads older than the TTL. Never raises."""
    ttl_h = config.ATTACHMENT_TTL_H if ttl_h is None else ttl_h
    cutoff = time.time() - ttl_h * 3600
    try:
        entries = os.listdir(upload_dir())
    except OSError:
        return
    for aid in entries:
        d = os.path.join(upload_dir(), aid)
        try:
            if valid_id(aid) and os.path.getmtime(d) < cutoff:
                shutil.rmtree(d)
        except OSError:
            pass


def granted_dirs(atts):
    """The upload directories a run carrying `atts` may read (file_safety.upload_grant)."""
    return tuple(os.path.dirname(a["path"]) for a in atts or [] if a.get("path"))


def note(atts):
    """The in-context note telling a run what the user attached. None when nothing was.

    Claude's Read tool shows it an image; the local runtime's and Codex's do not. The note is the
    same on every backend because a wall can re-dispatch a run to another one after it is built,
    so it states the rule both need: an image you did not SEE is never described."""
    if not atts:
        return None
    lines = [f"- {a['name']} ({a['type']}, {_human(a['size'])}): {a['path']}" for a in atts]
    return ("The user attached these files to their message. They are part of the request: "
            "read the ones it concerns with your file tools before answering.\n"
            + "\n".join(lines)
            + "\nIf your tools cannot display an attached image, say it could not be viewed and "
              "work from the rest of the request; never describe what an image you did not see "
              "shows.")


def image_label(meta):
    """What an inlined image leaves behind in anything persisted: never its bytes (#162)."""
    return f"[image: {meta['name']}, {meta['type']}, {meta['size']} bytes]"


def image_parts(atts):
    """`(part, label)` per attached INLINE_IMAGES file, as an OpenAI `image_url` data URI part.
    An unreadable file is skipped; the note still names it."""
    out = []
    for a in atts or []:
        if a.get("type") not in INLINE_IMAGES or not a.get("path"):
            continue
        try:
            with open(a["path"], "rb") as f:
                data = base64.b64encode(f.read()).decode()
        except OSError:
            continue
        out.append(({"type": "image_url", "image_url": {"url": f"data:{a['type']};base64,{data}"}},
                    image_label(a)))
    return out


def judge_note(atts):
    """The judge has no tools and never sees the files, so it must not fail a result for
    describing them."""
    if not atts:
        return ""
    names = ", ".join(a["name"] for a in atts)
    return ("\n\nThe user attached files to this request ({}). The capability could read them; "
            "you cannot. Do not fail the result for stating what they contain, or for a claim "
            "you could only check by opening them.").format(names)


def _human(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
