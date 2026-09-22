"""Slack channel administration as an MCP server — the actions no Slack MCP server exposes.

Every Slack integration Otto already has is read/post: the claude.ai Slack connector
(`slack_search_*`, `slack_send_message`), Otto's own listener (`slack.py`, which calls
`conversations.history|info|list|replies` and nothing else), and every off-the-shelf Slack MCP
server. So a run asked to tidy up stale `#inc-` channels could enumerate them perfectly and
then had no move: `runbook-rb-a0e1681f` found all 11 and archived none.

Standalone by design — stdlib and `urllib` only, no Otto imports. It is spawned as a
subprocess whose environment has had every `OTTO_*` name stripped (`mcp_client._inherited_env`),
so it cannot reach `config.secret`; the token arrives as `SLACK_TOKEN` in the def's own `env`,
resolved on the way to `data/.mcp-active.json`.

Registering it is an Admin act, not an install step — `policy.add_mcp_def` + activation, the
same gate every other server passes. `docs/slack.md` carries the def and the scopes.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# `SLACK_API_BASE` exists so the spawned server can be pointed at a stand-in Slack. It is the
# only seam an end-to-end test has — everything else about this process (its env, its token,
# its stdio framing) is set by the parent, and a test that stubs `_call` in-process proves the
# dispatch table and nothing about what actually goes over the wire.
API = os.environ.get("SLACK_API_BASE") or "https://slack.com/api/"
TOKEN_ENV = "SLACK_TOKEN"
_TIMEOUT_S = 20
_MAX_RETRIES = 3

# What a Slack `error` code means for the operator, and what to do about it. Slack's own
# strings name the failing API concept, not the fix: `missing_scope` does not say which scope,
# and `restricted_action` reads like a bug when it is a workspace setting. A run that reports
# the raw code sends its reader to the API docs; one that reports these does not.
_ERRORS = {
    "missing_scope":
        "the token lacks the scope for this call — a user token needs `channels:write` for a "
        "public channel and `groups:write` for a private one (a bot token: `channels:manage`). "
        "Add it in the Slack app's OAuth & Permissions, reinstall, and replace the token.",
    "not_in_channel":
        "the token's identity is not a member of that channel. A bot must be invited; a user "
        "token must belong to someone who has joined it.",
    "already_archived": "that channel is already archived — nothing to do.",
    "channel_not_found":
        "no channel with that id or name is visible to this token. `list_channels` shows what is.",
    "cant_archive_general":
        "#general cannot be archived — Slack refuses this for the workspace's default channel.",
    "restricted_action":
        "a workspace setting reserves channel management for admins (Settings → Permissions → "
        "Channel Management). The scope is present; the policy is what refuses.",
    "method_not_supported_for_channel_type":
        "that conversation type cannot be archived — a DM or group DM has no archive.",
    "invalid_auth": "the token is rejected. It may have been revoked or rotated.",
    "not_authed": f"no token reached the server — its def must set {TOKEN_ENV} in `env`.",
    "token_revoked": "the token has been revoked; reinstall the Slack app and replace it.",
}

TOOLS = [
    {"name": "list_channels",
     "description": ("List Slack channels the token can see, with creation date and archive "
                     "state. Use this to resolve a channel NAME to the id the archive tools "
                     "take, and to check what is already archived before acting."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "name_prefix": {"type": "string",
                             "description": "Only channels whose name starts with this, e.g. 'inc-'."},
             "include_archived": {"type": "boolean",
                                  "description": "Include already-archived channels (default false)."},
             "include_private": {"type": "boolean",
                                 "description": "Include private channels (default false)."},
             "limit": {"type": "integer",
                       "description": "Maximum channels to return (default 200)."},
         }}},
    {"name": "archive_channel",
     "description": ("Archive one Slack channel. Archiving is reversible (unarchive_channel) "
                     "but it removes every member and hides the channel, so archive exactly "
                     "the channels asked for, one call each, and report any that refuse."),
     "inputSchema": {
         "type": "object",
         "properties": {"channel": {"type": "string",
                                    "description": "Channel id (C…) or name, with or without '#'."}},
         "required": ["channel"]}},
    {"name": "unarchive_channel",
     "description": "Restore a channel archived in error. Takes the same id or name.",
     "inputSchema": {
         "type": "object",
         "properties": {"channel": {"type": "string",
                                    "description": "Channel id (C…) or name, with or without '#'."}},
         "required": ["channel"]}},
]


def _token():
    return os.environ.get(TOKEN_ENV, "")


def _call(method, params=None, post=False):
    """One Slack API call, as `{"ok": bool, ...}`. Never raises: a transport failure comes back
    in the same shape as a Slack-level one, so one branch handles both at the call site.

    429 is retried honouring `Retry-After` — `conversations.list` is a paged Tier-2 method and
    a sweep over a large workspace WILL hit it, at which point giving up loses the whole page."""
    if not _token():
        return {"ok": False, "error": "not_authed"}
    params = params or {}
    for attempt in range(_MAX_RETRIES):
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            API + method + ("" if post else "?" + urllib.parse.urlencode(params)),
            data=body if post else None,
            headers={"Authorization": f"Bearer {_token()}",
                     "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _MAX_RETRIES - 1:
                time.sleep(min(float(e.headers.get("Retry-After") or 1), 30))
                continue
            return {"ok": False, "error": f"http_{e.code}"}
        except Exception as e:  # noqa: BLE001 — a transport failure is a result, not a crash
            if attempt < _MAX_RETRIES - 1:
                time.sleep(1)
                continue
            return {"ok": False, "error": f"transport: {e}"}
    return {"ok": False, "error": "unreachable"}


def _explain(resp):
    """A Slack failure as a sentence the run can put in its report verbatim."""
    code = str(resp.get("error") or "unknown_error")
    hint = _ERRORS.get(code)
    extra = resp.get("needed")
    if code == "missing_scope" and extra:
        hint = f"{hint} Slack says it needs: {extra}."
    return f"{code} — {hint}" if hint else code


def _channels(include_archived=False, include_private=False, hard_cap=2000):
    """Every visible channel, paged. Returns (list, error_or_None)."""
    types = "public_channel" + (",private_channel" if include_private else "")
    out, cursor = [], ""
    while len(out) < hard_cap:
        params = {"limit": 200, "types": types,
                  "exclude_archived": "false" if include_archived else "true"}
        if cursor:
            params["cursor"] = cursor
        resp = _call("conversations.list", params)
        if not resp.get("ok"):
            return out, _explain(resp)
        out += resp.get("channels") or []
        cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            break
    return out, None


def _resolve(ref):
    """A channel id for `ref`, which may already be one. Returns (id, error_or_None).

    A name is resolved against the channel list rather than guessed at, and the lookup spans
    archived and private channels so `unarchive_channel` can find its target and a private
    channel is refused for the right reason."""
    ref = (ref or "").strip().lstrip("#")
    if not ref:
        return None, "no channel given"
    if ref[:1] in ("C", "G") and ref.isupper() and ref.isalnum():
        return ref, None
    chans, err = _channels(include_archived=True, include_private=True)
    if err:
        return None, err
    hit = [c for c in chans if c.get("name") == ref]
    if not hit:
        return None, f"channel_not_found — no channel named '{ref}' is visible to this token"
    return hit[0].get("id"), None


def _archive(args, method):
    ref = args.get("channel")
    cid, err = _resolve(ref)
    if err:
        return f"FAILED {ref}: {err}", True
    resp = _call(method, {"channel": cid}, post=True)
    if not resp.get("ok"):
        return f"FAILED {ref} ({cid}): {_explain(resp)}", True
    verb = "unarchived" if method.endswith("unarchive") else "archived"
    return f"OK {ref} ({cid}) {verb}", False


def _list(args):
    chans, err = _channels(bool(args.get("include_archived")), bool(args.get("include_private")))
    if err:
        return f"FAILED: {err}", True
    prefix = (args.get("name_prefix") or "").lstrip("#")
    limit = int(args.get("limit") or 200)
    rows = [c for c in chans if not prefix or (c.get("name") or "").startswith(prefix)]
    rows.sort(key=lambda c: c.get("created") or 0)
    lines = [f"{c.get('id')}\t{c.get('name')}\tcreated="
             f"{time.strftime('%Y-%m-%d', time.gmtime(c.get('created') or 0))}"
             f"\tarchived={bool(c.get('is_archived'))}\tmembers={c.get('num_members', '?')}"
             for c in rows[:limit]]
    head = f"{len(rows)} channel(s)" + (f" matching '{prefix}'" if prefix else "")
    if len(rows) > limit:
        head += f", showing {limit}"
    return "\n".join([head, *lines]), False


def dispatch(name, args):
    """One tool call -> (text, is_error). The whole tool surface, so a test can drive it
    without the JSON-RPC frame."""
    args = args or {}
    if name == "list_channels":
        return _list(args)
    if name == "archive_channel":
        return _archive(args, "conversations.archive")
    if name == "unarchive_channel":
        return _archive(args, "conversations.unarchive")
    return f"no such tool: {name}", True


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve(stdin=None):
    """The stdio JSON-RPC loop. One request per line, same framing `mcp_client` speaks."""
    for line in (stdin or sys.stdin):
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid,
                   "result": {"protocolVersion": "2025-06-18",
                              "serverInfo": {"name": "slack-admin", "version": "1"},
                              "capabilities": {"tools": {}}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            text, is_error = dispatch(p.get("name"), p.get("arguments"))
            _send({"jsonrpc": "2.0", "id": rid,
                   "result": {"content": [{"type": "text", "text": text}],
                              "isError": is_error}})
        elif method and method.startswith("notifications/"):
            pass
        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": f"no such method: {method}"}})


if __name__ == "__main__":
    serve()
