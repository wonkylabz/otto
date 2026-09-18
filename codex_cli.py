"""Thin wrapper around headless OpenAI Codex (`codex exec`). Used INSIDE Temporal activities
(never inside workflow code — subprocess calls are non-deterministic).

The THIRD execution backend (issue #115), beside `claude_cli` (`claude -p`) and `local_runtime`
(a hand-rolled tool loop over an OpenAI-compatible endpoint). It is its own runtime rather than
a mode of the local one for a measured reason: codex-cli 0.155.0 removed `wire_api = "chat"`, so
Codex speaks ONLY the Responses API and cannot be driven through `/chat/completions` at all.

`run_json` returns the same dict shape `claude_cli.run_json` does — result / is_error /
total_cost_usd / usage / session_id / tools_used / tools_failed — because `engine.run_attempt`,
the verify ladder, the audit row and every test double read that contract and must not learn a
second one.

Everything below that is not obvious from the docs was measured against codex-cli 0.155.0 on
2026-09-18; each measurement is recorded at the line it constrains.
"""
import json
import os
import subprocess
import tempfile
import threading
import time

import claude_cli
import config
import error_classifier
import file_safety
from ui import trace

# `claude_cli` owns the transcript directory, the credential scrub, the process-group kill and
# the TTL sweep. They are imported, never re-implemented: a transcript records what a run did
# INCLUDING the times it handled a credential, and two scrubbers drift (see
# claude_cli.transcript_line — it is THE one writer for every backend).
#
# FUNCTIONS may be aliased; the DIRECTORY may not. A module-level `TRANSCRIPTS =
# claude_cli.TRANSCRIPTS` binds the string at import, and `test_support.redirect_live_state`
# re-points the ORIGINAL through `sys.modules` — so the copy kept pointing at the real `data/`
# and the suite wrote into the running service's transcript directory (this checkout IS the live
# service's cwd). The functions below are immune: each reads `claude_cli.TRANSCRIPTS` when it is
# called, which is the only thing that survives a re-point.
transcript_path = claude_cli.transcript_path
plan_transcript_path = claude_cli.plan_transcript_path
keep_walled_transcript = claude_cli.keep_walled_transcript

_SESSION_PREFIX = "codex-"

# Which binary to spawn. An absolute path is sometimes REQUIRED, not a convenience: a
# version-manager shim (asdf, mise, nvm) resolves its version from the CURRENT DIRECTORY, and
# Otto runs every capability from a cwd of its own — an isolated clone, a scratch dir, a
# registered repo. Measured here: `codex --version` works from the checkout and fails from a
# workspace with "No version is set for command codex", which arrives as the run's entire
# result. `claude` never hit this only because it is not installed through a shim.
CODEX_BIN = os.environ.get("OTTO_CODEX_BIN") or "codex"

# Codex's sandbox policy. `read-only` is what reproduces `claude -p --permission-mode plan`:
# measured, a run asked to create a file under it emitted no file and reported the refusal
# itself ("read-only file system"), so this is real enforcement rather than a prompt-level ask.
PLAN_SANDBOX = "read-only"
WRITE_SANDBOX = "workspace-write"

# Codex keeps its session files and app-server state under `$CODEX_HOME`. Pointed at Otto's own
# `data/` rather than `~/.codex` for two reasons: a run confined to a read-only root cannot write
# the operator's home ("failed to initialize in-process app-server client: Read-only file
# system"), and a resume needs those files to survive, so they cannot live in the throwaway
# workspace either. Beside `local-sessions/`, and deliberately not matched by
# `file_safety._otto_state_globs` (`data/*.json` is direct children only) — masking it would
# break resume.
CODEX_HOME = os.path.join(config.DATA_DIR, "codex-home")

# Run Codex even when the kernel-level read guard is unavailable. OFF by default: Codex's own
# sandbox cannot express a read deny at all, and the gap is not theoretical — see `confinement`
# for the canary measurement. Env-only, never a runtime setting, for the same reason
# `OTTO_SECRET_COMMAND` is: this one decides whether a run can read the plaintext key store, and
# the settings API is unauthenticated.
ALLOW_UNGUARDED = os.environ.get("OTTO_CODEX_ALLOW_UNGUARDED", "") == "1"

UNGUARDED_MESSAGE = (
    "\u26d4 **Stopped — nothing ran.** The Codex backend needs a working `bwrap` to enforce "
    "Otto's read deny-set: its own sandbox confines writes but cannot deny a read, so a run "
    "could read `data/models.json` (endpoint API keys in plaintext), `otto.db` and every other "
    "run's transcripts.\n\nInstall `bubblewrap` and enable unprivileged user namespaces on the "
    "machine running the worker, or choose a different execution model. Set "
    "`OTTO_CODEX_ALLOW_UNGUARDED=1` only if that exposure is acceptable to you.")


def is_codex_session(sid):
    """True when a session id was minted by this runtime. The resume half of
    `gateway.backend_of`: a session is bound for life to the runtime that minted it, and
    `claude -p --resume codex-…` is rejected outright."""
    return bool(sid) and str(sid).startswith(_SESSION_PREFIX)


def thread_id(sid):
    """The bare Codex thread uuid inside one of our session ids. `codex exec resume` takes the
    uuid, so the prefix that routes the resume back here has to come off first."""
    return str(sid)[len(_SESSION_PREFIX):] if is_codex_session(sid) else str(sid or "")


def codex_env():
    """The environment every `codex` subprocess is spawned with.

    NOT `os.environ`, for the reason `claude_cli.child_env` documents: `run.sh` exports the
    repo's gitignored `.env` into the worker, and Codex launches MCP servers of its own, so
    whatever the CLI inherits third-party code inherits too. One strip list for all three
    backends — it lives in `mcp_client` with its twins."""
    import mcp_client            # noqa: PLC0415 — deferred, mcp_client imports claude_cli
    return mcp_client.claude_env()


# --- the wire ---------------------------------------------------------------------------
#
# MEASURED, and none of it is in the docs:
#
#   stdout is clean JSONL; stderr is NON-JSON tracing (`ERROR codex_api::endpoint::
#   responses_websocket: ...`) and must never reach the parser.
#
#   {"type":"thread.started","thread_id":"<uuid>"}                      <- the session id
#   {"type":"turn.started"}
#   {"type":"item.started","item":{...}}
#   {"type":"item.completed","item":{"type":"command_execution","command":...,
#                                    "aggregated_output":...,"exit_code":0,"status":...}}
#   {"type":"item.completed","item":{"type":"agent_message","text":"..."}}   <- the answer
#   {"type":"item.completed","item":{"type":"error","message":"..."}}
#   {"type":"turn.completed","usage":{"input_tokens":...,"output_tokens":...}}
#   {"type":"error","message":"..."}
#
# A turn emits SEVERAL `agent_message` items (the model narrates between tool calls), so the
# answer is the LAST one — a first-match parse returns "I'll create that file now.".
_RESULT_ITEM = "agent_message"

# What Codex calls its tools -> what Otto calls them. The judge is handed this list as the
# turn's real grant (`judging.verify`), and every other name in that prompt is a Claude tool
# name, so a raw "command_execution" reads as a tool the judge has never heard of.
_TOOL_NAMES = {
    "command_execution": "Bash",
    "file_change": "Edit",
    "patch_apply": "Edit",
    "web_search": "WebSearch",
    "mcp_tool_call": "mcp",
}

# A wall is "this will fail the same way every time", so it latches the ladder off this backend
# instead of spending two more attempts reaching the identical refusal. Matched on the CLI's own
# words because that is the only thing it reliably reports — see `_exit_code_is_not_a_verdict`.
# A credential is wrong, a quota is spent or a model does not exist no matter WHERE the CLI
# says so, so these are matched over the events and stderr alike.
_WALLS = (
    ("auth", ("401 unauthorized", "missing bearer", "invalid api key", "not logged in",
              "unauthorized", "please run `codex login`")),
    ("quota", ("insufficient_quota", "exceeded your current quota", "billing hard limit")),
    ("bad_model", ("model_not_found", "does not exist or you do not have access",
                   "unknown model")),
)

# "The endpoint is down" is a claim about RIGHT NOW, and codex's stderr is per-retry tracing —
# it logs a line for every attempt, including ones it then recovered from. Matched over the
# `error` EVENTS only: an event is the CLI reporting the turn's outcome, a stderr line is it
# narrating one retry. Read off stderr, a genuinely timed-out turn that happened to log one
# blip classified as a deterministic wall and latched the whole ladder off this backend.
_TRANSIENT_WALLS = (
    ("overloaded", ("503 service unavailable", "502 bad gateway", "connection refused",
                    "dns error", "failed to lookup address")),
)


def wall_reason(events, stderr=""):
    """Which deterministic wall this run died on, or None. Returned as a plain string so it
    crosses the activity-result boundary as JSON, the same shape `error_classifier.claude_wall`
    uses. See `_TRANSIENT_WALLS` for why the two inputs are separate."""
    both = ((events or "") + "\n" + (stderr or "")).lower()
    for reason, markers in _WALLS:
        if any(m in both for m in markers):
            return reason
    ev = (events or "").lower()
    for reason, markers in _TRANSIENT_WALLS:
        if any(m in ev for m in markers):
            return reason
    return None


def _note_tool(item, worked, failed):
    """Record one completed item as a tool that RETURNED or one that FAILED.

    Same contract as `claude_cli._note_tools`: "was called" is not "was available", and
    crediting a refused tool as present turns a truthful "that was blocked" into what the judge
    reads as an invented excuse. Best-effort and total — an unrecognised shape adds nothing and
    never raises, because a transcript detail must not be able to break a run."""
    try:
        name = _TOOL_NAMES.get(item.get("type"))
        if not name:
            return
        code, status = item.get("exit_code"), (item.get("status") or "")
        bad = (code not in (0, None)) or status in ("failed", "aborted")
        (failed if bad else worked).add(name)
    except Exception:  # noqa: BLE001 - observability must never break the stream loop
        pass


def _toml_value(v):
    """One Python value as the TOML literal `-c key=value` expects.

    Codex parses the value half as TOML and falls back to "raw string" when that fails — so a
    wrong quoting never errors, it silently configures something else. Measured both ways: an
    unquoted `model=owner/Name-30B` is taken as a literal that nothing serves, and a
    JSON-quoted inline table arrives as the STRING `{name="vllm",...}` and is rejected with
    `invalid type: string`. Hence a real serializer rather than `json.dumps` — which agrees
    with TOML for strings and numbers, and disagrees for exactly the two shapes a provider and
    an MCP server need (inline tables, and `true`/`false`)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + "}"
    return json.dumps(str(v))          # TOML basic string — same escaping as JSON


def _config_args(overrides):
    """`-c key=value` pairs, values serialized as TOML (see `_toml_value`)."""
    out = []
    for key, value in (overrides or {}).items():
        out += ["-c", f"{key}={_toml_value(value)}"]
    return out


def confinement(sandbox, cwd):
    """How this turn is confined: `("bwrap", argv_prefix)` or `("codex", [])`.

    MEASURED, and it decides the whole shape of the call:

    * Codex's own sandbox is a WRITE allowlist and nothing else. `-s workspace-write` genuinely
      refused a write under `$HOME` (the model wrote into the cwd instead and reported success —
      which is why this is measured against the filesystem, never read off the model's answer).
      But READS are unrestricted in every mode: under `-s read-only` a run read
      `data/models.json`, the endpoint API keys in plaintext, straight off disk. Probed against
      `--strict-config`, there is no knob for it — `sandbox_read_only.*` and `sandbox_deny_read`
      are both rejected as unknown fields.
    * Nesting the two sandboxes does not work. Inside `bwrap`, Codex's own confinement cannot
      initialise and EVERY shell command fails, not just the denied ones — the turn degenerates
      into the model narrating its way around a broken environment.

    So they are alternatives, not layers, and `bwrap` is preferred: it enforces the writes AND
    the read deny-set that Codex cannot express. Codex then runs with
    `--dangerously-bypass-approvals-and-sandbox`, which is exactly what that flag is for
    ("intended solely for running in environments that are externally sandboxed").

    Without a usable `bwrap` (no binary, or userns disabled — macOS) the only other option is
    Codex's own sandbox, where the write guard holds and the READ deny-set does not. Measured
    with a control, which is the whole reason this refuses by default: a unique canary written
    into a read-denied `data/*.json` appeared ZERO times in the transcript under `bwrap` and
    TWICE under the fallback. `run_json` therefore WALLS instead of running unguarded, unless
    `OTTO_CODEX_ALLOW_UNGUARDED=1` says the operator has decided otherwise."""
    if not file_safety.sandbox_available():
        return "codex", []
    # Read-only root + the deny-set masks, with exactly two writable holes: this turn's
    # workspace, and Codex's own state. `--tmpfs /tmp` gives the throwaway scratch every shell
    # expects; it also MASKS a cwd that lives under /tmp, so such a cwd is re-bound after it
    # (later mounts win) — the same trap `local_runtime._bwrap_argv` documents.
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--tmpfs", "/tmp", "--die-with-parent"]
    argv += file_safety.read_deny_mounts(cwd)
    argv += ["--bind", CODEX_HOME, CODEX_HOME, "--setenv", "CODEX_HOME", CODEX_HOME]
    if cwd:
        real = os.path.realpath(cwd)
        # A read-only turn gets a read-only workspace: the sandbox mode is the grant, and
        # binding the tree writable for a plan pass would hand it back what plan mode removes.
        argv += ["--ro-bind" if sandbox == PLAN_SANDBOX else "--bind", real, real,
                 "--chdir", real]
    return "bwrap", argv


def build_cmd(prompt, *, model=None, resume_session=None, sandbox=None, cwd=None,
              last_message=None, config_overrides=None, effort=None, external_sandbox=False):
    """The argv for one `codex exec` turn. Split out so the flag contract is testable without
    spawning anything — three of these flags were chosen against a measured refusal.

    RESUME TAKES A DIFFERENT SUBCOMMAND AND A SMALLER FLAG SET. Measured: `codex exec resume`
    rejects BOTH `-s/--sandbox` and `-C/--cd` ("unexpected argument"), so a resumed turn could
    neither re-declare its sandbox nor be re-pointed at a workspace. Both therefore travel the
    way that works on BOTH paths — the sandbox as `-c sandbox_mode=…` (accepted on resume, and
    proven to still enforce: a write under it was refused), the cwd as the SUBPROCESS's cwd.
    One spelling per fact, or the write guard silently lapses on turn 2."""
    cmd = [CODEX_BIN, "exec"]
    if resume_session:
        cmd.append("resume")
    cmd.append("--json")
    # Otto runs capabilities in isolated clones and in bare scratch dirs; refusing to start
    # outside a git repo would make the backend unusable for every non-repo run.
    cmd.append("--skip-git-repo-check")
    # `~/.codex/config.toml` is the operator's own file. An unanchored run must not inherit it,
    # for the reason `--setting-sources user` exists on the Claude path: whatever the WORKER's
    # environment happens to carry is not this run's configuration.
    cmd.append("--ignore-user-config")
    if external_sandbox:
        # `bwrap` is the confinement, and the two cannot be nested (see `confinement`). This is
        # the flag's documented purpose; it is passed ONLY on the branch that has already
        # established a kernel-level sandbox around this process.
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    overrides = dict(config_overrides or {})
    if not external_sandbox:
        overrides["sandbox_mode"] = sandbox or PLAN_SANDBOX
    if model:
        overrides["model"] = model
    # Advisory on this backend exactly as it is on the local one: normalized so an unknown value
    # cannot mean "ran at the default effort while every layer reports the pick honoured".
    effort = config.effort_level(effort)
    if effort:
        overrides["model_reasoning_effort"] = effort
    cmd += _config_args(overrides)
    if last_message:
        # The final answer written straight to a file. Preferred over reconstructing it from the
        # stream because a turn emits SEVERAL agent_message items and only the last is the
        # answer; the stream stays the fallback for a turn that dies before writing it.
        cmd += ["-o", last_message]
    if not resume_session and cwd and not external_sandbox:
        # Under bwrap the cwd is `--chdir`'d by the wrapper and the workspace is the only
        # writable bind, so `--cd` would be a second, weaker statement of the same fact.
        cmd += ["--cd", cwd]
    # `--` first: a prompt beginning with a dash is otherwise parsed as a flag, and on the
    # resume path the session id and the prompt are two positionals that must not be reordered.
    cmd.append("--")
    if resume_session:
        cmd.append(thread_id(resume_session))
    cmd.append(prompt)
    return cmd


def run_json(prompt, allowed_tools=None, model=None, timeout=None, resume_session=None,
             system_context=None, cwd=None, transcript=None, on_event=None, abort=None,
             meta=None, permission_mode=None, effort=None, steer=None, model_entry=None,
             config_overrides=None):
    """One headless `codex exec` turn, in `claude_cli.run_json`'s return contract.

    `allowed_tools` is accepted and NOT forwarded: Codex has no per-tool permission flag, so the
    grant is the SANDBOX POLICY, never a tool list. It is still read — an empty-of-write-tools
    grant selects the read-only sandbox — so the argument is load-bearing, just not as argv.

    `file_safety`'s deny set is enforced by `confinement` instead — by the KERNEL, not by the
    CLI. `claude -p` gets it via `permissions.deny`; Codex has no equivalent and cannot express
    a read deny at all, so a `bwrap` around the process is what holds it.

    `steer` is accepted and cannot be delivered: `codex exec` takes its prompt once, from argv
    or stdin, and has no mid-turn user-message channel. It is recorded in the transcript and
    reported back through `steer_unsupported` rather than silently dropped, so the supervisor's
    steer budget is not spent on a channel that does not exist."""
    timeout = config.LOCAL_RUN_TIMEOUT_S if timeout is None else timeout
    sandbox = PLAN_SANDBOX if permission_mode == "plan" else _sandbox_for(allowed_tools)
    os.makedirs(CODEX_HOME, exist_ok=True)
    guard, prefix = confinement(sandbox, cwd)
    if guard != "bwrap" and not ALLOW_UNGUARDED:
        # A WALL, not a failure: it fails the same way every attempt, so the ladder must
        # re-dispatch rather than spend two more rungs reaching the identical refusal.
        return {"result": UNGUARDED_MESSAGE, "is_error": True, "total_cost_usd": 0,
                "usage": {}, "session_id": None, "guard": guard, "read_guard": False,
                "wall_reason": "read_guard_unavailable", "wall_detail": UNGUARDED_MESSAGE,
                "tools_used": [], "tools_failed": []}
    # `-o` is written by CODEX, so it passes through neither `transcript_line`'s scrubber nor
    # `claude_cli.gc_transcripts` (which only unlinks `*.jsonl`) — an answer quoting a
    # credential would sit on disk in plaintext, unscrubbed, forever. It is therefore a
    # throwaway: unique per CALL and deleted in `finally`, whatever happens. Unique per call and
    # not per PROCESS because the Temporal worker runs activities concurrently in one process
    # and the cheap tiers pass no transcript at all — a pid-named path let two live turns write
    # and unlink the same file, so one run could read the other's final message as its own.
    #
    # Under `CODEX_HOME`, NOT `TRANSCRIPTS`: `data/transcripts/**` is read-denied, so the
    # sandbox masks it with an empty tmpfs — the child would write its answer into the mask and
    # the parent would read the real, still-empty path, silently losing `-o` and falling back to
    # the stream on every confined run. `CODEX_HOME` is the one directory both sides share, and
    # unlike a copy of another module's constant it follows `redirect_live_state` (it is this
    # module's own store, and is listed in `test_support._DATA_STORES`).
    fd, last_path = tempfile.mkstemp(prefix="codex-last-", suffix=".txt", dir=CODEX_HOME)
    os.close(fd)
    # The prompt and everything Otto tells the run that is NOT the request travel together here:
    # `codex exec` has no `--append-system-prompt`, so `system_context` has to be part of the
    # one prompt it accepts. It is still recorded SEPARATELY in the meta line below — a
    # transcript that cannot say what the model was told cannot be debugged.
    full_prompt = f"{system_context}\n\n{prompt}" if system_context else prompt
    cmd = prefix + build_cmd(full_prompt, model=model, resume_session=resume_session,
                             sandbox=sandbox, cwd=cwd, last_message=last_path, effort=effort,
                             config_overrides=config_overrides,
                             external_sandbox=(guard == "bwrap"))
    trace("CODEX", f"{'resume ' if resume_session else ''}sandbox={sandbox} guard={guard} "
                   f"model={model}")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                # The cwd is the PROCESS's on the resume path (`--cd` is refused
                                # there) and both on the fresh one, so a resumed turn lands in
                                # the same tree either way.
                                cwd=cwd, start_new_session=True,
                                env=dict(codex_env(), CODEX_HOME=CODEX_HOME))
    except OSError as e:
        # The binary is missing or not executable — the whole reason `OTTO_CODEX_BIN` exists.
        # A WALL, because it fails identically every attempt, and an error DICT, because this
        # module's contract is "an error dict, never a raise": left to propagate, it reached the
        # activity as an unclassified crash. The temp file is dropped on the way out — nothing
        # else ever would, since `gc_transcripts` only unlinks `*.jsonl`.
        _unlink(last_path)
        detail = f"{CODEX_BIN}: {e}"
        return {"result": error_classifier.codex_wall_message("cli_missing", detail),
                "is_error": True, "total_cost_usd": 0, "usage": {}, "session_id": None,
                "guard": guard, "read_guard": guard == "bwrap",
                "wall_reason": "cli_missing", "wall_detail": detail[:2000],
                "tools_used": [], "tools_failed": []}
    sink = None
    if transcript:
        claude_cli.gc_transcripts()
        os.makedirs(os.path.dirname(transcript), exist_ok=True)
        open(transcript, "w").close()
        sink = open(transcript, "a")
        sink.write(claude_cli.transcript_line({
            "type": "otto-meta", "backend": "codex", "prompt": prompt, "model": model,
            "system_context": system_context, "effort": config.effort_level(effort),
            "sandbox": sandbox, "cwd": cwd, "at": time.time(),
            # WHICH guard served this turn. The fallback enforces the writes and NOT the read
            # deny-set, so a transcript that cannot say which one ran cannot answer whether
            # this run could have read the key store.
            "guard": guard, "read_guard": guard == "bwrap",
            "argv": [a for a in cmd if a != full_prompt],
            "supervised": on_event is not None, **(meta or {})}))
        if steer is not None:
            sink.write(claude_cli.transcript_line({
                "type": "otto-steer-unsupported", "at": time.time(),
                "text": "codex exec has no mid-turn user-message channel"}))
        sink.flush()

    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        claude_cli.kill_tree(proc)

    stderr_buf = []
    threading.Thread(target=claude_cli._drain, args=(proc.stderr, stderr_buf),
                     daemon=True).start()
    watchdog = threading.Timer(timeout, _kill)
    watchdog.start()
    if abort is not None:
        def _abort_watch():
            while proc.poll() is None:
                if abort.wait(0.5):
                    claude_cli.kill_tree(proc)
                    return
        threading.Thread(target=_abort_watch, daemon=True).start()

    session, answer, usage, errors = None, None, {}, []
    # `turn.completed` is THE end-of-turn marker, and the only one — this backend has no single
    # terminal `result` event like `claude -p`. Measured: a turn killed at 3s had already
    # emitted an `agent_message` carrying the model's opening narration ("I'll help you
    # summarise the history of…"), so treating "we have an agent_message" as "we have an
    # answer" reported a timed-out attempt as a SUCCESS whose result was a sentence of preamble.
    # A turn is finished when it says it is finished.
    turn_done = False
    worked, failed = set(), set()
    try:
        for line in proc.stdout:
            if sink:
                sink.write(claude_cli.transcript_line(line))
                sink.flush()                     # live tailers (chat progress) see it now
            try:
                event = json.loads(line)
            except ValueError:
                continue                         # a stray non-JSON line is not a turn
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 - a watcher must never break the run
                    pass
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "thread.started" and event.get("thread_id"):
                session = _SESSION_PREFIX + str(event["thread_id"])
            elif kind == "turn.completed":
                turn_done = True
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
            elif kind == "turn.failed":
                turn_done = True
                errors.append(json.dumps(event.get("error") or event)[:2000])
            elif kind == "error" and event.get("message"):
                errors.append(str(event["message"]))
            elif kind == "item.completed" and isinstance(event.get("item"), dict):
                item = event["item"]
                if item.get("type") == _RESULT_ITEM and item.get("text"):
                    answer = str(item["text"])   # LAST one wins — the turn narrates as it goes
                elif item.get("type") == "error" and item.get("message"):
                    errors.append(str(item["message"]))
                _note_tool(item, worked, failed)
        proc.wait()
    finally:
        watchdog.cancel()
        stderr = (stderr_buf[0] if stderr_buf else "").strip()
        if sink:
            if stderr:
                sink.write(claude_cli.transcript_line({"type": "stderr",
                                                       "text": stderr[:20_000]}))
            if timed_out.is_set():
                sink.write(claude_cli.transcript_line(
                    {"type": "otto-timeout", "after_s": timeout,
                     "after_result": answer is not None}))
            sink.close()
        # `-o` is the primary answer and the stream the fallback: the file holds exactly the
        # final message, while the stream needs the last-wins rule above to get there. Both are
        # only an ANSWER at all once the turn declared itself finished.
        #
        # Read and DROPPED here, in the same `finally`, because the file is Codex's own output:
        # it never passes `transcript_line`'s scrubber, so an answer quoting a credential would
        # otherwise sit on disk in plaintext — and `claude_cli.gc_transcripts` only unlinks
        # `*.jsonl`, so nothing would ever collect it. The scrubbed copy in the transcript is
        # the durable record; this one exists for the length of one call.
        try:
            answer = (_read_last_message(last_path) or answer) if turn_done else None
        finally:
            _unlink(last_path)

    out = {"result": answer or "", "is_error": False, "total_cost_usd": 0,
           "usage": usage, "session_id": session, "guard": guard,
           "read_guard": guard == "bwrap",
           "tools_used": sorted(worked), "tools_failed": sorted(failed - worked)}
    if steer is not None:
        out["steer_unsupported"] = True
    if abort is not None and abort.is_set():
        return dict(out, result=f"(aborted by supervisor: {abort.reason})"[:400],
                    is_error=True, aborted=True)
    # THE EXIT CODE IS NOT A VERDICT. Measured: a run that failed to authenticate at all — ten
    # `error` events, five over WebSocket and five more after falling back to HTTPS — exited
    # **0**, while a resume against the same dead credentials exited 1. `claude -p`'s contract
    # (a `result` event, else the exit status) therefore does not transfer: the wall has to be
    # read out of the stream, and a run with no answer is a failure whatever the status says.
    # The wall is classified BEFORE the timeout branch, deliberately. A dead credential is not
    # fast: measured, the CLI retries 5 times over WebSocket and 5 more after falling back to
    # HTTPS, which took 15s on one run and outlived a 180s watchdog on another. Read as a
    # timeout, that wall becomes a harness death — it draws on `max_harness_retries` and the
    # ladder never latches off this backend, so every later rung reaches the same refusal.
    # Whatever error events DID arrive before the kill still name the reason.
    reason = wall_reason("\n".join(errors), stderr)
    if reason and not answer:
        detail = (errors[-1] if errors else stderr)
        return dict(out, is_error=True, wall_reason=reason, wall_detail=detail[:2000],
                    # The CODEX remedy, never the generic endpoint one: this backend has no
                    # `api_key_env` and no `OTTO_SECRET_COMMAND` entry to go and check.
                    result=error_classifier.codex_wall_message(reason, detail))
    if timed_out.is_set() and not turn_done:
        return dict(out, result="(timed out)", is_error=True)
    if answer:
        if timed_out.is_set():
            out["late_exit"] = True
        return out
    # No answer and nothing we recognise — same contract as every other backend: an error dict,
    # never a raise.
    return dict(out, is_error=True,
                result=("\n".join(errors) or stderr or "(no output from codex exec)")[:4000])


def _sandbox_for(allowed_tools):
    """Read-only unless the grant actually contains a write tool.

    The sandbox IS the tool grant on this backend — there is no `--allowedTools` — so a read cap
    must not be handed a writable workspace just because nothing said otherwise. Fails CLOSED:
    an unrecognised or empty grant is read-only."""
    names = {str(t).split("(")[0].strip() for t in (allowed_tools or [])}
    # DERIVED from the two lists, never a third constant: `config.WRITE_TOOLS` is
    # `READ_TOOLS` plus the mutating ones, so a tool added to either list stays in sync here
    # instead of quietly leaving a read cap with a writable workspace.
    write_only = set(config.WRITE_TOOLS) - set(config.READ_TOOLS)
    return WRITE_SANDBOX if names & write_only else PLAN_SANDBOX


def _read_last_message(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def available():
    """Is `codex` on PATH and runnable? Used by `doctor` — a configured Codex entry with no CLI
    behind it is a day-one misconfiguration that otherwise surfaces as a failed run."""
    try:
        out = subprocess.run([CODEX_BIN, "--version"], capture_output=True, text=True,
                             timeout=10, env=codex_env())
        return (out.returncode == 0, (out.stdout or out.stderr).strip()[:80])
    except (OSError, subprocess.SubprocessError) as e:  # noqa: BLE001
        return (False, str(e)[:120])
