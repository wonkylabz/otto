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
import chats
import claude_cli
import config
import conventions
import delivery
import engine
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

def setUpModule():
    """Hermetic settings store — see the identical note in test_integration.setUpModule."""
    config._SETTINGS_PATH = os.path.join(tempfile.mkdtemp(prefix="otto-settings-"), "absent.json")
    # Hermetic project list, for the same reason and one sharper one: `workspace.refresh_repos`
    # runs `git fetch` on every REGISTERED checkout, so with the developer's real projects.json in
    # place the suite reaches the network and rewrites refs inside their actual repos (observed:
    # all six of them fetched during one `python -m unittest` run). Tests that need repos register
    # their own temp ones by re-pointing this same constant.
    registry.PROJECTS_FILE = os.path.join(tempfile.mkdtemp(prefix="otto-projects-"), "absent.json")
    # Hermetic Slack runtime state — see the identical note in test_integration.setUpModule. Classes
    # that exercise cursors/threads re-point this same constant at their own temp file.
    slack._STATE = os.path.join(tempfile.mkdtemp(prefix="otto-slack-"), "slack-state.json")
    # Hermetic PR-review config + state, same reasoning as the Slack one: `decide()`'s shell
    # writes the state file on every poll, so an un-repointed test marks the developer's real
    # review queue as already-reviewed and those PRs are then never picked up again.
    _tmp_prrev = tempfile.mkdtemp(prefix="otto-prreview-")
    pr_review._CFG = os.path.join(_tmp_prrev, "pr-review.json")
    pr_review._STATE = os.path.join(_tmp_prrev, "pr-review-state.json")
    # Hermetic push bookkeeping (dedupe keys, last-push health, gate action tokens). A test that
    # sends a push otherwise poisons the LIVE dedupe window — the next real approval push inside
    # config.NTFY_DEDUPE_S would be dropped as a duplicate and the phone would never ring.
    delivery._STATE = os.path.join(tempfile.mkdtemp(prefix="otto-notify-"), "notify-state.json")
    # Hermetic gateway stats/model-health store: every gateway call bumps counters here, and the
    # local runtime records model health here too, so without this a plain test run rewrites the
    # developer's live /api/health numbers (and could leave a phantom "model failing" badge).
    gateway._STATS_PATH = os.path.join(tempfile.mkdtemp(prefix="otto-gwstats-"), "gateway-stats.json")
    # Hermetic stores in data/otto.db (audit, chats, memory, solutions, behaviors, knowledge).
    # This was per-class opt-in, so any class reaching a writer without re-pointing it logged into
    # the developer's LIVE trail: 163 phantom entries accumulated there, including a capability
    # that exists only as a fixture, scoring 10 runs at 100% on /api/stats. The trail is immutable
    # by design, so those rows are permanent. All stores resolve through one of these three
    # aliases; classes needing their own DB re-point the same constants.
    # Hermetic Admin stores: data/models.json (endpoints + API keys + phase assignment) and
    # data/policy.json (cap risk/enabled — the approval gate's input). This was per-class
    # opt-in like the DB aliases once were, so any class reaching a WRITER without re-pointing
    # them rewrote the developer's live Admin config; gateway.save round-trips the file, so a
    # stray write silently normalizes it. Classes needing their own re-point the same constants.
    _tmp_admin = tempfile.mkdtemp(prefix="otto-admin-")
    gateway._PATH = os.path.join(_tmp_admin, "models.json")
    policy._PATH = os.path.join(_tmp_admin, "policy.json")
    _tmp_db = os.path.join(tempfile.mkdtemp(prefix="otto-db-"), "otto.db")
    engine._DB = chats._DB = knowledge._DB = _tmp_db


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

