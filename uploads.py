"""Web-chat attachments: the at-rest store, the per-run staging copy, and the TTL sweep.

The composer used to send text only, so a screenshot had to be pasted inline (lossy) or saved
somewhere a run could read and named by path. This is the storage half of fixing that; the
transport half is `server._post_uploads`, and the "tell the run about it" half is
`engine.run_attempt`.

**Why the design is "deny at rest, copy per run" rather than "allow this run's own files".**
`file_safety` expresses read denial as path globs, and a matching deny beats any allow
(`file_safety` docstring, established by probe). So there is no way to say "run A may read
`data/uploads/<id>` but run B may not": a per-workflow allow cannot be carved back out of a
deny. What CAN be expressed is a blanket deny plus a location that is not denied — hence two
directories with different rules:

- `data/uploads/` — the at-rest store. Read- AND write-denied to every run, Otto-cwd runs
  included (it is in `file_safety._secret_store_globs`, the tier that survives the
  Otto-introspection exemption). Nothing a run does can reach an attachment sitting here.
- `data/run-files/<wid>-a<attempt>/` — this attempt's staged copies, created just before the
  spawn and deleted in a `finally` after it. NOT read-denied: that is the accepted tradeoff,
  "exposed only for the attempt's lifetime". It IS write-denied, so a run cannot plant a file
  here for a later run to read as someone else's attachment.

Full per-run isolation is not achievable with path-glob deny rules, and pretending otherwise
would be a claim this module cannot enforce. What is enforced: nothing at rest is readable,
and a staging directory does not outlive the attempt that made it (barring a worker killed
mid-attempt, which the TTL sweep backstops).

No Temporal import: the server (upload, retrieve, validate) and the worker (stage, unstage)
both use this, and the server runs without Temporal.
"""
import os
import re
import shutil
import time
import uuid

import config
import storage

# The two stores. Module-level constants derived from config.DATA_DIR so `test_support`'s
# redirect table re-points them for the suite — nothing here may write the live data/.
UPLOADS_DIR = os.path.join(config.DATA_DIR, "uploads")
RUN_FILES_DIR = os.path.join(config.DATA_DIR, "run-files")

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_NAME_MAX = 120                       # a 4 KB filename is a filesystem hazard, not a feature
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Served inline by `GET /api/uploads/<id>`. Everything else — SVG included, since inline SVG
# is a script-injection vector in a same-origin document — goes out as an attachment.
INLINE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def _upload_dir(uid):
    return os.path.join(UPLOADS_DIR, uid)


def ensure_dirs():
    """Create both stores. Called on upload, on stage, and at server/worker startup.

    Eager rather than lazy-for-the-caller because `file_safety.read_deny_mounts` masks a `/**`
    deny with a tmpfs only when the directory EXISTS on the host — a store that has never been
    created has no bwrap mount, and the first upload of a fresh install would be the thing
    without one."""
    for root in (UPLOADS_DIR, RUN_FILES_DIR):
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass

def _meta_path(uid):
    return os.path.join(_upload_dir(uid), "meta.json")


def new_id():
    return uuid.uuid4().hex


def is_valid_id(uid):
    """Is `uid` one of OUR ids? The id is a path segment, so this is the check that keeps
    `../` out of the filesystem — validated before any join, never sanitised after."""
    return isinstance(uid, str) and bool(_ID_RE.match(uid))


def safe_name(name):
    """A display-and-disk-safe basename. The client's filename is untrusted input: it may
    carry directories (`../../etc/passwd`), a leading dot (a dotfile in a served directory),
    control characters, or be enormous.

    Basename only, then control characters and path separators stripped, then leading dots and
    spaces (so `.git` and `.env` cannot be the stored name), then length-capped. Empty after
    all that becomes "attachment"."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = _CONTROL_RE.sub("", base).replace("\u200b", "")
    base = base.lstrip(". ")
    base = re.sub(r"[ /:*?\"<>|]+", "_", base).strip()
    return (base[:_NAME_MAX] or "attachment")


def store(name, mime, data):
    """Save one upload; returns its metadata dict {id, name, size, mime, stored}.

    The bytes go to `data/uploads/<id>/<safe name>` and the metadata to a `meta.json` sidecar
    in the same directory — one directory per upload, so the TTL sweep, the retrieve route and
    the staging copy all resolve an id to a path without parsing a filename back out."""
    uid = new_id()
    safe = safe_name(name)
    directory = _upload_dir(uid)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, safe), "wb") as fh:
        fh.write(data)
    meta = {"id": uid, "name": safe, "size": len(data), "mime": str(mime or ""),
            "stored": time.time()}
    storage.write_json(_meta_path(uid), meta)
    gc()
    return meta


def get(uid):
    """The metadata for an unexpired upload, or None.

    None means "no such attachment" to every caller — an unknown id, a malformed one and an
    expired one are deliberately indistinguishable, so the route cannot be used to enumerate
    which ids exist. The TTL is checked here rather than left to the sweep, because the sweep
    is best-effort and a stale-but-present file must still read as gone."""
    if not is_valid_id(uid):
        return None
    meta = storage.read_json(_meta_path(uid), None)
    if not isinstance(meta, dict) or not meta.get("name"):
        return None
    if time.time() - float(meta.get("stored") or 0) > config.setting("upload_ttl_h") * 3600:
        return None
    if not os.path.isfile(os.path.join(_upload_dir(uid), str(meta["name"]))):
        return None
    return meta


def path_for(meta):
    return os.path.join(_upload_dir(meta["id"]), str(meta["name"]))


def stage_dir(wid, attempt):
    return os.path.join(RUN_FILES_DIR, f"{wid}-a{attempt}")


def stage(ids, wid, attempt):
    """Copy these uploads into a per-attempt directory and return the staged paths.

    Per ATTEMPT, not per run: the verify ladder re-runs the same request, and a retry must not
    read a file a previous attempt may have had write-adjacent contact with. Returns [] for no
    ids, so a run with no attachments costs nothing and changes nothing.

    An id that has expired or never existed is skipped rather than fatal — the server validated
    it at submit time, and a long gate wait can legitimately outlive the TTL. Silently dropping
    it is the wrong outcome, so `note()` says how many of the requested attachments are missing."""
    ids = [i for i in (ids or []) if is_valid_id(i)]
    if not ids:
        return []
    gc()
    directory = stage_dir(wid, attempt)
    os.makedirs(directory, exist_ok=True)
    out = []
    for uid in ids:
        meta = get(uid)
        if meta is None:
            continue
        dest = os.path.join(directory, safe_name(meta["name"]))
        try:
            shutil.copyfile(path_for(meta), dest)
        except OSError:
            continue
        out.append(dest)
    return out


def unstage(wid, attempt):
    """Remove this attempt's staging directory. Called from a `finally`, so it must never
    raise — a cleanup failure must not replace the run's real result or error."""
    try:
        shutil.rmtree(stage_dir(wid, attempt), ignore_errors=True)
    except OSError:
        pass


def gc():
    """Best-effort TTL sweep of BOTH directories (mirrors `claude_cli.gc_transcripts`).

    Runs opportunistically on every upload and every stage, which is how a service that is
    never restarted keeps both directories bounded. The staged sweep is the backstop for the
    `finally` that never ran: a worker SIGKILLed mid-attempt leaves its copies behind, and
    nothing else would ever remove them."""
    _sweep(UPLOADS_DIR)
    _sweep(RUN_FILES_DIR)


def _sweep(root):
    ttl = config.setting("upload_ttl_h") * 3600
    if not os.path.isdir(root):
        return
    cutoff = time.time() - ttl
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        p = os.path.join(root, name)
        try:
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def note(paths, backend="claude", missing=0):
    """The attachment note that rides in `system_context` (and so in the transcript's meta
    line): a transcript that cannot say what the model was told cannot be debugged.

    The Claude backend gets the paths and is told to Read them — measured against
    `file_safety.settings_arg()` and `config.READ_TOOLS`, a `claude -p` run reads a file outside
    its cwd (no cwd, a repo-mode clone, Otto's checkout) with no permission refusal and no
    `--add-dir`, so the staging directory needs no grant of its own.

    The local and Codex backends get the opposite note. Neither can put image bytes on the
    wire: `local_runtime` builds text-only message content and `codex exec` gets a string
    prompt. Following `mcp_client.connector_note`, the absence is DECLARED rather than left
    silent — told nothing, a model invents an answer for the thing it cannot see."""
    if backend == "claude":
        if not paths and not missing:
            return None
        lines = []
        for p in paths or []:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            lines.append(f"- {p} (name: {os.path.basename(p)}, {size} bytes)")
        head = ("ATTACHMENTS for this request are staged on disk at these paths. Use the Read "
                "tool on them; an image is returned as an image, so describe what you actually "
                "see rather than guessing from the filename:\n" + "\n".join(lines))
        if missing:
            head += (f"\n{missing} requested attachment(s) are no longer available (expired or "
                     "removed) — say which parts of the request you could not check because of "
                     "that.")
        return head
    if not paths and not missing:
        return None
    return (f"{len(paths or []) + missing} file attachment(s) came with this request, and THIS "
            "backend cannot receive them: attachments are staged on disk for the Claude "
            "backend only, and this runtime has no way to read them. Say in your reply that the "
            "attachment(s) were not available to you and answer the text of the request only. "
            "Do not guess at, invent, or describe the contents of a file you could not open.")
