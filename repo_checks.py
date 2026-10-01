"""The target repo's OWN verification command, run against a repo-mode PR before any LLM judge
reads it (issue #135).

Every other check a post-PR round gets is a model reading prose, so a PR that does not compile
could pass review. This is the one piece of evidence that is not stochastic: the repo's declared
suite, run in a fresh clone of the PR's head branch.

The command is DECLARED on the registration (`registry` entry `verify`), never sniffed — a
guessed build system fails open on exactly the repos that need it most. No declaration means
`skipped`, which no consumer may read as a pass.

It executes repo code as the operator, so it only ever runs under `bwrap`: read-only root,
writes confined to the clone and a scratch /tmp, the read deny-set masked
(`file_safety.read_deny_mounts`), Otto's credentials stripped from the env. No usable sandbox
means `error`, never an unconfined run.
"""
import os
import signal
import subprocess
import time

import file_safety
import mcp_client
import registry
import workspace
from ui import trace

TIMEOUT_S = float(os.environ.get("OTTO_REPO_CHECKS_TIMEOUT_S", "900"))
TAIL_CHARS = 4_000          # a failing suite says why at the END of its output
_NOT_RUNNABLE = (126, 127)  # bash: found but not executable / command not found
# A suite needs no cloud or forge credential, and the command is settable over the API.
_CRED_DIRS = (".aws", ".config/gh", ".kube", ".docker", ".azure", ".config/gcloud")


def declared(repo):
    """The verification command registered for `repo`, or "" when none is declared."""
    r = workspace.resolve(repo)
    return (registry.project_meta(r["path"]).get("verify") or "").strip() if r else ""


def _tail(text):
    """The last TAIL_CHARS of the output, with a marker when the head was cut."""
    text = (text or "").strip()
    if len(text) <= TAIL_CHARS:
        return text
    return (f"[…first {len(text) - TAIL_CHARS} of {len(text)} characters cut — only the end "
            "of the output is shown]\n" + text[-TAIL_CHARS:])


def sandbox_argv(path, command):
    """bwrap argv confining `command` to writes under `path` (and a scratch /tmp)."""
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp"] + file_safety.read_deny_mounts(path)
    for d in _CRED_DIRS:
        cred = os.path.join(os.path.expanduser("~"), d)
        if os.path.isdir(cred):
            argv += ["--tmpfs", cred]
    # After the masks: a clone under /tmp, or under a masked directory, must stay reachable.
    argv += ["--bind", path, path, "--chdir", path,
             "--setenv", "XDG_CACHE_HOME", "/tmp/.cache",
             "--unshare-pid", "--die-with-parent", "bash", "-lc", command]
    return argv


def _execute(path, command, timeout):
    """(state, exit, output) for one sandboxed run of `command` in `path`."""
    try:
        proc = subprocess.Popen(sandbox_argv(path, command), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                env=mcp_client._inherited_env(), start_new_session=True)
    except OSError as e:
        return "error", None, f"could not start: {e}"
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return "error", None, (out or "") + f"\n[timed out after {int(timeout)}s]"
    if proc.returncode in _NOT_RUNNABLE:
        return "error", proc.returncode, out
    return ("pass" if proc.returncode == 0 else "fail"), proc.returncode, out


def run(repo, pr_url, run_id, timeout=None):
    """Run `repo`'s declared verification against `pr_url`'s head branch, in a throwaway clone.

    Returns {state, command, exit, tail, duration_s, reason}, state one of:
    pass | fail (non-zero exit) | error (could not be run: no sandbox, no clone, command
    missing, timeout) | skipped (nothing declared)."""
    command = declared(repo)
    res = {"state": "skipped", "command": command, "exit": None, "tail": "", "duration_s": 0,
           "reason": ""}
    if not command:
        res["reason"] = "no verification command is declared for this repo"
        return res
    if not file_safety.sandbox_available():
        res.update(state="error", reason="no usable bwrap sandbox on this machine — the repo's "
                                         "own code is never run unconfined")
        return res
    branch = workspace.pr_branch(repo, pr_url)
    if not branch:
        res.update(state="error", reason="the PR's head branch could not be resolved")
        return res
    t0 = time.monotonic()
    try:
        ws = workspace.provision(repo, run_id, from_branch=True, branch=branch)
    except ValueError as e:
        res.update(state="error", reason=f"the PR's branch could not be checked out: {e}")
        return res
    try:
        state, code, out = _execute(ws["path"], command, timeout or TIMEOUT_S)
    finally:
        workspace.cleanup(run_id)
    res.update(state=state, exit=code, tail=_tail(out),
               duration_s=round(time.monotonic() - t0, 1))
    if state == "error":
        res["reason"] = ("the command could not be run" if code in _NOT_RUNNABLE
                         else "the command did not finish")
    trace("CHECKS", f"{repo}: {state} (exit {code}) in {res['duration_s']}s")
    return res


def judge_note(res):
    """The FACT a passing or unrunnable check contributes to the judge's prompt ("" for none).
    A pass narrows what is left to decide; it never licenses a PASS on its own."""
    state = (res or {}).get("state")
    if state == "pass":
        return (f"Deterministic evidence: the repo's own declared verification "
                f"(`{res['command']}`) PASSED against this PR's head. That says the suite is "
                "green, NOT that the request was answered — still judge the findings.")
    if state == "error":
        return (f"The repo's declared verification (`{res.get('command')}`) could NOT be run: "
                f"{res.get('reason')}. Treat it as unknown — neither passing nor failing.")
    return ""


def critique(res):
    """The fix round's instruction when the declared suite FAILED."""
    return (f"The repo's own declared verification (`{res['command']}`) FAILED against this "
            f"PR's head (exit {res['exit']}). Make it pass. The end of its output:\n\n"
            f"{res['tail']}")
