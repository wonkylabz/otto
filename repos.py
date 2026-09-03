"""Managed clones for repos registered by REMOTE URL (issue #69 follow-up).

A project repo is identified by its remote URL, not by where it happens to sit on this
machine. Everything downstream of registration still needs a checkout ON DISK — project
capability discovery (`registry.project_skills`), the CLAUDE.md conventions digest
(`conventions.digest`), the READ-run source note, and the clone source `workspace.provision`
cuts its isolated workspace from — so Otto keeps its OWN shallow clone per registered URL
under `data/repos/<slug>` and hands that path to all of them.

The operator's own checkout stays OPTIONAL: registering one alongside the URL makes it the
effective path (fast, offline, no second copy of a 200MB history), and `file_safety` keeps
write-denying it. Without one, the managed clone is the effective path. Either way the URL is
the identity, so re-registering the same repo from another machine resolves the same entry.

Auth is `gh`'s credential helper — the same credential the PR path already uses, so a private
repo needs no new secret. The helper is injected per-command rather than written into the
user's global git config: Otto must not reconfigure the machine it runs on.
"""
import os
import re
import shutil
import subprocess

import config
from ui import trace

MANAGED = os.path.join(config.DATA_DIR, "repos")
CLONE_TIMEOUT_S = float(os.environ.get("OTTO_REPO_CLONE_TIMEOUT_S", "600"))

# `gh auth git-credential` answers a credential query from gh's own keyring. The leading EMPTY
# helper is not cosmetic: git APPENDS helpers, so without it the user's inherited helpers run
# first and a stale one can answer with a dead token before gh is ever asked.
_GH_CRED = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]

# Scheme-less `git@host:owner/name`, `ssh://git@host/owner/name`, `https://host/owner/name`, with
# or without a `.git` suffix or a trailing slash. Anything else is not a git remote we can clone.
_URL_RE = re.compile(
    r"^(?:(?P<scheme>https?|ssh|git)://)?(?:(?P<user>[A-Za-z0-9._-]+)@)?"
    r"(?P<host>[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?::\d+)?[:/]"
    r"(?P<path>[A-Za-z0-9._/-]+?)(?:\.git)?/?$")


def _run(args, cwd=None, timeout=600):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:  # noqa: BLE001
        return 1, "", str(e)


def parse(url):
    """`{url, host, owner, name, slug}` for a git remote URL, or None if it isn't one. Pure.

    `url` is normalised to the https form — that is what `gh`'s credential helper answers for,
    and it is what `_origin_slug` (the PR containment check) already knows how to read. An ssh
    remote the operator pastes is therefore accepted and stored as https, so the two spellings
    of one repo can never register as two entries."""
    m = _URL_RE.match((url or "").strip())
    if not m:
        return None
    parts = [p for p in m.group("path").split("/") if p]
    if len(parts) < 2:
        return None                       # a host with no owner/name is not a repo
    owner, name = "/".join(parts[:-1]), parts[-1]
    host = m.group("host").lower()
    return {"url": f"https://{host}/{owner}/{name}.git", "host": host,
            "owner": owner, "name": name, "slug": slug(name)}


def slug(name):
    """Directory name for a managed clone — the repo NAME alone. `project_namespace` and the
    `<repo>:<cap>` catalogue prefix are both derived from the path basename, so an `owner--name`
    directory would rename every project capability and orphan that project's memory namespace.
    Two repos sharing a name are refused at `ensure` rather than disambiguated here (registry
    already keys namespaces and capability prefixes on the name, so they would collide anyway)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "repo"


def managed_path(url):
    """Where the managed clone for `url` lives. PURE — no disk, no subprocess, no network.

    That matters: `registry.projects()` calls this for every URL-registered repo, and
    `file_safety` calls `projects()` on every single `claude -p` invocation. It also means the
    path resolves identically before and after the clone lands, so a registration whose clone
    failed reads as a repo with no capabilities rather than as a missing key.

    Returns None for an unparseable URL."""
    info = parse(url)
    return os.path.join(MANAGED, info["slug"]) if info else None


def origin_of(path):
    """The `origin` remote of a checkout, normalised to the https form so it compares equal to a
    stored URL regardless of which spelling it was cloned with. None if not a git repo."""
    rc, out, _ = _run(["git", "-C", path, "remote", "get-url", "origin"], timeout=15)
    if rc != 0 or not out:
        return None
    info = parse(out)
    return info["url"] if info else out


def ensure(url, depth=1):
    """Clone the managed copy of `url` if it isn't there yet; return (path, error).

    Shallow by default — nothing reading this clone needs history, and `workspace.provision`
    re-fetches the true base tip from `origin` before it branches, so a depth-1 cache cannot
    put a run on a stale base. On failure NOTHING is left behind: a half-written clone that
    later reads as "registered" is worse than no clone."""
    info = parse(url)
    if not info:
        return None, "not a git remote URL"
    path = managed_path(info["url"])
    if os.path.isdir(os.path.join(path, ".git")):
        # Reusing a directory claimed by a DIFFERENT remote would silently hand back the wrong
        # repo — and since the capability prefix and memory namespace are both this basename,
        # the two would be indistinguishable everywhere downstream. Say so instead.
        seen = origin_of(path)
        if seen not in (None, info["url"]):
            return None, f"a different repo is already registered as '{info['slug']}' ({seen})"
        return path, None
    os.makedirs(MANAGED, exist_ok=True)
    shutil.rmtree(path, ignore_errors=True)
    args = ["git", *_GH_CRED, "clone", "--depth", str(int(depth)), "--", info["url"], path]
    rc, _, err = _run(args, timeout=CLONE_TIMEOUT_S)
    if rc != 0:
        shutil.rmtree(path, ignore_errors=True)
        return None, (err or "git clone failed")[-400:]
    trace("WORKSPACE", f"cloned managed copy of {info['url']} -> {path}")
    return path, None


def refresh(path, default=None):
    """Bring a managed clone up to date with its remote. Unlike `workspace.refresh_repos` (which
    only ever fetches, because that checkout belongs to the USER) this one hard-resets the tree:
    the managed clone is Otto's, nobody edits it, and a stale CLAUDE.md or `.claude/` dir there
    is exactly the wrong-answer class the URL registration was meant to remove."""
    if not is_managed(path):
        return False
    ref = default or "HEAD"
    rc, _, err = _run(["git", "-C", path, *_GH_CRED, "fetch", "--depth", "1", "origin", "--", ref],
                      timeout=CLONE_TIMEOUT_S)
    if rc != 0:
        trace("WORKSPACE", f"managed refresh skipped for {path}: {err[:120]}")
        return False
    rc, _, _ = _run(["git", "-C", path, "reset", "--hard", "FETCH_HEAD"])
    return rc == 0


def is_managed(path):
    """True when `path` is one of Otto's own clones — pure. The guard on anything that WRITES to
    a checkout, so a user's live repo can never be hard-reset by the refresh above."""
    root = os.path.abspath(MANAGED)
    p = os.path.abspath(path or "")
    return p != root and (p + os.sep).startswith(root + os.sep)


def discard(url):
    """Remove the managed clone for `url` (deregistration). Never touches a user checkout."""
    path = managed_path(url)
    if path and is_managed(path) and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
        return True
    return False
