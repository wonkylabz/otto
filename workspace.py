"""Isolated repo workspaces (issue #57).

When a task must MODIFY a repository, Otto provisions an ISOLATED clone of an allowlisted
repo, runs the (write-gated) capability there with `cwd` set to it, then pushes a BRANCH +
opens a DRAFT PR — never a push to the default branch. The workspace is cleaned up afterwards.

The allowlist is the registered project repos (`registry.projects()`) — we never clone an
arbitrary URL (which matters because of the unattended event/webhook ingress). A registered
repo is cloned from its LOCAL path (fast, offline), but its `origin` is repointed at the
path's real `origin` remote so the push + PR land on the actual host (e.g. GitHub). Each run
gets its own workspace keyed on the workflow id, so concurrent swarm sub-tasks never collide.
"""
import os
import re
import shutil
import subprocess
import time

import config
import registry
import repos
from ui import trace

WORKSPACES = os.path.join(config.DATA_DIR, "workspaces")
GC_MAX_AGE_H = float(os.environ.get("OTTO_WORKSPACE_TTL_H", "24"))   # sweep stale leftovers

# A registered checkout is whatever state the user left it in — it can be many commits behind its
# remote (measured on the live install: `infra`'s local `master` was 8 commits behind
# `origin/master`). That staleness silently poisons BOTH paths: a read run answering from the local
# tree reports old state as current, and `provision` cuts its PR base off the stale local branch
# tip. So the remote refs are refreshed with `git fetch` — never `git pull`: the live checkout is
# the user's workspace (often parked on a feature branch, often with uncommitted changes), and
# Otto's whole premise is that it never mutates it. Fetch touches only `.git` refs; the working
# tree is untouched, so there is nothing to conflict.
REPO_FETCH_AGE_S = float(os.environ.get("OTTO_REPO_FETCH_AGE_S", "900"))   # 0 = never refresh
FETCH_TIMEOUT_S = float(os.environ.get("OTTO_REPO_FETCH_TIMEOUT_S", "120"))

# The approved plan is otherwise ephemeral — it lives in workflow state and the audit trail, so
# the reviewer of the draft PR sees the diff but never what was approved to produce it. It reaches
# the reviewer as a PR COMMENT, never a committed file: a run-scoped record of one review has no
# business in the target repo's history, where it outlives the review and accumulates one file per
# run forever.
PLAN_COMMENT = os.environ.get("OTTO_PLAN_COMMENT", "1") != "0"
PLAN_COMMENT_CHARS = 60_000     # GitHub rejects a comment body over 65_536


def _run(args, cwd=None, timeout=600):
    """Run a git/gh command; return (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:  # noqa: BLE001
        return 1, "", str(e)


# The identity Otto commits under when the MACHINE has none. `git commit` hard-fails with
# "Author identity unknown" if neither user.name nor user.email resolves, and `_finalize` reads
# that as `committed=False` — so repo mode goes quiet: no commit, no push, no PR, and the run
# still reports a workspace it wrote nothing to. That is the default state of a fresh machine
# and of every CI runner (measured: 6 workspace tests red on GitHub Actions, green on a laptop).
_FALLBACK_AUTHOR = ("Otto", "otto@localhost")


def _identity(path):
    """`-c user.*` args for a commit, and ONLY when the clone can't resolve an identity itself.

    Passing them unconditionally would be worse than the bug it fixes: every repo-mode PR would
    be authored by "Otto" instead of the operator, on machines that were configured correctly.
    So probe first (`git config` resolves local -> global -> system) and fill only a real gap."""
    if all(_run(["git", "-C", path, "config", "--get", k])[1]
           for k in ("user.name", "user.email")):
        return []
    return ["-c", f"user.name={_FALLBACK_AUTHOR[0]}", "-c", f"user.email={_FALLBACK_AUTHOR[1]}"]


def _safe(run_id):
    return re.sub(r"[^0-9A-Za-z._-]+", "-", str(run_id or "run")).strip("-") or "run"


def branch_name(run_id):
    """Deterministic, ref-safe branch name for a run. Pure (unit-testable)."""
    return f"otto/{_safe(run_id)}"


def valid_branch(name):
    """A `branch` for `from_branch=True` re-provisioning can arrive from client-supplied chat
    state (`/api/continue`'s `git_branch`), so it must be validated as an ordinary git ref
    name before it ever reaches a git argv — never trust it as a flag-safe token. Rejects
    anything that could be mistaken for an option (leading `-`) or an unsafe ref
    (`..`, control chars, `.lock` suffix). Pure (unit-testable) — the trust-boundary check at
    the API layer (`server.py`) and the last-line-of-defense check in `provision()` both call
    this same function."""
    return bool(name) and not name.startswith("-") and ".." not in name \
        and not name.endswith(".lock") \
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", name))


def workspace_path(run_id):
    return os.path.join(WORKSPACES, _safe(run_id))


def _is_github(url):
    """Only attempt `gh pr create` against a GitHub remote — pure (unit-testable)."""
    return bool(url) and "github.com" in url


def _git_origin(path):
    rc, out, _ = _run(["git", "-C", path, "remote", "get-url", "origin"])
    return out if rc == 0 and out else None


def _current_branch(path):
    rc, out, _ = _run(["git", "-C", path, "branch", "--show-current"])
    return out if rc == 0 and out else None


def _default_branch(src):
    """The DEFAULT branch of a git repo (what a PR should target), regardless of what branch
    happens to be checked out. We must NOT clone off the live checkout's current branch: if the
    user left the repo on a feature branch, `git clone <localpath>` copies THAT branch's tip as
    the clone's base, so the isolated workspace silently inherits unrelated work — which both
    produces a bogus "no changes to push" (the work is already in the base) and risks Otto
    opening a PR that duplicates the feature branch. Prefer origin/HEAD, then main/master, then
    fall back to the current branch as a last resort."""
    rc, out, _ = _run(["git", "-C", src, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if rc == 0 and out:
        return out.split("/", 1)[-1]
    for b in ("main", "master"):
        rc, _, _ = _run(["git", "-C", src, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}"])
        if rc == 0:
            return b
    return _current_branch(src)


def _last_fetch_age_s(path):
    """Seconds since this checkout last fetched, from `.git/FETCH_HEAD`'s mtime — git already
    records this, so there's no separate state file to keep in sync (and none to go stale itself).
    Returns None for a checkout that has never fetched, which counts as due."""
    try:
        return max(0.0, time.time() - os.path.getmtime(os.path.join(path, ".git", "FETCH_HEAD")))
    except OSError:
        return None


def refresh_repos(max_age_s=None):
    """`git fetch` every registered checkout whose refs are older than `max_age_s`, so a run that
    reads a repo sees the CURRENT default branch rather than wherever the user's checkout was left.

    Best-effort by design: a failure (offline, auth prompt, hung remote) is traced and skipped, not
    raised. The read path degrades to the same staleness it had before this existed, which is
    strictly better than failing a run over a fetch. Bounded by `FETCH_TIMEOUT_S` per repo so a
    black-holed remote can't stall the ladder.

    Returns the names actually refreshed (for tracing/tests)."""
    window = REPO_FETCH_AGE_S if max_age_s is None else max_age_s
    if window <= 0:
        return []
    done = []
    for r in git_repos():
        age = _last_fetch_age_s(r["path"])
        if age is not None and age < window:
            continue
        if repos.is_managed(r["path"]):
            # Otto's OWN clone, so fetching refs is not enough: the reason it exists is that
            # `project_skills` and `conventions.digest` read files out of its WORKING TREE, and
            # a fetch leaves those at whatever the clone landed on. `repos.refresh` hard-resets
            # it — legal here and nowhere else, because nobody but Otto edits this copy.
            if repos.refresh(r["path"]):
                done.append(r["name"])
            continue
        # --prune keeps deleted remote branches from lingering as phantom refs a later run reads.
        rc, _, err = _run(["git", "-C", r["path"], "fetch", "--quiet", "--prune", "origin"],
                          timeout=FETCH_TIMEOUT_S)
        if rc == 0:
            done.append(r["name"])
        else:
            trace("WORKSPACE", f"fetch of {r['name']} failed (using local refs): {err[:120]}")
    if done:
        trace("WORKSPACE", f"refreshed remote refs: {', '.join(done)}")
    return done


def _refresh_base(path, default):
    """Move a fresh clone's base onto the REMOTE's default-branch tip.

    `provision` clones from the LOCAL path because it's fast and works offline, but that means the
    base is the local `refs/heads/<default>` — which is only as current as the user's last pull. On
    the live install that was 8 commits behind, so every repo-mode PR was being cut from stale code:
    phantom conflicts, and a diff that re-does work already on master. The clone's `origin` has
    already been repointed at the real remote by this point, so one shallow fetch gets the true tip.

    Best-effort: on failure the clone keeps its local base (the old behaviour) and the run
    continues, since a PR from a slightly-old base still beats no run at all."""
    if not default:
        return False
    rc, _, err = _run(["git", "-C", path, "fetch", "--depth", "1", "origin", "--", default],
                      timeout=FETCH_TIMEOUT_S)
    if rc != 0:
        trace("WORKSPACE", f"base refresh skipped ({default} not fetchable): {err[:120]}")
        return False
    rc, _, err = _run(["git", "-C", path, "reset", "--hard", "FETCH_HEAD"])
    if rc != 0:
        trace("WORKSPACE", f"base refresh skipped (reset failed): {err[:120]}")
        return False
    return True


def git_repos():
    """Registered project repos that are git working trees — the allowlist + UI picker source.
    Returns a list of {name, path, origin}."""
    repos = []
    for path in registry.projects():
        if os.path.isdir(os.path.join(path, ".git")):
            repos.append({"name": os.path.basename(path.rstrip("/")) or path,
                          "path": path, "origin": _git_origin(path)})
    return repos


def resolve(repo):
    """Map a repo identifier (name or absolute path) to a registered git repo, or None if it
    isn't allowlisted. THE allowlist guard — never provision an unregistered repo."""
    if not repo:
        return None
    repo = repo.strip()
    target = os.path.abspath(os.path.expanduser(repo))
    for r in git_repos():
        if repo == r["name"] or target == r["path"]:
            return r
    return None


def _origin_slug(origin):
    """`owner/name` (lowercased) from a git origin URL (https or ssh), or None."""
    m = re.search(r"github\.com[:/]+([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$",
                  (origin or "").strip())
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def pr_branch(repo, pr_url):
    """The head branch of `pr_url`, but ONLY when that PR belongs to the allowlisted `repo` — so
    a resumed follow-up can never resolve (and later push a fix to) a PR in a DIFFERENT repo.
    Best-effort: returns a valid branch name or None. `pr_url` comes from Otto's own audit trail
    and is matched against a strict pattern before it reaches a `gh` argv."""
    r = resolve(repo)
    if not r or not pr_url:
        return None
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/\d+",
                     pr_url.strip())
    if not m or _origin_slug(r["origin"]) != f"{m.group(1)}/{m.group(2)}".lower():
        return None                          # unparseable, or a PR in another repo — refuse
    rc, out, _ = _run(["gh", "pr", "view", pr_url, "--json", "headRefName", "--jq", ".headRefName"])
    branch = out.strip() if rc == 0 else ""
    return branch if branch and valid_branch(branch) else None


# A PR reference in a request: an explicit URL, `owner/repo#N`, or a bare `#N` / `PR #N`.
# Bare `#N` is deliberately included even though it is USUALLY an issue: GitHub draws issues and
# pull requests from ONE number sequence, so `#N` is never both, and `gh pr view N` answers which
# it is definitively. Guessing from the wording instead would be the unreliable half of this.
_PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)")
_PR_SLUG_RE = re.compile(r"\b([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)\b")
# `repo#N` — the owner omitted, which is how a request written by a human who knows the repo
# actually reads. Measured: `web-3b6f2613` said "(ci#106)" and NEITHER of the other two
# patterns matched it — the slug form wants an owner, and `#` there follows `y`, not a space —
# so the run got a default-branch clone for code that lives only on that PR.
_PR_REPO_RE = re.compile(r"\b([A-Za-z0-9._-]+)#(\d+)\b")
_PR_NUM_RE = re.compile(r"(?:^|[\s(\[])#(\d{1,7})\b")


def request_pr_refs(request, slug=None):
    """Every PR-shaped reference in `request`, as a list of (slug_or_None, number) in the order
    they appear. Pure — resolving which of them is actually an open pull request costs a `gh`
    call and belongs to `pr_target`.

    `slug` (this repo's `owner/name`) filters the qualified forms: a request naming
    `other-org/other-repo#12` must not steer a run provisioned against THIS repo, which is the
    same containment `pr_branch` applies to a URL."""
    text = str(request or "")
    out = []
    for m in list(_PR_URL_RE.finditer(text)) + list(_PR_SLUG_RE.finditer(text)):
        ref = f"{m.group(1)}/{m.group(2)}".lower()
        if slug and ref != slug.lower():
            continue                      # a PR in someone else's repo — not ours to check out
        out.append((ref, int(m.group(3))))
    # Strip the qualified matches before scanning the looser forms, or `owner/repo#12` yields 12
    # twice — harmless for correctness, but it makes the probe order (and its log line) lie.
    bare = _PR_SLUG_RE.sub(" ", _PR_URL_RE.sub(" ", text))
    # `repo#N`, accepted ONLY when the bare name is this repo's. Without that bound it would
    # match any `word#123`, which is how an unrelated number becomes a branch switch.
    name = (slug or "").split("/")[-1].lower()
    for m in _PR_REPO_RE.finditer(bare):
        if name and m.group(1).lower() == name:
            out.append((slug.lower(), int(m.group(2))))
    bare = _PR_REPO_RE.sub(" ", bare)
    out += [(None, int(m.group(1))) for m in _PR_NUM_RE.finditer(bare)]
    return [*dict.fromkeys(out)]


def _viewer_login():
    """The GitHub account `gh` is authenticated as. Used to keep `pr_target` to the operator's
    OWN pull requests; None when it can't be determined, which fails closed there."""
    rc, out, _ = _run(["gh", "api", "user", "--jq", ".login"])
    login = out.strip()
    return login if rc == 0 and login else None


def pr_target(repo, request, limit=3):
    """The OPEN pull request this request asks to work ON, as {number, url, branch}, or None.

    THE BUG THIS EXISTS FOR. A fresh submit naming a pull request got a clone branched off the
    DEFAULT branch, because `from_branch=True` was only ever reachable from the post-PR fix loop
    and a resumed chat. So "fix the `hf download` call at line ~148 in infra#498" ran against a
    master where that file is 82 lines long and has no such call: the first model to notice
    (`web-d2438694` attempt 1) had no sanctioned way to reach the code and burned its attempt
    trying to work out how the platform intended it to, and the two after it never noticed at
    all — they edited the default branch's unrelated version and shipped it as a NEW PR (#503)
    against the wrong base, which the judge, four review rounds and QA all passed.

    OPEN is the whole discriminator, and it is the right one in both directions: work asked of
    an open PR belongs on that PR's branch, while "revert #498" or "fix the bug #498 introduced"
    names a MERGED PR, resolves to no target here, and correctly starts a fresh branch off the
    default. An issue number resolves to no target either — `gh pr view` refuses it.

    Bounded to `limit` probes so a request quoting a long issue thread full of `#N` cannot turn
    one provision into dozens of network round-trips. Best-effort throughout: any failure means
    "no target", i.e. exactly the fresh-branch behaviour that predates this function."""
    r = resolve(repo)
    if not r:
        return None
    slug = _origin_slug(r["origin"])
    if not slug:
        return None                       # can't prove a PR belongs to this repo — don't guess
    viewer = None                      # resolved lazily, once, only if a candidate resolves
    for _, num in request_pr_refs(request, slug=slug)[:limit]:
        rc, out, _ = _run(["gh", "pr", "view", str(num), "--repo", slug, "--json",
                           "state,url,headRefName,isCrossRepository,author"])
        if rc != 0 or not out:
            continue                      # not a PR (an issue number), or gh is unavailable
        try:
            import json as _json
            d = _json.loads(out)
        except ValueError:
            continue
        if d.get("state") != "OPEN" or d.get("isCrossRepository"):
            # A fork's head branch does not exist on our origin, so `provision(from_branch=True)`
            # could not fetch it — treat it as no target rather than failing the provision.
            continue
        branch = (d.get("headRefName") or "").strip()
        if not branch or not valid_branch(branch):
            continue
        # ONLY the operator's own pull requests. Merely NAMING a PR is weak evidence of intent
        # ("add a test like the one in #480", "same as #480 but for staging"), and acting on it
        # against a colleague's branch would push commits into their review — a worse outcome
        # than the wrong-base PR this function exists to prevent. Against your own open PR the
        # same inference is the ordinary workflow. Fails closed: an unresolvable viewer means no
        # target, i.e. the fresh-branch behaviour that predates this.
        viewer = viewer or _viewer_login()
        if not viewer or (d.get("author") or {}).get("login") != viewer:
            trace("WORKSPACE", f"PR #{num} is not {viewer or 'the operator'}'s — "
                               f"branching off the default instead")
            continue
        trace("WORKSPACE", f"request names open PR #{num} in {slug} — targeting branch {branch}")
        return {"number": num, "url": d.get("url") or "", "branch": branch}
    return None


# A path-shaped token in a request: at least one `/` and a file-ish tail. Deliberately narrow —
# a false hit here would tell a run its own request is ungrounded, which is worse than silence.
# The trailing `:N` is the `path:line` form this repo's own conventions use, and it BINDS the
# number to that file — stronger evidence than a free-floating "line ~N" elsewhere in the text.
_PATH_RE = re.compile(r"(?<![\w/])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8})(?::(\d{1,6}))?\b")
_LINE_RE = re.compile(r"\b(?:at\s+)?lines?\s*~?\s*(\d{1,6})\b", re.I)


def grounding(path, request):
    """Concrete claims `request` makes about the checked-out tree that the tree contradicts.

    Returns a list of plain-English mismatch lines (empty when everything checks out). Purely
    deterministic — file existence and line counts, no model — so it costs nothing and cannot
    itself hallucinate a defect.

    WHY A CHECK THIS BLUNT IS WORTH HAVING. In `web-d2438694` the request said "line ~148 of
    `runbooks/vllm/upload-weights.sh`" and the provisioned branch's copy of that file was 82
    lines long. Nothing in Otto compared the two: the executor edited whatever it found, and
    all four judges asked only "does the output satisfy the request", never "does this tree
    contain the thing the request is about". A file that is missing, or half the length the
    request points into, is the cheapest possible evidence that the run is aimed at the wrong
    branch — and it is evidence available BEFORE the money is spent.

    Advisory by construction. It is handed to the executor and the judge as a note, never used
    to block a run: the request may legitimately name a file it is asking to CREATE, or a line
    in a file it is about to grow."""
    out = []
    text = str(request or "")
    if not path or not os.path.isdir(path):
        return out
    # Strip URLs first, then reject any candidate whose FIRST segment looks like a hostname.
    # Without both, `https://github.com/acme/infra/blob/main/setup.py` is read as a repo path and
    # reported missing — telling the run its own request is ungrounded, which is worse than
    # saying nothing and is the one thing this function must never do.
    scrubbed = re.sub(r"https?://\S+", " ", text)
    named = [(m.group(1), int(m.group(2)) if m.group(2) else None)
             for m in _PATH_RE.finditer(scrubbed)
             if "." not in m.group(1).split("/")[0]]
    named = [*dict.fromkeys(named)]
    for rel, at_line in named[:12]:
        # Never let a request's own text escape the workspace: `..` or an absolute path would
        # make this report on a file the run cannot see anyway.
        if rel.startswith("/") or ".." in rel.split("/"):
            continue
        full = os.path.join(path, rel)
        if not os.path.exists(full):
            # Only interesting when the repo has NO such file anywhere — a bare basename match
            # elsewhere in the tree means the request just wrote the path loosely.
            base = os.path.basename(rel)
            hits = _find_basename(path, base)
            if hits:
                out.append(f"the request names `{rel}`, which does not exist on this branch; "
                           f"the closest match in the tree is `{hits[0]}`")
            else:
                out.append(f"the request names `{rel}`, which does not exist anywhere on this "
                           f"branch")
            continue
        # A line number the file cannot have. Prefer one attached to THIS path (`file.kts:1437`)
        # over a free-floating "line ~N", which may be talking about a different file.
        if at_line is None:
            m = _LINE_RE.search(text)
            if not m:
                continue
            at_line = int(m.group(1))
        want = at_line
        try:
            with open(full, "rb") as f:
                have = sum(1 for _ in f)
        except OSError:
            continue
        # A generous margin: "~148" against a 140-line file is ordinary imprecision, against an
        # 82-line one it means the request is describing a different revision of the file.
        if want > have * 1.2 + 10:
            out.append(f"the request points at line {want} of `{rel}`, but this branch's copy "
                       f"is only {have} lines long")
    return out


def _find_basename(path, base, limit=1):
    """Tracked files in the clone whose basename matches `base`. `git ls-files` rather than a
    walk: it is one call, and it already excludes `.git` and everything gitignored."""
    rc, out, _ = _run(["git", "-C", path, "ls-files", "--", f"*/{base}", base])
    if rc != 0 or not out:
        return []
    return out.splitlines()[:limit]


def gc(max_age_h=None):
    """Best-effort sweep of workspaces older than the TTL — so a run that died before cleanup
    (a hard activity failure) can't leak clones forever."""
    max_age_h = GC_MAX_AGE_H if max_age_h is None else max_age_h
    if not os.path.isdir(WORKSPACES):
        return
    cutoff = time.time() - max_age_h * 3600
    for name in os.listdir(WORKSPACES):
        p = os.path.join(WORKSPACES, name)
        try:
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                trace("WORKSPACE", f"gc'd stale workspace {name}")
        except OSError:
            pass


def provision(repo, run_id, from_branch=False, branch=None):
    """Clone an allowlisted repo into an isolated workspace. Returns {path, branch, repo,
    origin, head}. Raises ValueError if the repo isn't allowlisted or a git step fails (the
    partial clone, if any, is cleaned up before raising — never leaked on disk).

    Normally creates a FRESH branch off the default branch. With `from_branch=True` (a QA fix
    round, or a resumed chat follow-up) it instead checks out an EXISTING branch from the
    remote — so a fix/follow-up lands on the same PR rather than opening a new one. `branch`
    names that existing branch explicitly (needed when the branch that carries the work isn't
    `branch_name(run_id)` — e.g. a capability that drove its own git and opened its own PR on
    its own branch name); defaults to `branch_name(run_id)` when omitted."""
    r = resolve(repo)
    if not r:
        raise ValueError(f"repo '{repo}' is not an allowlisted project repo")
    gc()
    path = workspace_path(run_id)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(WORKSPACES, exist_ok=True)
    try:
        # Shallow clone from the LOCAL path (fast, offline); repoint origin at the real remote so
        # the push + PR land on the host, not back on the local checkout. Clone the repo's DEFAULT
        # branch explicitly (NOT the live checkout's current branch) so the workspace always starts
        # from a clean base — see `_default_branch`.
        clone = ["git", "clone", "--depth", "1"]
        default = None if from_branch else _default_branch(r["path"])
        if default:
            clone += ["--branch", default]
        rc, _, err = _run(clone + [r["path"], path])
        if rc != 0:
            raise ValueError(f"clone failed: {err[:300]}")
        if r["origin"]:
            _run(["git", "-C", path, "remote", "set-url", "origin", r["origin"]])
            # ... then re-base the clone on the REMOTE's default tip, not the local branch's.
            # Skipped for from_branch: that path fetches an existing PR branch just below, and
            # resetting to the default branch first would throw away the work it's resuming.
            if not from_branch and _refresh_base(path, default):
                trace("WORKSPACE", f"base refreshed to origin/{default}")
        target_branch = (branch or branch_name(run_id)) if from_branch else branch_name(run_id)
        if from_branch:
            # `branch` can arrive from client-supplied chat state (`/api/continue`'s
            # `git_branch`) — validate it's an ordinary ref name before it reaches git's argv,
            # and pass `--` so it can never be parsed as a flag even if validation is loosened
            # later (argument-injection defense in depth).
            if not valid_branch(target_branch):
                raise ValueError(f"invalid branch name: {target_branch!r}")
            # The branch only exists on the real remote (the original run pushed it), not in this
            # shallow local clone — fetch its tip and check it out so the follow-up amends the
            # same PR.
            rc, _, err = _run(["git", "-C", path, "fetch", "--depth", "1", "origin", "--", target_branch])
            if rc != 0:
                raise ValueError(f"fetch of existing branch {target_branch} failed: {err[:300]}")
            rc, _, err = _run(["git", "-C", path, "checkout", "-B", target_branch, "FETCH_HEAD"])
        else:
            rc, _, err = _run(["git", "-C", path, "checkout", "-b", target_branch])
        if rc != 0:
            raise ValueError(f"branch failed: {err[:300]}")
    except ValueError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    _, head, _ = _run(["git", "-C", path, "rev-parse", "HEAD"])
    trace("WORKSPACE", f"provisioned {r['name']} -> {path} on {target_branch}"
          + (" (existing)" if from_branch else ""))
    return {"path": path, "branch": target_branch, "repo": r["name"], "origin": r["origin"], "head": head}


def _dirty(path):
    _, out, _ = _run(["git", "-C", path, "status", "--porcelain"])
    return bool(out)


def _agent_pr(path, exclude=None):
    """A capability may drive its OWN git — its own branch, commit, push, and `gh pr create`
    (e.g. sre-minion). That work lands on a branch OTHER than Otto's `otto/<run>`, so
    finalize's view of its own untouched branch would wrongly report "no changes to push"
    while a real PR exists. Look for an open PR whose head is a local branch the capability
    created inside the clone; return {branch, url} for the first hit, else None."""
    if not _is_github(_git_origin(path)):
        return None
    rc, out, _ = _run(["git", "-C", path, "for-each-ref", "--format", "%(refname:short)", "refs/heads"])
    if rc != 0:
        return None
    for b in out.splitlines():
        b = b.strip()
        if not b or b == exclude:
            continue
        rc, url, _ = _run(["gh", "pr", "list", "--head", b, "--state", "open",
                           "--json", "url", "--jq", ".[0].url"], cwd=path)
        if rc == 0 and url.strip():
            return {"branch": b, "url": url.strip()}
    return None


def _existing_pr_url(path, branch):
    """Resolve the URL of the PR already open on `branch` — used by finalize's existing_pr
    path, which pushes to an already-open PR but (unlike a fresh `gh pr create`) has no URL
    in hand otherwise. Best-effort: returns None on any failure."""
    if not _is_github(_git_origin(path)):
        return None
    rc, out, _ = _run(["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"], cwd=path)
    return out.strip() if rc == 0 and out.strip() else None


def plan_comment_body(run_id, plan, request=None, cap=None, concerns=None, when=None):
    """Render the approved-plan record. Pure — the body `post_plan` comments onto the PR.

    Marked with an `<!-- otto-plan:<id> -->` comment (the delivery-idempotency convention) so a
    later round of the same run can recognize its own comment and not post it again, and stamped
    as a point-in-time record: it is what was approved BEFORE the run, never a description of
    what the diff ended up being."""
    head = (request or "").strip().splitlines()[0][:100] if request else f"Otto run {run_id}"
    out = [f"## {head}", "", f"<!-- otto-plan:{run_id} -->", ""]
    meta = [f"run `{run_id}`"] + ([f"capability `{cap}`"] if cap else []) + ([when] if when else [])
    out += [f"Plan approved before execution — {', '.join(meta)}.",
            "Point-in-time record of what was approved; not maintained after the run.", ""]
    if request:
        out += ["### Request", "", request.strip(), ""]
    out += ["### Approved plan", "", (plan or "").strip(), ""]
    if concerns:
        out += ["### Plan review concerns", "",
                "Raised by the plan critic and accepted at the gate anyway (advisory):", ""]
        out += [f"- {str(c).strip()}" for c in concerns] + [""]
    return "\n".join(out)


def _plan_already_posted(pr_url, run_id, cwd=None):
    """True when this run's plan comment is already on the PR. `gh` failing (no auth, rate
    limit, deleted PR) returns True — a missed comment is a lost courtesy, a duplicated one on
    every QA/review round is noise in a human's inbox, so ambiguity fails toward NOT posting."""
    rc, out, _ = _run(["gh", "pr", "view", pr_url, "--json", "comments",
                       "-q", ".comments[].body"], cwd=cwd)
    return f"<!-- otto-plan:{run_id} -->" in out if rc == 0 else True


def post_plan(path, pr_url, run_id, plan, request=None, cap=None, concerns=None):
    """Post the approved plan as a comment on the run's PR. Returns True if a comment was
    posted. Best-effort, never raises: the plan is a courtesy to the reviewer and must never be
    what sinks a run whose actual work succeeded and is already pushed.

    Deliberately NOT a committed file. `finalize` runs again for every QA and review fix round,
    so the marker check is what keeps one plan to one PR."""
    if not (PLAN_COMMENT and pr_url and (plan or "").strip()):
        return False
    cwd = path if path and os.path.isdir(path) else None
    if _plan_already_posted(pr_url, run_id, cwd=cwd):
        return False
    body = plan_comment_body(run_id, plan, request=request, cap=cap, concerns=concerns,
                     when=time.strftime("%Y-%m-%d"))[:PLAN_COMMENT_CHARS]
    rc, _, err = _run(["gh", "pr", "comment", pr_url, "--body", body], cwd=cwd)
    if rc != 0:
        trace("WORKSPACE", f"plan comment not posted: {err[:200]}")
        return False
    trace("WORKSPACE", f"plan comment posted to {pr_url}")
    return True


def finalize(run_id, title=None, base_head=None, existing_pr=False, branch=None, body=None,
             plan=None, request=None, cap=None, concerns=None):
    """`_finalize` (commit/push/PR), then the approved plan onto whatever PR that resolved to.

    One wrapper rather than a call at each of `_finalize`'s four PR-bearing exits — the plan
    must reach the capability's own PR and a resumed run's existing PR too, not just the one
    `gh pr create` opened here."""
    out = _finalize(run_id, title=title, base_head=base_head, existing_pr=existing_pr,
                    branch=branch, body=body)
    post_plan(workspace_path(run_id), out.get("pr_url"), run_id, plan,
              request=request, cap=cap, concerns=concerns)
    return out


def _finalize(run_id, title=None, base_head=None, existing_pr=False, branch=None, body=None):
    """Commit any remaining changes, push the branch, and open a DRAFT PR. Returns
    {branch, pushed, committed, pr_url, detail}. Best-effort: nothing to push, no GitHub
    remote, or no `gh` is REPORTED, not raised. Skips the push entirely when the run produced
    no commit (no empty branches/PRs).

    With `existing_pr=True` (a QA fix round, or a resumed chat follow-up) the branch already
    has an open PR, so we just commit + push to UPDATE it and skip `gh pr create` entirely.
    `branch` names that branch explicitly — needed when it isn't `branch_name(run_id)` (e.g. a
    capability-driven PR the workspace was re-provisioned onto); defaults to `branch_name(run_id)`."""
    path = workspace_path(run_id)
    branch = branch or branch_name(run_id)
    if not os.path.isdir(path):
        return {"branch": branch, "pushed": False, "committed": False, "pr_url": None,
                "detail": "workspace gone"}
    committed = False
    if _dirty(path):
        _run(["git", "-C", path, "add", "-A"])
        rc, _, _ = _run(["git", "-C", path, *_identity(path), "commit",
                         "-m", (title or "otto: automated change")[:100]])
        committed = rc == 0
    # Whether OTTO's own branch has anything to push — judged on that branch's tip, NOT on the
    # checked-out HEAD: a capability that drove its own git leaves the clone parked on ITS branch,
    # so HEAD would be ahead of base while `otto/<run>` is still empty.
    _, otto_tip, _ = _run(["git", "-C", path, "rev-parse", branch])
    otto_has_work = committed or (bool(otto_tip) and bool(base_head) and otto_tip != base_head)
    # A capability's OWN PR wins whether or not Otto's branch also has work. Checking this only
    # in the no-work branch (as this did) meant a cap that both drove its own git AND left the
    # tree dirty got TWO pull requests for one run: measured on `web-2f640059`, where sre-minion
    # opened the properly stacked #355 (observe) -> #356 (enforce) on its own branches, and
    # finalize then pushed `otto/<run>` and opened #357 — a byte-for-byte duplicate of #355,
    # against master, outside the stack, with the gating the whole run existed to produce
    # silently dropped. Reviewers cannot tell which one is the deliverable.
    agent = None if existing_pr else _agent_pr(path, exclude=branch)
    if not otto_has_work:
        # Otto's own branch is untouched — but the capability may have run its own git and
        # opened its own PR on a different branch. Surface that instead of a bogus "no changes".
        if agent:
            trace("WORKSPACE", f"finalize: capability opened its own PR {agent['url']}")
            return {"branch": agent["branch"], "pushed": True, "committed": False,
                    "pr_url": agent["url"], "detail": "opened by the capability"}
        return {"branch": branch, "pushed": False, "committed": False,
                "pr_url": _existing_pr_url(path, branch) if existing_pr else None,
                "detail": "no changes in the isolated clone"}
    rc, _, perr = _run(["git", "-C", path, "push", "-u", "origin", branch])
    if rc != 0:
        return {"branch": branch, "pushed": False, "committed": committed, "pr_url": None,
                "detail": perr[:300]}
    if existing_pr:
        pr_url = _existing_pr_url(path, branch)
        trace("WORKSPACE", f"finalize {branch}: pushed fix to existing PR {pr_url or '-'}")
        return {"branch": branch, "pushed": True, "committed": committed, "pr_url": pr_url,
                "detail": "pushed to existing PR"}
    if agent:
        # Otto's branch IS pushed above (the work is recoverable, and it costs nothing), but no
        # second PR is opened for it — the capability's is the deliverable, and it is the URL the
        # advisory carve-out and the review loop must key on so review runs against the real diff.
        trace("WORKSPACE", f"finalize: capability already opened {agent['url']} — "
                           f"pushed {branch} without a second PR")
        return {"branch": agent["branch"], "pushed": True, "committed": committed,
                "pr_url": agent["url"],
                "detail": f"opened by the capability (Otto's {branch} pushed, no duplicate PR)"}
    pr_url, detail = None, ""
    if _is_github(_git_origin(path)):
        rc, out, perr = _run(
            ["gh", "pr", "create", "--draft", "--head", branch,
             "--title", (title or "Otto automated change")[:120],
             "--body", (body or f"Automated change by Otto for: {(title or '').strip()[:200]}")[:4000]],
            cwd=path)
        if rc == 0:
            m = re.search(r"https?://\S+", out)
            pr_url = m.group(0) if m else out
        else:
            # A capability that drove its own git may already have opened the PR on OTTO's
            # branch — `gh pr create` then fails with "already exists: <url>". Dropping that
            # URL sends a run WITH an open PR to Blocked (the advisory carve-out and the
            # review loop both key on pr_url). Ask `gh` (authoritative) and fall back to the
            # URL in stderr, scoped to that message so an unrelated failure can't yield one.
            pr_url = _existing_pr_url(path, branch)
            if not pr_url and "already exists" in perr:
                m = re.search(r"https?://\S+", perr)
                pr_url = m.group(0).rstrip(".,)") if m else None
            # Same marker `_agent_pr` uses — the branch is `otto/<run_id>`, unique to this run,
            # so a PR already on it was opened by the capability's own git.
            detail = ("opened by the capability" if pr_url
                      else f"pushed; PR not opened: {perr[:200]}")
    else:
        detail = "pushed; non-GitHub remote (no PR opened)"
    trace("WORKSPACE", f"finalize {branch}: pushed=True pr={pr_url or '-'}")
    return {"branch": branch, "pushed": True, "committed": committed, "pr_url": pr_url, "detail": detail}


def cleanup(run_id):
    shutil.rmtree(workspace_path(run_id), ignore_errors=True)


# --- in-place edit detection (issue #59) ----------------------------------
# A run that ISN'T in repo-mode can still mutate a live checkout via Bash (`cd /repo && git …`)
# — the unsafe path workspaces replace. We snapshot the registered repos' git state around such
# a run and flag any that changed, so an in-place edit is visible instead of silent. Limitation:
# only REGISTERED project repos are watched (we can't see edits to repos Otto doesn't know).

def _repo_state(path):
    _, head, _ = _run(["git", "-C", path, "rev-parse", "HEAD"])
    _, dirty, _ = _run(["git", "-C", path, "status", "--porcelain"])
    return {"head": head, "dirty": bool(dirty)}


def snapshot():
    """git HEAD + dirty state of every registered git repo (keyed by name)."""
    return {r["name"]: {"path": r["path"], **_repo_state(r["path"])} for r in git_repos()}


def diff(before, after):
    """Registered repos whose HEAD moved or dirty-state flipped between two snapshots. PURE
    (unit-testable)."""
    changed = []
    for name, now in (after or {}).items():
        was = (before or {}).get(name)
        if was is None:
            continue
        if now.get("head") != was.get("head") or now.get("dirty") != was.get("dirty"):
            changed.append({"name": name, "path": now.get("path"),
                            "from": (was.get("head") or "")[:8], "to": (now.get("head") or "")[:8],
                            "dirty": now.get("dirty")})
    return changed
