"""Web-chat attachments: the at-rest store, per-attempt staging copies, and the TTL sweeps.

`data/uploads/` is read- and write-denied to every run (file_safety), so a run only ever sees a
copy staged under `data/run-files/` for the lifetime of ONE attempt. No Temporal import: the
server (upload, retrieve, validate) and the worker (stage, unstage) both use this module.
"""
import os
import re
import shutil
import time
import uuid

import config
import storage

UPLOADS_DIR = os.path.join(config.DATA_DIR, "uploads")
RUN_FILES_DIR = os.path.join(config.DATA_DIR, "run-files")

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u200b]")
_NAME_MAX = 120
_META = "meta.json"

# Served inline by `GET /api/uploads/<id>`; everything else (SVG included) is a download.
INLINE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def ensure_dirs():
    """Create both stores — `file_safety.read_deny_mounts` only masks a directory that exists."""
    for root in (UPLOADS_DIR, RUN_FILES_DIR):
        os.makedirs(root, exist_ok=True)


def is_valid_id(uid):
    """The id is a path segment: validated before any join, never sanitised after."""
    return isinstance(uid, str) and bool(_ID_RE.match(uid))


def safe_name(name):
    """A display- and disk-safe basename for an untrusted client filename."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = _CONTROL_RE.sub("", base).lstrip(". ")
    base = re.sub(r"[ /:*?\"<>|]+", "_", base).strip()[:_NAME_MAX] or "attachment"
    # `meta.json` and its `.lock` are the sidecar's own names in the same directory.
    return base[:4] + "_" + base[4:] if base.lower().startswith(_META) else base


def safe_mime(mime):
    """The declared type is echoed as a response header, so anything but a plain token goes."""
    m = str(mime or "").strip().lower()
    return m if _MIME_RE.match(m) else "application/octet-stream"


def store(name, mime, data):
    """Save one upload; returns its public metadata {id, name, size, mime}."""
    uid = uuid.uuid4().hex
    directory = os.path.join(UPLOADS_DIR, uid)
    os.makedirs(directory, exist_ok=True)
    safe = safe_name(name)
    with open(os.path.join(directory, safe), "wb") as fh:
        fh.write(data)
    meta = {"id": uid, "name": safe, "size": len(data), "mime": safe_mime(mime),
            "stored": time.time()}
    storage.write_json(os.path.join(directory, _META), meta)
    gc()
    return public(meta)


def public(meta):
    return {k: meta[k] for k in ("id", "name", "size", "mime")}


def _ttl_s():
    return float(config.setting("upload_ttl_h")) * 3600


def remaining_s(meta):
    """Seconds until this upload expires (never negative)."""
    return max(0.0, _ttl_s() - (time.time() - float(meta.get("stored") or 0)))


def get(uid):
    """Metadata for an unexpired upload, else None — unknown, malformed and expired look alike."""
    if not is_valid_id(uid):
        return None
    meta = storage.read_json(os.path.join(UPLOADS_DIR, uid, _META), None)
    if not isinstance(meta, dict) or meta.get("id") != uid or not meta.get("name"):
        return None
    if remaining_s(meta) <= 0 or not os.path.isfile(path_for(meta)):
        return None
    return meta


def path_for(meta):
    return os.path.join(UPLOADS_DIR, meta["id"], os.path.basename(str(meta["name"])))


def _unique(name, used):
    stem, ext = os.path.splitext(name)
    out, n = name, 1
    while out.lower() in used:
        n += 1
        out = f"{stem}-{n}{ext}"
    used.add(out.lower())
    return out


def stage(ids, wid, part):
    """Copy these uploads into a fresh directory for one attempt -> (directory, paths, missing).

    `missing` counts requested ids that could not be staged (expired, removed, unreadable), so
    the run can be told rather than silently handed fewer files. The directory name carries a
    nonce: two attempts sharing `wid` and `part` must never share, or remove, one directory."""
    ids = list(ids or [])
    if not ids:
        return None, [], 0
    gc()
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{wid}-{part}")
    directory = os.path.join(RUN_FILES_DIR, f"{label}-{uuid.uuid4().hex[:8]}")
    os.makedirs(directory)
    paths, used = [], set()
    try:
        for uid in ids:
            meta = get(uid)
            if meta is None:
                continue
            dest = os.path.join(directory, _unique(safe_name(meta["name"]), used))
            try:
                shutil.copyfile(path_for(meta), dest)
            except OSError:
                continue
            paths.append(dest)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    if not paths:
        shutil.rmtree(directory, ignore_errors=True)
        directory = None
    return directory, paths, len(ids) - len(paths)


def unstage(directory):
    """Remove one staging directory. Runs in a `finally`, so it never raises, and it refuses
    anything that is not a direct child of RUN_FILES_DIR."""
    if not directory:
        return
    target = os.path.abspath(directory)
    if os.path.dirname(target) != os.path.abspath(RUN_FILES_DIR):
        return
    shutil.rmtree(target, ignore_errors=True)


def gc():
    """Best-effort TTL sweep, run on every upload and every stage. Staged copies get the short
    `RUN_FILES_TTL_H`: one older than any attempt can run is an orphan of a killed worker."""
    _sweep(UPLOADS_DIR, _ttl_s())
    _sweep(RUN_FILES_DIR, config.RUN_FILES_TTL_H * 3600)


def _sweep(root, ttl_s):
    try:
        names = os.listdir(root)
    except OSError:
        return
    cutoff = time.time() - ttl_s
    for name in names:
        p = os.path.join(root, name)
        try:
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def note(paths, backend="claude", missing=0):
    """The attachment note for `system_context`, or None when there is nothing to say.

    Only `claude -p` can open the staged copies (measured: no refusal and no `--add-dir` from no
    cwd, a clone cwd or Otto's cwd); the local and Codex runtimes are told so instead, the way
    `mcp_client.connector_note` declares a connector it cannot reach."""
    paths = list(paths or [])
    if not paths and not missing:
        return None
    gone = (f"{missing} attached file(s) could not be staged (expired or removed): say which "
            "part of the request you could not check because of that." if missing else "")
    if backend == "claude" and not paths:
        return "ATTACHMENTS: " + gone
    if backend != "claude":
        names = ", ".join(os.path.basename(p) for p in paths)
        return (f"ATTACHMENTS UNAVAILABLE: {len(paths) + missing} file(s) came with this request"
                + (f" ({names})" if names else "") + ", and this backend cannot open them — "
                "attachments reach the Claude backend only. Say plainly in your reply that you "
                "could not see them, answer only what the text of the request allows, and never "
                "guess at or describe their contents.")
    lines = []
    for p in paths:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        lines.append(f"- {p} ({os.path.basename(p)}, {size} bytes)")
    return ("ATTACHMENTS: the user attached these files to this request, staged on disk for this "
            "attempt only:\n" + "\n".join(lines) + "\nOpen each one with the Read tool — an "
            "image comes back as an image, so describe what you actually see, never what the "
            "filename suggests. If you hand the work to a subagent, give it these paths: it "
            "cannot see this note." + ("\n" + gone if gone else ""))
