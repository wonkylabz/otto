"""Self-update: fast-forward the checkout to origin/main and restart the service.

The server only PREFLIGHTS and launches; the work runs in `apply()`, started through
`systemd-run --user` so it lives outside the service's cgroup and survives the restart it
triggers. A restart that never reports the new revision is rolled back to the old sha.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import api_auth
import config
import estop
import storage

REMOTE, BRANCH = "origin", "main"
FETCH_EVERY_S = 900
HEALTH_WAIT_S = 180
_ROOT = os.path.dirname(os.path.abspath(__file__))
_WF_FILES = re.compile(r"^(workflows|wf_[a-z_]+)\.py$")
_PATH = None    # tests re-point this


def path():
    return _PATH or os.path.join(config.DATA_DIR, "update-state.json")


def _git(*args, root=None, timeout=60):
    r = subprocess.run(["git", *args], cwd=root or _ROOT, capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _read():
    return storage.read_json(path(), {})


def _merge(**kw):
    return storage.mutate_json(path(), lambda d: {**d, **kw}, {})


def service_unit(cgroup_file="/proc/self/cgroup"):
    """The systemd unit this process runs in, or "" when not under one (`./run.sh`, macOS)."""
    try:
        with open(cgroup_file) as f:
            text = f.read()
    except OSError:
        return ""
    units = [u for u in re.findall(r"/([^/\s]+\.service)(?=/|$)", text, re.M)
             if not u.startswith(("user@", "run-"))]
    return units[-1] if units else ""


def fetch(root=None):
    """`git fetch` and cache how far behind we are. Shells out — never call it from a request
    the UI's spinner awaits."""
    code, _, err = _git("fetch", "--quiet", REMOTE, BRANCH, root=root, timeout=120)
    if code:
        return _merge(fetched_at=time.time(), fetch_error=err[-300:])
    _, log, _ = _git("log", "--format=%h%x09%s", f"HEAD..{REMOTE}/{BRANCH}", root=root)
    commits = [dict(zip(("sha", "title"), ln.split("\t", 1))) for ln in log.splitlines() if ln]
    return _merge(fetched_at=time.time(), fetch_error="", behind=len(commits), commits=commits[:50])


def summary():
    """Cheap (one file read) — rides `/api/health`."""
    d = _read()
    return {"supported": bool(service_unit()), "behind": d.get("behind", 0),
            "job": (d.get("job") or {}).get("state", "")}


def changed_files(root=None):
    _, out, _ = _git("diff", "--name-only", f"HEAD...{REMOTE}/{BRANCH}", root=root)
    return [f for f in out.splitlines() if f]


def blockers(runs, root=None, unit=None):
    """Why an update must not start now. `runs` = needs-you buckets (`in_flight`, `awaiting_*`)."""
    out = []
    if not (unit if unit is not None else service_unit()):
        out.append("Otto isn't running as a systemd service — update it by hand.")
        return out
    job = _read().get("job") or {}
    if job.get("state") == "running" and time.time() - job.get("started_at", 0) < 900:
        out.append("An update is already running.")
    _, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD", root=root)
    if branch != BRANCH:
        out.append(f"The checkout is on '{branch}', not '{BRANCH}'.")
    _, dirty, _ = _git("status", "--porcelain", "--untracked-files=no", root=root)
    if dirty:
        out.append("The checkout has uncommitted changes.")
    code, _, _ = _git("merge-base", "--is-ancestor", "HEAD", f"{REMOTE}/{BRANCH}", root=root)
    if code:
        out.append(f"HEAD has commits {REMOTE}/{BRANCH} lacks — a fast-forward isn't possible.")
    files = changed_files(root)
    if "install.sh" in files:
        out.append("install.sh changed — run ./install.sh instead.")
    if runs.get("in_flight"):
        out.append(f"{len(runs['in_flight'])} run(s) in flight — wait for them to finish.")
    parked = [*runs.get("awaiting_approval", []), *runs.get("awaiting_clarification", [])]
    if parked and any(_WF_FILES.match(os.path.basename(f)) for f in files):
        out.append(f"{len(parked)} run(s) parked at a gate would resume on changed workflow "
                   "code — decide them first.")
    return out


def launch(port, unit, root=None):
    """Start `apply` in its own transient unit. Returns (ok, error)."""
    head = _git("rev-parse", "HEAD", root=root)[1]
    _merge(job={"state": "running", "from": head[:7], "started_at": time.time(), "log": []})
    argv = ["systemd-run", "--user", "--collect", f"--unit=otto-update-{int(time.time())}",
            f"--working-directory={root or _ROOT}", sys.executable,
            os.path.join(root or _ROOT, "updater.py"), "apply", head, str(port), unit]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if r.returncode:
        _merge(job={"state": "failed", "from": head[:7], "error": r.stderr[-300:]})
        return False, r.stderr[-300:]
    return True, ""


def _health_revision(port):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health",
                                 headers={api_auth.HEADER: api_auth.token()})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r).get("revision", "")


def _wait_for(port, sha, deadline_s):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            rev = _health_revision(port)
            if rev and sha.startswith(rev):
                return True
        except Exception:  # noqa: BLE001 - down mid-restart
            pass
        time.sleep(3)
    return False


def apply(old_sha, port, unit, root=None, restart=None, wait=None):
    """Runs OUTSIDE the service. pull → pip → restart → confirm, else roll back."""
    root = root or _ROOT
    restart = restart or (lambda: subprocess.run(["systemctl", "--user", "restart", unit],
                                                  check=True, timeout=60))
    wait = wait or (lambda sha: _wait_for(port, sha, HEALTH_WAIT_S))
    log = []

    def step(msg):
        log.append(msg)
        _merge(job={**(_read().get("job") or {}), "log": log[-20:]})

    we_paused = not estop.engaged()
    if we_paused:
        estop.engage("updating Otto")
    state, err, new = "failed", "", old_sha
    try:
        reqs = "requirements.txt" in changed_files(root)
        code, _, e = _git("merge", "--ff-only", f"{REMOTE}/{BRANCH}", root=root)
        if code:
            raise RuntimeError(f"fast-forward failed: {e[-200:]}")
        new = _git("rev-parse", "HEAD", root=root)[1]
        step(f"pulled {old_sha[:7]} → {new[:7]}")
        if reqs:
            _pip(root)
            step("installed requirements.txt")
        restart()
        step("restarted")
        if wait(new):
            state = "done"
        else:
            step("new build never came up — rolling back")
            _git("reset", "--hard", old_sha, root=root)
            if reqs:
                _pip(root)
            restart()
            state = "rolled_back" if wait(old_sha) else "failed"
    except Exception as e:  # noqa: BLE001 - recorded, never raised: nobody is listening
        err = str(e)[-300:]
    finally:
        if state == "failed":
            estop.engage(f"update failed — {err or 'open Update for details'}")
        elif we_paused:
            estop.release()
        _merge(job={"state": state, "from": old_sha[:7], "to": new[:7], "error": err,
                    "finished_at": time.time(), "log": log[-20:]}, behind=0 if state == "done"
               else _read().get("behind", 0))
    return state


def _pip(root):
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "-r",
                    os.path.join(root, "requirements.txt")], check=True, timeout=600)


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "apply":
    apply(sys.argv[2], int(sys.argv[3]), sys.argv[4])
