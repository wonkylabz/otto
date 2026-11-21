"""Slack channel administration as an MCP server — the actions no Slack MCP server exposes.

Plus a read/post pair (`channel_history`, `post_message`) for a capability that keeps its
state IN a channel: each post is one item, a reaction on it is the human's verdict. The
claude.ai connector can't serve that on the local backend, and Otto's runbook delivery posts
one message per run, so N items share one reaction target. Both also take a PERSON (`@name`
or a `U…` id) and open the direct message with them; `list_users` resolves a name to that id.

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
        "the token lacks the scope for this call. Listing needs `channels:read` (+ `groups:read` "
        "for private); archiving needs `channels:write` (+ `groups:write`) on a user token, "
        "`channels:manage` on a bot; reading needs `channels:history`, posting `chat:write`; a "
        "direct message needs `users:read` to find the person and `im:write` to open it "
        "(`im:history` to read it). Add it in the Slack app's OAuth & Permissions, reinstall, "
        "and replace the token.",
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
    "user_not_found": "no such user — `list_users` shows who this token can see.",
    "user_not_visible": "that user is not visible to this token (another workspace, or restricted).",
    "user_disabled": "that user's account is deactivated — a direct message cannot reach them.",
    "cannot_dm_bot": "that user is a bot; Slack does not open a direct message with a bot.",
    "invalid_auth": "the token is rejected. It may have been revoked or rotated.",
    "not_authed": f"no token reached the server — its def must set {TOKEN_ENV} in `env`.",
    "token_revoked": "the token has been revoked; reinstall the Slack app and replace it.",
}

# One message line in a history listing. Model-bound, so a long message is clipped WITH a marker
# — an unmarked cut reads as the whole message.
_TEXT_CHARS = 600

# Where a read or a post goes. A PERSON is spelled with '@' (or given by id) so a channel and a
# person sharing a name are never confused; a bare name that matches no channel is tried as one.
_TARGET = ("Channel id (C…/G…/D…) or name ('#name'), or a person for a direct message: "
           "'@name' or a user id (U…/W…).")

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
    {"name": "list_users",
     "description": ("List people in the workspace (id, handle, display and real name) — the "
                     "lookup for a direct message. Bots and deactivated accounts are left out."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "query": {"type": "string",
                       "description": "Only people whose handle, display or real name contains this."},
             "limit": {"type": "integer",
                       "description": "Maximum people to return (default 200)."},
         }}},
    {"name": "channel_history",
     "description": ("Read a channel's messages, newest first, each with its ts, author and "
                     "REACTIONS (emoji name, count, who reacted). Use it to see what was already "
                     "posted and how people reacted to each message. Given a person, reads the "
                     "direct message with them."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "channel": {"type": "string",
                         "description": _TARGET},
             "limit": {"type": "integer",
                       "description": "Maximum messages to return (default 200, max 1000)."},
             "oldest": {"type": "string",
                        "description": "Only messages after this Slack ts (optional)."},
         },
         "required": ["channel"]}},
    {"name": "post_message",
     "description": ("Post one message to a channel, or send a direct message to a person, and "
                     "return its ts. Slack mrkdwn: *bold*, _italic_, <url|label>. Link previews "
                     "are off."),
     "inputSchema": {
         "type": "object",
         "properties": {
             "channel": {"type": "string",
                         "description": _TARGET},
             "text": {"type": "string", "description": "The message body."},
         },
         "required": ["channel", "text"]}},
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
        hint = (f"the token lacks `{extra}`, which this call needs. Add it in the Slack app's "
                "OAuth & Permissions, reinstall, and replace the token.")
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


def _is_id(ref, prefixes):
    return ref[:1] in prefixes and ref.isupper() and ref.isalnum()


def _users(hard_cap=5000):
    """Every person the token can see, paged — bots, Slackbot and deactivated accounts dropped,
    since none can be messaged. Returns (list, error_or_None)."""
    out, cursor = [], ""
    while len(out) < hard_cap:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = _call("users.list", params)
        if not resp.get("ok"):
            return out, _explain(resp)
        out += [u for u in resp.get("members") or []
                if not (u.get("deleted") or u.get("is_bot") or u.get("id") == "USLACKBOT")]
        cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor:
            break
    return out, None


def _names(u):
    prof = u.get("profile") or {}
    return [n for n in (u.get("name"), prof.get("display_name"), u.get("real_name"),
                        prof.get("real_name")) if n]


def _user(ref):
    """A user id for a person's handle or name. Returns (id, error_or_None).

    A message to the WRONG person is a leak no retry undoes, so this never guesses: an exact
    handle/display/real-name match wins, else a first-name match, and more than one candidate at
    either step is refused with the candidates named."""
    want = ref.strip().lstrip("@").casefold()
    users, err = _users()
    if err:
        return None, err
    exact = [u for u in users if want in (n.casefold() for n in _names(u))]
    first = [u for u in users
             if want in {(n.split() or [""])[0].casefold() for n in _names(u)}]
    for hits in (exact, first):
        if len(hits) == 1:
            return hits[0].get("id"), None
        if len(hits) > 1:
            who = ", ".join(f"{u.get('id')} ({' / '.join(_names(u))})" for u in hits[:10])
            return None, (f"ambiguous — '{ref}' matches {len(hits)} people: {who}. "
                          "Pass the user id.")
    return None, f"user_not_found — nobody named '{ref}' is visible to this token"


def _open_dm(uid):
    """The direct-message channel with one person, opened if it does not exist yet."""
    resp = _call("conversations.open", {"users": uid}, post=True)
    if not resp.get("ok"):
        return None, _explain(resp)
    return (resp.get("channel") or {}).get("id"), None


def _resolve(ref, people=False):
    """A channel id for `ref`, which may already be one. Returns (id, error_or_None).

    A name is resolved against the channel list rather than guessed at, and the lookup spans
    archived and private channels so `unarchive_channel` can find its target and a private
    channel is refused for the right reason.

    `people`: `ref` may also name a person ('@name', a U…/W… id, or a bare name no channel has),
    resolved to the direct message with them — for reading and posting, never archiving."""
    ref = (ref or "").strip()
    if people and ref.startswith("@"):
        uid, err = (ref[1:], None) if _is_id(ref[1:], ("U", "W")) else _user(ref)
        return (None, err) if err else _open_dm(uid)
    if people and _is_id(ref, ("U", "W")):
        return _open_dm(ref)
    people = people and not ref.startswith("#")      # '#name' is a channel, never a person
    ref = ref.lstrip("#")
    if not ref:
        return None, "no channel given"
    if _is_id(ref, ("C", "G") + (("D",) if people else ())):
        return ref, None
    chans, err = _channels(include_archived=True, include_private=True)
    if err and err.startswith("missing_scope"):
        # No `groups:read` — the private half of the lookup is what refused, not the name.
        chans, err = _channels(include_archived=True)
    if err:
        return None, err
    hit = [c for c in chans if c.get("name") == ref]
    if hit:
        return hit[0].get("id"), None
    if people:
        uid, uerr = _user(ref)
        if uid:
            return _open_dm(uid)
        if not uerr.startswith("user_not_found"):
            return None, uerr
    return None, (f"channel_not_found — no channel named '{ref}' is visible to this token"
                  + (" and no person has that name" if people else ""))


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


def _list_users(args):
    users, err = _users()
    if err:
        return f"FAILED: {err}", True
    q = (args.get("query") or "").strip().lstrip("@").casefold()
    limit = int(args.get("limit") or 200)
    rows = [u for u in users if not q or any(q in n.casefold() for n in _names(u))]
    lines = [f"{u.get('id')}\t@{u.get('name')}\t" + " / ".join(_names(u)[1:]) for u in rows[:limit]]
    head = f"{len(rows)} person(s)" + (f" matching '{q}'" if q else "")
    if len(rows) > limit:
        head += f", showing {limit}"
    return "\n".join([head, *lines]), False


def _history(args):
    ref = args.get("channel")
    cid, err = _resolve(ref, people=True)
    if err:
        return f"FAILED {ref}: {err}", True
    limit = max(1, min(int(args.get("limit") or 200), 1000))
    msgs, cursor = [], ""
    while len(msgs) < limit:
        params = {"channel": cid, "limit": min(200, limit - len(msgs))}
        if args.get("oldest"):
            params["oldest"] = args["oldest"]
        if cursor:
            params["cursor"] = cursor
        resp = _call("conversations.history", params)
        if not resp.get("ok"):
            return f"FAILED {ref} ({cid}): {_explain(resp)}", True
        msgs += resp.get("messages") or []
        cursor = ((resp.get("response_metadata") or {}).get("next_cursor") or "").strip()
        if not cursor or not resp.get("has_more"):
            break
    lines = [f"{len(msgs)} message(s) in {ref} ({cid}), newest first"]
    for m in msgs[:limit]:
        who = (f"bot:{(m.get('bot_profile') or {}).get('name') or m.get('bot_id')}"
               if m.get("bot_id") else f"user:{m.get('user', '?')}")
        reacts = ",".join(f"{r.get('name')}x{r.get('count', 0)}({'|'.join(r.get('users') or [])})"
                          for r in m.get("reactions") or []) or "none"
        text = " ".join((m.get("text") or "").split())
        if len(text) > _TEXT_CHARS:
            text = text[:_TEXT_CHARS] + f" …[clipped, {len(text) - _TEXT_CHARS} more chars]"
        day = time.strftime("%Y-%m-%d", time.gmtime(float(m.get("ts") or 0)))
        lines.append(f"ts={m.get('ts')}\t{day}\t{who}\treactions={reacts}\t{text}")
    return "\n".join(lines), False


def _post(args):
    ref, text = args.get("channel"), (args.get("text") or "").strip()
    if not text:
        return "FAILED: empty message", True
    cid, err = _resolve(ref, people=True)
    if err:
        return f"FAILED {ref}: {err}", True
    resp = _call("chat.postMessage", {"channel": cid, "text": text, "unfurl_links": "false",
                                      "unfurl_media": "false"}, post=True)
    if not resp.get("ok"):
        return f"FAILED {ref} ({cid}): {_explain(resp)}", True
    return f"OK posted to {ref} ({cid}) ts={resp.get('ts')}", False


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
    if name == "list_users":
        return _list_users(args)
    if name == "channel_history":
        return _history(args)
    if name == "post_message":
        return _post(args)
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
