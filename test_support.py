"""Shared fixtures for the split test suite.

test_core.py was 15,297 lines and the repo's #1 churn file — every change touched it, and a
1,100-test file is where a test that proves nothing hides. The classes are now grouped by the
layer they cover, mirroring `.claude/rules/*.md`; everything they SHARE lives here.

`setUpModule` is imported by every test module so unittest calls it once per module: it
re-points each live-state alias at a temp dir. Splitting the suite without carrying it into
all of them would have put the phantom-row bug (`LiveStoreIsolationTests`) straight back.
"""
import ast
import glob
import contextlib
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import board
import chats
import claude_cli
import config
import conventions
import delivery
import engine
import estop
import file_safety
import memory
import error_classifier
import events
import gateway
import intents
import judging
import knowledge
import local_runtime
import mcp_client
import plans
import policy
import pr_review
import privacy
import registry
import repos
import server
import workspace
import runbooks
import scheduler
import slack
import slack_state
import storage
import supervisor
import contextlib

try:                                       # the Temporal layer — absent under a bare python3
    import activities
    import workflows
    _HAS_TEMPORAL = True
except Exception:  # noqa: BLE001
    _HAS_TEMPORAL = False

# Every live-state path Otto derives from `config.DATA_DIR`, as (module, attribute, relative
# name). The suite stands ONE temp directory in for `data/` and re-derives all of them from it,
# so a store is redirected by construction rather than by someone remembering to add a fourth
# mkdtemp. `LiveStoreIsolationTests.test_the_redirect_table_covers_every_data_dir_store` scans
# the source for module-level DATA_DIR joins and fails if one is missing from this table, which
# is what makes the list self-maintaining: a store added tomorrow is covered or the suite is red.
_DATA_STORES = (
    ("board", "_CFG", "board.json"),
    ("claude_cli", "TRANSCRIPTS", "transcripts"),
    ("config", "DB_PATH", "otto.db"),
    ("conventions", "_STORE", "conventions.json"),
    ("delivery", "_STATE", "notify-state.json"),
    ("events", "_RULES", "event-rules.json"),
    ("events", "_SEEN_FILE", "event-replay.json"),
    ("gateway", "_PATH", "models.json"),
    ("gateway", "_STATS_PATH", "gateway-stats.json"),
    ("local_runtime", "SESSIONS", "local-sessions"),
    ("mcp_client", "_CATALOGUE", "mcp-tools.json"),
    ("policy", "_PATH", "policy.json"),
    ("policy", "_CUSTOM", "capabilities.json"),
    ("policy", "_MCPDEF", "mcp-servers.json"),
    ("policy", "_CONN_CACHE", "mcp-connectors-cache.json"),
    ("pr_review", "_CFG", "pr-review.json"),
    ("pr_review", "_STATE", "pr-review-state.json"),
    ("registry", "CUSTOM_FILE", "capabilities.json"),
    ("registry", "PROJECTS_FILE", "projects.json"),
    ("repos", "MANAGED", "repos"),
    ("scheduler", "_LEGACY_STORE", "schedules.json"),
    ("server", "_DISMISSED_PATH", "dismissed.json"),
    ("server", "_RETRIES_PATH", "retries.json"),
    ("slack", "_CFG", "slack.json"),
    ("slack", "_STATE", "slack-state.json"),
    ("workspace", "WORKSPACES", "workspaces"),
)

# Stores whose path is resolved LAZILY (on first use, from config.DATA_DIR) rather than bound at
# import. Re-pointing DATA_DIR covers a resolver that has not run yet; clearing the cache is what
# covers one that has — an earlier module in the same process may already have frozen the live
# path into it. Kept beside the table above so the two are read together.
_LAZY_STORES = (
    ("config", "_SETTINGS_PATH", None),   # settings.json — None means "fall through to DATA_DIR"
    ("estop", "_PATH", None),             # the ESTOP sentinel
    ("runbooks", "_STORE", None),         # runbooks.json, via runbooks.store_path()
)


def redirect_live_state():
    """Stand a fresh temp directory in for `data/` and re-point every store at it.

    NOTHING in the suite may write to the developer's real data/. This used to be per-class
    opt-in, and the cost was not hypothetical: 163 phantom rows accumulated in the LIVE audit
    trail — which is immutable by design, so they are permanent — including a fixture-only
    capability that scored 10 runs at 100% on /api/stats. The DB aliases were then redirected
    module-wide, but every OTHER store stayed opt-in and two leaked the same way (issue #41):
    the pool tests' fake stdio server sat in the live data/mcp-tools.json, its `broken` entry
    carrying a `failed` stamp that suppresses a real server of that name for
    LOCAL_MCP_PROBE_TTL_S, and a bundle-import test wrote fixture caps into data/capabilities.json.

    The specific damage a missed store does, store by store, is why this is one call and not a
    convention: `slack-state.json` — a bogus cursor makes a real channel deaf; `projects.json` —
    `workspace.refresh_repos` git-fetches every registered checkout, so an unpinned run reaches
    the network and rewrites refs inside the developer's actual repos (observed: all six fetched
    during one run); `pr-review-state.json` — a stray poll marks their real review queue
    already-reviewed and only a genuine re-request brings those PRs back; `notify-state.json` —
    a test push poisons the dedupe window, so the next real approval push inside NTFY_DEDUPE_S is
    dropped and the phone never rings; `models.json`/`policy.json` — the Admin config, which
    gateway.save round-trips and so silently normalizes; `gateway-stats.json` — the live
    /api/health numbers and a phantom "model failing" badge; `settings.json` — a knob the
    developer flipped in Admin changes what the suite tests.

    The temp dir stands in for `data/` and is deliberately a CHILD of a second temp dir rather
    than the mkdtemp root itself, because `file_safety._otto_root()` is `dirname(DATA_DIR)` —
    i.e. "Otto's own checkout". With DATA_DIR as the mkdtemp root, that resolves to the SYSTEM
    temp dir, and two things follow: `<tmpdir>/.env` joins the deny set, and any run whose cwd is
    under the system temp dir is treated as an Otto-introspection run, so `_reads_allowed_from`
    exempts it and Otto's own state stops being read-denied. That is invisible on macOS, where
    `tempfile.gettempdir()` is a per-user `/var/folders/...` path, and fires on Linux, where it is
    the shared `/tmp` that tests legitimately use as an unrelated cwd. Mirroring the real layout
    (`<checkout>/data`) keeps `_otto_root()` a private directory nothing else can collide with.

    Returns the temp directory, for a test that wants to inspect what was written."""
    root = os.path.join(tempfile.mkdtemp(prefix="otto-home-"), "data")
    os.makedirs(root, exist_ok=True)
    # config.DATA_DIR itself, because not every path is a module constant: the per-run MCP config
    # (`engine._mcp_config_path`, `activities`' `.mcp-active.json`) and file_safety's deny globs
    # join it at CALL time, and those are the writes no table can enumerate.
    config.DATA_DIR = root
    for mod, attr, rel in _DATA_STORES:
        setattr(sys.modules[mod], attr, os.path.join(root, rel))
    for mod, attr, value in _LAZY_STORES:
        setattr(sys.modules[mod], attr, value)
    # The three DB aliases are copies of config.DB_PATH taken at import, so re-pointing the
    # constant above does not move them. All six stores in otto.db resolve through one of these.
    engine._DB = chats._DB = knowledge._DB = config.DB_PATH
    return root


def setUpModule():
    """Hermetic live state for every class in the module — see redirect_live_state()."""
    redirect_live_state()


class _Cap:
    """Minimal capability stand-in — mcp_client only reads `declared_tools`."""
    def __init__(self, tools=(), name="c"):
        self.name, self.declared_tools = name, list(tools)


_FAKE_MCP_SERVER = '''
import json, sys
TOOLS = [{"name": "echo", "description": "echo back", "inputSchema":
          {"type": "object", "properties": {"text": {"type": "string"}}}},
         {"name": "boom", "description": "always fails", "inputSchema": {"type": "object"}}]
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
send({"jsonrpc": "2.0", "method": "notifications/message",
      "params": {"level": "info", "data": "a banner before anything"}})
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    m, rid = msg.get("method"), msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18",
              "serverInfo": {"name": "fake", "version": "1"}, "capabilities": {"tools": {}}}})
    elif m == "tools/list":
        cur = (msg.get("params") or {}).get("cursor")
        if not cur:
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"tools": TOOLS[:1], "nextCursor": "page2"}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[1:]}})
    elif m == "tools/call":
        p = msg.get("params") or {}
        if p.get("name") == "boom":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "it broke"}], "isError": True}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": "echo: " + str((p.get("arguments") or {}).get("text"))},
                {"type": "image", "data": "..."}]}})
    elif m and m.startswith("notifications/"):
        pass
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}})
'''


def _cap_stub():
    c = registry.Capability("custom", "worker", "a worker")
    c.risk = "write"
    return c


def _fake_embed(texts, model_name=None):
    """Deterministic stand-in for gateway.embed: a 3-dim topic vector so cosine ranking is
    testable without a real embedding model/network."""
    def vec(t):
        t = (t or "").lower()
        return [float("vpn" in t), float("rds" in t or "database" in t), float("cat" in t or "poem" in t)]
    return [vec(t) for t in texts]


@contextlib.contextmanager
def _patched_registry_dirs(*, agents, skills, plugins, custom, projects):
    saved = (registry.AGENTS_DIR, registry.SKILLS_DIR, registry.PLUGINS_FILE,
             registry.CUSTOM_FILE, registry.PROJECTS_FILE)
    registry.AGENTS_DIR, registry.SKILLS_DIR = agents, skills
    registry.PLUGINS_FILE, registry.CUSTOM_FILE, registry.PROJECTS_FILE = plugins, custom, projects
    try:
        yield
    finally:
        (registry.AGENTS_DIR, registry.SKILLS_DIR, registry.PLUGINS_FILE,
         registry.CUSTOM_FILE, registry.PROJECTS_FILE) = saved


def _storage_hammer(path, worker_id, iterations):
    """Module-level so multiprocessing can pickle it: each worker appends `iterations`
    entries through the locked read-modify-write path."""
    for i in range(iterations):
        storage.mutate_json(path, lambda data: data + [[worker_id, i]], default=[])


_UI_ROOT = os.path.dirname(os.path.abspath(__file__))
_UI_TAG = re.compile(r'^<script src="/js/([\w.-]+)"></script>$|'
                     r'^<link rel="stylesheet" href="/css/([\w.-]+)">$', re.M)


def ui_src():
    """The UI as the BROWSER receives it: `web/index.html` with every `<script src>` and
    stylesheet `<link>` replaced by the file's own text, in document order.

    `web/index.html` is a tag list now, so a test that opened it directly would assert
    against 14 KB of tags and pass vacuously. Inlining here keeps every existing assertion
    valid — including the ordering ones (`src.index(a) < src.index(b)`), which only mean
    anything against one document in load order. This is the ONE reader; a new UI file needs
    no change here as long as it is pulled in by a tag.
    """
    path = os.path.join(_UI_ROOT, "web", "index.html")
    with open(path, encoding="utf-8") as fh:
        doc = fh.read()

    def inline(m):
        js, css = m.group(1), m.group(2)
        sub = os.path.join("js", js) if js else os.path.join("css", css)
        with open(os.path.join(_UI_ROOT, "web", sub), encoding="utf-8") as fh:
            body = fh.read()
        return (f"<script>\n{body}</script>" if js else f"<style>\n{body}</style>")

    return _UI_TAG.sub(inline, doc)
