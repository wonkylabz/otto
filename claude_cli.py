"""Thin wrapper around headless Claude Code. Used INSIDE Temporal activities
(never inside workflow code - subprocess calls are non-deterministic).

Runs `claude -p` with `--output-format stream-json` (issue #89) so the full exchange —
assistant turns, tool calls, tool results — streams out line-by-line while the turn
executes. Each event is appended to a per-run transcript (`data/transcripts/
<wid>-a<attempt>.jsonl`) as it arrives, which is what the run-detail view, live chat
progress, and the mid-attempt budget kill consume. The FINAL `result` event carries the
exact same fields the old `--output-format json` object did (result / total_cost_usd /
usage / session_id / is_error), so run_json's return contract is unchanged and callers
(and their test mocks) don't know the difference.
"""
import json
import os
import subprocess
import threading
import time

import config
import file_safety

TRANSCRIPTS = os.path.join(config.DATA_DIR, "transcripts")


def plan_transcript_path(wid):
    """The plan preview's transcript. The preview is a full agentic `claude -p` pass with
    PLAN_TOOLS, but it wrote nothing here — so its tool calls were invisible in Debug and the
    board's model chip (which resolves the model BY reading a transcript) stayed blank for the
    whole phase, which reads as "no model is running". Its own name, not `-a0`, so nothing
    that enumerates execution attempts mistakes it for one."""
    return os.path.join(TRANSCRIPTS, f"{wid}-plan.jsonl")


def transcript_path(wid, attempt):
    """Canonical transcript location for one execution attempt of one workflow."""
    return os.path.join(TRANSCRIPTS, f"{wid}-a{attempt}.jsonl")


def gc_transcripts(ttl_h=None):
    """Best-effort sweep of transcripts older than the TTL (mirrors workspace.gc) — run
    opportunistically before each captured run so the directory can't grow forever."""
    ttl_h = config.TRANSCRIPT_TTL_H if ttl_h is None else ttl_h
    if not os.path.isdir(TRANSCRIPTS):
        return
    cutoff = time.time() - ttl_h * 3600
    for name in os.listdir(TRANSCRIPTS):
        p = os.path.join(TRANSCRIPTS, name)
        try:
            if name.endswith(".jsonl") and os.path.getmtime(p) < cutoff:
                os.unlink(p)
        except OSError:
            pass


def _drain(stream, sink):
    """Read a pipe to EOF into `sink` (list) from a thread, so a chatty stderr can never
    fill its pipe buffer and deadlock the child while we're blocked reading stdout."""
    try:
        sink.append(stream.read() or "")
    except (OSError, ValueError):
        pass


def _note_tools(event, seen, worked, failed):
    """Record which tools this turn called, and whether each call actually RETURNED anything.

    A `--allowedTools` list does not describe the real grant: a subagent cap contributes its own
    `tools:` frontmatter and MCP servers are inherited from the user's config, so what a turn can
    reach is routinely a superset. But "was called" is not "was available" either — a connector
    the operator has not granted answers every call with "you haven't granted it yet". Both halves
    matter to the judge: crediting a refused tool as present makes a truthful "that source was
    blocked" read as an invented excuse (measured on probe-e2e-0001).

    `seen` maps tool_use id -> name so a later `tool_result` can be attributed. Best-effort and
    total: an unrecognised shape adds nothing and never raises, because a transcript detail must
    not be able to break a run."""
    try:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name"):
                seen[block.get("id")] = str(block["name"])
            elif block.get("type") == "tool_result":
                name = seen.get(block.get("tool_use_id"))
                if name:
                    (failed if block.get("is_error") else worked).add(name)
    except Exception:  # noqa: BLE001 - observability must never break the stream loop
        pass


def run_json(prompt, allowed_tools=None, model=None, timeout=900, mcp_config_path=None,
             resume_session=None, system_context=None, cwd=None, transcript=None,
             on_event=None, abort=None, meta=None, permission_mode=None,
             disallowed_tools=None, setting_sources=None, strict_mcp=False, steer=None,
             effort=None):
    """One headless Claude Code turn. Returns the parsed final `result` event — the same
    dict shape `--output-format json` produced. With `transcript` set, every stream event
    (and stderr) is appended there as it arrives; without it, nothing is captured (the
    cheap gateway tiers pass no transcript).

    `on_event` is the stream-loop watcher seam (issue #143; also where the mid-attempt
    budget kill #100 and activity heartbeats #133 plug in): called with each parsed stream
    event, on the reader thread. Watcher errors are swallowed — a watcher can observe but
    never break the run — and the callback must return fast (it blocks stream reading).

    `abort` (a supervisor.Abort) is the ENFORCE-mode kill switch: when the supervisor sets
    it mid-attempt, the child is killed and the call returns an error dict carrying the
    supervisor's reason — the verify ladder folds it into the next attempt.

    `steer` (a supervisor.Steer) is the non-destructive counterpart: instructions queued on it
    are written to the LIVE child as extra user messages, which it consumes at its next turn
    boundary without losing any context. That requires a different invocation — the prompt moves
    off argv and onto stdin under `--input-format stream-json` — so it is taken ONLY when a
    steer channel is passed, and every unsteered call keeps the exact argv it always had.
    Measured against claude 2.1.251: an instruction written at t=9s of a five-step task was
    consumed when the in-flight tool call returned at t=13.5s and redirected the agent."""
    # Streaming stdin is what makes mid-run steering possible; it also changes the child's exit
    # contract (see the read loop — it no longer EOFs on its own once the result arrives, it
    # waits for more input), so the two are introduced together and never apart.
    streaming_in = steer is not None
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if streaming_in:
        # --replay-user-messages echoes our injected messages back on stdout, so the transcript
        # shows the steer in the position the model actually received it — the same reason
        # `system_context` is recorded in the meta line: a transcript that cannot say what the
        # model was told cannot be debugged.
        cmd += ["--input-format", "stream-json", "--replay-user-messages"]
    else:
        cmd.insert(2, prompt)
    if model:
        cmd += ["--model", model]
    # How hard the model thinks. Normalized here rather than trusted: an unknown value is a
    # WARNING on stderr, not a failure, so the run silently proceeds at the default effort while
    # every caller believes it applied (see config.effort_level).
    effort = config.effort_level(effort)
    if effort:
        cmd += ["--effort", effort]
    # `--permission-mode plan` is how the read-only PLAN preview reads private tickets: without
    # it, a SCOPED Bash allow (e.g. Bash(gh issue view:*)) still trips Claude Code's command
    # classifier, which gates any NETWORK command to interactive approval that a headless
    # `claude -p` can never satisfy ("This command requires approval") — so `gh issue view`
    # died and the planner fell back to "I could not read issue #N". Plan mode reclassifies the
    # scoped read-only network commands as allowed AND independently forbids every mutation
    # (Write/Edit and outward-facing bash like `gh pr create`/`git push`), so it also hardens
    # the preview. (Blanket `Bash` in READ/WRITE_TOOLS bypasses the classifier, so normal
    # execution never hit this — the bug was isolated to the scoped PLAN_TOOLS pass.)
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    # `--allowedTools` is a PERMISSION filter — every built-in tool, skill and agent stays in
    # the system prompt regardless, re-read on every turn of every attempt. `--disallowedTools`
    # is what actually unloads them (config.DISALLOWED_TOOLS / ALL_BUILTIN_TOOLS). Beyond the
    # token bill this is a quality lever: a small model picking from ~35 tools it may not use
    # follows instructions worse than one shown the 10 it may.
    if disallowed_tools:
        cmd += ["--disallowedTools", *disallowed_tools]
    # `user` skips project+local setting sources, i.e. whatever repo the WORKER happens to sit
    # in. Otto's own CLAUDE.md (a guide to editing Otto) was being loaded into every run that
    # isn't anchored to a repo of its own — thousands of tokens per turn of instructions about
    # an unrelated codebase. Only ever passed when there is no cwd (engine.run_attempt): a
    # repo-mode or project cap MUST keep its repo's CLAUDE.md and .claude/ config.
    # `is not None`, not truthiness: "" is a MEANINGFUL value — it loads no settings sources at
    # all, which is the only way to run with no CLAUDE.md whatsoever. Omitting the flag is NOT
    # that; the CLI then defaults to user+project+local, i.e. strictly MORE than "user". Measured
    # from a neutral cwd, asking for a path that only the user's global CLAUDE.md defines:
    # `--setting-sources user` answered it, no flag answered it, `--setting-sources ""` said NONE.
    if setting_sources is not None:
        cmd += ["--setting-sources", setting_sources]
    # Unconditional path deny-list (file_safety.py) — the one write guard that does not depend on
    # a human reading a plan. Applied to EVERY turn including the plan preview: `--permission-mode
    # plan` already forbids mutations, but the deny costs nothing there and means no call site can
    # forget it. `--settings` takes inline JSON, so there is no temp file to manage.
    #   Order matters only in that this must not be merged into `disallowed_tools`: the same rule
    # on --disallowedTools is accepted and does nothing (see file_safety's docstring).
    deny = file_safety.settings_arg(allow_cwd=cwd)
    if deny:
        cmd += ["--settings", deny]
    if mcp_config_path:
        cmd += ["--mcp-config", mcp_config_path]
    # Only for calls that use no tools at all — Otto adds no MCP servers of its own
    # (policy.active_mcp_config is normally None), so the servers a capability needs are
    # INHERITED from the user's config and `--strict-mcp-config` would strip every one.
    if strict_mcp:
        cmd += ["--strict-mcp-config"]
    if resume_session:
        cmd += ["--resume", resume_session]
    if system_context:
        cmd += ["--append-system-prompt", system_context]

    # `stdin` is passed ONLY when steering, so an unsteered call reaches Popen with byte-identical
    # arguments to the ones it always had — the pre-steering invocation is the well-tested one and
    # must not become a special case of the new one.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=cwd, **({"stdin": subprocess.PIPE} if streaming_in else {}))
    send_lock = threading.Lock()

    def _close_stdin():
        if getattr(proc, "stdin", None) is not None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def _send_user(text):
        """Write one user message to the live child. False once the pipe is gone (the child
        exited, or the watchdog/abort killed it) — never raises: an undeliverable steer must
        cost a log line, not the attempt."""
        with send_lock:
            if getattr(proc, "stdin", None) is None or proc.poll() is not None:
                return False
            try:
                proc.stdin.write(json.dumps(
                    {"type": "user",
                     "message": {"role": "user",
                                 "content": [{"type": "text", "text": text}]}}) + "\n")
                proc.stdin.flush()
                return True
            except (BrokenPipeError, ValueError, OSError):
                return False

    if streaming_in:
        _send_user(prompt)
    sink = None
    if transcript:
        gc_transcripts()
        os.makedirs(os.path.dirname(transcript), exist_ok=True)
        open(transcript, "w").close()  # truncate any stale file at this path
        # Reopened in append mode (O_APPEND) rather than kept as the initial "w" handle: the
        # supervisor (issue #143) appends its own checkpoint lines to this same path from a
        # background thread via a second fd, and O_APPEND is what keeps concurrent appends
        # from two fds atomic-at-EOF instead of one clobbering the other's bytes.
        sink = open(transcript, "a")
        # A meta first line so the transcript is self-describing (the stream itself never
        # echoes the invocation). Consumers skip unknown types. `meta` lets the caller stamp
        # extra facts (e.g. fallback_from when this run replaces a failed local attempt —
        # the board's model chip reads them).
        # `system_context` is recorded ALONGSIDE the prompt, not folded into it. Everything Otto
        # tells a run that is not the request itself travels this argument — the approved plan,
        # the workspace-mismatch note, the output contract, recalled memory — and none of it was
        # in the transcript, so Debug could not answer "what was this model actually told?".
        # Measured the hard way: a check for the mismatch note in `prompt` came back False on a
        # run that had been given one, because the note was never in that field to begin with.
        sink.write(json.dumps({"type": "otto-meta", "prompt": prompt, "model": model,
                               "system_context": system_context,
                               # Same reason as system_context: effort changes what the model did,
                               # so a transcript that cannot say which level served the run cannot
                               # answer "was this a max-effort attempt or not?".
                               "effort": effort,
                               "cwd": cwd, "at": time.time(),
                               "supervised": on_event is not None, **(meta or {})}) + "\n")
        sink.flush()
    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        try:
            proc.kill()
        except OSError:
            pass

    steer_thread = None
    if steer is not None:
        def _steer_watch():
            # Same shape as _abort_watch: poll so the thread dies with the child instead of
            # leaking one blocked forever. Deliveries land between turns because that is when
            # the CLI reads stdin — never mid-tool-call.
            while proc.poll() is None:
                for instruction in steer.take():
                    ok = _send_user(config.STEER_MESSAGE.format(instruction=instruction))
                    # Own fd, not `sink`: the supervisor already appends through a second
                    # O_APPEND handle for exactly this reason — two threads sharing one file
                    # object interleave, two O_APPEND fds do not.
                    if transcript:
                        try:
                            with open(transcript, "a") as f:
                                f.write(json.dumps({"type": "otto-steer", "at": time.time(),
                                                    "text": instruction,
                                                    "delivered": ok}) + "\n")
                        except OSError:
                            pass
                time.sleep(0.5)
        steer_thread = threading.Thread(target=_steer_watch, daemon=True)
        steer_thread.start()

    stderr_buf = []
    stderr_thread = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf), daemon=True)
    stderr_thread.start()
    watchdog = threading.Timer(timeout, _kill)
    watchdog.start()
    if abort is not None:
        def _abort_watch():
            # Poll rather than a bare wait() so this thread exits with the child instead of
            # leaking one blocked-forever daemon thread per run.
            while proc.poll() is None:
                if abort.wait(0.5):
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    return
        threading.Thread(target=_abort_watch, daemon=True).start()

    final, tail = None, ""
    # Every tool the turn ACTUALLY called, harvested from the stream it is already parsing.
    # This is the only ground truth about the grant: `allowed_tools` is a FLOOR, not the set —
    # an `agent` cap's own `tools:` frontmatter and every inherited MCP server add to it. Handing
    # the judge the floor as if it were exhaustive is what made it call real Calendar/Gmail/Slack
    # results fabricated (see judging.verify).
    seen, worked, failed = {}, set(), set()
    try:
        for line in proc.stdout:
            if sink:
                sink.write(line if line.endswith("\n") else line + "\n")
                sink.flush()                     # live tailers (chat progress) see it now
            tail = line
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 - a watcher must never break the run
                    pass
            if isinstance(event, dict):
                if event.get("type") == "result":
                    final = event
                    if streaming_in:
                        # With stdin still open the child waits for another message instead of
                        # ending the stream, so the read loop would block until the watchdog
                        # killed it and the attempt would report "(timed out)" having already
                        # succeeded. The result event IS the end of the turn: stop reading and
                        # let `finally` close stdin, which is what lets the child exit.
                        break
                _note_tools(event, seen, worked, failed)
        # Closing stdin is what tells a streaming-input child the turn is over; it must happen
        # BEFORE the wait or the wait is the hang. Repeated in `finally` because every early
        # exit from this block (abort kill, watchdog, a raise) needs it too, and close() twice
        # is a no-op.
        _close_stdin()
        proc.wait()
    finally:
        _close_stdin()
        watchdog.cancel()
        # A steer's transcript record is written by that thread AFTER the delivery that unblocks
        # the child's result, so returning without joining races the record away — under suite
        # load it lost, and the transcript is the only place it can afterwards be said what the
        # model was actually told. The loop exits on proc.poll(), already true by here.
        if steer_thread is not None:
            steer_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        stderr = (stderr_buf[0] if stderr_buf else "").strip()
        if sink:
            if stderr:
                sink.write(json.dumps({"type": "stderr", "text": stderr[:20_000]}) + "\n")
            if timed_out.is_set():
                sink.write(json.dumps({"type": "otto-timeout", "after_s": timeout}) + "\n")
            sink.close()

    # A tool that succeeded even once was available. One that ONLY ever failed was not — and an
    # output reporting that source as blocked is telling the truth, so the judge must be told.
    tools_used = sorted(worked)
    tools_failed = sorted(failed - worked)
    if abort is not None and abort.is_set():
        return {"result": f"(aborted by supervisor: {abort.reason})"[:400],
                "is_error": True, "total_cost_usd": 0, "aborted": True,
                "tools_used": tools_used, "tools_failed": tools_failed}
    if timed_out.is_set():
        return {"result": "(timed out)", "is_error": True, "total_cost_usd": 0,
                "tools_used": tools_used, "tools_failed": tools_failed}
    if final is None:
        # The stream ended without a result event (crash, auth failure, garbage output) —
        # same contract as the old JSONDecodeError branch: an error dict, never a raise.
        return {"result": (tail or stderr)[:4000], "is_error": True, "total_cost_usd": 0,
                "tools_used": tools_used, "tools_failed": tools_failed}
    final["tools_used"] = tools_used
    final["tools_failed"] = tools_failed
    return final
