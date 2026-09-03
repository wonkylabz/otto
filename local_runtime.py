"""LOCAL execution runtime — the non-Claude execution backend (issue: run whole tasks on a
local model, no `claude -p`).

`claude_cli.run_json` drives Claude Code; this module is its OpenAI-compatible mirror: a
tool-use loop against a LOCAL /chat/completions endpoint (vLLM / Ollama / LM Studio). The
model is offered real tool definitions (Bash, Read, Grep, Glob, WebFetch — plus Edit/Write
for write-risk caps), each tool call it emits is executed here, the result is fed back, and
the loop continues until the model produces a final text answer.

Contract-compatible on purpose:
  * run_json(...) returns the SAME dict shape claude_cli.run_json returns
    (result / total_cost_usd / usage / session_id / is_error), so the verify ladder, audit,
    budgets, and the workflow never know which backend ran.
  * Transcript events are written in the Claude stream-json shapes (`assistant` messages
    with text/tool_use blocks, `user` messages with tool_result blocks), so the live chat
    progress endpoint and the shadow supervisor (`supervisor.compact_event`) work unchanged.
  * Sessions persist to data/local-sessions/<sid>.json; session ids are prefixed "local-",
    which is how engine.run_attempt routes a resumed follow-up back to this runtime.

Guardrails: the offered tools are the intersection of the caller's per-risk allowlist and
what this runtime can serve — a read-risk cap is never offered Edit/Write (and a tool call
outside the offered set returns an error result to the model, mutating nothing). As on the
Claude path, Bash is inherently open-ended — the approval gate, not the toolset, is the real
guard for writes.

MCP: served by `mcp_client` for STDIO servers the caller names in `mcp_servers` (New Relic,
Kubernetes, Grafana, AWS, Vanta, …). claude.ai connectors (Gmail/Calendar/Slack) cannot be
served here — their OAuth lives inside Claude Code — so `engine.run_attempt` keeps a cap that
needs one on the Claude backend rather than letting it discover the gap one hallucinated call
at a time. Caps that need a repo `.mcp.json` (`cap.mcp_config`) also stay on Claude.
"""
import glob as globmod
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import config
import error_classifier
import file_safety
import gateway
import mcp_client

SESSIONS = os.path.join(config.DATA_DIR, "local-sessions")

# Bounds. Tool output is truncated (a huge `cat` would blow the local model's context);
# the turn budget stops a model that loops forever re-running the same command.
_TOOL_OUT_CHARS = 16_000
_READ_MAX_LINES = 2_000
_WEB_MAX_CHARS = 8_000
_MAX_CONTINUATIONS = 3   # stitch at most this many max_tokens-cut final answers


def session_path(sid):
    return os.path.join(SESSIONS, f"{sid}.json")


def new_session_id():
    return "local-" + uuid.uuid4().hex[:12]


def is_local_session(sid):
    """True when a session id was minted by this runtime — how a resumed follow-up is
    routed back here instead of `claude -p --resume` (which would reject it anyway)."""
    return bool(sid) and str(sid).startswith("local-")


def gc_sessions(ttl_h=None):
    """Best-effort sweep of stale session files (mirrors claude_cli.gc_transcripts)."""
    ttl_h = config.TRANSCRIPT_TTL_H if ttl_h is None else ttl_h
    if not os.path.isdir(SESSIONS):
        return
    cutoff = time.time() - ttl_h * 3600
    for name in os.listdir(SESSIONS):
        p = os.path.join(SESSIONS, name)
        try:
            if name.endswith(".json") and os.path.getmtime(p) < cutoff:
                os.unlink(p)
        except OSError:
            pass


# --- tool implementations ---------------------------------------------------
# Names + argument shapes deliberately match the Claude Code tools the caps' own
# instructions reference, so an inlined SKILL.md that says "use Grep" still works.

_TOOL_SCHEMAS = {
    "Bash": {"description": "Run a shell command and return its combined stdout+stderr.",
             "parameters": {"type": "object", "properties": {
                 "command": {"type": "string", "description": "the command to run"}},
                 "required": ["command"]}},
    "Read": {"description": "Read a text file. Returns numbered lines.",
             "parameters": {"type": "object", "properties": {
                 "file_path": {"type": "string"},
                 "offset": {"type": "integer", "description": "1-based first line"},
                 "limit": {"type": "integer", "description": "max lines"}},
                 "required": ["file_path"]}},
    "Grep": {"description": "Search file contents recursively for a regex pattern.",
             "parameters": {"type": "object", "properties": {
                 "pattern": {"type": "string"},
                 "path": {"type": "string", "description": "directory or file to search (default .)"}},
                 "required": ["pattern"]}},
    "Glob": {"description": "List files matching a glob pattern (supports **).",
             "parameters": {"type": "object", "properties": {
                 "pattern": {"type": "string"}}, "required": ["pattern"]}},
    "WebFetch": {"description": "HTTP GET a URL and return the (truncated) body text.",
                 "parameters": {"type": "object", "properties": {
                     "url": {"type": "string"}}, "required": ["url"]}},
    "Edit": {"description": "Replace an exact string in a file (must match exactly once "
                            "unless replace_all).",
             "parameters": {"type": "object", "properties": {
                 "file_path": {"type": "string"}, "old_string": {"type": "string"},
                 "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
                 "required": ["file_path", "old_string", "new_string"]}},
    "Write": {"description": "Write content to a file (creating or overwriting it).",
              "parameters": {"type": "object", "properties": {
                  "file_path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["file_path", "content"]}},
}


def _abspath(p, cwd):
    p = os.path.expanduser(p or "")
    return p if os.path.isabs(p) else os.path.join(cwd or os.getcwd(), p)


def _t_bash(args, cwd):
    res = subprocess.run(["bash", "-lc", args["command"]], cwd=cwd or None,
                         capture_output=True, text=True,
                         timeout=config.LOCAL_TOOL_TIMEOUT_S)
    out = (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
    out = out.strip() or "(no output)"
    if res.returncode != 0:
        out += f"\n(exit code {res.returncode})"
    return out


def _t_read(args, cwd):
    path = _abspath(args["file_path"], cwd)
    _read_guard(path, cwd)
    offset = max(1, int(args.get("offset") or 1))
    limit = min(int(args.get("limit") or _READ_MAX_LINES), _READ_MAX_LINES)
    with open(path, errors="replace") as f:
        lines = f.readlines()
    picked = lines[offset - 1:offset - 1 + limit]
    return "".join(f"{i}\t{line}" for i, line in enumerate(picked, start=offset)) or "(empty file)"


def _t_grep(args, cwd):
    root = _abspath(args.get("path") or ".", cwd)
    res = subprocess.run(["grep", "-rnI", "--exclude-dir=.git", "-e", args["pattern"], root],
                         capture_output=True, text=True, timeout=config.LOCAL_TOOL_TIMEOUT_S)
    # Grep returns matching LINES, so it reads contents exactly as Read does. Guarding the ROOT
    # instead would not hold: the denied set is files under `data/`, never `data/` itself, so
    # `grep -r <pattern> data/` passes an ancestor check and prints models.json anyway. Filter
    # the OUTPUT — that is precise wherever the search started, including `/`.
    out = [ln for ln in res.stdout.splitlines()
           if not file_safety.is_read_denied(ln.split(":", 1)[0], allow_cwd=cwd)]
    return "\n".join(out).strip() or "(no matches)"


def _t_glob(args, cwd):
    hits = globmod.glob(os.path.join(cwd or os.getcwd(), args["pattern"]), recursive=True)
    return "\n".join(sorted(hits)[:500]) or "(no matches)"


def _t_webfetch(args, cwd):
    url = args["url"]
    if not re.match(r"^https?://", url):
        raise ValueError("only http(s) URLs")
    req = urllib.request.Request(url, headers={"User-Agent": "otto-local-runtime"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read(200_000).decode("utf-8", errors="replace")
    return body[:_WEB_MAX_CHARS]


def _deny_guard(path, cwd=None):
    """The LOCAL backend runs its own tool loop, so `claude -p`'s permission system — which is
    what enforces file_safety on the Claude backend — never sees these writes. Same deny-list,
    enforced here instead, or the guard would hold on one backend and not the other. `cwd` is
    this run's own working directory, forwarded so a project capability keeps write access to
    its own repo (see `file_safety.denied_globs`).

    `Bash` is NOT covered: intercepting `echo x > f` means parsing a shell, and a guard that
    catches the obvious form while missing `tee`/`sed -i`/`python -c` reads as protection it
    isn't. Stated in .claude/rules/gateway-backends.md rather than faked here."""
    if file_safety.is_denied(path, allow_cwd=cwd):
        raise ValueError(f"refused: {path} is on Otto's write deny-list (file_safety.py)")


def _read_guard(path, cwd=None):
    """Same shape as `_deny_guard`, for the much smaller READ deny set (Otto's own runtime
    state — see file_safety). `claude -p` enforces this from a `Read(...)` deny rule and covers
    `cat` with it; this runtime drives its own tools, so it has to check here or the guard holds
    on one backend and not the other.

    `Bash` is NOT covered, for the same reason it isn't for writes: catching `cat` while missing
    `sed -n`/`python -c`/`sqlite3` reads as protection it isn't. This is the one place the two
    backends genuinely differ, and it is stated in .claude/rules/gateway-backends.md."""
    if file_safety.is_read_denied(path, allow_cwd=cwd):
        raise ValueError(f"refused: {path} is on Otto's read deny-list (file_safety.py)")


def _t_edit(args, cwd):
    path = _abspath(args["file_path"], cwd)
    _deny_guard(path, cwd)
    with open(path, errors="replace") as f:
        text = f.read()
    old, new = args["old_string"], args["new_string"]
    n = text.count(old)
    if n == 0:
        raise ValueError("old_string not found in file")
    if n > 1 and not args.get("replace_all"):
        raise ValueError(f"old_string occurs {n} times; pass replace_all or make it unique")
    with open(path, "w") as f:
        f.write(text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1))
    return f"edited {path}"


def _t_write(args, cwd):
    path = _abspath(args["file_path"], cwd)
    _deny_guard(path, cwd)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(args["content"])
    return f"wrote {path}"


_TOOL_IMPL = {"Bash": _t_bash, "Read": _t_read, "Grep": _t_grep, "Glob": _t_glob,
              "WebFetch": _t_webfetch, "Edit": _t_edit, "Write": _t_write}


def _offered_tools(allowed_tools, mcp_specs=()):
    """The tools handed to the model: the caller's per-risk allowlist ∩ what this runtime can
    serve — the built-ins below, plus whatever `mcp_client` resolved for the run's servers
    (already allowlist-filtered by the pool). Anything else unknown is silently dropped; the
    risk-based guardrail (a read cap never sees Edit/Write) rides in on the allowlist."""
    allowed = set(allowed_tools or [])
    names = [n for n in _TOOL_IMPL if n in allowed]
    return [{"type": "function",
             "function": {"name": n, **_TOOL_SCHEMAS[n]}} for n in names] + list(mcp_specs)


def _run_tool(name, args, cwd, offered_names, mcp=None):
    """Execute one tool call, always returning TEXT for the model (errors included — the
    model gets to read the failure and adapt, mirroring how Claude Code surfaces tool
    errors). A call outside the offered set mutates nothing."""
    if name not in offered_names:
        return f"Error: tool '{name}' is not available in this run."
    if mcp is not None and name in mcp:
        return _clip(mcp.call(name, args or {}))
    try:
        out = _TOOL_IMPL[name](args or {}, cwd)
    except subprocess.TimeoutExpired:
        out = f"Error: {name} timed out after {config.LOCAL_TOOL_TIMEOUT_S:.0f}s"
    except Exception as e:  # noqa: BLE001 - tool failures are data for the model, not crashes
        out = f"Error: {e}"
    return _clip(out)


def _clip(out):
    """Bound one tool result. Applies to MCP results too — a `pods_list` or an NRQL dump is
    exactly the kind of payload that would otherwise eat a small local context window."""
    out = str(out)
    if len(out) > _TOOL_OUT_CHARS:
        out = out[:_TOOL_OUT_CHARS] + f"\n… (truncated at {_TOOL_OUT_CHARS} chars)"
    return out


# --- recovering text-embedded tool calls ------------------------------------
# Some local models (qwen3 family especially) emit tool calls as TEXT inside the message
# content instead of the structured OpenAI `tool_calls` field — this happens when the serving
# stack has no tool-call parser wired up (vLLM `--tool-call-parser hermes|qwen3_coder`). The
# loop would otherwise see no `tool_calls`, treat the turn as a final answer, and deliver the
# raw `<think>…</think><tool_call>…` syntax as the "result" (report: a qwen3.6 PR review that
# returned its reasoning + an unrun `<function=Bash>` block, then failed verify). We recover
# them here so the loop executes them exactly as if the server had structured them.
_TOOLCALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FUNC_RE = re.compile(r"<function=([^>\s]+)\s*>(.*?)</function>", re.DOTALL | re.IGNORECASE)
_PARAM_RE = re.compile(r"<parameter=([^>\s]+)\s*>\n?(.*?)\n?</parameter>", re.DOTALL | re.IGNORECASE)


# The reasoning-leak helpers live in gateway (the lower-level owner of raw-model-output
# interpretation, alongside looks_like_thinking/reasoning_text) so the cheap-tier completion
# path and the verdict parsers reuse the same logic. Re-exported here under their long-standing
# names — this module, its tests, and CLAUDE.md reference local_runtime._strip_reasoning /
# _is_reasoning_stream.
_strip_reasoning = gateway._strip_reasoning
_is_reasoning_stream = gateway._is_reasoning_stream

_RESTART_MIN = 200     # only judge parts this long — short continuations can look alike harmlessly
_RESTART_PREFIX = 120  # a duplicated opening of this many normalized chars == a restart


def _restarted(part, parts):
    """True when a 'continue where you left off' turn RE-EMITS an earlier part's opening instead
    of continuing. Some local models answer a continuation nudge by starting the whole reply over;
    stitching those accretes near-duplicate truncated copies into one huge blob (run web-96799819:
    a review restarted 4x into a 97KB result). Model-agnostic — keys on a duplicated opening, not
    any model's phrasing. Only judges parts long enough (>= _RESTART_MIN) for the signal to be
    real, so a genuine short continuation is never penalised."""
    norm = re.sub(r"\s+", " ", part or "").strip()
    if len(norm) < _RESTART_MIN:
        return False
    head = norm[:_RESTART_PREFIX]
    return any(re.sub(r"\s+", " ", p or "").strip()[:_RESTART_PREFIX] == head for p in parts)


def _recover_tool_calls(content, offered_names):
    """Parse text-embedded tool calls out of `content`, in either the Hermes JSON form
    (`<tool_call>{"name":…,"arguments":{…}}</tool_call>`) or the qwen3_coder XML form
    (`<tool_call><function=Bash><parameter=command>…</parameter></function></tool_call>`, with
    or without the outer `<tool_call>` wrapper). Returns `(calls, residual_content)` where
    `calls` are structured exactly like native `tool_calls`. Only names this runtime actually
    implements are accepted, so ordinary prose that mentions a tag never misfires; the per-risk
    guardrail still applies downstream at `_run_tool` (via `offered_names`)."""
    text = _strip_reasoning(content or "")
    calls = []

    def _add(name, args):
        if name in _TOOL_IMPL and isinstance(args, dict):
            calls.append({"id": f"call_local_{len(calls)}", "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args)}})

    def _parse_block(block):
        fm = re.search(r"<function=([^>\s]+)", block)
        if fm:   # XML function form (robust to a missing </parameter>/</function> on a cut turn)
            _add(fm.group(1), {k: v.strip() for k, v in _PARAM_RE.findall(block)})
            return
        try:     # Hermes JSON form
            obj = json.loads(block.strip())
        except ValueError:
            return
        if isinstance(obj, dict) and obj.get("name"):
            _add(obj["name"], obj.get("arguments") or obj.get("parameters") or {})

    blocks = _TOOLCALL_RE.findall(text)
    if blocks:
        for b in blocks:
            _parse_block(b)
        residual = _TOOLCALL_RE.sub("", text)
    else:   # no <tool_call> wrapper — accept bare <function=…>…</function>
        found = _FUNC_RE.findall(text)
        for name, body in found:
            _add(name, {k: v.strip() for k, v in _PARAM_RE.findall(body)})
        residual = _FUNC_RE.sub("", text) if found else text
    return calls, residual.strip()


# --- the loop ----------------------------------------------------------------

class LocalWall(RuntimeError):
    """This endpoint cannot serve ANY run — the engine re-dispatches the attempt to Claude
    instead of spending the verify ladder reaching the same dead end. Carries the classifier's
    `Reason` so the engine can report which wall it hit without re-parsing a message.

    ToolsUnsupported and Unavailable predate the classifier and stay as named subclasses: they
    are the two the engine already keys its own flags on, and existing tests name them."""
    reason = error_classifier.Reason.unknown

    def __init__(self, message, reason=None):
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class ToolsUnsupported(LocalWall):
    """The serving stack REJECTS tool definitions (vLLM without --enable-auto-tool-choice /
    --tool-call-parser). Not a model-quality failure: no retry on this backend can ever
    succeed for a tool-using run, so the engine falls the attempt back to Claude instead of
    burning the whole verify ladder (observed: 3 dead attempts + a needs-human banner for a
    config flag)."""
    reason = error_classifier.Reason.tools_unsupported


class Unavailable(LocalWall):
    """The local server is momentarily unreachable — a 502/503/504 ('temporarily unavailable,
    try again later') or a connection-level error (refused/reset). TRANSIENT infra, not a
    model-quality failure, so `_chat_step` backs off in place before raising this. Stays local
    by design (no Claude fallback): a persistently-down server dead-ends with this actionable
    reason instead of a bare 'HTTP 503' the ladder re-hits identically (report: a qwen3.6 run
    that had passed the plan gate died on a 503 blip with nothing executed)."""
    reason = error_classifier.Reason.overloaded


class RunDeadline(Exception):
    """The RUN's wall clock expired while a model call was in flight. Deliberately NOT a
    LocalWall: the endpoint is healthy, we simply ran out of budget, so latching the ladder
    off local (or lighting the health badge) would punish a working server for our own clock.
    `run_json` turns it into the same "(timed out)" the between-turns deadline check produces."""


# Which statuses a backoff is even worth spending. Sourced from the classifier rather than
# listed here so one table decides, and 500 joins them: an internal error is at least as likely
# to be transient as a bad gateway, but it used to be permanent while 502/503/504 backed off.
def _is_transient(status, detail=""):
    return error_classifier.classify(status, detail).action is error_classifier.Action.retry_in_place


_TRANSIENT_CODES = frozenset({500, 502, 503, 504, 429})


def _retry_after_s(e):
    """A server's `Retry-After` in seconds (int form only — an HTTP-date form is ignored),
    clamped to the configured max so a hostile/huge value can't stall the run."""
    try:
        v = float((e.headers or {}).get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(v, config.LOCAL_RETRY_MAX_BACKOFF_S)) if v >= 0 else None


def _post(m, body, timeout):
    """One raw POST to the local model's /chat/completions. Kept separate so tests can
    script the model's turns without a network."""
    req = urllib.request.Request(
        m["base_url"].rstrip("/") + "/chat/completions",
        method="POST", headers=gateway.request_headers(m), data=json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _context_fit(detail):
    """Parse a server's context-overflow 400 into (max_context, prompt_tokens), or None.
    vLLM phrases it: 'This model's maximum context length is 16384 tokens. … your prompt
    contains at least 8193 input tokens'. Pure + unit-tested."""
    m1 = re.search(r"maximum context length is (\d+)", detail or "")
    m2 = re.search(r"(\d+) input tokens", detail or "")
    return (int(m1.group(1)), int(m2.group(1))) if m1 and m2 else None


_PRUNE_KEEP_LAST = 2      # most recent tool results stay verbatim — likely still in play
_PRUNE_TOOL_CHARS = 300   # what an elided tool output keeps (enough to know what it was)
_PRUNE_MARK = "elided to fit the context window"   # also the already-pruned sentinel


def _prune(messages):
    """Compact an over-long wire history, cheapest-to-lose first: OLD tool outputs (they
    dominate the token count and the assistant has already digested them into its own
    turns), then long old assistant prose. Returns (pruned_copy, changed) — changed=False
    when nothing prunable remains (the _PRUNE_MARK sentinel keeps an already-elided message
    from counting as fresh progress, which would loop forever). This is what keeps a long
    session usable on a small-context model (observed: a github-pr-review session whose
    PR-diff tool outputs alone outgrew a 16k window)."""
    out = [dict(m) for m in messages]
    changed = False
    tool_idx = [i for i, m in enumerate(out) if m.get("role") == "tool"]
    for i in (tool_idx[:-_PRUNE_KEEP_LAST] if _PRUNE_KEEP_LAST else tool_idx):
        c = str(out[i].get("content") or "")
        if len(c) > _PRUNE_TOOL_CHARS and _PRUNE_MARK not in c:
            out[i]["content"] = (c[:_PRUNE_TOOL_CHARS]
                                 + f"\n…[{len(c) - _PRUNE_TOOL_CHARS} chars of old tool "
                                   f"output {_PRUNE_MARK}]")
            changed = True
    if not changed:
        for m in out[:-4]:   # keep the recent turns whole
            if m.get("role") == "assistant":
                c = str(m.get("content") or "")
                if len(c) > 1500 and _PRUNE_MARK not in c:
                    m["content"] = c[:1500] + f"\n…[{_PRUNE_MARK}]"
                    changed = True
    return out, changed


def _chat_step(m, body, timeout, _rounds=10, deadline=None):
    """One model call, hardened: HTTP errors carry the SERVER'S error body (a bare 'HTTP
    Error 400' hid the real reason across three debugging rounds), and a context-overflow
    400 is recovered in two escalating moves. The server's own numbers are USELESS for
    arithmetic — vLLM caps its 'prompt contains at least N tokens' at just-over-the-limit,
    so N tracks whatever max_tokens we ask for (observed: six 'refits' that each gained ~65
    tokens, run web-85951a17). So on each overflow:
      1. PRUNE the history (old tool outputs, then old assistant prose) — the usual real
         cause is an accreted session, and this frees the most tokens;
      2. once nothing is left to prune, HALVE max_tokens down to a 256 floor — covers the
         fresh-long-prompt case where there's no history to compact;
      3. both spent -> a real 'context window full' failure for the retry ladder.

    Returns (response, compacted) — `compacted` is the pruned history when step 1 fired, and
    None otherwise. The caller MUST adopt it as its own history: pruning only a local copy of
    the wire body meant every later turn rebuilt the full history and re-paid the same 400,
    and the session saved to disk kept growing, so every future resume re-paid it too."""
    detail, max_len, prompt_tokens = "", 0, 0
    transient = 0
    compacted = None   # the history as it had to be shrunk to fit — handed back to the caller
    for _ in range(max(1, _rounds)):
        try:
            return _post(m, body, timeout), compacted
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                detail = ""
            v = error_classifier.classify(e.code, detail)
            if v.reason is error_classifier.Reason.tools_unsupported:
                raise ToolsUnsupported(
                    "the local model server rejects tool calls — start vLLM with "
                    "--enable-auto-tool-choice and --tool-call-parser <parser>") from None
            # A deterministic rejection — bad credentials, no credit. Retrying reaches the same
            # answer, so it becomes a wall immediately rather than after the backoffs.
            if v.is_wall:
                raise LocalWall(f"{v.message} — {detail[:160] or e.reason}", v.reason) from None
            # Transient ("try again later") — back off in place instead of surfacing a bare error
            # the local-only ladder would re-hit identically. 429 and 500 are here too now: the
            # first means the server is serving someone else, the second is usually a blip.
            if v.action is error_classifier.Action.retry_in_place:
                if transient < config.LOCAL_RETRY_ATTEMPTS:
                    transient += 1
                    after = _retry_after_s(e)
                    time.sleep(after if after is not None
                               else min(config.LOCAL_RETRY_MAX_BACKOFF_S,
                                        config.LOCAL_RETRY_BACKOFF_S * (2 ** (transient - 1))))
                    continue
                # Budget spent. Unavailable keeps its own name and message for the unreachable
                # case, which is the one an operator sees most and acts on.
                w = error_classifier.escalate(v)
                if v.reason is error_classifier.Reason.overloaded:
                    raise Unavailable(
                        f"local model server unavailable after {transient + 1} attempts "
                        f"(HTTP {e.code}) — start/restart the local server, then Retry") from None
                raise LocalWall(f"{w.message} (after {transient + 1} attempts)", w.reason) from None
            # Whether this 400 is an overflow is `classify`'s call, not a regex at this call
            # site: only vLLM spells out both numbers, while OpenAI ("your messages resulted
            # in N tokens") and a bare `context_length_exceeded` carry none. Gating the prune
            # on _context_fit meant every non-vLLM endpoint fell through to the anonymous
            # RuntimeError below, which the verify ladder reads as a model-quality failure and
            # retries with the identical over-long prompt twice more.
            if v.action is not error_classifier.Action.prune:
                raise RuntimeError(f"HTTP {e.code}: {detail or e.reason}") from None
            fit = _context_fit(detail)          # numbers for the final message, when offered
            if fit:
                max_len, prompt_tokens = fit
            pruned, changed = _prune(body.get("messages") or [])
            if changed:
                body = {**body, "messages": pruned}
                compacted = pruned
                continue
            cur = int(body.get("max_tokens") or config.LOCAL_EXEC_MAX_TOKENS)
            if cur > 256:
                body = {**body, "max_tokens": max(256, cur // 2)}
                continue
            raise RuntimeError(
                "context window full "
                + (f"(≥{prompt_tokens} prompt tokens vs {max_len} max — " if max_len
                   else "(")
                + "the conversation/tool history no longer fits this model)") from None
        except (urllib.error.URLError, TimeoutError) as e:
            # Connection-level failure (refused/reset/DNS/socket timeout) — the server is down
            # or restarting. Same transient treatment as a 5xx: back off, then fail clean.
            # TimeoutError (== socket.timeout) is NOT a URLError subclass, so a read that
            # outlived its timeout used to escape this handler into run_json's bare
            # `except Exception`: no backoff, no classification, just a raw error string.
            # But `timeout` is clamped to what's left of the RUN, so the commonest read
            # timeout is our own deadline on a healthy server — blaming the endpoint there
            # would latch the whole ladder off local over our clock.
            if deadline is not None and time.time() >= deadline:
                raise RunDeadline() from None
            reason = getattr(e, "reason", e)
            if transient < config.LOCAL_RETRY_ATTEMPTS:
                transient += 1
                time.sleep(min(config.LOCAL_RETRY_MAX_BACKOFF_S,
                               config.LOCAL_RETRY_BACKOFF_S * (2 ** (transient - 1))))
                continue
            raise Unavailable(
                f"cannot reach the local model server after {transient + 1} attempts "
                f"({reason}) — start/restart the local server, then Retry") from None
    raise RuntimeError(f"context overflow persisted after {_rounds} rounds: {detail}")


def _emit(sink, on_event, event):
    """Write one transcript event + feed the watcher — same semantics as claude_cli's
    stream loop (watcher errors are swallowed; the transcript is flushed per line)."""
    if sink:
        sink.write(json.dumps(event) + "\n")
        sink.flush()
    if on_event is not None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001 - a watcher must never break the run
            pass


def _assistant_event(msg):
    """The model's OpenAI-shaped turn as a Claude stream-json `assistant` event, so
    transcript consumers (progress endpoint, supervisor) need no second format."""
    blocks = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": str(msg["content"])})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"raw": fn.get("arguments")}
        blocks.append({"type": "tool_use", "id": tc.get("id"),
                       "name": fn.get("name"), "input": args})
    return {"type": "assistant", "message": {"content": blocks}}


def _wire(messages):
    """Messages as actually SENT to the server. Assistant messages are stored verbatim as
    the server returned them — decoration fields included (`reasoning`, `refusal`,
    `annotations`, `function_call: null`, …) — but echoing those back on the next request
    is a hard 400 on vLLM (resume run web-2310063f), and gemma's chat template rejects a
    system message anywhere but the start. Keep only the wire-legal fields, coerce null
    content to '' — and never replay reasoning text (it's thinking, not conversation)."""
    out = []
    for m in messages:
        if not isinstance(m, dict) or not m.get("role"):
            continue
        w = {"role": m["role"],
             "content": "" if m.get("content") is None else m["content"]}
        if m.get("tool_calls"):
            w["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            w["tool_call_id"] = m["tool_call_id"]
        out.append(w)
    return out


def _load_session(sid):
    try:
        with open(session_path(sid)) as f:
            return json.load(f).get("messages") or []
    except (OSError, ValueError):
        return []


def _save_session(sid, messages, model=None):
    try:
        os.makedirs(SESSIONS, exist_ok=True)
        with open(session_path(sid), "w") as f:
            json.dump({"messages": messages, "at": time.time(), "model": model}, f)
    except OSError:
        pass   # a lost session only costs resumability, never the run


def session_model(sid):
    """The pool-entry NAME that minted this session, or None (a pre-#299 file, or no session).
    A resume is bound to this runtime by its id, but the model was picked independently — so
    without this the caller falls back to the phase assignment and can hand a CLAUDE pool entry
    to the local runtime. Callers resolve the name against the live pool and tolerate a miss:
    a model deleted from the pool since must not make the session unresumable."""
    if not is_local_session(sid):
        return None
    try:
        with open(session_path(sid)) as f:
            return json.load(f).get("model") or None
    except (OSError, ValueError):
        return None


def resume_entry(session_id):
    """The pool entry that can serve a RESUME of `session_id`, or None if the pool holds nothing
    on that backend.

    A session is bound for life to the backend that MINTED it: `claude -p --resume` rejects a
    `local-` id outright ("is not a UUID and does not match any session title"), and this runtime
    has no history for a Claude uuid. The model ENTRY is resolved independently, though — a phase
    assignment, cap_exec, a chat override — so every resume path has to snap it back onto the
    session's own backend, which is why this is one function and not a copy per caller
    (engine.run_attempt, plans.plan_preview). Prefers the model that minted the session, else ANY
    entry on that backend: local history is plain, portable messages, so a pre-#299 file that
    records no model at all is still resumable."""
    use_local = is_local_session(session_id)

    def _on_this_backend(e):
        return bool(e) and (e.get("provider") != "claude") == use_local

    bound = gateway.resolve_model(session_model(session_id))
    if _on_this_backend(bound):
        return bound
    return next((e for e in gateway.load().get("pool", []) if _on_this_backend(e)), None)


def fork_session(sid):
    """Copy a local session's history into a THROWAWAY id, or None when there is nothing to fork.

    What `claude -p --permission-mode plan --resume` gives the Claude backend for free: print-mode
    copies the history FORWARD into a new session, so a read-only preview reads the conversation
    without ever writing back into it. This runtime resumes IN PLACE — `run_json` saves the turn
    under the same id — so a preview run straight against the real session would leave the plan
    instruction and the plan itself in the history the actual execution resumes next. The caller
    owns the fork and should `drop_session` it when done."""
    messages = _load_session(sid)
    if not messages:
        return None
    fork = new_session_id()
    _save_session(fork, messages, session_model(sid))
    return fork if os.path.exists(session_path(fork)) else None


def drop_session(sid):
    """Delete a session file — a spent `fork_session`. Best-effort: a leaked fork costs disk
    until `gc_sessions` sweeps it, never correctness."""
    if not is_local_session(sid):
        return
    try:
        os.remove(session_path(sid))
    except OSError:
        pass


def run_json(prompt, allowed_tools=None, model_entry=None, timeout=None,
             resume_session=None, system_context=None, cwd=None, transcript=None,
             on_event=None, abort=None, mcp_servers=None, mcp_request=None,
             mcp_require_score=False, steer=None, effort=None):
    """One execution turn on a LOCAL model — the drop-in counterpart of
    claude_cli.run_json (same return contract, same transcript side-effects, same
    supervisor `abort` kill-switch semantics — checked between turns and tool calls).

    `steer` (a supervisor.Steer) is the same non-destructive mid-run correction the Claude
    backend takes, and it is strictly SIMPLER here: the Claude path needs `--input-format
    stream-json` to reach a child process's stdin, whereas this loop owns `messages` outright, so
    a steer is one appended user turn. Local models are the reason to keep the two at parity —
    a run that gets steered on Claude and silently does not on a local model would make the
    feature's behaviour a function of which tier happened to serve it.

    `effort` is the counterpart of `claude -p --effort`, sent as the OpenAI-compatible
    `reasoning_effort` body field. It is ADVISORY here and deliberately not reported as anything
    stronger: measured against vLLM (qwen38-flash-next, 2026-09-01) the field is accepted with no
    400 and no observable change, so a server that does not implement it ignores it silently. Sent
    only when a level is set, so an endpoint that rejects unknown fields is unaffected by default."""
    m = model_entry or {}
    # Normalized once, here, not per turn — and via the same normalizer the Claude backend uses,
    # so "default" means "send no field" on both backends rather than a level named twice.
    effort_level = config.effort_level(effort)
    if not m.get("base_url"):
        return {"result": "(local runtime: model has no base_url)", "is_error": True,
                "total_cost_usd": 0}

    # MCP servers for this run (stdio only — see the module docstring). Started BEFORE the
    # history is assembled so a server that fails to launch can be declared in the system
    # context: a model told "New Relic is unavailable" adapts, one left to discover it burns
    # turns re-calling a tool that will never work.
    mcp = None
    if mcp_servers and config.LOCAL_MCP:
        try:
            mcp = mcp_client.Pool(mcp_servers, allowed_tools=allowed_tools,
                                  request=mcp_request or prompt,
                                  require_score=mcp_require_score)
        except Exception:  # noqa: BLE001 - an unreadable MCP registry costs tools, not the run
            mcp = None
        if mcp and mcp.errors:
            system_context = "\n\n".join(filter(None, [
                system_context,
                "Unavailable this run (do NOT attempt these, work with what you have and say "
                "in your report what you could not check): "
                + "; ".join(f"{n} MCP ({why})" for n, why in mcp.errors.items())]))

    if resume_session and is_local_session(resume_session):
        sid = resume_session
        messages = _load_session(sid)
        # System context merges into the OPENING system message (or becomes one) — never
        # appended mid-history: gemma-style chat templates 400 on a non-leading system role.
        # Skip if this exact context is already in the opener (every resume sends the same
        # resume contract; without the guard it accretes once per follow-up).
        if system_context:
            if messages and messages[0].get("role") == "system":
                opener = str(messages[0].get("content") or "")
                if system_context not in opener:
                    messages[0] = {"role": "system",
                                   "content": opener + "\n\n" + system_context}
            else:
                messages.insert(0, {"role": "system", "content": system_context})
    else:
        sid = new_session_id()
        messages = ([{"role": "system", "content": system_context}] if system_context else [])
    messages.append({"role": "user", "content": prompt})

    tools = _offered_tools(allowed_tools, mcp.specs if mcp else ())
    offered_names = {t["function"]["name"] for t in tools}

    sink = None
    if transcript:
        gc_sessions()
        os.makedirs(os.path.dirname(transcript), exist_ok=True)
        open(transcript, "w").close()
        sink = open(transcript, "a")
        _emit(sink, None, {"type": "otto-meta", "prompt": prompt, "model": m.get("model"),
                           "cwd": cwd, "at": time.time(), "runtime": "local",
                           "tools": sorted(offered_names),
                           "mcp": sorted(mcp_servers or []) if mcp else [],
                           "mcp_errors": (mcp.errors if mcp else {}),
                           "mcp_trimmed": (mcp.trimmed if mcp else 0),
                           "supervised": on_event is not None})

    # No literal default here: a hardcoded one is invisible to every env knob, and the last
    # one (900s) silently outlived a Claude path that had already moved to 1100s.
    deadline = time.time() + (timeout if timeout is not None else config.LOCAL_RUN_TIMEOUT_S)
    usage = {"input_tokens": 0, "output_tokens": 0}
    result, is_error = None, False
    parts, continuations = [], 0   # stitched final answer across max_tokens cutoffs
    nudged = False                 # one shot at converting a reasoning-only turn into an answer
    tools_unsupported = False      # server rejects the tools param → engine re-dispatches to Claude
    # The endpoint could not be reached at all after the backoffs. Distinct from the model
    # answering badly: retrying it on the same server fails identically, so the engine latches
    # the rest of the verify ladder onto Claude instead of spending every attempt on a dead
    # endpoint (observed: run web-e5248517 burned all three attempts on the same HTTP 503).
    unavailable = False
    wall_reason = None             # error_classifier.Reason value when a deterministic wall hit
    tools_used = set()             # tools this turn actually CALLED — the judge's real grant
    max_turns = m.get("max_turns") or config.LOCAL_RUNTIME_MAX_TURNS
    try:
        for _turn in range(max_turns):
            if abort is not None and abort.is_set():
                result, is_error = f"(aborted by supervisor: {abort.reason})"[:400], True
                break
            if time.time() > deadline:
                result, is_error = "(timed out)", True
                break
            # Mid-run steering, taken at the same turn boundary the Claude backend's child reads
            # its stdin at: after the previous turn's tool results are already appended, so the
            # history stays well-formed (a user turn between an assistant tool call and its
            # results is what 400s a strict chat template).
            if steer is not None:
                for instruction in steer.take():
                    messages.append({"role": "user",
                                     "content": config.STEER_MESSAGE.format(instruction=instruction)})
                    _emit(sink, on_event, {"type": "otto-steer", "text": instruction,
                                           "delivered": True})
            body = {"model": m["model"], "temperature": 0,
                    "max_tokens": config.LOCAL_EXEC_MAX_TOKENS, "messages": _wire(messages)}
            if effort_level:
                body["reasoning_effort"] = effort_level
            if tools:
                body["tools"] = tools
            data, compacted = _chat_step(m, body,
                                         min(config.LOCAL_EXEC_TIMEOUT_S,
                                             max(1, deadline - time.time())), deadline=deadline)
            if compacted is not None:
                # The history no longer fits and was shrunk to make this call. Keep the shrunk
                # form: it is what the model actually saw, and it is what the next turn (and
                # every later resume of this session) must send.
                messages = compacted
            u = data.get("usage") or {}
            usage["input_tokens"] += u.get("prompt_tokens", 0) or 0
            usage["output_tokens"] += u.get("completion_tokens", 0) or 0
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            # A server with no tool-call parser leaves tool calls as text in `content`. Recover
            # them into the structured field BEFORE emitting/appending, so the transcript shows
            # real tool_use blocks and the loop runs them instead of delivering the raw syntax.
            if not (msg.get("tool_calls")):
                recovered, residual = _recover_tool_calls(str(msg.get("content") or ""), offered_names)
                if recovered:
                    msg["tool_calls"] = recovered
                    msg["content"] = residual
            messages.append(msg)
            _emit(sink, on_event, _assistant_event(msg))
            calls = msg.get("tool_calls") or []
            if not calls:
                # message_text: content may be null/empty with the text in a reasoning field
                # (qwen3 `reasoning_content` / gemma-4 `reasoning`) — never lose the final
                # answer to that.
                # finish_reason "length" = the answer was CUT by max_tokens, not finished.
                # A completions API just stops there (run web-7b957bc6: a 13k-char PR review
                # ending mid-hunk, silently delivered as complete) — so ask the model to
                # continue where it left off, bounded, and stitch the parts together. Only
                # REAL content is continued/stitched: a length-cut that produced nothing but
                # reasoning means the model never started answering — continuing mid-think
                # stitches thinking fragments into garbage (observed live at tiny budgets).
                raw = str(msg.get("content") or "")
                # Strip a leaked think-stream, but ONLY when one is present — otherwise keep the
                # content byte-for-byte so continuation-stitching preserves boundary whitespace.
                part = _strip_reasoning(raw) if re.search(r"</?think>", raw, re.IGNORECASE) else raw
                # An UNPARSEABLE tool call (a `<tool_call>`/`<function=…>` the recovery above
                # couldn't structure — typically one truncated mid-emission by the token limit)
                # must never ship as the answer: it's tool syntax, not a result. Fail with an
                # actionable reason so the ladder retries/escalates instead of delivering garbage.
                if re.search(r"<tool_call|<function=", part, re.IGNORECASE):
                    result, is_error = (
                        "(local model emitted a tool call the runtime could not parse — likely "
                        "truncated by the token limit, or the server's --tool-call-parser doesn't "
                        "match the model's format: Qwen3 models emit XML "
                        "(<function=…><parameter=…>) that the 'hermes' parser can't read — use "
                        "the Qwen3 XML parser (--tool-call-parser qwen3_xml) — or raise "
                        "OTTO_LOCAL_EXEC_MAX_TOKENS)"), True
                    break
                # Only stitch a length-cut part that's real ANSWER material. An unfenced
                # reasoning stream cut by max_tokens must NOT be stitched — "continue where you
                # left off" just accretes more chain-of-thought (report web-ccbb5378: 4 stitched
                # turns = 27KB of deliberation). Fall through to the nudge instead.
                reasoning = gateway.looks_like_thinking(part) or _is_reasoning_stream(part)
                if (choice.get("finish_reason") == "length" and part.strip()
                        and not reasoning and continuations < _MAX_CONTINUATIONS):
                    if _restarted(part, parts):
                        # The model restarted its answer instead of continuing — stitching would
                        # accrete duplicated truncated copies (the 97KB-blob bug). Fail cleanly so
                        # a fresh run's verify ladder retries/escalates and a resume run gets an
                        # honest short error, never the blob.
                        result, is_error = (
                            "(local model kept restarting its answer after an output-limit cutoff "
                            "instead of continuing — the reply couldn't be completed within the "
                            "token budget; raise OTTO_LOCAL_EXEC_MAX_TOKENS)"), True
                        break
                    continuations += 1
                    parts.append(part)
                    messages.append({"role": "user", "content":
                                     "Your reply was cut off by the output token limit. "
                                     "Continue EXACTLY where you left off — no preamble, "
                                     "no repetition."})
                    continue
                # Reasoning-only final: the model thought but never gave a clean answer — whether
                # the reasoning came in a `reasoning_content`/`reasoning` field with empty content,
                # a leaked <think> stream, or (qwen3.6) unfenced first-person deliberation. NEVER
                # deliver it as the result. Nudge ONCE for the actual answer; a model that still
                # won't produce one is a failed attempt for the retry ladder.
                if not parts and (not part.strip() or reasoning):
                    thinking = part if reasoning else gateway.reasoning_text(msg)
                    if thinking.strip() and not nudged:
                        nudged = True
                        part = ""   # a leaked think-stream is not answer material
                        messages.append({"role": "user", "content":
                                         "You reasoned but did not give the final answer. "
                                         "Reply now with ONLY the final answer/result — no "
                                         "reasoning, no meta-commentary, no notes to self."})
                        continue
                    if reasoning:
                        part = ""   # nudge exhausted: fail below rather than deliver thinking
                result = "".join(parts + [part]).strip()
                if not result:
                    # Still nothing: a FAILED attempt with a reason the retry ladder and
                    # the user can act on.
                    reason = choice.get("finish_reason") or "no content"
                    result, is_error = (f"(local model produced reasoning but no final "
                                        f"answer — finish_reason: {reason}; if 'length', "
                                        f"raise OTTO_LOCAL_EXEC_MAX_TOKENS)"), True
                break
            for tc in calls:
                fn = tc.get("function") or {}
                if fn.get("name"):
                    tools_used.add(str(fn["name"]))   # ground truth for the judge's tool grant
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                if abort is not None and abort.is_set():
                    out = "Error: attempt aborted by the supervisor before this tool call"
                elif time.time() > deadline:
                    out = "Error: run timed out before this tool call"
                else:
                    out = _run_tool(fn.get("name"), args, cwd, offered_names, mcp)
                messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": out})
                _emit(sink, on_event, {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tc.get("id"), "content": out}]}})
        else:
            result, is_error = f"(local runtime hit the {max_turns}-turn budget)", True
    except RunDeadline:
        # Same outcome as the between-turns check above: a failed attempt the ladder retries,
        # never a wall — no `wall_reason`, no health write, local stays eligible.
        result, is_error = "(timed out)", True
    except LocalWall as e:
        # One handler for every deterministic wall. The two legacy flags stay set for the exact
        # reasons the engine already keys on them; `wall_reason` carries the rest, so a new
        # reason needs no new boolean threaded through the engine and the workflow.
        result, is_error = f"(local runtime error: {e})"[:500], True
        wall_reason = e.reason.value if hasattr(e.reason, "value") else str(e.reason)
        tools_unsupported = isinstance(e, ToolsUnsupported)
        unavailable = isinstance(e, Unavailable)
        # Health: only "cannot serve any run" conditions light the badge, which every LocalWall
        # is by definition — a bad-but-served answer never reaches here.
        gateway.record_health(m.get("name"), False, str(e))
    except Exception as e:  # noqa: BLE001 - contract: an error dict, never a raise
        result, is_error = f"(local runtime error: {e})"[:500], True
    finally:
        # Persist the session only for a SUCCESSFUL turn: saving on error left the failed
        # resume's unanswered user message (and duplicated context) in the stored history,
        # compounding on every retry (observed: a session with three stacked user turns).
        if not is_error:
            _save_session(sid, [mm for mm in messages if isinstance(mm, dict)], m.get("name"))
            # A completed turn proves the server is serving — clears an earlier unhealthy mark
            # without any separate reset path (gateway._set_health is last-write-wins).
            gateway.record_health(m.get("name"), True, "completed an execution turn")
        final = {"type": "result", "result": result or "", "is_error": is_error,
                 "total_cost_usd": 0, "session_id": sid, "runtime": "local",
                 "usage": dict(usage)}
        _emit(sink, on_event, final)
        if sink:
            sink.close()
        if mcp is not None:
            mcp.close()
    return {"result": result or "", "is_error": is_error, "total_cost_usd": 0,
            "session_id": sid, "usage": dict(usage), "tools_unsupported": tools_unsupported,
            "wall_reason": wall_reason, "tools_used": sorted(tools_used), "tools_failed": [],
            "unavailable": unavailable}
