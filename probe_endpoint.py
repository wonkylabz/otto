#!/usr/bin/env python3
"""Which models on an OpenAI-compatible endpoint can actually do what Otto needs?

    ./.venv/bin/python probe_endpoint.py                 # every configured local endpoint
    ./.venv/bin/python probe_endpoint.py OpenAI          # one endpoint, by name
    ./.venv/bin/python probe_endpoint.py OpenAI gpt-5.5  # one model

Three tiny calls per model, one per thing that has been observed to differ between models on
the SAME endpoint (issue #10):

  max_tokens   the classic output-budget parameter, renamed `max_completion_tokens` from the
               gpt-5 generation on.
  temperature  Otto sends 0 for determinism; the newer models accept only their default.
  tools        whether function tools work on /chat/completions AT ALL. This is the one that
               decides whether a model can be an EXECUTION model: the local agent runtime is a
               tool-calling loop, so a model that cannot take tools here can never drive it,
               however good it is.

Why this is a script and not a table in the docs: the answer is per-model, changes with every
model generation, and differs between two models served by the same endpoint. A table would be
stale within weeks and there is no way to tell from the outside that it had gone stale — so
`docs/openai-models.md` carries the structure and a DATED snapshot, and points here to refresh
it. Read-only: it never writes config, and every call is capped at a few hundred tokens.
"""
import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.request

import gateway

# Not chat models (audio/image/embedding/…), or served only by other endpoints. Probing them
# adds a screenful of "not a chat model" that buries the rows anyone is reading this for.
_NOT_CHAT = re.compile(r"tts|transcribe|whisper|embedding|dall-e|moderation|image|audio|"
                       r"realtime|deep-research|search|sora|computer-use|davinci|babbage|"
                       r"^curie|^ada|^text-")
# A dated id is the same model as its floating alias; listing both doubles the table for nothing.
_DATED = re.compile(r"-20\d\d-\d\d-\d\d$")

_TOOL = {"type": "function", "function": {"name": "noop", "description": "capability probe",
                                          "parameters": {"type": "object", "properties": {}}}}


def _call(entry, body, timeout=90):
    """POST one body; None when the server accepted it, else its error message."""
    req = urllib.request.Request(entry["base_url"].rstrip("/") + "/chat/completions",
                                 method="POST", headers=gateway.request_headers(entry),
                                 data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}").get("error", {}).get("message", "") or f"HTTP {e.code}"
        except Exception:  # noqa: BLE001
            return f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return f"unreachable: {e}"


def _tool_verdict(msg):
    """What a tools probe's outcome MEANS. Several failures look alike and are not alike: a
    deprecated model and a model that refuses tools both 400, and only one of them is a reason
    not to pick it for execution."""
    if msg is None:
        return "ok"
    m = msg.lower()
    if "deprecated" in m:
        return "deprecated"
    if "not a chat model" in m or "could not be found" in m:
        return "not a chat model"
    # "Function tools with reasoning_effort are not supported for X in /v1/chat/completions."
    # The model itself chats fine here — only tools are refused. This is issue #10's wall.
    if "function tools" in m and "not supported" in m:
        return "NO TOOLS"
    if "v1/responses" in m:
        return "responses API only"
    # The model answered and ran out of our deliberately small probe budget — that is a pass.
    if "output limit" in m or ("max_tokens" in m and "higher" in m):
        return "ok"
    return msg[:60]


def probe(entry, model_id):
    """(max_tokens, temperature-0, tools) for one model, each 'ok' or why not."""
    msgs = [{"role": "user", "content": "hi"}]
    classic = _call(entry, {"model": model_id, "messages": msgs, "max_tokens": 16})
    if classic and "does not exist" in classic.lower():
        return None
    temp = _call(entry, {"model": model_id, "messages": msgs,
                         "max_completion_tokens": 16, "temperature": 0})
    # A bigger budget here than the other two: a reasoning model spends tokens thinking before
    # it emits a tool call, and a budget death would read as a refusal.
    tools = _call(entry, {"model": model_id, "messages": msgs,
                          "max_completion_tokens": 256, "tools": [_TOOL]})
    return ("ok" if not classic else "no", "ok" if not temp else "no", _tool_verdict(tools))


def endpoint_report(entry, only=None):
    print(f"\n=== {entry.get('endpoint') or entry['name']} — {entry['base_url']}")
    try:
        found = gateway.discover_models(entry["base_url"], entry.get("api_key_env"),
                                        headers=entry.get("headers"))
    except Exception as e:  # noqa: BLE001
        print(f"    cannot list models: {e}")
        return
    ids = [g["id"] for g in found]
    if only:
        ids = [i for i in ids if i == only]
    else:
        ids = sorted(i for i in ids if not _NOT_CHAT.search(i) and not _DATED.search(i))
    if not ids:
        print("    no chat models to probe")
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(zip(ids, pool.map(lambda i: probe(entry, i), ids)))
    print(f"    {'model':26} {'max_tokens':11} {'temp=0':8} tools")
    print("    " + "-" * 64)
    usable, blocked = [], []
    for mid, r in rows:
        if r is None:
            continue
        classic, temp, tools = r
        if tools in ("deprecated", "not a chat model"):
            continue
        shown = mid if len(mid) <= 26 else mid[:23] + "..."
        print(f"    {shown:26} {classic:11} {temp:8} {tools}")
        (usable if tools == "ok" else blocked).append(mid)
    print()
    print("    max_tokens / temp=0 need no action — Otto learns each from the server's own 400")
    print("    and remembers it on the model entry (`quirks`).")
    print()
    # The BLOCKED list is the answer anyone runs this for, and it is the short one. Printing the
    # usable list in full was a paragraph of ids that buried the four that matter.
    print(f"    Can drive the agentic loop (EXECUTION): {len(usable)} of {len(usable) + len(blocked)}")
    if blocked:
        print("    CANNOT be an execution model — tool-free tiers only:")
        for mid in blocked:
            print(f"      - {mid}")


def main(argv):
    cfg = gateway.load()
    want, only = (argv + [None, None])[:2]
    entries = [m for m in cfg.get("pool", [])
               if m.get("provider") != "claude" and m.get("base_url")]
    if want:
        entries = [m for m in entries if want in (m.get("endpoint"), m.get("name"))]
        if not entries:
            print(f"no configured endpoint or model named {want!r}")
            return 1
    seen, uniq = set(), []
    for m in entries:                       # one report per ENDPOINT, not per model on it
        key = (m["base_url"], m.get("api_key_env"))
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    for entry in uniq:
        endpoint_report(entry, only)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
