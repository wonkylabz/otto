"""First-run / environment doctor — answers "why is this install less capable than my main one?"

A fresh Otto install degrades SILENTLY: an empty ~/.claude leaves only the stock caps (so
routing lands on the general fallbacks), an unregistered repo disables repo-mode/auto-engage,
an unauthenticated `gh` breaks the board/PR features, an unreachable local model dead-ends
local execution — none of which errors at startup. The doctor makes each gap loud and
actionable. Pure environment INSPECTION — it never mutates anything.

Surfaces: `python3 doctor.py` (CLI, exit 1 on any FAIL), `GET /api/doctor` (server), and the
end of `./install.sh` (best-effort, informational).

Statuses: ok (working) | warn (feature silently degraded — the fresh-install trap) |
fail (core function broken).
"""
import os
import shutil
import subprocess

import config
import policy
import registry


def _check(name, status, detail, hint=""):
    return {"name": name, "status": status, "detail": detail, "hint": hint}


# --- individual checks (catalogue-based ones take `caps` injected, for tests) ----------------

def check_claude_cli():
    path = shutil.which("claude")
    if not path:
        return _check("claude CLI", "fail", "`claude` not found on PATH",
                      "install Claude Code and run `claude` once to log in — Otto executes "
                      "everything through `claude -p`")
    return _check("claude CLI", "ok", path)


def check_gh():
    if not shutil.which("gh"):
        return _check("gh CLI", "warn", "`gh` not found on PATH",
                      "install GitHub CLI + `gh auth login` — the board queue, repo-mode PRs, "
                      "and ticket-reading capabilities all use it")
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return _check("gh CLI", "ok", "authenticated")
        return _check("gh CLI", "warn", "installed but not authenticated",
                      "`gh auth login` — board queue / repo-mode PRs / ticket reads need it")
    except Exception as e:  # noqa: BLE001
        return _check("gh CLI", "warn", f"auth check errored: {e}", "`gh auth status` by hand")


def check_temporal():
    missing = []
    if not shutil.which("temporal"):
        missing.append("`temporal` CLI not on PATH")
    try:
        import temporalio  # noqa: F401
    except ImportError:
        missing.append("`temporalio` not importable from this interpreter")
    if missing:
        return _check("Temporal", "warn", "; ".join(missing),
                      "re-run ./install.sh (or use the direct path: `python3 server.py` — "
                      "no schedules/board/durable runs)")
    return _check("Temporal", "ok", "CLI + python SDK present")


def check_catalogue(caps):
    enabled = [c for c in caps if c.enabled]
    if not enabled:
        return _check("capability catalogue", "fail", "no enabled capabilities",
                      "check data/policy.json — even the stock caps are disabled")
    real = [c for c in enabled
            if c.source not in ("stock",) and not getattr(c, "general", False)]
    if not real:
        return _check(
            "capability catalogue", "warn",
            f"only the {len(enabled)} stock/built-in caps — no user agents, skills, plugins, or "
            "project caps discovered",
            "routing will land on the general fallbacks for everything: sync your ~/.claude "
            "(agents/skills), install plugins, or register project repos in the Admin tab")
    return _check("capability catalogue", "ok",
                  f"{len(enabled)} enabled ({len(real)} beyond the stock set)")


def _resolve(name, caps):
    c = next((c for c in caps if c.name == name), None)
    if c is None and ":" in name:                       # tolerate a kind:name config value
        bare = name.split(":", 1)[1]
        c = next((c for c in caps if c.name == bare), None)
    return c


def check_config_caps(caps):
    """The loop/fallback caps named in config must resolve in THIS install's catalogue —
    a missing one makes the QA/review loop silently report 'unavailable' on every run."""
    problems = []
    for label, name in (("QA_CAP", config.QA_CAP), ("REVIEW_CAP", config.REVIEW_CAP),
                        ("WORKER_CAP", config.WORKER_CAP)):
        c = _resolve(name, caps)
        if c is None:
            problems.append(f"{label}='{name}' not in the catalogue")
        elif not c.enabled:
            problems.append(f"{label}='{name}' is disabled")
    if problems:
        return _check("configured caps", "warn", "; ".join(problems),
                      "set OTTO_QA_CAP / OTTO_REVIEW_CAP to caps that exist here, or "
                      "re-enable them in the Admin tab — the QA/review loops silently skip "
                      "otherwise")
    return _check("configured caps", "ok",
                  f"QA={config.QA_CAP}, review={config.REVIEW_CAP}, worker={config.WORKER_CAP}")


def check_project_repos():
    repos = registry.projects()
    if not repos:
        return _check("project repos", "warn", "no repos registered",
                      "register repos in Admin → Project repos — repo-mode (isolated clone + "
                      "draft PR), auto-engage, and per-project memory only work on registered "
                      "repos")
    return _check("project repos", "ok", f"{len(repos)} registered")


def check_models(gateway):
    """Execution backend + reachability of any local model that a phase or override uses.
    `gateway` is passed in (not imported at module top) so tests can stub it."""
    cfg = gateway.load()
    entry = gateway.exec_model_entry(cfg=cfg)
    used = set((cfg.get("assign") or {}).values()) | set((cfg.get("cap_local_exec") or {}).values())
    checks = []
    for m in cfg.get("pool", []):
        if m.get("provider") == "claude" or m["name"] not in used:
            continue
        probe = gateway.test_model(m["name"])
        if not probe.get("ok"):
            checks.append(f"local model '{m['name']}' unreachable ({probe.get('detail', '')[:80]})")
    backend = f"execution backend: {entry['name']} ({'claude -p' if entry.get('provider') == 'claude' else 'local runtime'})"
    if checks:
        return _check("models", "warn", "; ".join(checks) + f" — {backend}",
                      "start the local server or reassign those phases to a reachable model "
                      "(Admin → LLM models); cheap tiers fall back to Claude, but local "
                      "EXECUTION stays local by design and will dead-end")
    return _check("models", "ok", backend)


def _probe_tool_calls(m, gateway):
    """Can this local server accept the `tools` parameter? One tiny chat completion with a
    dummy tool (max_tokens=1). Returns (ok, detail): True / False (rejected — the local agent
    runtime can never run on it) / None (couldn't tell; reachability reports that separately)."""
    import json
    import urllib.error
    import urllib.request
    body = {"model": m["model"], "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "tools": [{"type": "function", "function": {
                "name": "noop", "description": "capability probe",
                "parameters": {"type": "object", "properties": {}}}}]}
    req = urllib.request.Request(m["base_url"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers=gateway.request_headers(m))
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True, ""
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            detail = ""
        if e.code == 400 and ("--enable-auto-tool-choice" in detail or "tool" in detail.lower()):
            return False, detail
        return None, detail
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:120]


def check_exec_tool_calls(gateway):
    """A LOCAL execution model must accept tool calls or the local agent runtime can never run
    — every execution silently re-dispatches to Claude (`local_incapable`), which reads as
    'phases are local, where do my Claude credits go?' (observed: Mistral 24b on vLLM without
    the tool-call flags — weeks of invisible Claude spend). Probed here so it's loud on day one.
    Under OTTO_LOCAL_FALLBACK=0 the same misconfiguration STOPS runs instead of overspending —
    see check_local_fallback."""
    entry = gateway.exec_model_entry(cfg=gateway.load())
    if entry.get("provider") == "claude" or not entry.get("base_url"):
        return _check("exec tool calls", "ok",
                      f"execution on '{entry.get('name')}' via claude -p (tools inherent)")
    ok, detail = _probe_tool_calls(entry, gateway)
    if ok is False:
        return _check("exec tool calls", "warn",
                      f"execution model '{entry['name']}' REJECTS tool calls — every execution "
                      "silently re-dispatches to Claude (local_incapable)",
                      "start the server with tool support, e.g. vLLM: --enable-auto-tool-choice "
                      "--tool-call-parser <parser matching the model>")
    if ok is None:
        return _check("exec tool calls", "ok",
                      f"'{entry['name']}': tool support unverified ({detail or 'no response'})")
    return _check("exec tool calls", "ok", f"'{entry['name']}' accepts tool calls")


def check_data_dir():
    d = config.DATA_DIR
    if not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            return _check("data dir", "fail", f"{d} missing and uncreatable: {e}", "fix permissions")
    if not os.access(d, os.W_OK):
        return _check("data dir", "fail", f"{d} not writable", "fix permissions")
    return _check("data dir", "ok", d)


def check_estop():
    """Is the global pause engaged? The doctor's question is "why is this install doing less than
    I expect", and an engaged stop is the most total answer there is — every ingress refuses, and
    nothing else in the output would hint at why.

    Deliberately NOT a setup_wizard step, unlike other fixable gaps: a pause is a value a human
    set on purpose, and the wizard's own rule is that it never overwrites one."""
    import estop
    st = estop.status()
    if not st["engaged"]:
        return _check("estop", "ok", "not paused")
    reason = st.get("reason") or "no reason given"
    return _check("estop", "warn", f"PAUSED — no new work will start ({reason})",
                  f"release with: rm {st['path']}")


def check_local_fallback(gateway):
    """Which way the Claude-fallback flag is set, and whether it can actually bite. Reported
    because the two modes fail in OPPOSITE ways and the symptom is otherwise mystifying: with
    fallback ON a broken local endpoint spends Claude credits while every run looks fine (see
    check_exec_tool_calls); with it OFF the same endpoint stops runs dead. Knowing which mode you
    are in is the first question either symptom raises."""
    entry = gateway.exec_model_entry(cfg=gateway.load())
    local_exec = entry.get("provider") != "claude" and entry.get("base_url")
    if config.LOCAL_FALLBACK:
        return _check("local fallback", "ok",
                      "OTTO_LOCAL_FALLBACK=1 — a failing local model is covered by Claude "
                      "(work lands; local failures are quiet)",
                      "set OTTO_LOCAL_FALLBACK=0 to make local failures stop the run instead")
    return _check("local fallback", "ok",
                  "OTTO_LOCAL_FALLBACK=0 (strict) — a failing local model STOPS the run with a "
                  f"loud reason; `verify` still falls back{'' if local_exec else '. Note: execution is on Claude anyway, so strict mode only affects the local-assigned tiers'}",
                  "" if local_exec else "assign a local execution model in Admin for strict mode "
                                        "to cover execution too")


def check_optional_env():
    off = []
    if not config.secret("OTTO_EVENT_SECRET"):
        off.append("event/webhook ingress (OTTO_EVENT_SECRET)")
    if not config.secret("OTTO_NTFY_TOPIC"):
        off.append("push notifications (OTTO_NTFY_TOPIC)")
    if off:
        return _check("optional features", "ok", "disabled: " + ", ".join(off),
                      "opt-in via .env when needed")
    return _check("optional features", "ok", "event ingress + push notifications configured")


def check_secret_provider():
    """A configured helper that resolves NOTHING is the failure mode worth naming: the secrets
    stay unset, every feature they gate stays silently off, and the .env looks intentional."""
    st = config.secret_status()
    from_cmd = [n for n, s in st["secrets"].items() if s["source"] == "command"]
    if not st["command"]:
        return _check("secret provider", "ok", "none — secrets read from env/.env",
                      "set OTTO_SECRET_COMMAND in .env to resolve them from a password manager "
                      "instead (e.g. `pass show otto/{name}`)")
    if not from_cmd:
        return _check("secret provider", "warn",
                      f"OTTO_SECRET_COMMAND set ({st['command']}) but it resolved none of "
                      f"{', '.join(config.SECRET_SPECS)}",
                      "run it by hand with a real name substituted for {name} — a non-zero exit, "
                      f"empty output or a prompt past {st['timeout_s']}s all read as 'unset'")
    return _check("secret provider", "ok",
                  f"{st['command']} — resolved {len(from_cmd)}: {', '.join(from_cmd)}")


def run_checks(caps=None):
    """All checks, catalogue loaded once. Returns [{name, status, detail, hint}]."""
    if caps is None:
        caps = registry.apply_policy(registry.load(), policy.load())
    import gateway
    return [
        check_claude_cli(),
        check_gh(),
        check_temporal(),
        check_data_dir(),
        check_estop(),
        check_catalogue(caps),
        check_config_caps(caps),
        check_project_repos(),
        check_models(gateway),
        check_exec_tool_calls(gateway),
        check_local_fallback(gateway),
        check_secret_provider(),
        check_optional_env(),
    ]


def summary(checks):
    return {"fails": sum(1 for c in checks if c["status"] == "fail"),
            "warns": sum(1 for c in checks if c["status"] == "warn")}


_ICON = {"ok": "✓", "warn": "⚠", "fail": "✗"}


def main():
    checks = run_checks()
    for c in checks:
        line = f" {_ICON[c['status']]} {c['name']:22} {c['detail']}"
        print(line)
        if c["hint"] and c["status"] != "ok":
            print(f"   ↳ {c['hint']}")
    s = summary(checks)
    print(f"\n{s['fails']} problem(s), {s['warns']} warning(s)")
    return 1 if s["fails"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
