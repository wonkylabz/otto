"""Guided first run — turns each `doctor.py` gap into an offered fix.

`install.sh` leaves a working stack that is also almost entirely unconfigured: no repos
registered, no event secret, no Slack token, no push topic. `doctor.py` already names every one
of those gaps, but naming them is where it stops — the operator is left to find the file, the
key and the format for each. This walks the same list and offers to fix what is fixable.

Runs from `./install.sh --guided` or standalone: `./.venv/bin/python setup_wizard.py`.

Three properties it must keep, because it runs at the exact moment nobody is watching closely:

  * **Non-interactive is a no-op.** install.sh is the unattended path (CI, a fresh box, a
    re-run after `git pull`). Without a TTY on stdin this prints one line and exits 0, so
    piping the installer can never block on a prompt nothing will answer.
  * **It never overwrites.** A key already set in `.env` is reported and skipped, never
    re-asked and never clobbered. Re-running offers only what is still missing — the same
    idempotence install.sh has, for the same reason: it is the recovery tool too.
  * **It never echoes a secret.** Tokens are read with getpass, `.env` is chmod 600 on every
    write, and a value already present is reported as set, never printed back.

Editing `.env` in place (rather than rewriting it) is deliberate: the file is seeded from
`.env.example`, which carries the documentation for every knob. A regenerated `.env` would be
correct and useless.
"""
import getpass
import os
import re
import secrets
import shutil
import subprocess
import sys

import config

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(ROOT, ".env")

_BOLD, _DIM, _OK, _WARN, _OFF = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"
if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
    _BOLD = _DIM = _OK = _WARN = _OFF = ""


# --- .env editing -------------------------------------------------------------------------

def env_value(key, text=None):
    """The value `key` is SET to in .env, or "". A commented-out line is not set."""
    text = env_text() if text is None else text
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    return (m.group(1).strip().strip("'\"") if m else "")


def env_text():
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def set_env(key, value):
    """Set `key` in .env without disturbing the rest of the file.

    Three cases, in order: an already-set line is REPLACED, a commented template line
    (`# OTTO_NTFY_TOPIC=`, which .env.example is mostly made of) is uncommented in place so the
    value lands under the paragraph explaining it, and otherwise the key is appended.
    """
    text = env_text()
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=", text, re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", line, text, count=1, flags=re.M)
    elif re.search(rf"^#\s*{re.escape(key)}=", text, re.M):
        text = re.sub(rf"^#\s*{re.escape(key)}=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n\n{line}\n"
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    # 600 BEFORE the rename: between os.replace and a later chmod, .env is briefly world-readable
    # under the default umask — and it holds an xoxp- token that reads and posts as the operator.
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_PATH)
    os.environ[key] = value          # so a later step in THIS run sees it (doctor re-reads env)


# --- prompting ----------------------------------------------------------------------------

def interactive():
    """A TTY on BOTH ends. stdin alone is not enough: `./install.sh --guided | tee log` leaves
    stdin a terminal while the prompts themselves go to a pipe nobody is reading."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def ask(prompt, default=""):
    try:
        got = input(f"  {prompt} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130)
    return got or default


def confirm(prompt, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    got = ask(f"{prompt} {hint}").lower()
    return default if not got else got.startswith("y")


def secret_input(prompt):
    """Read a secret without echoing it. Returns "" on an empty entry (= skip)."""
    try:
        return getpass.getpass(f"  {prompt} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130)


def _say(msg, mark=" "):
    print(f"      {mark} {msg}")


# --- steps ---------------------------------------------------------------------------------
#
# Each returns (title, status) where status is one of: "fixed", "skipped", "ok".
# A step must be safe to run when its gap is ALREADY closed — that is the re-run path.

def step_secret_provider():
    title = "secret provider"
    if config.SECRET_COMMAND:
        _say(f"already set: {config.SECRET_COMMAND}", _OK + "✓" + _OFF)
        return title, "ok"
    print(f"      Otto's secrets default to plaintext in {_BOLD}.env{_OFF}. A helper command can")
    print("      resolve them from your password manager instead — env first, helper second.")
    print(f"      {_DIM}pass:      pass show otto/{{name}}{_OFF}")
    print(f"      {_DIM}1Password: op read op://otto/{{name}}/credential{_OFF}")
    print(f"      {_DIM}Bitwarden: bw get password otto/{{name}}{_OFF}")
    if not confirm("Configure a secret provider?", default=False):
        _say("skipped — secrets stay in .env", _DIM + "·" + _OFF)
        return title, "skipped"
    cmd = ask("Helper command ({name} = the variable name, blank to skip):")
    if not cmd:
        return title, "skipped"
    set_env("OTTO_SECRET_COMMAND", cmd)
    config.secret_reset()
    resolved = [n for n in config.SECRET_SPECS if config.secret(n)]
    if resolved:
        _say(f"wrote OTTO_SECRET_COMMAND — resolves {', '.join(resolved)}", _OK + "✓" + _OFF)
    else:
        # Loud on purpose: every failure mode of the helper reads as "unset", which is
        # indistinguishable from a correctly-empty .env until a feature silently does nothing.
        _say(f"wrote it, but it resolved NOTHING yet — run it by hand with a real name "
             f"substituted for {{name}}", _WARN + "⚠" + _OFF)
    return title, "fixed"


def _generated_secret(title, key, nbytes, what):
    """Shared shape for the two secrets Otto can just generate for you."""
    if config.secret(key):
        _say("already set", _OK + "✓" + _OFF)
        return title, "ok"
    print(f"      {what}")
    if not confirm("Generate one now?"):
        _say("skipped", _DIM + "·" + _OFF)
        return title, "skipped"
    set_env(key, secrets.token_hex(nbytes))
    _say(f"wrote {key} to .env (not shown here — read it from .env)", _OK + "✓" + _OFF)
    return title, "fixed"


def step_event_secret():
    return _generated_secret(
        "event ingress", "OTTO_EVENT_SECRET", 32,
        "The webhook ingress (POST /api/events/<source>) 503s until this is set. It is the\n"
        "      HMAC-SHA256 key senders sign the body with.")


def step_ntfy_topic():
    return _generated_secret(
        "push notifications", "OTTO_NTFY_TOPIC", 12,
        "Pushes to ntfy when a run blocks on you. The topic name is the ONLY credential —\n"
        "      anyone who guesses it reads every push — so it must be unguessable, not memorable.")


def step_slack_token():
    title = "Slack auto-answer"
    if config.secret("OTTO_SLACK_USER_TOKEN"):
        _say("token already set", _OK + "✓" + _OFF)
        return title, "ok"
    print("      Otto can answer allowlisted Slack DMs/mentions AS YOU. Needs a USER token")
    print(f"      ({_BOLD}xoxp-…{_OFF}, not a bot token) with these user scopes:")
    print(f"      {_DIM}im:history im:read mpim:history channels:history groups:history{_OFF}")
    print(f"      {_DIM}chat:write users:read search:read{_OFF}")
    if not confirm("Paste a Slack user token now?", default=False):
        _say("skipped — the Events tab explains the setup when you want it",
             _DIM + "·" + _OFF)
        return title, "skipped"
    tok = secret_input("Token (not echoed; blank to skip):")
    if not tok:
        return title, "skipped"
    if not tok.startswith("xoxp-"):
        # A bot token authenticates fine and then answers as a BOT, silently defeating the
        # whole point of the feature — worth catching at paste time, not at first reply.
        if not confirm(f"That does not look like a user token (expected xoxp-…). Use it anyway?",
                       default=False):
            return title, "skipped"
    set_env("OTTO_SLACK_USER_TOKEN", tok)
    _say("wrote OTTO_SLACK_USER_TOKEN — still OFF until you enable it and set an "
         "allowlist in the Events tab", _OK + "✓" + _OFF)
    return title, "fixed"


def _is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))


def step_project_repos():
    import registry
    import repos
    title = "project repos"
    existing = registry.projects()
    if existing:
        _say(f"{len(existing)} already registered: " +
             ", ".join(os.path.basename(p) for p in existing[:5]), _OK + "✓" + _OFF)
        if not confirm("Register another?", default=False):
            return title, "ok"
    else:
        print("      Repo-mode (isolated clone → draft PR), auto-engage, per-project memory and")
        print("      the CLAUDE.md conventions digest are ALL off until a repo is registered.")
    added = 0
    while True:
        raw = ask("Repo URL, or the path to a checkout you already have (blank to finish):")
        if not raw:
            break
        info = repos.parse(raw)
        if info and not os.path.isdir(os.path.expanduser(raw)):
            _say(f"cloning {info['url']} …")
            path, err = repos.ensure(info["url"])
            if err:
                _say(f"clone failed: {err.splitlines()[-1][:120]}", _WARN + "⚠" + _OFF)
                continue
            root = registry.add_project("", info["url"])
        else:
            path = os.path.abspath(os.path.expanduser(raw))
            if not os.path.isdir(path):
                _say(f"not a repo URL and no such directory: {path}", _WARN + "⚠" + _OFF)
                continue
            if not _is_git_repo(path):
                # Not fatal — a worktree or a submodule has no .git DIRECTORY — but registering a
                # non-repo silently disables repo-mode for it, which is the gap we came here to close.
                if not confirm(f"{path} has no .git directory. Register it anyway?", default=False):
                    continue
            origin = repos.origin_of(path) or ""
            root = registry.add_project(path, origin)
        caps = _project_cap_count(root)
        _say(f"registered {_BOLD}{registry.project_namespace(root)}{_OFF} ({caps})",
             _OK + "✓" + _OFF)
        added += 1
    if added:
        return title, "fixed"
    return title, ("ok" if existing else "skipped")


def _project_cap_count(path):
    """What the operator actually gets from this repo — the number that tells them whether they
    registered the directory they meant to."""
    n = 0
    for kind in ("agents", "skills"):
        d = os.path.join(path, ".claude", kind)
        if os.path.isdir(d):
            n += len([e for e in os.listdir(d) if not e.startswith(".")])
    has_md = os.path.isfile(os.path.join(path, "CLAUDE.md"))
    bits = [f"{n} project capabilit{'y' if n == 1 else 'ies'}"]
    if has_md:
        bits.append("CLAUDE.md found — its rules bind the judge")
    return ", ".join(bits)


def step_gh_auth():
    title = "gh CLI"
    if not shutil.which("gh"):
        _say("gh not installed — the board queue, repo-mode PRs and ticket reads need it: "
             "https://cli.github.com", _WARN + "⚠" + _OFF)
        return title, "skipped"
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        r = None
    if r is not None and r.returncode == 0:
        _say("authenticated", _OK + "✓" + _OFF)
        return title, "ok"
    print("      Not authenticated — repo-mode cannot open PRs and the board queue cannot read.")
    if not confirm("Run `gh auth login` now?"):
        _say("skipped — run `gh auth login` when you want repo-mode", _DIM + "·" + _OFF)
        return title, "skipped"
    # Inherits this terminal on purpose: gh auth login is a device-code flow that needs both a
    # real TTY and the operator's browser. Capturing its output would hide the code.
    subprocess.run(["gh", "auth", "login"])
    ok = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
    _say("authenticated" if ok else "still not authenticated",
         (_OK + "✓" + _OFF) if ok else (_WARN + "⚠" + _OFF))
    return title, ("fixed" if ok else "skipped")


STEPS = [step_secret_provider, step_event_secret, step_ntfy_topic, step_slack_token,
         step_project_repos, step_gh_auth]


# --- driver ----------------------------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return 0
    if not interactive():
        print("guided setup: skipped (not a terminal). Run `./.venv/bin/python setup_wizard.py` "
              "when you have one, or `python3 doctor.py` to see what is unconfigured.")
        return 0
    if not os.path.exists(ENV_PATH):
        example = os.path.join(ROOT, ".env.example")
        if os.path.exists(example):
            shutil.copyfile(example, ENV_PATH)
            os.chmod(ENV_PATH, 0o600)

    print(f"\n{_BOLD}Otto — guided setup{_OFF}")
    print(f"{_DIM}Everything here is optional and re-runnable. Enter skips, nothing already set")
    print(f"is overwritten, and no secret is printed back.{_OFF}\n")

    results = []
    for i, step in enumerate(STEPS, 1):
        # Resolved fresh each step: an earlier step may have just written the value this one
        # checks (the secret provider, most obviously, can resolve the three that follow it).
        config.secret_reset()
        title, status = step_header(i, step)
        results.append((title, status))
        print()

    fixed = [t for t, s in results if s == "fixed"]
    skipped = [t for t, s in results if s == "skipped"]
    print(f"{_BOLD}Done.{_OFF} " +
          (f"configured: {', '.join(fixed)}. " if fixed else "nothing changed. ") +
          (f"skipped: {', '.join(skipped)}." if skipped else ""))
    if fixed:
        # .env is read by run.sh at startup and exported to both children — an edit made now is
        # invisible to a service that is already running.
        print(f"\n{_WARN}Restart Otto to pick up the .env changes:{_OFF}")
        print("  systemctl --user restart otto     # or: launchctl kickstart -k gui/$(id -u)/com.otto")
    print("\nWhat is still degraded:  ./.venv/bin/python doctor.py")
    return 0


def step_header(i, step):
    name = step.__name__.replace("step_", "").replace("_", " ")
    print(f"{_BOLD}[{i}/{len(STEPS)}] {name}{_OFF}")
    return step()


if __name__ == "__main__":
    raise SystemExit(main())
