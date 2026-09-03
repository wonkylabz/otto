"""Paths no run may write, enforced by the executor rather than by a judgement.

Otto's write guard is the approval gate: a human reads a plan and says yes. That gate is real,
but it is a judgement about a *plan*, and `config.READ_TOOLS` still contains unscoped `Bash` —
Otto's own rules already concede a read cap can mutate external state. This is the second layer
under it, and the only one that holds without anyone reading anything: a small set of paths
that stay unwritable whatever the request said, whatever the plan said, and whatever the model
decided.

Deliberately NOT a general sandbox. It denies the handful of files where a write is never the
task and is always someone else's bad day: SSH private keys, Otto's own credentials and audit
trail, and the settings file that would let a run turn this very guard off for the next one.
Plus one broader category: every REGISTERED project repo's live checkout (issue #59) — a run
with no cwd of its own has no business editing ANY of them in place, isolation is the point of
repo-mode, and `allow_cwd` is the single exemption (a project capability's own designated repo).

Writes are the main event. Reads are denied for ONE narrow set — Otto's own runtime state under
`data/` — and left alone everywhere else, so a run can still inspect what it must not edit
(`~/.aws/credentials` stays readable on purpose: "which profiles exist" is a routine question in
this operator's work, and the write deny is what stops it being rewritten).

`data/` is denied to READ because it is not source, it is the service's live memory: `otto.db`
holds every chat, memory row and audit entry across every project and Slack conversation,
`models.json` holds endpoint API keys in plaintext, and `transcripts/` holds the full text of
other runs. A repo-mode run has no reason to read any of it, and `web-d2438694` is what it looks
like when one does — a model that could not find the code it was asked to fix spent twenty
minutes and 784k input tokens reading `otto.db`, `transcripts/` and `local-sessions/` trying to
reverse-engineer how the platform would deliver its change. `data/workspaces/**` is deliberately
NOT denied: every repo-mode clone lives there.

## How it is enforced (all four of these were established by probe, not by documentation)

- The rule must be spelled `Edit(...)`. A `Write(...)` rule is silently ignored for file
  permission checks — the CLI prints a notice and writes anyway. `Edit(...)` covers every
  file-editing tool.
- An absolute path needs a DOUBLE slash: `Edit(//home/u/.ssh/**)`. With one slash the rule
  parses, raises nothing, and matches nothing — indistinguishable from a working guard until
  someone checks the file. `_rule` is the only place that builds this string.
- It has to arrive as `permissions.deny` via `--settings`. The same rule on `--disallowedTools`
  does not bite.
- A matching deny beats an explicit `--allowedTools Bash`/`Write` grant, and covers Bash
  redirection (`echo x > file`), not merely the Write and Edit tools.

Guarded by `test_core.FileSafetyTests`, whose syntax cases exist because every one of those
four mistakes produces a guard that looks installed and blocks nothing.

Ported in spirit from hermes-agent's `agent/file_safety.py` (MIT), which enforces the same idea
in-process because Hermes owns its own tool loop. Otto delegates execution to `claude -p`, so
the list has to be handed to the executor instead.
"""
import os

import config

# Extra paths from the operator, `os.pathsep`-separated. Additive only — there is deliberately
# no way to REMOVE a built-in entry, since the env of a run is exactly what a compromised run
# would try to edit first.
_EXTRA_ENV = "OTTO_WRITE_DENY"


def _home():
    return os.path.expanduser("~")


def _sentinel_name():
    """The ESTOP filename, read from estop rather than duplicated. Imported lazily: estop pulls
    in `ui`, and this module is imported by claude_cli on the hot execution path."""
    import estop
    return estop.SENTINEL_NAME


def _otto_root():
    """Otto's own checkout — the parent of DATA_DIR, resolved live rather than at import so a
    relocated install (or a test pointing DATA_DIR at a temp dir) protects the right tree."""
    return os.path.dirname(os.path.abspath(config.DATA_DIR))


def _registered_repo_paths():
    """Every registered project repo that's an actual git working tree, as absolute paths.
    Deliberately NOT `workspace.git_repos()` — that also shells out to `git remote get-url`
    per repo (for the origin field this caller doesn't need), and this runs on every `claude
    -p` invocation. `registry.projects()` is a JSON read; only the `.git` check touches disk."""
    import registry
    out = []
    for path in registry.projects():
        p = os.path.realpath(os.path.expanduser(path))
        if os.path.isdir(os.path.join(p, ".git")):
            out.append(p)
    return out


_GLOB_CHARS = "*?["


def _both_spellings(pattern):
    """`pattern` plus its symlink-resolved twin, when they differ.

    A deny glob is handed to the executor as text and matched against whatever path the model
    actually used — which may be either side of a symlink. `os.path.realpath` can't be run on
    the pattern as a whole (the glob segments name nothing on disk), so only the leading
    glob-free prefix is resolved and the wildcard tail is re-attached. Both spellings are
    emitted rather than one being chosen, because either may be the one a run writes through:
    on macOS `/tmp` and `/var` are symlinks into `/private`, so the temp dirs the tests (and
    repo-mode clones under a relocated DATA_DIR) live in have two equally valid names."""
    parts = os.path.expanduser(pattern).split(os.sep)
    cut = next((i for i, seg in enumerate(parts) if any(c in seg for c in _GLOB_CHARS)),
               len(parts))
    prefix = os.sep.join(parts[:cut]) or os.sep
    resolved = os.path.join(os.path.realpath(prefix), *parts[cut:])
    return [pattern] if resolved == pattern else [pattern, resolved]


def denied_globs(allow_cwd=None):
    """The protected set, as absolute path globs. Order is stable so the rules (and therefore
    the command line, and therefore any diff of it) are reproducible.

    Every REGISTERED project repo's live checkout is denied too (issue #59) — a run with no
    cwd of its own (the general worker, routed with no repo picked) had nothing stopping it
    from reaching sideways into a sibling checkout and editing it in place, live, uncommitted:
    the in-place-edit guard (`workspace.diff`) only DETECTED that after the fact, never
    prevented it. `allow_cwd` is the one exemption — a project capability's own cwd IS a
    registered repo path, and it's trusted to drive its own git there directly (e.g.
    sre-minion); denying it too would break that feature outright. A repo-mode run's cwd is
    the isolated CLONE (under `data/workspaces/`), never a registered path, so no registered
    repo ever matches the exemption there — the live checkout stays denied regardless."""
    home, root = _home(), _otto_root()
    out = [
        # SSH private keys and the file that decides who may log in as this user. `~/.ssh/config`
        # is deliberately absent: it carries no key bytes and editing it (host aliases,
        # ProxyJump) is a plausible real task.
        os.path.join(home, ".ssh", "id_*"),
        os.path.join(home, ".ssh", "authorized_keys"),
        # Cloud and forge credentials. Denying the write only — a run may still READ these to
        # answer "which profiles exist", which is a routine question in this operator's work.
        os.path.join(home, ".aws", "credentials"),
        os.path.join(home, ".config", "gh", "hosts.yml"),
        # The file that grants tool permissions to every future run, including permission to
        # edit the things above. A run that can write here can switch this guard off.
        os.path.join(home, ".claude", "settings.json"),
        # Otto's own secrets (OTTO_SLACK_USER_TOKEN, the event HMAC) ...
        os.path.join(root, ".env"),
        # ... its audit trail, which is immutable by design and the only durable record that a
        # run happened at all — a run able to rewrite it can erase itself. `otto.db*` catches the
        # WAL and SHM sidecars, which are just as good a way to corrupt it.
        os.path.join(config.DATA_DIR, "otto.db*"),
        # ... the JSON state stores (policy, models, projects, board, runbooks, settings, slack
        # state): a write here re-risks a capability or re-points an ingress for every LATER run.
        os.path.join(config.DATA_DIR, "*.json"),
        # ... the execution transcripts, for the same reason as the trail.
        os.path.join(config.DATA_DIR, "transcripts", "**"),
        # ... and the global-pause sentinel. Deleting it RELEASES the pause, so without this a
        # run can hand itself the one lever an operator has for stopping Otto — the same
        # self-escalation as ~/.claude/settings.json above, and the reason `rm` had to be
        # measured rather than assumed: a deny rule does cover deletion through Bash, verified
        # against a control that deleted the file when the rule was absent. Named from
        # estop.SENTINEL_NAME so renaming the sentinel cannot silently unprotect it.
        os.path.join(config.DATA_DIR, _sentinel_name()),
        # DELIBERATELY absent: data/workspaces/**. Every repo-mode clone lives there, so denying
        # DATA_DIR wholesale silently blocks the entire repo-mode feature — the thing most write
        # runs exist to do. Guarded by test_core.FileSafetyTests.
    ]
    extra = os.environ.get(_EXTRA_ENV, "")
    out += [p for p in (x.strip() for x in extra.split(os.pathsep)) if p]
    allow = os.path.realpath(os.path.expanduser(allow_cwd)) if allow_cwd else None
    for p in _registered_repo_paths():
        if allow and (allow == p or allow.startswith(p + os.sep)):
            continue                    # this run's own project-cap cwd — trusted to edit it
        out.append(os.path.join(p, "**"))
    # Dict, not set: order is part of this function's contract (see the docstring).
    return [*dict.fromkeys(s for g in out for s in _both_spellings(g))]


def _otto_state_globs():
    """Otto's own runtime state under `data/`, enumerated rather than denied wholesale.

    `data/**` would be one line and would silently disable repo-mode: every clone lives in
    `data/workspaces/`, and a matching deny beats any allow, so there is no way to carve it back
    out. The write list avoids `data/**` for exactly this reason — this mirrors its entries."""
    d = config.DATA_DIR
    return [
        os.path.join(d, "otto.db*"),          # audit trail, memory, chats, solutions, knowledge
        os.path.join(d, "*.json"),            # models.json carries endpoint API keys in plaintext
        os.path.join(d, "*.log"),             # the frozen pre-SQLite audit copies
        os.path.join(d, "transcripts", "**"),  # every other run's full tool-by-tool transcript
        os.path.join(d, "local-sessions", "**"),
        os.path.join(d, "memory", "**"),
    ]


def _reads_allowed_from(allow_cwd):
    """Is `allow_cwd` a run that legitimately reads Otto's own state?

    Yes for a run whose cwd is Otto's checkout — that is the audit-runs skill and anything else
    deliberately pointed at this service, and denying it would break the one capability whose
    entire job is reading the trail. No for a cwd UNDER `data/`, which is where repo-mode clones
    live: a run editing someone else's repo is not an Otto-introspection run, and that is the
    exact case this guard exists for."""
    if not allow_cwd:
        return False
    cwd = os.path.realpath(os.path.expanduser(allow_cwd))
    root, data = os.path.realpath(_otto_root()), os.path.realpath(config.DATA_DIR)
    if cwd == data or cwd.startswith(data + os.sep):
        return False
    return cwd == root or cwd.startswith(root + os.sep)


def read_denied_globs(allow_cwd=None):
    """Paths no run may READ, as absolute globs. See the module docstring for why the set is
    this small. Empty when the run is entitled to Otto's state (`_reads_allowed_from`)."""
    if _reads_allowed_from(allow_cwd):
        return []
    out = [*_otto_state_globs(), os.path.join(_otto_root(), ".env")]
    return [*dict.fromkeys(s for g in out for s in _both_spellings(g))]


def is_read_denied(path, allow_cwd=None):
    """Would a read of `path` be refused? Same matching as `is_denied`, against the read set —
    the local runtime needs to ask, since `claude -p`'s permission system never sees its tools."""
    return _matches_any(path, read_denied_globs(allow_cwd=allow_cwd))


def _rule(glob_path, tool="Edit"):
    """One `permissions.deny` entry. The leading extra slash is load-bearing — see the module
    docstring. Kept as the single place that knows the spelling.

    `Read(...)` denies reads and, measured against a control, covers `cat` through Bash the same
    way `Edit(...)` covers `echo x > file` — the deny is on the PATH, not on the tool that
    reaches it."""
    return f"{tool}(/{glob_path})" if glob_path.startswith("/") else f"{tool}({glob_path})"


def deny_rules(allow_cwd=None):
    return ([_rule(p) for p in denied_globs(allow_cwd=allow_cwd)]
            + [_rule(p, "Read") for p in read_denied_globs(allow_cwd=allow_cwd)])


def settings_arg(allow_cwd=None):
    """The value for `claude -p --settings`: an inline JSON string, so there is no temp file to
    manage or leave behind. Returns None when the set is empty (nothing to enforce).

    `allow_cwd` is this run's own cwd (the workflow's resolved `cwd`, whatever it is) —
    forwarded so a project capability keeps write access to its own repo; see
    `denied_globs`."""
    import json
    rules = deny_rules(allow_cwd=allow_cwd)
    if not rules:
        return None
    return json.dumps({"permissions": {"deny": rules}}, separators=(",", ":"))


def _match(path, pattern):
    """Segment-wise glob: `*` matches within ONE path segment, `**` matches any number of them.

    Not `fnmatch`, whose `*` happily crosses `/` — under it `data/*.json` also matches
    `data/workspaces/clone/pkg.json`, i.e. every repo-mode clone, which is precisely the
    over-match this module must not have. The executor's own matcher is segment-wise, so this
    has to be too or `is_denied` answers a different question than the deny rule enforces."""
    import fnmatch
    p, g = path.split(os.sep), pattern.split(os.sep)

    def go(i, j):
        while j < len(g):
            if g[j] == "**":
                return any(go(k, j + 1) for k in range(i, len(p) + 1))
            if i >= len(p) or not fnmatch.fnmatchcase(p[i], g[j]):
                return False
            i, j = i + 1, j + 1
        return i == len(p)

    return go(0, 0)


def is_denied(path, allow_cwd=None):
    """Would a write to `path` be refused? Otto never enforces with this — the executor does —
    but the local runtime, the doctor and the tests need to ask, and no caller should
    re-implement the matching. `allow_cwd` is this run's own cwd; see `denied_globs`.

    BOTH spellings of the target are tried, for the same reason `denied_globs` emits both of a
    pattern: a symlinked path resolves one way and is written the other, and a guard that only
    knows one of them is off on this platform (macOS `/tmp` and `/var` are symlinks into
    `/private`, so a run whose cwd is a temp dir defeated the local-backend guard entirely)."""
    return _matches_any(path, denied_globs(allow_cwd=allow_cwd))


def _matches_any(path, globs):
    """`path` against a glob list, trying BOTH spellings of the target — see `is_denied`."""
    expanded = os.path.expanduser(path)
    targets = {os.path.realpath(expanded), os.path.abspath(expanded)}
    for g in globs:
        gp = os.path.expanduser(g)
        if any(_match(t, gp) for t in targets):
            return True
        # A directory pattern denies everything beneath it, so `data/transcripts/**` covers the
        # directory itself as well as its contents.
        if gp.endswith(os.sep + "**") and any(_match(t, gp[:-3]) for t in targets):
            return True
    return False
