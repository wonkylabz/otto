"""Self-update: fast-forward the checkout to origin/main and restart the service.

The server only PREFLIGHTS and launches; the work runs in `apply()`, started as its own
service-manager job (`systemd-run --user`, or a one-shot LaunchAgent on macOS) so it survives
the restart it triggers. A restart that never reports the new revision is rolled back.
"""
import json
import os
import plistlib
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
ALREADY_RUNNING = "An update is already running."
_ROOT = os.path.dirname(os.path.abspath(__file__))
_WF_FILES = re.compile(r"^(workflows|wf_[a-z_]+)\.py$")
_PATH = None    # tests re-point this
_SERVICE = []   # the resolved "kind:name", cached once found


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


def _split(svc):
    """"kind:name" -> (kind, name). A bare name is a systemd unit (state written before macOS)."""
    kind, sep, name = svc.partition(":")
    return (kind, name) if sep else ("systemd", svc)


def _systemd_unit(cgroup_file="/proc/self/cgroup"):
    """The systemd unit this process runs in, or "" when not under one."""
    try:
        with open(cgroup_file) as f:
            text = f.read()
    except OSError:
        return ""
    units = [u for u in re.findall(r"/([^/\s]+\.service)(?=/|$)", text, re.M)
             if not u.startswith(("user@", "run-"))]
    return units[-1] if units else ""


def _launchctl(*args):
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=10)
    return r.returncode, r.stdout


def _ancestors():
    out, pid = set(), os.getpid()
    for _ in range(32):
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=5)
        try:
            pid = int(r.stdout.strip())
        except ValueError:
            break
        if pid <= 1:
            break
        out.add(pid)
    return out


def _launchd_job(env=None, ancestors=_ancestors):
    """The LaunchAgent label this process runs under, or "". launchd sets XPC_SERVICE_NAME, but
    a Terminal shell carries one too ("application.…", "0") — so the job's own pid must be one
    of our ancestors, the launchd analogue of reading our cgroup."""
    label = (env if env is not None else os.environ).get("XPC_SERVICE_NAME", "")
    if not label or label == "0" or label.startswith("application."):
        return ""
    try:
        code, out = _launchctl("print", f"gui/{os.getuid()}/{label}")
        m = re.search(r"^\s*pid = (\d+)", out, re.M)
        return label if not code and m and int(m.group(1)) in ancestors() else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def service():
    """"systemd:<unit>" / "launchd:<label>" serving this process, or "" (a manual `./run.sh`).
    Only a resolved answer is cached: a transient launchctl failure must not latch "unsupported"."""
    if not _SERVICE:
        if sys.platform == "darwin":
            label = _launchd_job()
            found = f"launchd:{label}" if label else ""
        else:
            unit = _systemd_unit()
            found = f"systemd:{unit}" if unit else ""
        if not found:
            return ""
        _SERVICE.append(found)
    return _SERVICE[0]


def _restart_argv(svc):
    kind, name = _split(svc)
    if kind == "launchd":
        return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{name}"]
    return ["systemctl", "--user", "restart", name]


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
    job = d.get("job") or {}
    state = job.get("state", "")
    if state == "running" and not _job_running(job):
        state = "failed"
    return {"supported": bool(service()), "behind": d.get("behind", 0), "job": state}


def _job_running(job):
    """A "running" job whose unit is gone died without recording it — it isn't running."""
    if job.get("state") != "running":
        return False
    if not job.get("pid"):
        return time.time() - job.get("started_at", 0) < 60      # apply never checked in
    if job.get("unit") and not _job_alive(job["unit"]):
        return False
    # Past the worst case (pip + two health waits) it is a dead updater, not a slow one.
    return time.time() - job.get("started_at", 0) < 1800


def changed_files(root=None):
    _, out, _ = _git("diff", "--name-only", f"HEAD...{REMOTE}/{BRANCH}", root=root)
    return [f for f in out.splitlines() if f]


def blockers(runs, root=None, svc=None):
    """Why an update must not start now. `runs` = needs-you buckets (`in_flight`, `awaiting_*`)."""
    out = []
    if not (svc if svc is not None else service()):
        out.append("Otto isn't running as a systemd or launchd service — update it by hand.")
        return out
    if _job_running(_read().get("job") or {}):
        out.append(ALREADY_RUNNING)
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


def _job_alive(unit):
    kind, name = _split(unit)
    try:
        if kind == "launchd":
            code, out = _launchctl("print", f"gui/{os.getuid()}/{name}")
            return not code and "state = running" in out
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", name],
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True     # unknown = still running; this rides every /api/health poll


def _launchd_log():
    return os.path.join(config.DATA_DIR, "logs", "updater.log")


def _job_log(unit):
    kind, name = _split(unit)
    try:
        if kind == "launchd":
            with open(_launchd_log()) as f:
                return f.read().strip()[-300:]
        r = subprocess.run(["journalctl", "--user", "-u", name, "-n", "5", "-o", "cat",
                            "--no-pager"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip()[-300:]
    except (OSError, subprocess.SubprocessError):
        return ""


def _spawn(argv):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stderr[-300:]


def _launchd_plist(label, argv, root):
    """One-shot: no KeepAlive, so launchd never re-runs it after `apply` exits."""
    return {"Label": label, "ProgramArguments": argv, "WorkingDirectory": root,
            "RunAtLoad": True, "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")},
            "StandardOutPath": _launchd_log(), "StandardErrorPath": _launchd_log()}


def _start_job(job, argv, root, previous="", spawn=_spawn):
    """Start `argv` as its own service-manager job named `job` ("kind:name")."""
    kind, name = _split(job)
    if kind != "launchd":
        return spawn(["systemd-run", "--user", "--collect", f"--unit={name}",
                      f"--working-directory={root}", *argv])
    domain = f"gui/{os.getuid()}"
    if _split(previous)[0] == "launchd":
        # An exited one-shot stays loaded; a fresh label per run means nothing waits on this.
        spawn(["launchctl", "bootout", f"{domain}/{_split(previous)[1]}"])
    os.makedirs(os.path.dirname(_launchd_log()), exist_ok=True)
    open(_launchd_log(), "w").close()
    plist = os.path.join(config.DATA_DIR, "update-job.plist")
    with open(plist, "wb") as f:
        plistlib.dump(_launchd_plist(name, argv, root), f)
    return spawn(["launchctl", "bootstrap", domain, plist])


def launch(port, svc, root=None, spawn=None, ack_s=15):
    """Start `apply` as its own job and wait for it to check in. Returns (ok, error). Starting a
    job succeeds as soon as it exists, so that exit code says nothing about whether `apply` ran."""
    root = root or _ROOT
    head = _git("rev-parse", "HEAD", root=root)[1]
    seen = _read().get("job") or {}
    stale = not _job_running(seen)
    claimed = []

    def claim(d):
        job = d.get("job") or {}
        if job.get("state") == "running" and not (stale and job.get("started_at") == seen.get("started_at")):
            return d
        claimed.append(1)
        return {**d, "job": {"state": "running", "from": head[:7], "started_at": time.time(),
                             "unit": job_name, "log": []}}
    ts = int(time.time())
    job_name = (f"launchd:com.otto.update.{ts}" if _split(svc)[0] == "launchd"
                else f"systemd:otto-update-{ts}")
    storage.mutate_json(path(), claim, {})
    if not claimed:
        return False, ALREADY_RUNNING
    code, err = _start_job(job_name, [sys.executable, os.path.join(root, "updater.py"), "apply",
                                      head, str(port), svc],
                           root, previous=seen.get("unit", ""), spawn=spawn or _spawn)
    end = time.time() + ack_s
    while not code and time.time() < end:
        job = _read().get("job") or {}
        if job.get("pid") or job.get("state") != "running":     # checked in, or already done
            return True, ""
        time.sleep(0.5)
    err = err if code else f"the updater never started: {_job_log(job_name) or 'no output'}"
    failed = {"state": "failed", "from": head[:7], "error": err, "finished_at": time.time()}
    # Conditional: an apply that finished in the gap has written its own, truer outcome.
    storage.mutate_json(path(), lambda d: {**d, "job": failed}
                        if (d.get("job") or {}).get("state") == "running" else d, {})
    return False, err


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


def _in_flight(port):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/needs-you",
                                 headers={api_auth.HEADER: api_auth.token()})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if not d.get("temporal") or d.get("error"):
        raise RuntimeError("can't confirm nothing is running")
    return len((d.get("buckets") or {}).get("in_flight", []))


class _Abort(Exception):
    """Refused before anything changed."""


def apply(old_sha, port, svc, root=None, restart=None, wait=None, busy=None, pip=None):
    """Runs OUTSIDE the service. pause → pull → pip → restart → confirm, else roll back."""
    root = root or _ROOT
    restart = restart or (lambda: subprocess.run(_restart_argv(svc), check=True, timeout=60))
    wait = wait or (lambda sha: _wait_for(port, sha, HEALTH_WAIT_S))
    busy = busy or (lambda: _in_flight(port))
    pip = pip or (lambda: _pip(root))
    log = []

    def step(msg):
        log.append(msg)
        _merge(job={**(_read().get("job") or {}), "log": log[-20:]})

    def rollback():
        _git("reset", "--hard", old_sha, root=root)
        if reqs:
            pip()
        restart()
        return "rolled_back" if wait(old_sha) else "failed"

    gave_up = []

    def check_in(d):
        job = d.get("job") or {}
        if job and job.get("state") != "running":
            gave_up.append(1)       # launch() already reported us dead — don't act behind it
            return d
        return {**d, "job": {**job, "pid": os.getpid()}}
    storage.mutate_json(path(), check_in, {})
    if gave_up:
        return "aborted"
    we_paused = not estop.engaged()
    if we_paused:
        estop.engage("updating Otto")
    state, err, new, reqs = "failed", "", old_sha, False
    try:
        # Re-checked under the pause: a run started after the server's preflight would lose
        # its attempt to the restart.
        try:
            n = busy()
        except Exception as e:  # noqa: BLE001
            raise _Abort(str(e)) from e
        if n:
            raise _Abort(f"{n} run(s) started in flight — try again when they finish")
        reqs = "requirements.txt" in changed_files(root)
        code, _, e = _git("merge", "--ff-only", f"{REMOTE}/{BRANCH}", root=root)
        if code:
            raise _Abort(f"fast-forward failed: {e[-200:]}")
        new = _git("rev-parse", "HEAD", root=root)[1]
        step(f"pulled {old_sha[:7]} → {new[:7]}")
        if reqs:
            pip()
            step("installed requirements.txt")
        restart()
        step("restarted")
        if wait(new):
            state = "done"
        else:
            step("new build never came up — rolling back")
            state = rollback()
    except _Abort as e:
        state, err = "aborted", str(e)[-300:]
    except Exception as e:  # noqa: BLE001 - recorded, never raised: nobody is listening
        err = str(e)[-300:]
        # Never leave the tree on new code under an old process: the worker re-imports
        # workflow code per task, so it would run new workflows against old activities.
        if _git("rev-parse", "HEAD", root=root)[1] != old_sha:
            step(f"{err} — rolling back")
            try:
                state = rollback()
            except Exception as e2:  # noqa: BLE001
                err = f"{err}; rollback: {e2}"[-300:]
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
