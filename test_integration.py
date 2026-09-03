"""Integration / E2E tests for the `claude -p` paths — with Claude fully MOCKED.

Two layers, no network and no tokens:
  * HTTP API (route -> clarify -> run -> audit) driven over a real ephemeral socket,
    with `engine._claude` and `gateway.complete` stubbed. Stdlib-only.
  * The Temporal workflow's signal path (clarification + approve/deny) on an isolated
    in-memory task queue. Self-skips when temporalio isn't installed, so the suite stays
    install-free.

    python3 -m unittest test_integration -v
"""
import json
import os
import shutil
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from socketserver import ThreadingTCPServer

import config
import contracts
import delivery
import engine
import events
import gateway
import policy
import local_runtime
import registry

try:
    from concurrent.futures import ThreadPoolExecutor

    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    import activities
    _HAS_TEMPORAL = True
except Exception:  # noqa: BLE001
    _HAS_TEMPORAL = False


async def _time_skipping_env(attempts=3):
    """`WorkflowEnvironment.start_time_skipping()`, retried. Use this, never the raw call.

    It boots a real ephemeral Temporal test server and gives up on a hard-coded 5s connect
    timeout. That is generous on a laptop and tight on a cold CI runner, so roughly one job in
    six died with "Failed connecting to test server after 5 seconds" — on a DIFFERENT test each
    time, since any of the 51 call sites can be the unlucky one. A red suite that moves around
    like that says nothing about the code and trains everyone to re-run instead of read it.

    Retry the STARTUP, never the test body: a flaky server boot and a flaky assertion look
    identical in a log, and only one of them is safe to paper over."""
    import asyncio
    last = None
    for i in range(attempts):
        try:
            return await WorkflowEnvironment.start_time_skipping()
        except RuntimeError as e:                # the Rust bridge raises a bare RuntimeError
            if "test server" not in str(e):      # a real failure must not be retried into noise
                raise
            last = e
            await asyncio.sleep(0.5 * (i + 1))
    raise last


# The execution BACKEND is now chosen from the models config (a local execution model routes
# runs at local_runtime instead of claude -p), so tests must never read the developer's real
# data/models.json — a live Admin pick would silently redirect every mocked run. Pin a
# Claude-only config for the whole module; classes that test local dispatch re-patch in setUp.
_MODULE_CFG = {
    "pool": [{"name": "claude-sonnet", "provider": "claude", "model": "claude-sonnet-4-6"}],
    "assign": {t: "claude-sonnet" for t in gateway.TASKS},
}
_orig_gateway_load = None
_orig_pr_copy = None


def setUpModule():
    global _orig_gateway_load, _orig_pr_copy
    # Hermetic settings store: config.setting() resolves env > data/settings.json > code default, so
    # a developer who flipped a knob in Admin would otherwise change what this suite tests. Point it
    # at a path that cannot exist so every test sees the code defaults (or its own monkeypatch).
    config._SETTINGS_PATH = os.path.join(tempfile.mkdtemp(prefix="otto-settings-"), "absent.json")
    # Hermetic project list — `workspace.refresh_repos` git-fetches every registered checkout, so
    # the developer's real projects.json would put the network (and their repos' refs) in the path
    # of this suite. See the fuller note in test_core.setUpModule.
    registry.PROJECTS_FILE = os.path.join(tempfile.mkdtemp(prefix="otto-projects-"), "absent.json")
    # Hermetic Slack runtime state: poll_slack / deliver_result now write watched-thread records
    # (read cursors + session ids), so an unpatched suite would mutate the developer's live
    # data/slack-state.json — and a bogus cursor there makes a real channel deaf.
    import slack
    slack._STATE = os.path.join(tempfile.mkdtemp(prefix="otto-slack-"), "slack-state.json")
    # Hermetic PR-review config + state — see the identical note in test_support.setUpModule: a
    # stray poll marks the developer's real review queue as already-reviewed, and only a genuine
    # re-request on GitHub would ever bring those PRs back.
    import pr_review
    _tmp_prrev = tempfile.mkdtemp(prefix="otto-prreview-")
    pr_review._CFG = os.path.join(_tmp_prrev, "pr-review.json")
    pr_review._STATE = os.path.join(_tmp_prrev, "pr-review-state.json")
    # Hermetic push bookkeeping (dedupe keys, last-push health, gate action tokens). A test that
    # sends a push otherwise poisons the LIVE dedupe window — the next real approval push inside
    # config.NTFY_DEDUPE_S would be dropped as a duplicate and the phone would never ring.
    delivery._STATE = os.path.join(tempfile.mkdtemp(prefix="otto-notify-"), "notify-state.json")
    # Hermetic gateway stats/model-health store — see the identical note in test_core.setUpModule:
    # a suite run must not rewrite the developer's live /api/health numbers or leave a phantom
    # "model failing" badge behind.
    gateway._STATS_PATH = os.path.join(tempfile.mkdtemp(prefix="otto-gwstats-"), "gateway-stats.json")
    # Hermetic stores in data/otto.db (audit, chats, memory, solutions, behaviors, knowledge).
    # This was per-class opt-in, so any class reaching a writer without re-pointing it logged into
    # the developer's LIVE trail: 163 phantom entries accumulated there, including a capability
    # that exists only as a fixture, scoring 10 runs at 100% on /api/stats. The trail is immutable
    # by design, so those rows are permanent. All stores resolve through one of these three
    # aliases; classes needing their own DB re-point the same constants.
    import chats, knowledge
    _tmp_db = os.path.join(tempfile.mkdtemp(prefix="otto-db-"), "otto.db")
    engine._DB = chats._DB = knowledge._DB = _tmp_db
    _orig_gateway_load = gateway.load
    gateway.load = lambda: json.loads(json.dumps(_MODULE_CFG))
    # PR title/body drafting (engine.pr_copy) makes a gateway call inside finalize_workspace;
    # pin it module-wide so no repo-mode test ever reaches a model. Its real behavior is
    # covered by test_core.PrCopyTests with gateway.complete mocked.
    _orig_pr_copy = engine.pr_copy
    engine.pr_copy = lambda request, summary=None: {
        "title": (request or "Otto automated change")[:120], "body": "test body"}
    # Hermetic Admin stores: data/models.json (endpoints + API keys + phase assignment) and
    # data/policy.json (cap risk/enabled — the approval gate's input). This was per-class
    # opt-in like the DB aliases once were, so any class reaching a WRITER without re-pointing
    # them rewrote the developer's live Admin config; gateway.save round-trips the file, so a
    # stray write silently normalizes it. Classes needing their own re-point the same constants.
    _tmp_admin = tempfile.mkdtemp(prefix="otto-admin-")
    gateway._PATH = os.path.join(_tmp_admin, "models.json")
    policy._PATH = os.path.join(_tmp_admin, "policy.json")


def tearDownModule():
    gateway.load = _orig_gateway_load
    engine.pr_copy = _orig_pr_copy


def _post(base, path, payload):
    """POST JSON; return (status, parsed-body) without raising on 4xx/5xx."""
    req = urllib.request.Request(
        base + path, method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(base, path):
    """GET JSON; return (status, parsed-body) without raising on 4xx/5xx."""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _post_event(base, source, payload, secret="itest-secret", bad_sig=False):
    """POST a signed event; returns (status, body)."""
    import hashlib
    import hmac
    raw = json.dumps(payload).encode()
    sig = "bad" if bad_sig else hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        base + "/api/events/" + source, method="POST", data=raw,
        headers={"Content-Type": "application/json", "X-Otto-Signature": sig})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class CsrfOriginGuardTests(unittest.TestCase):
    """The API is unauthenticated (localhost, single user), so a cross-site POST must be refused
    before it reaches any handler — otherwise any page the user visits can start a write run or
    approve its own gate. Guard is server.Handler._csrf_ok; these drive it over real HTTP."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.httpd = ThreadingTCPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _status(self, path="/api/nonexistent", headers=None):
        """POST and return the status only. An ALLOWED request reaches the dispatcher and 404s on
        this unknown path; a refused one never gets there and 403s."""
        req = urllib.request.Request(
            self.base + path, method="POST", data=b"{}",
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_a_request_with_no_origin_is_allowed(self):
        # curl, the webhook senders and these tests send no Origin — only browsers do.
        self.assertEqual(self._status(), 404)

    def test_a_same_origin_browser_post_is_allowed(self):
        self.assertEqual(self._status(headers={"Origin": self.base,
                                               "Sec-Fetch-Site": "same-origin"}), 404)
        # Trailing slash is the same origin, not a different one.
        self.assertEqual(self._status(headers={"Origin": self.base + "/"}), 404)

    def test_a_cross_site_origin_is_refused(self):
        self.assertEqual(self._status(headers={"Origin": "https://evil.example"}), 403)
        # Right host, wrong port — a different local app is still cross-origin.
        self.assertEqual(self._status(
            headers={"Origin": "http://127.0.0.1:%d" % (self.port + 1)}), 403)
        # A hostname that merely CONTAINS a local one doesn't count.
        self.assertEqual(self._status(headers={"Origin": "http://localhost.evil.example"}), 403)
        # A malformed port must not crash the guard into a 500 (urlparse defers the raise to .port).
        self.assertEqual(self._status(headers={"Origin": "http://127.0.0.1:notaport"}), 403)

    def test_an_opaque_origin_is_refused(self):
        # Sandboxed iframe / file:// page — nothing to check against, so never trusted.
        self.assertEqual(self._status(headers={"Origin": "null"}), 403)

    def test_sec_fetch_site_refuses_even_without_an_origin(self):
        self.assertEqual(self._status(headers={"Sec-Fetch-Site": "cross-site"}), 403)
        self.assertEqual(self._status(headers={"Sec-Fetch-Site": "same-site"}), 403)
        self.assertEqual(self._status(headers={"Sec-Fetch-Site": "none"}), 404)

    def test_the_event_ingress_is_exempt_because_it_carries_its_own_hmac(self):
        # A webhook sender is not a browser; its authentication is the signature, not the origin.
        self.assertNotEqual(self._status("/api/events/itest",
                                         headers={"Origin": "https://hooks.example"}), 403)

    def test_an_explicitly_allowed_origin_passes(self):
        # Escape hatch for the same-origin proxy used to drive the UI headlessly.
        srv = self.server
        orig = srv._ALLOWED_ORIGINS
        srv._ALLOWED_ORIGINS = {"http://localhost:9999"}
        self.addCleanup(setattr, srv, "_ALLOWED_ORIGINS", orig)
        self.assertEqual(self._status(headers={"Origin": "http://localhost:9999/"}), 404)
        self.assertEqual(self._status(headers={"Origin": "http://localhost:9998"}), 403)


class _FakeClaude:
    """Stand-in for engine._claude — records calls, returns canned JSON (no subprocess)."""

    def __init__(self, result="the report body", cost=0.01):
        self.calls = []
        self.result, self.cost = result, cost

    def __call__(self, prompt, allowed_tools=None, model=None, mcp_config_path=None,
                 resume_session=None, system_context=None, timeout=900, cwd=None, **kwargs):
        self.calls.append({"prompt": prompt, "model": model, "tools": allowed_tools,
                           "resume": resume_session, "cwd": cwd})
        return {"result": self.result, "total_cost_usd": self.cost, "session_id": "test-sess"}


class HttpApiTests(unittest.TestCase):
    """route -> clarify -> run -> audit over real HTTP, Claude mocked. (The write GATE is
    browser-mediated on this path, so approve/deny is covered by the workflow test below.)"""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

        readcap = registry.Capability("skill", "demo-read", "a read-only status report")
        readcap.risk = "read"
        writecap = registry.Capability("skill", "demo-write", "creates something")
        writecap.risk = "write"
        cls._orig_caps = server.CAPS
        server.CAPS = [readcap, writecap]

        # Hush the engine/gateway trace prints so test output stays readable.
        cls._noop = lambda *a, **k: None
        cls._traces = [(engine, "trace"), (engine, "say"), (gateway, "trace")]
        cls._orig_traces = [(m, n, getattr(m, n)) for m, n in cls._traces]
        for m, n in cls._traces:
            setattr(m, n, cls._noop)

        cls._orig_claude = engine._claude
        cls.fake = _FakeClaude()
        engine._claude = cls.fake

        cls._orig_complete = gateway.complete
        cls.routing = ["0"]          # index routing picks
        cls.clarify_reply = ["OK"]   # clarify gateway response

        def fake_complete(task, prompt):
            return {"routing": cls.routing[0], "clarify": cls.clarify_reply[0],
                    "verify": "PASS", "memory": "NONE"}.get(task, "")
        gateway.complete = fake_complete

        cls._tmp = tempfile.mkdtemp(prefix="otto-itest-")
        cls._orig_db = engine._DB
        engine._DB = os.path.join(cls._tmp, "otto.db")

        # Event ingress: enable, point at a temp rule file, and capture workflow starts
        # (so the endpoint can be exercised without a live Temporal server).
        cls._orig_secret, events.SECRET = events.SECRET, "itest-secret"
        cls._orig_rules = events._RULES
        events._RULES = os.path.join(cls._tmp, "event-rules.json")
        with open(events._RULES, "w") as f:
            json.dump([{"source": "itest", "template": "handle {what}",
                        "cap": "demo-write", "auto_approve": True}], f)
        cls.started = []

        async def fake_wf_start(wid, params):
            cls.started.append({"id": wid, "params": params})
        cls._orig_wf_start, server._wf_start = server._wf_start, fake_wf_start

        cls.httpd = ThreadingTCPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        events.SECRET = cls._orig_secret
        events._RULES = cls._orig_rules
        cls.server._wf_start = cls._orig_wf_start
        engine._claude = cls._orig_claude
        gateway.complete = cls._orig_complete
        engine._DB = cls._orig_db
        cls.server.CAPS = cls._orig_caps
        for m, n, fn in cls._orig_traces:
            setattr(m, n, fn)

    def test_accept_endpoint_records_the_override_and_dismisses(self):
        # Route wiring over real HTTP: the button's POST must land, write the row, and hide the
        # card. Isolated from the developer's real dismissed-ids store.
        srv = self.server
        orig, srv._DISMISSED_PATH = srv._DISMISSED_PATH, os.path.join(self._tmp, "dismissed.json")
        self.addCleanup(setattr, srv, "_DISMISSED_PATH", orig)
        real, engine._extract_solution = engine._extract_solution, lambda *a, **k: ""
        self.addCleanup(setattr, engine, "_extract_solution", real)
        engine.record_terminal("wf-http-acc", "ship the alert",
                               {"kind": "skill", "name": "demo-write"}, "verify_exhausted")
        st, body = _post(self.base, "/api/needs-you/accept", {"id": "wf-http-acc"})
        self.assertEqual((st, body.get("ok")), (200, True))
        self.assertIn("wf-http-acc", srv._dismissed_ids())
        self.assertTrue(any(e.get("outcome") == "human_accepted"
                            and e.get("workflow") == "wf-http-acc"
                            for e in engine.iter_audit_entries()))

    def test_accept_of_an_unknown_run_404s(self):
        st, body = _post(self.base, "/api/needs-you/accept", {"id": "wf-nope"})
        self.assertEqual(st, 404)
        self.assertIn("no audit history", body["error"])

    def test_memory_endpoints_are_global_scoped(self):
        """/api/memory + /api/memory/clear over real HTTP. These two used to reach for
        data/memory.json with a raw open()/write_json, bypassing the store helpers entirely (and
        were covered by NO test), which is how they drifted. Both are GLOBAL-scoped by design:
        a project namespace is neither listed nor wiped here."""
        cap = registry.Capability("skill", "demo-read", "d")
        engine._remember(cap, "global req", ["a global fact"])
        engine._remember(cap, "proj req", ["a project fact"], project="/repos/acme-widgets")

        st, body = _get(self.base, "/api/memory")
        self.assertEqual(st, 200)
        self.assertEqual(body["count"], 1)                      # the project row is NOT listed
        self.assertEqual(body["facts_total"], 1)
        self.assertEqual(body["events"][0]["request"], "global req")
        self.assertNotIn("a project fact", json.dumps(body))

        st, _ = _post(self.base, "/api/memory/clear", {})
        self.assertEqual(st, 200)
        self.assertEqual(_get(self.base, "/api/memory")[1]["count"], 0)     # global wiped
        ns = engine._memory_ns("/repos/acme-widgets")
        self.assertEqual(len(engine.memory_events(ns)), 1)                  # namespace survives
        engine.clear_memory(every=True)                                     # ... until asked

    def test_memory_delete_forgets_one_fact_over_http(self):
        """/api/memory/delete: the Memory tab's per-fact "forget". Clearing the whole store was
        previously the only way to remove one wrong fact (e.g. "vLLM is not deployed in
        production"), which cost every right fact stored with it."""
        cap = registry.Capability("skill", "demo-read", "d")
        engine._remember(cap, "global req", ["vllm is not deployed in production",
                                             "the reaper sweeps in-progress cards every 300s"])
        row_id = _get(self.base, "/api/memory")[1]["events"][0]["id"]

        st, body = _post(self.base, "/api/memory/delete",
                         {"id": row_id, "fact": "vllm is not deployed in production"})
        self.assertEqual((st, body["ok"]), (200, True))
        after = _get(self.base, "/api/memory")[1]
        self.assertEqual(after["facts_total"], 1)                            # sibling survives
        self.assertNotIn("not deployed in production", json.dumps(after))

        # unknown fact -> 404, and nothing else is touched
        st, body = _post(self.base, "/api/memory/delete", {"id": row_id, "fact": "never stored"})
        self.assertEqual((st, body["ok"]), (404, False))
        self.assertEqual(_get(self.base, "/api/memory")[1]["facts_total"], 1)
        engine.clear_memory()

    def test_solutions_and_behaviors_endpoints_roundtrip(self):
        """/api/solutions + /api/behaviors and their delete/clear paths over real HTTP — also
        previously untested."""
        cap = registry.Capability("skill", "demo-read", "d")
        engine._remember_solution(cap, "renew the vpn certificate", "the vpn approach")
        row = engine.add_behavior("always run the tests first", scope="global")
        self.assertTrue(row and row["id"])           # truthy return — policy.import_profile counts it

        st, body = _get(self.base, "/api/solutions")
        self.assertEqual(st, 200)
        self.assertEqual([s["approach"] for s in body["solutions"]], ["the vpn approach"])
        st, body = _get(self.base, "/api/behaviors")
        self.assertEqual(st, 200)
        self.assertEqual([b["rule"] for b in body["behaviors"]], ["always run the tests first"])

        _post(self.base, "/api/behaviors/update", {"id": row["id"], "rule": "edited rule"})
        self.assertEqual(engine.behaviors()[0]["rule"], "edited rule")
        _post(self.base, "/api/behaviors/delete", {"id": row["id"]})
        self.assertEqual(engine.behaviors(), [])
        _post(self.base, "/api/solutions/clear", {})
        self.assertEqual(engine.solutions(), [])

    def test_conventions_endpoint_lists_repos_without_deriving(self):
        """Admin → Project repos reads what the judge is enforcing. GET must stay cache-only:
        it rides in loadAdmin's Promise.all, so a derivation here is the panel's load time."""
        import conventions
        orig = conventions.digest
        conventions.digest = lambda *a, **k: self.fail("GET /api/conventions must not derive")
        try:
            st, body = _get(self.base, "/api/conventions")
        finally:
            conventions.digest = orig
        self.assertEqual(st, 200)
        self.assertIsInstance(body["repos"], list)

    def test_conventions_refresh_only_accepts_a_registered_repo(self):
        """Re-derivation reads a path off disk and spends model calls, so the path is resolved
        against the trusted registry — never taken from the client."""
        st, body = _post(self.base, "/api/conventions/refresh", {"path": "/etc"})
        self.assertEqual(st, 400)
        self.assertIn("registered", body.get("error", ""))
        st, body = _post(self.base, "/api/conventions/refresh", {"path": ""})
        self.assertEqual(st, 400)

    def test_settings_get_and_post_roundtrip(self):
        """Admin → Runtime settings over real HTTP: GET reports value + provenance, POST persists a
        validated diff, and the change is visible to a subsequent config.setting() read (which is
        how the OTHER process — worker.py — picks up an Admin edit without a restart)."""
        st, body = _get(self.base, "/api/settings")
        self.assertEqual(st, 200)
        self.assertIn("local_fallback", body["settings"])
        self.assertTrue(body["settings"]["local_fallback"]["value"])       # default: fallback on
        self.assertEqual(body["settings"]["local_fallback"]["env"], "OTTO_LOCAL_FALLBACK")

        st, body = _post(self.base, "/api/settings",
                         {"settings": {"local_fallback": False, "max_attempts": 5,
                                       "plan_mode": "nonsense", "bogus_key": 1}})
        self.assertEqual(st, 200)
        self.assertFalse(body["settings"]["local_fallback"]["value"])
        self.assertEqual(body["settings"]["max_attempts"]["value"], 5)
        self.assertTrue(body["settings"]["max_attempts"]["stored"])
        self.assertEqual(body["settings"]["plan_mode"]["value"], config.PLAN_MODE)  # invalid dropped
        self.assertNotIn("bogus_key", body["settings"])
        self.assertIs(config.setting("local_fallback"), False)
        # Restore so later tests in this class see the defaults.
        _post(self.base, "/api/settings",
              {"settings": {"local_fallback": True, "max_attempts": config.MAX_VERIFY_ATTEMPTS}})
        self.assertIs(config.setting("local_fallback"), True)

    def test_delete_chat_terminates_inflight_workflow(self):
        """Deleting a chat whose turn is still in flight (a live run_id — e.g. paused awaiting
        approval) hard-stops the workflow and dismisses its board card, so it can't zombie;
        a chat with no run_id is left alone."""
        import asyncio
        import chats as chats_mod
        tmp = tempfile.mkdtemp(prefix="otto-delchat-")
        saved = (chats_mod._DB, self.server._DISMISSED_PATH,
                 self.server._wf_terminate, self.server.tc.run, self.server.TEMPORAL_OK)
        chats_mod._DB = os.path.join(tmp, "otto.db")
        self.server._DISMISSED_PATH = os.path.join(tmp, "dismissed.json")
        killed = []

        async def fake_terminate(wid):
            killed.append(wid)

        self.server._wf_terminate = fake_terminate
        self.server.tc.run = lambda coro: asyncio.run(coro)
        self.server.TEMPORAL_OK = True
        try:
            _post(self.base, "/api/chats/save", {"id": "c1", "run_id": "web-abc", "messages": []})
            st, _ = _post(self.base, "/api/chats/delete", {"id": "c1"})
            self.assertEqual(st, 200)
            self.assertEqual(killed, ["web-abc"])                     # workflow terminated
            self.assertIn("web-abc", self.server._dismissed_ids())    # card dismissed
            self.assertIsNone(chats_mod.get("c1"))                    # chat gone

            killed.clear()
            _post(self.base, "/api/chats/save", {"id": "c2", "messages": []})
            _post(self.base, "/api/chats/delete", {"id": "c2"})
            self.assertEqual(killed, [])                              # no run_id -> nothing killed
        finally:
            (chats_mod._DB, self.server._DISMISSED_PATH, self.server._wf_terminate,
             self.server.tc.run, self.server.TEMPORAL_OK) = saved

    # --- follow-up handoff (Temporal branch): a follow-up that DELEGATES a new task must NOT
    # resume the bound session (the PM-implements-the-code failure, PR #194) — the server
    # returns the extracted standalone task and the client re-submits it fresh.

    @unittest.skipUnless(_HAS_TEMPORAL, "the handoff check lives on the Temporal branch")
    def test_continue_hands_off_a_delegating_followup(self):
        self.started.clear()
        self.clarify_reply[0] = "TASK: Work on o/otto#134 — pin the temporal versions"
        st, body = _post(self.base, "/api/continue",
                         {"session_id": "sess-9", "cap": {"name": "demo-read"},
                          "message": "yes, work on that",
                          "prev": "I recommend ticket #134 (pin temporal versions). Want me to?"})
        self.assertEqual(st, 200)
        self.assertIn("#134", body["handoff"]["request"])
        self.assertEqual(self.started, [])          # nothing resumed — client re-submits fresh

    @unittest.skipUnless(_HAS_TEMPORAL, "the handoff check lives on the Temporal branch")
    def test_continue_answer_verdict_resumes_in_session(self):
        self.started.clear()
        self.clarify_reply[0] = "ANSWER"
        st, body = _post(self.base, "/api/continue",
                         {"session_id": "sess-9", "cap": {"name": "demo-read"},
                          "message": "prod please", "prev": "Which environment should I use?"})
        self.assertEqual(st, 200)
        self.assertIn("id", body)
        self.assertEqual(self.started[-1]["params"]["resume"], "sess-9")

    @unittest.skipUnless(_HAS_TEMPORAL, "the handoff check lives on the Temporal branch")
    def test_continue_without_prev_keeps_plain_resume(self):
        # An old client (no `prev`) must keep resume semantics — and spend no classifier call.
        self.started.clear()
        self.clarify_reply[0] = "TASK: should never be consulted"
        st, body = _post(self.base, "/api/continue",
                         {"session_id": "sess-9", "cap": {"name": "demo-read"},
                          "message": "yes, work on that"})
        self.assertEqual(st, 200)
        self.assertIn("id", body)
        self.assertEqual(self.started[-1]["params"]["resume"], "sess-9")

    # --- event/webhook ingress (needs TEMPORAL_OK, which == temporalio installed) ---

    @unittest.skipUnless(_HAS_TEMPORAL, "/api/submit needs TEMPORAL_OK")
    def test_submit_pins_explicit_capability(self):
        self.started.clear()
        st, body = _post(self.base, "/api/submit", {"request": "do it", "cap": "demo-write"})
        self.assertEqual(st, 200)
        p = self.started[-1]["params"]
        self.assertEqual(p["request"], "do it")
        self.assertEqual(p["cap"]["name"], "demo-write")   # pinned, skips Router #1
        self.assertEqual(p["cap"]["risk"], "write")        # resolved from trusted registry

    @unittest.skipUnless(_HAS_TEMPORAL, "/api/submit needs TEMPORAL_OK")
    def test_submit_unknown_cap_is_400(self):
        st, body = _post(self.base, "/api/submit", {"request": "x", "cap": "does-not-exist"})
        self.assertEqual(st, 400)

    @unittest.skipUnless(_HAS_TEMPORAL, "/api/submit needs TEMPORAL_OK")
    def test_submit_without_cap_routes(self):
        self.started.clear()
        st, body = _post(self.base, "/api/submit", {"request": "x"})
        self.assertEqual(st, 200)
        self.assertNotIn("cap", self.started[-1]["params"])   # falls through to routing

    @unittest.skipUnless(_HAS_TEMPORAL, "/api/submit needs TEMPORAL_OK")
    def test_submit_bare_pinned_cap_synthesizes_a_request(self):
        # A self-describing skill pinned with no args (e.g. "/slack-qna-harvest") must RUN, not
        # 400 on missing 'request' — the pinned cap is the instruction.
        self.started.clear()
        st, body = _post(self.base, "/api/submit", {"request": "", "cap": "demo-read"})
        self.assertEqual(st, 200)
        p = self.started[-1]["params"]
        self.assertEqual(p["cap"]["name"], "demo-read")
        self.assertTrue(p["request"])                          # a coherent request was synthesized
        self.assertIn("demo-read", p["request"])

    @unittest.skipUnless(_HAS_TEMPORAL, "/api/submit needs TEMPORAL_OK")
    def test_submit_empty_and_unpinned_is_400(self):
        # No cap AND no text → nothing to route, still an error.
        st, body = _post(self.base, "/api/submit", {"request": ""})
        self.assertEqual(st, 400)

    @unittest.skipUnless(_HAS_TEMPORAL, "/api/submit needs TEMPORAL_OK")
    def test_submit_threads_plan_mode_flag(self):
        # The composer's "Plan the task" opt-in flows to params["plan_mode"]; absent otherwise.
        self.started.clear()
        _post(self.base, "/api/submit", {"request": "big task", "plan_mode": True})
        self.assertTrue(self.started[-1]["params"].get("plan_mode"))
        self.started.clear()
        _post(self.base, "/api/submit", {"request": "big task"})
        self.assertNotIn("plan_mode", self.started[-1]["params"])

    def test_progress_reads_newest_attempt_transcript(self):
        # /api/progress (issue #97): the live-run status line the chat polls while a run
        # executes — tails the run's streaming transcript, no Temporal/worker needed.
        import claude_cli
        tmp = tempfile.mkdtemp(prefix="otto-progress-")
        orig, claude_cli.TRANSCRIPTS = claude_cli.TRANSCRIPTS, tmp
        try:
            events = [
                {"type": "otto-meta", "prompt": "x"},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "terraform fmt"}}]}},
            ]
            for attempt in (1, 2):
                with open(os.path.join(tmp, f"web-abc-a{attempt}.jsonl"), "w") as f:
                    for e in events:
                        f.write(json.dumps(e) + "\n")
            # a swarm child's transcript must not be mistaken for the parent's
            with open(os.path.join(tmp, "web-abc-s1-a9.jsonl"), "w") as f:
                f.write(json.dumps(events[1]) + "\n")
            st, body = _get(self.base, "/api/progress?id=web-abc")
            self.assertEqual(st, 200)
            self.assertTrue(body["found"])
            self.assertEqual(body["attempt"], 2)               # newest attempt wins, not -s1-a9
            self.assertEqual(body["events"], 2)
            self.assertIn("terraform fmt", body["last"])
            self.assertGreaterEqual(body["idle_s"], 0)
            self.assertIsNone(body["part"])                    # an execution attempt, not a round
            # A repo-mode run spends its last stretch in the post-PR review/QA rounds, which write
            # their OWN transcripts. Reading only "<wid>-aN" tails a file nothing has written since
            # RUN ended, so a busy review reported ~0 events and "possibly stuck" for 20+ minutes.
            rev = os.path.join(tmp, "web-abc-rev3-a1.jsonl")
            with open(rev, "w") as f:
                f.write(json.dumps(events[0]) + "\n")
                f.write(json.dumps(events[1]) + "\n")
            os.utime(rev, (time.time() + 5, time.time() + 5))   # newest file wins
            st, body = _get(self.base, "/api/progress?id=web-abc")
            self.assertEqual(body["part"], "review round 4")    # rounds are 0-indexed on disk
            self.assertLess(body["idle_s"], 60)                 # measured on the round, not the attempt
        finally:
            claude_cli.TRANSCRIPTS = orig
            shutil.rmtree(tmp, ignore_errors=True)

    def test_progress_unknown_or_hostile_wid_not_found(self):
        st, body = _get(self.base, "/api/progress?id=no-such-run")
        self.assertEqual(st, 200)
        self.assertFalse(body["found"])
        # a path-shaped wid is rejected before it can touch the filesystem
        st, body = _get(self.base, "/api/progress?id=..%2F..%2Fetc")
        self.assertFalse(body["found"])

    @unittest.skipUnless(_HAS_TEMPORAL, "event ingress needs TEMPORAL_OK")
    def test_event_bad_signature_rejected(self):
        st, body = _post_event(self.base, "itest", {"what": "x"}, bad_sig=True)
        self.assertEqual(st, 401)

    @unittest.skipUnless(_HAS_TEMPORAL, "event ingress needs TEMPORAL_OK")
    def test_event_no_matching_rule_ignored(self):
        st, body = _post_event(self.base, "no-such-source", {"what": "x"})
        self.assertEqual(st, 200)
        self.assertTrue(body.get("ignored"))

    @unittest.skipUnless(_HAS_TEMPORAL, "event ingress needs TEMPORAL_OK")
    def test_event_starts_unattended_workflow(self):
        self.started.clear()
        st, body = _post_event(self.base, "itest", {"what": "deploy"})
        self.assertEqual(st, 202)
        self.assertEqual(len(self.started), 1)
        p = self.started[0]["params"]
        self.assertEqual(p["request"], "handle deploy")     # rendered from the payload
        self.assertTrue(p["unattended"])                    # no human → skip clarify path
        self.assertEqual(p["approval"], "auto")             # rule's auto_approve → auto
        self.assertEqual(p["cap"]["name"], "demo-write")    # pinned cap resolved from registry…
        self.assertEqual(p["cap"]["risk"], "write")         # …with trusted risk (not from the rule)


class LocalExecutionTests(unittest.TestCase):
    """The local execution branch (issue #42) through engine.execute, with the local call
    MOCKED (gateway.local_execute) beside the usual engine._claude / gateway.complete seams:
    a tool-free read cap runs attempt 1 locally (no `claude -p`), a verify failure escalates
    to the Claude ladder, local unavailability falls back to Claude within the same attempt,
    and a write-risk or non-tool-free cap NEVER executes locally."""

    def setUp(self):
        # These script an exact verify-reply QUEUE, so they predate the confirmation
        # contract (an adverse verdict must reproduce) and would silently consume the next
        # scripted reply as a second sample. They exercise the LADDER, not judge sampling —
        # that has its own coverage in test_core.JudgeConfirmationTests.
        os.environ["OTTO_JUDGE_CONFIRMATIONS"] = "1"
        self.addCleanup(os.environ.pop, "OTTO_JUDGE_CONFIRMATIONS", None)
        self._noop = lambda *a, **k: None
        self._traces = [(engine, "trace"), (engine, "say"), (gateway, "trace")]
        self._orig_traces = [(m, n, getattr(m, n)) for m, n in self._traces]
        for m, n in self._traces:
            setattr(m, n, self._noop)
        self._tmp = tempfile.mkdtemp(prefix="otto-lxi-")
        self._paths = engine._DB
        engine._DB = os.path.join(self._tmp, "otto.db")

        self._orig_claude, self._orig_local = engine._claude, gateway.local_execute
        self._orig_complete = gateway.complete
        self.fake = _FakeClaude(result="claude did it")
        engine._claude = self.fake
        self.local_calls = []

        def fake_local(cap_name, prompt, system_context=None):
            self.local_calls.append({"cap": cap_name, "prompt": prompt, "system": system_context})
            return {"result": "local summary", "model": "local",
                    "tokens": {"input": 5, "output": 9, "cache_read": 0, "cache_write": 0}}
        gateway.local_execute = fake_local

        self.verify_replies = ["PASS"]

        def fake_complete(task, prompt):
            if task == "verify":
                return (self.verify_replies.pop(0) if len(self.verify_replies) > 1
                        else self.verify_replies[0])
            return "NONE"
        gateway.complete = fake_complete

        self.cap = registry.Capability("custom", "summarize", "summarize text")
        self.cap.risk, self.cap.tool_free = "read", True
        self.cap.prompt = "Summarize: {request}"

    def tearDown(self):
        engine._claude, gateway.local_execute = self._orig_claude, self._orig_local
        gateway.complete = self._orig_complete
        engine._DB = self._paths
        shutil.rmtree(self._tmp, ignore_errors=True)
        for m, n, fn in self._orig_traces:
            setattr(m, n, fn)

    def test_tool_free_read_cap_runs_locally(self):
        out = engine.execute("summarize the notes", self.cap)
        self.assertEqual(out["result"], "local summary")
        self.assertTrue(out["verified"])
        self.assertEqual(len(self.local_calls), 1)
        self.assertEqual(self.fake.calls, [], "claude -p must not run for a passing local attempt")
        self.assertEqual(out["cost"], 0)
        # The full invocation (cap prompt folded) went to the local model, not a bare request.
        self.assertIn("Summarize: summarize the notes", self.local_calls[0]["prompt"])

    def test_verify_failure_escalates_local_to_claude(self):
        self.verify_replies = ["FAIL\nmissing the numbers", "PASS"]
        out = engine.execute("summarize the notes", self.cap)
        self.assertEqual(len(self.local_calls), 1, "local rung is attempt 1 only")
        self.assertTrue(self.fake.calls, "attempt 2 must run on Claude")
        self.assertIn("missing the numbers", self.fake.calls[0]["prompt"])   # critique folded in
        self.assertEqual(out["result"], "claude did it")
        self.assertTrue(out["verified"])

    def test_local_unavailable_falls_back_to_claude_same_attempt(self):
        gateway.local_execute = lambda *a, **k: None
        out = engine.execute("summarize the notes", self.cap)
        self.assertTrue(self.fake.calls)
        self.assertEqual(out["result"], "claude did it")
        self.assertTrue(out["verified"])

    def test_write_cap_never_runs_locally(self):
        self.cap.risk = "write"          # even with a (stale) tool_free marker still set
        engine.execute("change the config", self.cap)
        self.assertEqual(self.local_calls, [])
        self.assertTrue(self.fake.calls)

    def test_non_tool_free_read_cap_stays_on_claude(self):
        self.cap.tool_free = False
        engine.execute("summarize the notes", self.cap)
        self.assertEqual(self.local_calls, [])
        self.assertTrue(self.fake.calls)


class LocalBackendDispatchTests(unittest.TestCase):
    """The LOCAL execution backend through engine.execute: when the resolved execution model
    is a local pool entry, the whole verify ladder runs on local_runtime.run_json (mocked) —
    `claude -p` is NEVER invoked, retries/escalation stay local, an MCP-needing cap falls
    back to Claude, and a 'local-…' session resume routes back to the local runtime."""

    CFG = {
        "pool": [
            {"name": "claude-sonnet", "provider": "claude", "model": "claude-sonnet-4-6"},
            {"name": "local", "provider": "openai", "base_url": "http://x/v1", "model": "q"},
        ],
        "assign": {"execution": "local"},
    }

    def setUp(self):
        # These script an exact verify-reply QUEUE, so they predate the confirmation
        # contract (an adverse verdict must reproduce) and would silently consume the next
        # scripted reply as a second sample. They exercise the LADDER, not judge sampling —
        # that has its own coverage in test_core.JudgeConfirmationTests.
        os.environ["OTTO_JUDGE_CONFIRMATIONS"] = "1"
        self.addCleanup(os.environ.pop, "OTTO_JUDGE_CONFIRMATIONS", None)
        self._noop = lambda *a, **k: None
        self._traces = [(engine, "trace"), (engine, "say"), (gateway, "trace")]
        self._orig_traces = [(m, n, getattr(m, n)) for m, n in self._traces]
        for m, n in self._traces:
            setattr(m, n, self._noop)
        self._tmp = tempfile.mkdtemp(prefix="otto-lbd-")
        self._paths = engine._DB
        engine._DB = os.path.join(self._tmp, "otto.db")

        self._orig_gwload = gateway.load
        gateway.load = lambda: json.loads(json.dumps(self.CFG))

        self._orig_claude = engine._claude
        self.fake = _FakeClaude(result="claude did it")
        engine._claude = self.fake

        self._orig_runtime = local_runtime.run_json
        self.runtime_calls = []

        def fake_runtime(prompt, allowed_tools=None, model_entry=None, timeout=900,
                         resume_session=None, system_context=None, cwd=None,
                         transcript=None, on_event=None, abort=None, mcp_servers=None,
                         mcp_request=None, mcp_require_score=False, steer=None, **kw):
            self.runtime_calls.append({"prompt": prompt, "tools": allowed_tools,
                                       "model": (model_entry or {}).get("name"),
                                       "resume": resume_session, "cwd": cwd,
                                       "mcp_servers": mcp_servers})
            return {"result": "local runtime did it", "is_error": False,
                    "total_cost_usd": 0, "session_id": "local-abc123",
                    "usage": {"input_tokens": 5, "output_tokens": 9}}
        local_runtime.run_json = fake_runtime

        self._orig_complete = gateway.complete
        self.verify_replies = ["PASS"]

        def fake_complete(task, prompt):
            if task == "verify":
                return (self.verify_replies.pop(0) if len(self.verify_replies) > 1
                        else self.verify_replies[0])
            return "NONE"
        gateway.complete = fake_complete

        self.cap = registry.Capability("custom", "helper", "does helpful things")
        self.cap.risk = "read"
        self.cap.prompt = "Help: {request}"

    def tearDown(self):
        gateway.load = self._orig_gwload
        engine._claude = self._orig_claude
        local_runtime.run_json = self._orig_runtime
        gateway.complete = self._orig_complete
        engine._DB = self._paths
        shutil.rmtree(self._tmp, ignore_errors=True)
        for m, n, fn in self._orig_traces:
            setattr(m, n, fn)

    def test_whole_ladder_runs_on_local_runtime_never_claude(self):
        self.verify_replies = ["FAIL\nnot enough", "FAIL\nstill not enough", "PASS"]
        out = engine.execute("help me", self.cap)
        self.assertEqual(self.fake.calls, [], "claude -p must never run for a local-exec cap")
        # Every ladder attempt — including the final 'escalation' one — stayed local.
        self.assertEqual(len(self.runtime_calls), 3)
        self.assertTrue(all(c["model"] == "local" for c in self.runtime_calls))
        self.assertEqual(out["result"], "local runtime did it")
        self.assertEqual(out["session_id"], "local-abc123")

    def test_write_cap_runs_locally_with_write_tools(self):
        self.cap.risk = "write"
        engine.execute("change it", self.cap)
        self.assertEqual(self.fake.calls, [])
        self.assertIn("Write", self.runtime_calls[0]["tools"])

    def test_write_cap_that_passes_verify_stays_fully_local(self):
        # The cost win is preserved: a capable local model that PASSES verify never touches
        # Claude, even for a write cap (issue #172 is safe-escalation, NOT a ban).
        self.cap.risk = "write"
        self.verify_replies = ["PASS"]
        out = engine.execute("change it", self.cap)
        self.assertEqual(self.fake.calls, [], "a passing local write must never escalate to Claude")
        self.assertEqual(len(self.runtime_calls), 1)
        self.assertTrue(out["verified"])

    def test_write_cap_failing_verify_escalates_off_local_to_claude(self):
        # THE issue #172 fix: a write cap that ran locally and FAILED verify must escalate off
        # local — the rest of the ladder runs on Claude instead of retrying the same weak local
        # model, which would dead-end or ship a shallow PR. (A READ cap keeps retrying locally —
        # test_whole_ladder_runs_on_local_runtime_never_claude proves that isn't regressed.)
        self.cap.risk = "write"
        self.verify_replies = ["FAIL\nshallow", "PASS"]
        out = engine.execute("change it", self.cap)
        self.assertEqual(len(self.runtime_calls), 1, "one local write attempt, then latched off")
        self.assertTrue(self.fake.calls, "attempt 2 must escalate to claude -p")
        self.assertTrue(out["verified"])
        self.assertEqual(out["result"], "claude did it")
        self.assertIsNone(out["needs_human"])
        # The local->Claude move is recorded with the write-escalation reason (not the
        # tool-incapable one), so the Audit tab / board chip explain WHY it left local.
        rows = list(engine.iter_audit_entries())
        esc = [r for r in rows if r.get("fallback_from") == "local"]
        self.assertTrue(esc, "the escalated attempt records the local->Claude fallback")
        self.assertIn("failed verification on the local backend", esc[-1].get("fallback_reason") or "")
        self.assertEqual(esc[-1].get("backend"), "claude")

    def test_tools_unsupported_redispatches_the_attempt_to_claude(self):
        # A vLLM without the tool-call flags rejects every tool-using local call — the SAME
        # attempt must run on Claude instead of failing the whole ladder (live failure:
        # 3 dead attempts + needs-human for a missing server flag).
        def no_tools_runtime(prompt, **kw):
            self.runtime_calls.append({"prompt": prompt})
            return {"result": "(local runtime error: tools rejected)", "is_error": True,
                    "total_cost_usd": 0, "session_id": "local-x",
                    "usage": {}, "tools_unsupported": True}
        local_runtime.run_json = no_tools_runtime
        out = engine.execute("help me", self.cap)
        self.assertEqual(len(self.runtime_calls), 1, "one fast 400, then Claude")
        self.assertTrue(self.fake.calls, "the attempt must re-dispatch to claude -p")
        self.assertEqual(out["result"], "claude did it")
        self.assertTrue(out["verified"])
        # The fallback is RECORDED: the audit row carries what the chosen model was and why
        # it couldn't run (surfaced on the Audit tab; the board chip mirrors it live).
        rows = list(engine.iter_audit_entries())
        att = [r for r in rows if r.get("outcome") == "ran"][-1]
        self.assertEqual(att.get("fallback_from"), "local")
        self.assertIn("tool calls", att.get("fallback_reason") or "")
        self.assertEqual(att.get("backend"), "claude")

    def test_tool_incapable_local_backend_finishes_whole_ladder_on_claude(self):
        # THE regression: the local server rejects tool calls on every attempt. Attempt 1
        # re-dispatches to Claude and latches local_disabled, so attempts 2-3 skip the local
        # runtime entirely and the FINAL rung runs on Claude too — instead of going back to the
        # broken server, erroring, and dead-ending at needs-human (the observed live failure).
        self.verify_replies = ["FAIL\nnot enough", "FAIL\nstill not enough", "PASS"]

        def no_tools_runtime(prompt, **kw):
            self.runtime_calls.append({"prompt": prompt})
            return {"result": "(local runtime error: tools rejected)", "is_error": True,
                    "total_cost_usd": 0, "session_id": "local-x",
                    "usage": {}, "tools_unsupported": True}
        local_runtime.run_json = no_tools_runtime
        out = engine.execute("help me", self.cap)
        self.assertEqual(len(self.runtime_calls), 1, "local tried once, then latched off")
        self.assertEqual(len(self.fake.calls), 3, "every rung ran on claude -p")
        self.assertTrue(out["verified"])
        self.assertEqual(out["result"], "claude did it")
        self.assertIsNone(out["needs_human"])

    def test_mcp_needing_cap_stays_on_claude(self):
        self.cap.mcp_config = "/some/repo/.mcp.json"
        engine.execute("help me", self.cap)
        self.assertEqual(self.runtime_calls, [])
        self.assertTrue(self.fake.calls, "an MCP-needing cap must fall back to claude -p")

    def test_local_session_resume_routes_to_local_runtime(self):
        out = engine.execute("follow up", self.cap, resume_session="local-abc123")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.runtime_calls[0]["resume"], "local-abc123")
        self.assertEqual(out["result"], "local runtime did it")

    def test_claude_session_resume_stays_on_claude_even_with_local_exec(self):
        engine.execute("follow up", self.cap, resume_session="claude-session-uuid")
        self.assertEqual(self.runtime_calls, [])
        self.assertEqual(self.fake.calls[0]["resume"], "claude-session-uuid")


class NeedsYouActionsTests(unittest.TestCase):
    """A Needs-you board card previously had no way to act on it (issue #116): Retry re-submits
    the original request as a fresh run, Dismiss just hides the card. Both build on plain
    read/write helpers around the audit log + a small dismissed-ids store — no Temporal needed."""

    def setUp(self):
        import server
        self.server = server
        self._orig_dismissed = server._DISMISSED_PATH
        server._DISMISSED_PATH = os.path.join(tempfile.mkdtemp(prefix="otto-dismissed-"),
                                              "dismissed.json")
        self._orig_retries = server._RETRIES_PATH
        server._RETRIES_PATH = os.path.join(tempfile.mkdtemp(prefix="otto-retries-"),
                                            "retries.json")
        self._orig_audit_db = engine._DB
        d = tempfile.mkdtemp(prefix="otto-audit-")
        engine._DB = os.path.join(d, "otto.db")

    def tearDown(self):
        self.server._DISMISSED_PATH = self._orig_dismissed
        self.server._RETRIES_PATH = self._orig_retries
        engine._DB = self._orig_audit_db

    def test_dismiss_is_idempotent_and_persists(self):
        self.assertEqual(self.server._dismissed_ids(), set())
        self.server._dismiss("wf-1")
        self.server._dismiss("wf-1")    # calling twice must not duplicate the entry
        self.assertEqual(self.server._dismissed_ids(), {"wf-1"})

    def test_run_origin_recovers_request_cap_and_repo_from_audit(self):
        engine.record_terminal("wf-2", "do the thing", {"kind": "skill", "name": "demo-write"},
                               "workflow_error", repo="infra")
        request, cap, repo, reached_run = self.server._run_origin("wf-2")
        self.assertEqual(request, "do the thing")
        self.assertEqual(cap, "skill:demo-write")
        self.assertEqual(repo, "infra")
        # This fixture only records the terminal row — no "ran" attempt ever happened, so there
        # is nothing for a retry to reuse (the run died before/without reaching execution).
        self.assertFalse(reached_run)

    def test_run_origin_unknown_workflow_returns_nothing(self):
        request, cap, repo, reached_run = self.server._run_origin("no-such-wf")
        self.assertIsNone(request)
        self.assertIsNone(cap)
        self.assertIsNone(repo)
        self.assertFalse(reached_run)

    def test_run_origin_detects_a_run_that_reached_execution(self):
        # An attempt that actually ran (engine._audit's default "ran" outcome) is what proves a
        # run passed its approval gate (or never needed one) — the signal a retry uses to decide
        # whether it can skip straight back to the run/verify ladder instead of starting over.
        engine._audit("wf-ran", "fix the alert", registry.Capability("agent", "sre-minion", "d"),
                      "did the thing", 0.1)
        engine.record_terminal("wf-ran", "fix the alert",
                               {"kind": "agent", "name": "sre-minion"}, "verify_exhausted")
        request, cap, repo, reached_run = self.server._run_origin("wf-ran")
        self.assertEqual(request, "fix the alert")
        self.assertTrue(reached_run)

    def test_accept_records_the_human_override_and_hides_the_card(self):
        # Accept is the OPPOSITE verdict to Dismiss ("stale, hide it") — it must leave a durable
        # row, or the override is indistinguishable from a dismissal the moment it's clicked.
        engine.record_terminal("wf-acc", "ship the alert", {"kind": "agent", "name": "sre-minion"},
                               "verify_exhausted", detail="opened PR #343", repo="infra")
        real, engine._extract_solution = engine._extract_solution, lambda *a, **k: "run terraform fmt"
        self.addCleanup(setattr, engine, "_extract_solution", real)
        engine.accept_run("wf-acc", "ship the alert", "agent:sre-minion",
                          self.server._run_result("wf-acc"), repo="infra")
        self.server._dismiss("wf-acc")
        rows = [e for e in engine.iter_audit_entries() if e.get("outcome") == "human_accepted"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["capability"], "agent:sre-minion")
        self.assertEqual(rows[0]["repo"], "infra")
        self.assertIn("wf-acc", self.server._dismissed_ids())
        self.assertTrue(any("terraform fmt" in s["approach"] for s in engine.solutions()))

    def test_accept_survives_a_dead_gateway(self):
        # Distilling the solution is a memory-tier LLM call; the acceptance must land regardless.
        engine.record_terminal("wf-acc2", "ship it", {"kind": "agent", "name": "sre-minion"},
                               "verify_exhausted")

        def boom(*a, **k):
            raise RuntimeError("gateway down")
        real, engine._extract_solution = engine._extract_solution, boom
        self.addCleanup(setattr, engine, "_extract_solution", real)
        engine.accept_run("wf-acc2", "ship it", "agent:sre-minion", "done")
        self.assertTrue(any(e.get("outcome") == "human_accepted"
                            for e in engine.iter_audit_entries()))

    def test_run_result_returns_the_last_recorded_result(self):
        engine.record_terminal("wf-res", "do it", "agent:x", "verify_exhausted", detail="first")
        engine.record_terminal("wf-res", "do it", "agent:x", "verify_exhausted", detail="last")
        self.assertEqual(self.server._run_result("wf-res"), "last")
        self.assertEqual(self.server._run_result("no-such-wf"), "")

    def test_outcome_preview_strips_needs_human_banner(self):
        # The board card already shows the needs-human warning via its own hint text — repeating
        # workflows.py's banner in the result preview just pushes the run's actual content out of
        # the truncated window instead of showing something informative.
        banner = ("⚠️ **Needs human review** — this did not pass automated verification after "
                  "all attempts. Treat the result below as unverified.\n\n")
        out = self.server._outcome_preview(banner + "Done. Here is the sre-minion agent's final report.")
        self.assertEqual(out, "Done. Here is the sre-minion agent's final report.")

    def test_outcome_preview_truncates_with_ellipsis(self):
        out = self.server._outcome_preview("x" * 300, limit=220)
        self.assertEqual(len(out), 221)
        self.assertTrue(out.endswith("…"))

    def test_outcome_preview_passes_through_plain_result_untouched(self):
        self.assertEqual(self.server._outcome_preview("all good, PR opened"), "all good, PR opened")

    def test_retry_is_recorded_and_overwritten_on_a_second_retry(self):
        # Clicking Retry starts a brand-new, unrelated workflow with no other link back to the
        # card it came from — the original card must be able to show what it turned into instead
        # of just sitting there with no sign anything happened.
        self.assertEqual(self.server._retries(), {})
        self.server._record_retry("wf-3", "web-aaaa1111")
        self.assertEqual(self.server._retries(), {"wf-3": "web-aaaa1111"})
        self.server._record_retry("wf-3", "web-bbbb2222")   # retried again -> latest wins
        self.assertEqual(self.server._retries(), {"wf-3": "web-bbbb2222"})

    def test_run_model_reads_newest_attempt_meta(self):
        import claude_cli
        d = tempfile.mkdtemp(prefix="otto-tr-")
        _orig, claude_cli.TRANSCRIPTS = claude_cli.TRANSCRIPTS, d
        try:
            with open(os.path.join(d, "wf-7-a1.jsonl"), "w") as f:
                f.write(json.dumps({"type": "otto-meta", "model": "claude-opus-4-8"}) + "\n")
            with open(os.path.join(d, "wf-7-a2.jsonl"), "w") as f:
                f.write(json.dumps({"type": "otto-meta", "model": "google/gemma-4",
                                    "runtime": "local"}) + "\n")
            with open(os.path.join(d, "wf-8-a1.jsonl"), "w") as f:
                f.write(json.dumps({"type": "otto-meta", "model": "claude-haiku-4-5",
                                    "fallback_from": "gemma4-26b",
                                    "fallback_reason": "server rejects tool calls"}) + "\n")
            self.assertEqual(self.server._run_model("wf-7"),
                             ("google/gemma-4", True, None, None))
            self.assertEqual(self.server._run_model("wf-8"),
                             ("claude-haiku-4-5", False, "gemma4-26b",
                              "server rejects tool calls"))
            self.assertEqual(self.server._run_model("wf-7-a1"), (None, False, None, None))
            self.assertEqual(self.server._run_model("no-such"), (None, False, None, None))
            self.assertEqual(self.server._run_model("../evil"), (None, False, None, None))
        finally:
            claude_cli.TRANSCRIPTS = _orig
            shutil.rmtree(d, ignore_errors=True)

    def test_terminate_endpoint_kills_and_dismisses(self):
        # The board's kill switch: terminating a run is acknowledging it, so the card is
        # auto-dismissed (its disappearance is the confirmation), and the Temporal terminate
        # goes through the patchable _wf_terminate seam.
        killed = []

        async def fake_terminate(wid):
            killed.append(wid)
        _orig_term, self.server._wf_terminate = self.server._wf_terminate, fake_terminate
        _orig_ok, self.server.TEMPORAL_OK = self.server.TEMPORAL_OK, True
        httpd = ThreadingTCPServer(("127.0.0.1", 0), self.server.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, body = _post(base, "/api/wf/terminate", {"id": "wf-9"})
            self.assertEqual(st, 200)
            self.assertTrue(body.get("ok"))
            self.assertEqual(killed, ["wf-9"])
            self.assertIn("wf-9", self.server._dismissed_ids())
            st, body = _post(base, "/api/wf/terminate", {"id": ""})
            self.assertEqual(st, 400)
        finally:
            httpd.shutdown(); t.join(timeout=5); httpd.server_close()
            self.server._wf_terminate, self.server.TEMPORAL_OK = _orig_term, _orig_ok

    def test_retry_endpoint_dismisses_the_source_card(self):
        # Retrying IS acknowledging the card: the endpoint must dismiss the source run so it
        # leaves "Needs review" — a card lingering after the click read as "did that work?".
        engine.record_terminal("wf-4", "do it again", {"kind": "skill", "name": "demo-write"},
                               "workflow_error")
        started = []

        async def fake_wf_start(wid, params):
            started.append({"id": wid, "params": params})
        _orig_start, self.server._wf_start = self.server._wf_start, fake_wf_start
        _orig_ok, self.server.TEMPORAL_OK = self.server.TEMPORAL_OK, True
        httpd = ThreadingTCPServer(("127.0.0.1", 0), self.server.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, body = _post(base, "/api/needs-you/retry", {"id": "wf-4"})
            self.assertEqual(st, 200)
            self.assertTrue(body.get("ok"))
            self.assertTrue(started, "a fresh workflow must have been started")
            self.assertIn("wf-4", self.server._dismissed_ids())
            self.assertEqual(self.server._retries().get("wf-4"), body["id"])
        finally:
            httpd.shutdown(); t.join(timeout=5); httpd.server_close()
            self.server._wf_start, self.server.TEMPORAL_OK = _orig_start, _orig_ok

    def test_retry_inherits_unattended_approval_without_a_reply_target(self):
        # A SCHEDULE-origin run has auto_approve but NO reply_to (it delivers to the chat
        # sidebar), so recovering the approval mode inside the `if reply_to:` block dropped it:
        # the retry ran interactive and a write cap hit the gate the schedule pre-authorizes.
        engine.record_terminal("wf-sched", "sre-pm refine tickets",
                               {"kind": "agent", "name": "demo-write"}, "workflow_error")
        started = []

        async def fake_wf_start(wid, params):
            started.append({"id": wid, "params": params})
        _orig_start, self.server._wf_start = self.server._wf_start, fake_wf_start
        _orig_ok, self.server.TEMPORAL_OK = self.server.TEMPORAL_OK, True
        _orig_in = self.server.tc.workflow_input
        # Exactly what the scheduler starts a job with (scheduler._schedule): no reply_to.
        self.server.tc.workflow_input = lambda wid: {
            "request": "sre-pm refine tickets", "scheduled": True, "auto_approve": True,
            "chat_key": "chat-mosaic-b643e7fd"}
        httpd = ThreadingTCPServer(("127.0.0.1", 0), self.server.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, _ = _post(base, "/api/needs-you/retry", {"id": "wf-sched"})
            self.assertEqual(st, 200)
            self.assertTrue(started, "a fresh workflow must have been started")
            params = started[0]["params"]
            self.assertTrue(params.get("unattended"),
                            "a scheduled run stays unattended on retry")
            self.assertEqual(params.get("approval"), "auto",
                             "auto_approve must survive the retry, or the gate fires")
            self.assertIsNone(params.get("reply_to"))
        finally:
            httpd.shutdown(); t.join(timeout=5); httpd.server_close()
            self.server._wf_start, self.server.TEMPORAL_OK = _orig_start, _orig_ok
            self.server.tc.workflow_input = _orig_in

    def test_retry_of_an_interactive_run_still_gates(self):
        # The other side of the same coin: a plain web run carries neither `scheduled` nor a
        # reply target, so its retry must stay INTERACTIVE (no unattended/approval keys) and
        # hit the normal approval gate. Guards against blanket-inheriting "auto". This fixture
        # never records a "ran" attempt (the run died before reaching execution — routing/
        # clarify/plan), so there is nothing to fast-retry either.
        engine.record_terminal("wf-web", "do it again",
                               {"kind": "skill", "name": "demo-write"}, "workflow_error")
        started = []

        async def fake_wf_start(wid, params):
            started.append({"id": wid, "params": params})
        _orig_start, self.server._wf_start = self.server._wf_start, fake_wf_start
        _orig_ok, self.server.TEMPORAL_OK = self.server.TEMPORAL_OK, True
        _orig_in = self.server.tc.workflow_input
        self.server.tc.workflow_input = lambda wid: {"request": "do it again"}
        httpd = ThreadingTCPServer(("127.0.0.1", 0), self.server.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, _ = _post(base, "/api/needs-you/retry", {"id": "wf-web"})
            self.assertEqual(st, 200)
            params = started[0]["params"]
            self.assertNotIn("unattended", params)
            self.assertNotIn("approval", params)
        finally:
            httpd.shutdown(); t.join(timeout=5); httpd.server_close()
            self.server._wf_start, self.server.TEMPORAL_OK = _orig_start, _orig_ok

    def test_retry_of_an_already_approved_run_skips_straight_back_to_execution(self):
        # An interactive write run that reached RUN (a "ran" audit row exists) already passed
        # its gate once — clicking Retry re-authorizes that same write rather than asking again
        # after burning a fresh multi-minute plan preview (user-reported). unattended=True also
        # skips CLARIFY; pinning `cap` (already existing behaviour) skips DECOMPOSE/ROUTER — so
        # this lands straight back in the run/verify ladder.
        cap = registry.Capability("agent", "sre-minion", "implements a ticket")
        engine._audit("wf-approved", "fix the alert naming", cap, "did the thing", 0.1)
        engine.record_terminal("wf-approved", "fix the alert naming",
                               {"kind": "agent", "name": "sre-minion"}, "verify_exhausted")
        _orig_caps, self.server.CAPS = self.server.CAPS, [cap]
        started = []

        async def fake_wf_start(wid, params):
            started.append({"id": wid, "params": params})
        _orig_start, self.server._wf_start = self.server._wf_start, fake_wf_start
        _orig_ok, self.server.TEMPORAL_OK = self.server.TEMPORAL_OK, True
        _orig_in = self.server.tc.workflow_input
        self.server.tc.workflow_input = lambda wid: {"request": "fix the alert naming"}
        httpd = ThreadingTCPServer(("127.0.0.1", 0), self.server.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, _ = _post(base, "/api/needs-you/retry", {"id": "wf-approved"})
            self.assertEqual(st, 200)
            params = started[0]["params"]
            self.assertTrue(params.get("unattended"))
            self.assertEqual(params.get("approval"), "auto")
            self.assertEqual(params.get("cap", {}).get("name"), "sre-minion")
        finally:
            httpd.shutdown(); t.join(timeout=5); httpd.server_close()
            self.server._wf_start, self.server.TEMPORAL_OK = _orig_start, _orig_ok
            self.server.tc.workflow_input = _orig_in
            self.server.CAPS = _orig_caps
            self.server.tc.workflow_input = _orig_in

    def test_retry_reattaches_to_the_dying_runs_own_chat_key(self):
        # A retried run must land back in the SAME chat thread the dying run was writing to, not
        # a text-similarity guess (chats.find_reattach) that silently fails and forks a brand-new
        # chat — leaving the Chats-tab thread frozen while the board's "Chat" link points at an
        # orphan holding the real result (user-reported). _wf_origin_chat_key recovers the
        # original run's own chat_key straight from its Temporal result/status; it must win over
        # find_reattach even when find_reattach WOULD have matched something else.
        engine.record_terminal("wf-orig", "do it again",
                               {"kind": "skill", "name": "demo-write"}, "workflow_error")
        started = []

        async def fake_wf_start(wid, params):
            started.append({"id": wid, "params": params})

        async def fake_origin_chat_key(wid):
            self.assertEqual(wid, "wf-orig")
            return "chat-original-thread"
        _orig_start, self.server._wf_start = self.server._wf_start, fake_wf_start
        _orig_origin, self.server._wf_origin_chat_key = self.server._wf_origin_chat_key, fake_origin_chat_key
        _orig_ok, self.server.TEMPORAL_OK = self.server.TEMPORAL_OK, True
        _orig_reattach = self.server.chats.find_reattach
        self.server.chats.find_reattach = lambda req: "chat-wrong-guess"
        httpd = ThreadingTCPServer(("127.0.0.1", 0), self.server.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, _ = _post(base, "/api/needs-you/retry", {"id": "wf-orig"})
            self.assertEqual(st, 200)
            params = started[0]["params"]
            self.assertEqual(params.get("chat_key"), "chat-original-thread")
            self.assertNotIn("chat_labels", params, "a real reattach must not be labelled 'retry'")
        finally:
            httpd.shutdown(); t.join(timeout=5); httpd.server_close()
            self.server._wf_start, self.server.TEMPORAL_OK = _orig_start, _orig_ok
            self.server._wf_origin_chat_key = _orig_origin
            self.server.chats.find_reattach = _orig_reattach

    def test_retry_falls_back_to_find_reattach_when_the_original_run_has_no_chat_key(self):
        # The original run had no Temporal chat_key (history aged out, or it never had a thread)
        # — retry must still fall back to the text-similarity guess rather than always forking.
        engine.record_terminal("wf-nochatkey", "do it again",
                               {"kind": "skill", "name": "demo-write"}, "workflow_error")
        started = []

        async def fake_wf_start(wid, params):
            started.append({"id": wid, "params": params})

        async def fake_origin_chat_key(wid):
            return None
        _orig_start, self.server._wf_start = self.server._wf_start, fake_wf_start
        _orig_origin, self.server._wf_origin_chat_key = self.server._wf_origin_chat_key, fake_origin_chat_key
        _orig_ok, self.server.TEMPORAL_OK = self.server.TEMPORAL_OK, True
        _orig_reattach = self.server.chats.find_reattach
        self.server.chats.find_reattach = lambda req: "chat-fallback-match"
        httpd = ThreadingTCPServer(("127.0.0.1", 0), self.server.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            st, _ = _post(base, "/api/needs-you/retry", {"id": "wf-nochatkey"})
            self.assertEqual(st, 200)
            params = started[0]["params"]
            self.assertEqual(params.get("chat_key"), "chat-fallback-match")
        finally:
            httpd.shutdown(); t.join(timeout=5); httpd.server_close()
            self.server._wf_start, self.server.TEMPORAL_OK = _orig_start, _orig_ok
            self.server._wf_origin_chat_key = _orig_origin
            self.server.chats.find_reattach = _orig_reattach


class WorkspaceRoundtripTests(unittest.TestCase):
    """Isolated repo workspace (issue #57), end-to-end against a LOCAL bare 'remote' — clone →
    branch → edit → commit → push. Fully offline (no network, no GitHub). The PR step is
    correctly skipped for a non-GitHub remote."""

    def _git(self, *a, cwd=None):
        import subprocess
        subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)

    def setUp(self):
        import workspace
        self.ws = workspace
        self.tmp = tempfile.mkdtemp(prefix="otto-wsrt-")
        self.bare = os.path.join(self.tmp, "remote.git")
        self._git("init", "--bare", self.bare)
        self.work = os.path.join(self.tmp, "myrepo")
        os.makedirs(self.work)
        self._git("init", self.work)
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            self._git("-C", self.work, "config", k, v)
        with open(os.path.join(self.work, "README.md"), "w") as f:
            f.write("hi\n")
        self._git("-C", self.work, "add", "-A")
        self._git("-C", self.work, "commit", "-m", "init")
        self._git("-C", self.work, "branch", "-M", "main")
        self._git("-C", self.work, "remote", "add", "origin", self.bare)
        self._git("-C", self.work, "push", "-u", "origin", "main")
        self._orig_proj = workspace.registry.PROJECTS_FILE
        self._orig_wsdir = workspace.WORKSPACES
        workspace.registry.PROJECTS_FILE = os.path.join(self.tmp, "projects.json")
        workspace.registry.save_projects([self.work])
        workspace.WORKSPACES = os.path.join(self.tmp, "workspaces")
        workspace.trace = lambda *a, **k: None

    def tearDown(self):
        self.ws.registry.PROJECTS_FILE = self._orig_proj
        self.ws.WORKSPACES = self._orig_wsdir

    def test_provision_edit_push_roundtrip(self):
        import subprocess
        ws = self.ws.provision("myrepo", "wf-rt-1")
        self.assertTrue(os.path.isdir(ws["path"]))
        self.assertEqual(ws["branch"], "otto/wf-rt-1")
        # the capability "does work" — write a new file in the workspace
        with open(os.path.join(ws["path"], "NEW.txt"), "w") as f:
            f.write("change\n")
        fin = self.ws.finalize("wf-rt-1", title="add NEW.txt", base_head=ws["head"])
        self.assertTrue(fin["pushed"])
        self.assertTrue(fin["committed"])
        self.assertIsNone(fin["pr_url"])              # non-GitHub remote -> no PR attempted
        # the branch actually landed in the 'remote'
        out = subprocess.run(["git", "-C", self.bare, "branch", "--list"],
                             capture_output=True, text=True).stdout.split()
        self.assertIn("otto/wf-rt-1", out)
        self.ws.cleanup("wf-rt-1")
        self.assertFalse(os.path.exists(ws["path"]))

    def test_unallowlisted_repo_is_refused(self):
        with self.assertRaises(ValueError):
            self.ws.provision("not-registered", "wf-rt-2")

    def test_finalize_skips_when_no_changes(self):
        ws = self.ws.provision("myrepo", "wf-rt-3")
        fin = self.ws.finalize("wf-rt-3", title="noop", base_head=ws["head"])
        self.assertFalse(fin["pushed"])               # nothing changed -> no empty branch/PR
        self.assertIn("no changes", fin["detail"])
        self.ws.cleanup("wf-rt-3")

    def _pushed_files(self, branch):
        import subprocess
        return subprocess.run(["git", "-C", self.bare, "ls-tree", "-r", "--name-only", branch],
                              capture_output=True, text=True).stdout.split()

    def test_approved_plan_never_lands_in_the_target_repo(self):
        """The plan reaches the reviewer as a PR comment. It must never be committed: a record
        of one review has no business in the repo's history, where it outlives the review and
        accumulates one file per run forever."""
        ws = self.ws.provision("myrepo", "wf-rt-4")
        with open(os.path.join(ws["path"], "NEW.txt"), "w") as f:
            f.write("change\n")
        fin = self.ws.finalize("wf-rt-4", title="add NEW.txt", base_head=ws["head"],
                               plan="1. Add NEW.txt\n2. Ship it", request="add a new file",
                               cap="general worker", concerns=["no rollback step"])
        self.assertTrue(fin["pushed"])
        self.assertEqual(["NEW.txt", "README.md"], sorted(self._pushed_files("otto/wf-rt-4")))
        self.ws.cleanup("wf-rt-4")

    def test_a_plan_alone_never_manufactures_an_empty_pr(self):
        """A run that changed nothing must stay pushless — the plan is not 'work'."""
        ws = self.ws.provision("myrepo", "wf-rt-5")
        fin = self.ws.finalize("wf-rt-5", title="noop", base_head=ws["head"],
                               plan="1. Do a thing", request="do a thing")
        self.assertFalse(fin["pushed"])
        self.assertFalse(fin["committed"])
        self.assertIn("no changes", fin["detail"])
        self.assertFalse(os.path.exists(os.path.join(ws["path"], "specs")))
        self.ws.cleanup("wf-rt-5")

    def test_finalize_posts_the_plan_to_whatever_pr_it_resolved(self):
        """`post_plan` hangs off `finalize`'s single exit, so it reaches the capability's own PR
        and a resumed run's existing PR — not just the one `gh pr create` opened here. This
        remote is not GitHub, so the PR url is injected to exercise the wiring alone."""
        ws = self.ws.provision("myrepo", "wf-rt-6")
        with open(os.path.join(ws["path"], "NEW.txt"), "w") as f:
            f.write("change\n")
        seen = {}
        orig_f, orig_p = self.ws._finalize, self.ws.post_plan
        self.ws._finalize = lambda *a, **k: {"branch": "b", "pushed": True, "committed": True,
                                             "pr_url": "https://github.com/o/r/pull/7",
                                             "detail": "opened by the capability"}
        self.ws.post_plan = lambda path, pr_url, run_id, plan, **kw: seen.update(
            pr=pr_url, plan=plan, **kw)
        try:
            fin = self.ws.finalize("wf-rt-6", title="add NEW.txt", base_head=ws["head"],
                                   plan="1. Add NEW.txt", request="add a new file")
        finally:
            self.ws._finalize, self.ws.post_plan = orig_f, orig_p
        self.assertEqual("https://github.com/o/r/pull/7", fin["pr_url"])
        self.assertEqual("https://github.com/o/r/pull/7", seen.get("pr"))
        self.assertEqual("1. Add NEW.txt", seen.get("plan"))
        self.ws.cleanup("wf-rt-6")

    def test_plan_comment_can_be_disabled(self):
        ws = self.ws.provision("myrepo", "wf-rt-7")
        with open(os.path.join(ws["path"], "NEW.txt"), "w") as f:
            f.write("change\n")
        orig, self.ws.PLAN_COMMENT = self.ws.PLAN_COMMENT, False
        try:
            fin = self.ws.finalize("wf-rt-7", title="add NEW.txt", base_head=ws["head"],
                                   plan="1. Add NEW.txt")
        finally:
            self.ws.PLAN_COMMENT = orig
        self.assertTrue(fin["pushed"])
        self.assertEqual(["NEW.txt", "README.md"], sorted(self._pushed_files("otto/wf-rt-7")))
        self.ws.cleanup("wf-rt-7")


class WebhookDeliveryTests(unittest.TestCase):
    """The result sink actually delivers (webhook kind) — stdlib only, no Temporal."""

    def test_webhook_posts_result(self):
        from http.server import BaseHTTPRequestHandler
        captured = {}

        class _Capture(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                captured["body"] = self.rfile.read(n)
                self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingTCPServer(("127.0.0.1", 0), _Capture)
        srv.daemon_threads = True
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        try:
            status = delivery.deliver({"kind": "webhook", "url": f"http://127.0.0.1:{port}/hook"},
                                      "all good", "incident")
            self.assertIn("posted to webhook", status)
            body = json.loads(captured["body"])
            self.assertEqual(body["result"], "all good")
            self.assertEqual(body["capability"], "incident")
        finally:
            srv.shutdown(); t.join(timeout=5); srv.server_close()


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class SlackListenerActivityTests(unittest.TestCase):
    """poll_slack activity glue: for each new message it starts an unattended run, posts the ack
    in-thread, resolves a PINNED cap from the trusted registry (never from the Slack payload), and
    advances the read cursor. slack.* seams are mocked — no network, no Temporal."""

    def setUp(self):
        import activities
        import slack
        self.activities, self.slack = activities, slack
        self._orig_caps = activities._caps
        cap = registry.Capability("skill", "answer-thing", "answers")
        cap.risk = "read"
        activities._caps = [cap]                          # so activities._cap("answer-thing") resolves
        self._orig = {n: getattr(slack, n)
                      for n in ("load", "enabled", "poll", "post", "start_run", "record_seen",
                                "watch_conversation", "thread_context", "channel_context")}
        slack.enabled = lambda cfg=None: True
        slack.load = lambda: {**slack._DEFAULTS, "enabled": True, "cap": "answer-thing",
                              "ack_template": "hold on…"}
        slack.poll = lambda cfg: [{"channel": "C7", "ts": "9.0", "thread_ts": None,
                                   "user": "U2", "text": "deploy?"}]
        self.posts, self.started, self.seen, self.watched = [], [], [], []
        slack.post = lambda ch, text, thread_ts=None: self.posts.append((ch, text, thread_ts)) or True
        slack.start_run = lambda wid, params: (self.started.append((wid, params)) or "started")
        slack.record_seen = lambda ch, ts: self.seen.append((ch, ts))
        slack.watch_conversation = lambda ch, root=None, wid=None, seen=None, pending=False: (
            self.watched.append((ch, root, wid, seen, pending)))
        slack.thread_context = lambda ch, root, **k: ["U2: earlier ask", "U1: earlier answer"]
        self.ctx_calls = []
        slack.channel_context = lambda ch, before, **k: (
            self.ctx_calls.append((ch, before))
            or ["U2: is ci working for you?",
                "the operator (the person you are answering for): it looks like, yeah"])

    def tearDown(self):
        self.activities._caps = self._orig_caps
        for n, v in self._orig.items():
            setattr(self.slack, n, v)

    def test_starts_run_posts_ack_and_advances_cursor(self):
        out = self.activities.poll_slack({})
        self.assertEqual(len(self.started), 1)
        wid, params = self.started[0]
        self.assertEqual(wid, "slack-C7-9-0")
        # cap resolved from the TRUSTED registry (name/kind/risk), not the raw payload string.
        self.assertEqual(params["cap"], {"name": "answer-thing", "kind": "skill", "risk": "read"})
        self.assertEqual(params["approval"], "ask")        # writes gate by default
        self.assertEqual(params["reply_to"],
                         {"kind": "slack_thread", "channel": "C7", "thread_ts": "9.0"})
        self.assertIn("deploy?", params["request"])
        self.assertEqual(self.posts, [("C7", "hold on…", "9.0")])   # ack posted in-thread
        self.assertEqual(self.seen, [("C7", "9.0")])                 # cursor advanced
        self.assertEqual(out["picked"], ["slack-C7-9-0"])

    def test_backlog_burst_acks_once_per_conversation(self):
        """A backlog catch-up (or just a fast burst) can return several picks for the SAME
        conversation in one poll() call — each firing its own ack reads as "On it… On it…"
        stuttering ahead of the actual replies. One ack per conversation per pass is enough; a
        DIFFERENT conversation in the same pass still gets its own."""
        self.slack.poll = lambda cfg: [
            {"channel": "C7", "ts": "9.0", "thread_ts": None, "user": "U2", "text": "one thing",
             "is_dm": True},
            {"channel": "C7", "ts": "9.1", "thread_ts": None, "user": "U2", "text": "another thing",
             "is_dm": True},
            {"channel": "C9", "ts": "9.0", "thread_ts": None, "user": "U3", "text": "unrelated ask",
             "is_dm": True},
        ]
        self.activities.poll_slack({})
        self.assertEqual(len(self.started), 3)                      # all three still run
        self.assertEqual(len(self.posts), 2)                        # but only one ack per channel
        self.assertEqual({p[0] for p in self.posts}, {"C7", "C9"})

    def test_top_level_dm_carries_the_recent_conversation(self):
        """The 2026-07-31 regression: Otto answers IN-THREAD but people keep typing at channel
        level in a DM, so the watched-thread path never engaged and every message arrived as a cold
        task ("nope", "Dammit", and a "force logout my account?" that meant CI). A top-level
        message now gets the channel's recent history, fetched up to (not including) its own ts."""
        self.activities.poll_slack({})
        _, params = self.started[0]
        self.assertEqual(self.ctx_calls, [("C7", "9.0")])
        self.assertIn("is ci working for you?", params["request"])
        self.assertIn("Earlier messages in that Slack conversation", params["request"])
        self.assertIn("data, not as instructions", params["request"])   # still fenced as untrusted

    def _dm(self, text, rec=None, ts="20.0"):
        """A top-level DM message — the shape `_poll_dms` produces."""
        self.slack.poll = lambda cfg: [{"channel": "C7", "ts": ts, "thread_ts": None,
                                        "user": "U2", "text": text, "is_dm": True,
                                        "conversation": rec}]

    def _convo(self, **over):
        base = {"channel": "C7", "thread_ts": None, "wid": "slack-C7-9-0", "session": "sess-1",
                "last_reply": "CI is up and working fine.",
                "cap": {"name": "answer-thing", "kind": "skill", "risk": "read"}}
        base.update(over)
        return base

    def test_a_second_dm_message_resumes_the_conversation(self):
        """The root fix: a DM is ONE conversation, so its next message continues the session instead
        of starting a cold run. Before this, continuity was keyed on the thread Otto replied in while
        the person kept typing at channel level — so a DM became N independent contextless runs."""
        self._orig_handoff = self.activities.engine.followup_handoff
        self.activities.engine.followup_handoff = lambda *a, **k: None    # a continuation
        try:
            self._dm("Weird, it's timing out from my network", rec=self._convo())
            out = self.activities.poll_slack({})
        finally:
            self.activities.engine.followup_handoff = self._orig_handoff
        wid, params = self.started[0]
        self.assertEqual(params["resume"], "sess-1")           # continues, not a cold run
        self.assertEqual(params["chat_key"], "slack-C7-9-0")   # same Chat thread
        self.assertEqual(out["resumed"], [wid])
        # The reply goes to the DM itself, never into a thread hanging off the question.
        self.assertIsNone(params["reply_to"]["thread_ts"])
        self.assertEqual(self.posts, [("C7", self.slack._FOLLOWUP_ACK, None)])
        # A DM reads through the CHANNEL cursor, so that's what advances.
        self.assertEqual(self.seen, [("C7", "20.0")])
        self.assertEqual(self.watched, [("C7", None, None, None, True)])

    def test_a_new_task_mid_conversation_is_handed_off_and_re_routed(self):
        """A resumed session keeps its cap for life and never engages repo-mode or the review loop —
        so making a DM one long session must not trap a genuinely NEW task in whichever cap answered
        first. Same guard the web path uses (engine.followup_handoff)."""
        self._orig_handoff = self.activities.engine.followup_handoff
        self.activities.engine.followup_handoff = lambda msg, prev, cap: "Fix the flaky build in repo X"
        try:
            self._dm("yes please, go do that", rec=self._convo())
            out = self.activities.poll_slack({})
        finally:
            self.activities.engine.followup_handoff = self._orig_handoff
        wid, params = self.started[0]
        self.assertNotIn("resume", params)                       # routed fresh…
        self.assertEqual(params["request"], "Fix the flaky build in repo X")   # …self-contained
        self.assertEqual(params["chat_key"], "slack-C7-9-0")     # still the same conversation
        self.assertEqual(out["handed_off"], [wid])
        self.assertEqual(out["resumed"], [])
        self.assertEqual(self.posts, [("C7", "hold on…", None)])   # full ack: it's a new task

    def test_a_dm_with_no_session_falls_back_to_transcript_context(self):
        """First contact, or the previous run died: still answerable, carrying the conversation so
        it can resolve "it"/"that" rather than asking."""
        self._dm("Can you force logout my account?", rec=self._convo(session=None))
        self.activities.poll_slack({})
        _, params = self.started[0]
        self.assertNotIn("resume", params)
        self.assertEqual(self.ctx_calls, [("C7", "20.0")])
        self.assertIn("is ci working for you?", params["request"])
        self.assertEqual(params["chat_key"], "slack-C7-9-0")     # same conversation, new session

    def test_duplicate_advances_cursor_without_reacking(self):
        self.slack.start_run = lambda wid, params: "duplicate"
        out = self.activities.poll_slack({})
        self.assertEqual(self.posts, [])                    # no ack on a duplicate
        self.assertEqual(self.seen, [("C7", "9.0")])        # but cursor still advances
        self.assertEqual(out["picked"], [])

    def test_failed_start_leaves_cursor_for_retry(self):
        self.slack.start_run = lambda wid, params: "failed"
        self.activities.poll_slack({})
        self.assertEqual(self.seen, [])                     # not advanced -> retried next poll

    def test_first_message_starts_tracking_its_conversation(self):
        self.activities.poll_slack({})
        # (channel, thread root, owning run id, cursor seed, in-flight). A top-level message is
        # tracked as the CHANNEL's conversation (root None) reading through the channel cursor, so
        # the next message resumes it — that key being the thread instead is what split one DM into
        # ten cold runs. The run is marked in flight so the next message waits for it.
        self.assertEqual(self.watched, [("C7", None, "slack-C7-9-0", None, True)])
        self.assertEqual(self.seen, [("C7", "9.0")])

    def _followup(self, **rec):
        base = {"channel": "C7", "thread_ts": "9.0", "cursor": "9.000000",
                "wid": "slack-C7-9-0", "session": "sess-1",
                "cap": {"name": "answer-thing", "kind": "skill", "risk": "read"}}
        base.update(rec)
        self.slack.poll = lambda cfg: [{"channel": "C7", "ts": "20.0", "thread_ts": "9.0",
                                        "user": "U2", "text": "and the other one?",
                                        "conversation": base, "in_thread": True}]

    def test_thread_followup_resumes_the_bound_session(self):
        self._followup()
        out = self.activities.poll_slack({})
        wid, params = self.started[0]
        self.assertEqual(wid, "slack-C7-20-0")               # its own run id (dedupe per message)
        self.assertEqual(params["resume"], "sess-1")         # …continuing the SAME conversation
        self.assertEqual(params["chat_key"], "slack-C7-9-0")  # …in the original Chat thread
        # The cap is re-resolved from the trusted registry, not taken from stored state.
        self.assertEqual(params["cap"], {"name": "answer-thing", "kind": "skill", "risk": "read"})
        self.assertEqual(params["reply_to"]["thread_ts"], "9.0")
        # Short ack (Otto has already introduced itself in this thread), and the THREAD cursor is
        # advanced — never the channel's, which would skip unhandled top-level messages.
        self.assertEqual(self.posts, [("C7", self.slack._FOLLOWUP_ACK, "9.0")])
        self.assertEqual(self.seen, [])
        self.assertEqual(self.watched, [("C7", "9.0", None, "20.0", True)])
        self.assertEqual(out["resumed"], ["slack-C7-20-0"])

    def test_followup_without_a_session_runs_fresh_with_thread_context(self):
        """The previous run never returned a session (it failed, or its cap is gone) — the reply
        still gets answered, as a fresh run carrying the thread transcript as context."""
        self._followup(session=None)
        out = self.activities.poll_slack({})
        _, params = self.started[0]
        self.assertNotIn("resume", params)
        self.assertEqual(params["chat_key"], "slack-C7-9-0")   # still the same conversation
        self.assertIn("earlier ask", params["request"])         # thread transcript folded in
        self.assertEqual(out["resumed"], [])

    def test_followup_with_an_unknown_cap_falls_back_to_a_fresh_run(self):
        self._followup(cap={"name": "deleted-cap", "kind": "skill", "risk": "read"})
        self.activities.poll_slack({})
        _, params = self.started[0]
        self.assertNotIn("resume", params)

    def test_pleasantry_in_a_thread_is_not_re_greeted(self):
        """"thanks!" mid-conversation needs no reply — greeting_template exists to introduce Otto
        to a stranger, and posting it as a thread's last word reads like a bot loop."""
        self._followup()
        self.slack.poll = lambda cfg: [{"channel": "C7", "ts": "20.0", "thread_ts": "9.0",
                                        "user": "U2", "text": "thanks!", "in_thread": True,
                                        "conversation": {"channel": "C7", "thread_ts": "9.0",
                                                         "session": "sess-1"}}]
        out = self.activities.poll_slack({})
        self.assertEqual(self.posts, [])                     # nothing said
        self.assertEqual(self.started, [])                   # nothing run
        self.assertEqual(self.seen, [])                      # channel cursor untouched
        self.assertEqual(self.watched, [("C7", "9.0", None, "20.0", False)])
        self.assertEqual(out["greeted"], ["slack-C7-20-0"])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowUnattendedTests(unittest.IsolatedAsyncioTestCase):
    """Unattended path: a pinned write with auto_approve runs to completion WITHOUT any
    approval signal, then delivers the result to its reply target."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "evt-write", "does a write")
        cap.risk = "write"
        self._orig = {n: getattr(engine, n)
                      for n in ("run_attempt", "verify", "record_attempt", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self.verify_unattended = []       # captures the flag the workflow threads to the judge
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: (
            self.verify_unattended.append(unattended), {"passed": True, "critique": ""})[1]
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. do the write", "cost": 0, "tokens": None}   # pre-approval preview
        engine.critique_plan = lambda *a, **k: {"concerns": []}

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None, wid=None, cwd=None,
                             recall=False, project=None, **kwargs):
            return {"workflow": wid or "wf-evt", "result": "did the thing", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}
        engine.run_attempt = fake_run_attempt

        self.delivered = []
        self._orig_deliver = delivery.deliver
        delivery.deliver = lambda reply_to, result, cap=None, run_id=None: (
            self.delivered.append((reply_to, result, cap)) or "ok")

        # Capture chat recording: open_chat -> chats.start_run (at start), record_chat ->
        # chats.finish_run (at finalize). Two-phase so the thread is visible while in flight.
        import chats
        self.chats = chats
        self.opened = []
        self.recorded = []
        self._orig_start = chats.start_run
        self._orig_finish = chats.finish_run
        chats.start_run = lambda *a, **k: self.opened.append((a, k))
        chats.finish_run = lambda *a, **k: self.recorded.append((a, k))

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        delivery.deliver = self._orig_deliver
        self.chats.start_run = self._orig_start
        self.chats.finish_run = self._orig_finish

    async def test_unattended_pinned_write_runs_and_delivers(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="evtq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip, deliver_result],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "renew the vpn", "unattended": True, "auto_approve": True,
                         "cap": {"name": "evt-write", "kind": "skill", "risk": "write"},
                         "reply_to": {"kind": "webhook", "url": "http://x"}},
                        id="evt-" + uuid.uuid4().hex[:8], task_queue="evtq")
        self.assertEqual(out["result"], "did the thing")    # ran with no approval signal
        self.assertEqual(len(self.delivered), 1)            # result delivered to reply_to
        self.assertEqual(self.delivered[0][1], "did the thing")

    async def test_unattended_clarify_pauses_then_runs(self):
        """A board-style unattended run with clarify=True PAUSES on a clarification question
        (so the card stays In Progress / the Board shows "Waiting on you") and folds the answer
        back in once signalled — instead of silently running and being marked Done."""
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        captured = {}

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None, wid=None,
                             cwd=None, recall=False, project=None, **kwargs):
            captured["request"] = request
            return {"workflow": wid or "wf-evt", "result": "did the thing", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}

        orig_clarify, orig_run = engine.clarify, engine.run_attempt
        engine.clarify = lambda req, c: "Which environment?"
        engine.run_attempt = fake_run_attempt
        try:
            async with await _time_skipping_env() as env:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    async with Worker(
                        env.client, task_queue="clarq", workflows=[OttoWorkflow],
                        activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding,
                                    verify_capability, record_attempt, record_skip, deliver_result],
                        activity_executor=ex,
                    ):
                        h = await env.client.start_workflow(
                            OttoWorkflow.run,
                            {"request": "fix the thing", "unattended": True, "auto_approve": True,
                             "clarify": True,
                             "cap": {"name": "evt-write", "kind": "skill", "risk": "write"}},
                            id="clar-" + uuid.uuid4().hex[:8], task_queue="clarq")
                        # Unattended, but clarify=True → it must WAIT, not auto-run.
                        for _ in range(100):
                            st = await h.query(OttoWorkflow.status)
                            if st["awaiting_clarification"]:
                                break
                            await asyncio.sleep(0.05)
                        else:
                            self.fail("clarify=True unattended run never reached awaiting_clarification")
                        self.assertEqual(st["question"], "Which environment?")
                        await h.signal(OttoWorkflow.provide_clarification, "prod")
                        out = await h.result()
        finally:
            engine.clarify, engine.run_attempt = orig_clarify, orig_run
        self.assertEqual(out["result"], "did the thing")
        self.assertIn("prod", captured["request"])         # the answer folded into the request

    async def test_scheduled_run_records_a_labelled_chat(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, open_chat, record_attempt,
                                record_chat, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="chatq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip, deliver_result, open_chat, record_chat],
                    activity_executor=ex,
                ):
                    await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "check builds", "scheduled": True,
                         "cap": {"name": "evt-write", "kind": "skill", "risk": "write"},
                         "auto_approve": True, "chat_key": "chat-sched1",
                         "chat_title": "check builds", "chat_labels": ["scheduled-job"]},
                        id="sch-" + uuid.uuid4().hex[:8], task_queue="chatq")
        # Opened at START (visible while in flight), keyed by chat_key, carrying the request.
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.opened[0][0][0], "chat-sched1")
        self.assertEqual(self.opened[0][0][1], "check builds")
        # Finalized at END with the run's result.
        self.assertEqual(len(self.recorded), 1)              # one run -> one finalize
        args, kw = self.recorded[0]
        self.assertEqual(args[0], "chat-sched1")             # keyed by the schedule's chat_key
        self.assertEqual(args[1], "did the thing")           # the run's result
        # Provenance labels are set when the thread is OPENED (start_run), not on finalize.
        self.assertEqual(self.opened[0][1]["labels"], ["scheduled-job"])
        # The judge is told the run is unattended (dead-end questions must FAIL verify).
        self.assertEqual(self.verify_unattended, [True])

    async def test_failed_run_finalizes_chat_placeholder(self):
        """Issue #79: an unattended run that FAILS mid-execution (after _open_chat wrote the
        pending placeholder) must still finalize the Chat thread — rewriting the placeholder into
        an error marker — instead of orphaning it as a perpetual 'working…' spinner. Force a
        mid-run failure by making verify raise (an unwrapped activity, so it propagates to the
        run() except block, unlike run_capability which is caught into the verify ladder)."""
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, open_chat, record_attempt,
                                record_chat, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)

        def boom(*a, **k):
            raise RuntimeError("verify blew up")
        engine.verify = boom
        with self.assertRaises(Exception):
            async with await _time_skipping_env() as env:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    async with Worker(
                        env.client, task_queue="failq", workflows=[OttoWorkflow],
                        activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding,
                                    verify_capability, record_attempt, record_skip,
                                    deliver_result, open_chat, record_chat],
                        activity_executor=ex,
                    ):
                        await env.client.execute_workflow(
                            OttoWorkflow.run,
                            {"request": "check builds", "scheduled": True,
                             "cap": {"name": "evt-write", "kind": "skill", "risk": "write"},
                             "auto_approve": True, "chat_key": "chat-fail1",
                             "chat_title": "check builds"},
                            id="fail-" + uuid.uuid4().hex[:8], task_queue="failq")
        # Placeholder was opened at START, then finalized on the FAILURE path (not orphaned).
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.opened[0][0][0], "chat-fail1")
        self.assertEqual(len(self.recorded), 1)              # finalized despite the failure
        args, _ = self.recorded[0]
        self.assertEqual(args[0], "chat-fail1")
        self.assertTrue(args[1].startswith("❌ **This run failed**"))

    async def test_unattended_ask_waits_for_approval_then_runs(self):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, plan_capability, record_attempt,
                                record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="askq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, plan_capability, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip, deliver_result],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "renew the vpn", "unattended": True, "approval": "ask",
                         "cap": {"name": "evt-write", "kind": "skill", "risk": "write"}},
                        id="ask-" + uuid.uuid4().hex[:8], task_queue="askq")
                    # An unattended "ask" write does NOT auto-run — it waits (shown on the Board).
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("unattended 'ask' write never reached awaiting_approval")
                    # Plan-first: the gate carries the concrete operations preview, not just the cap.
                    self.assertEqual(st["plan"], "1. do the write")
                    await h.signal(OttoWorkflow.approve, True)   # the Board's Approve button
                    out = await h.result()
        self.assertEqual(out["result"], "did the thing")

    async def test_gate_pushes_a_notification(self):
        """Issue #92: reaching the approval gate fires an owner push (via the notify_human
        activity -> delivery.notify) so a run blocked on approval reaches the phone."""
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, notify_human, plan_capability,
                                record_attempt, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        pushes = []
        orig_notify = delivery.notify
        # Mirrors delivery.notify's real signature on purpose: a new kwarg that lands only on
        # the real one raises TypeError inside the activity, which `_notify` swallows — the push
        # then silently never fires and no test can see it (same seam rule as _fake_claude).
        delivery.notify = lambda title, *, lines=None, detail=None, click=None, tags=None, \
            priority="high", kind=None, wid=None, actions=None: (
            pushes.append({"title": title, "lines": lines, "detail": detail, "wid": wid,
                           "tags": tags, "kind": kind, "priority": priority}) or True)
        try:
            async with await _time_skipping_env() as env:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    async with Worker(
                        env.client, task_queue="ntfq", workflows=[OttoWorkflow],
                        activities=[route_request, snapshot_settings, clarify_request, plan_capability,
                                    run_capability, resolve_pr_target, check_grounding, verify_capability, record_attempt,
                                    record_skip, deliver_result, notify_human],
                        activity_executor=ex,
                    ):
                        h = await env.client.start_workflow(
                            OttoWorkflow.run,
                            {"request": "renew the vpn", "unattended": True, "approval": "ask",
                             "cap": {"name": "evt-write", "kind": "skill", "risk": "write"}},
                            id="ntf-" + uuid.uuid4().hex[:8], task_queue="ntfq")
                        for _ in range(100):
                            st = await h.query(OttoWorkflow.status)
                            if st["awaiting_approval"]:
                                break
                            await asyncio.sleep(0.05)
                        else:
                            self.fail("never reached awaiting_approval")
                        await h.signal(OttoWorkflow.approve, True)
                        await h.result()
        finally:
            delivery.notify = orig_notify
        gate = [p for p in pushes if p["title"].startswith("Approval needed")]
        self.assertEqual(len(gate), 1)
        # End-to-end privacy split: the request reaches notify ONLY as `detail` (which
        # delivery.notify then drops unless OTTO_NTFY_DETAIL is set), never in the title or the
        # always-sent metadata lines. This assertion used to read `assertIn("renew the vpn",
        # body)` — that was the leak.
        self.assertIn("renew the vpn", gate[0]["detail"])
        self.assertNotIn("renew the vpn", gate[0]["title"])
        self.assertNotIn("renew the vpn", " ".join(gate[0]["lines"] or []))
        self.assertIn("evt-write · write", gate[0]["lines"])   # still actionable
        # The clean finish also fires a push tagged kind="complete" — delivery.notify (real,
        # not this mock) drops that kind unless OTTO_NTFY_ON_COMPLETE opts in.
        done = [p for p in pushes if p["title"].startswith("Otto finished")]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["kind"], "complete")
        # The gate push is the one a run is PARKED behind (it expires at gate_timeout_h and
        # auto-declines), so it is tiered above the finish push rather than equal to it, and it
        # carries the run id — that is what `click` deep-links the tap to.
        self.assertEqual(gate[0]["kind"], "approval")
        self.assertEqual(gate[0]["priority"], "max")
        self.assertEqual(done[0]["priority"], "low")
        self.assertTrue(gate[0]["wid"])

    async def test_interactive_run_does_not_push_on_completion(self):
        """A push exists to tell the owner something they cannot already see. An INTERACTIVE run's
        answer is rendering in the chat they are looking at, so a completion push there is Otto
        notifying them of what is on their screen — and it was the single biggest source of push
        volume (185 of 434 runs over 30 days were `web-*`). Unattended finishes still push; this
        one must not."""
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, notify_human, record_attempt,
                                record_skip, route_request, snapshot_settings, run_capability,
                                resolve_pr_target, check_grounding, suggest_repo,
                                snapshot_repos, estop_check, finalize_terminal,
                                verify_capability)
        pushes = []
        orig_notify = delivery.notify
        # An interactive run always clarifies (unlike the unattended ones this class otherwise
        # exercises), so the clarify seam has to be stubbed or it reaches a real model call and
        # the workflow parks forever waiting for an answer.
        orig_clarify = engine.clarify
        engine.clarify = lambda request, cap: None
        delivery.notify = lambda title, *, lines=None, detail=None, click=None, tags=None, \
            priority="high", kind=None, wid=None, actions=None: (
            pushes.append({"title": title, "kind": kind}) or True)
        try:
            async with await _time_skipping_env() as env:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    async with Worker(
                        env.client, task_queue="intq", workflows=[OttoWorkflow],
                        activities=[route_request, snapshot_settings, clarify_request,
                                    run_capability, resolve_pr_target, check_grounding,
                                    verify_capability, record_attempt, record_skip,
                                    deliver_result, notify_human, suggest_repo,
                                    snapshot_repos, estop_check, finalize_terminal],
                        activity_executor=ex,
                    ):
                        await env.client.execute_workflow(
                            OttoWorkflow.run,
                            # No `unattended`/`scheduled`: a human is sitting in the chat.
                            {"request": "renew the vpn", "approval": "auto",
                             "cap": {"name": "evt-write", "kind": "skill", "risk": "write"}},
                            id="int-" + uuid.uuid4().hex[:8], task_queue="intq")
        finally:
            delivery.notify = orig_notify
            engine.clarify = orig_clarify
        self.assertEqual([p for p in pushes if p["kind"] == "complete"], [],
                         "an interactive run pushed a completion the human was already watching")


class WfStateTransientFailureTests(unittest.TestCase):
    """`_wf_state`'s describe() can fail two ways that look identical to `except Exception` and
    mean opposite things. NOT_FOUND is authoritative — the id is gone. A deadline/transport error
    is the WEATHER: the workflow is still executing. Collapsing the second into "failed" paints a
    dead pipeline over a live run and stops the chat's watch loop for good — measured on
    web-8a600a4b, which the UI called failed while it went on executing for another 15 minutes."""

    def _state(self, exc):
        import asyncio
        import server
        import temporal_client as tc

        class FakeHandle:
            async def describe(self):
                raise exc

        class FakeClient:
            def get_workflow_handle(self, wid):
                return FakeHandle()

        orig = tc.client
        async def fake_client():
            return FakeClient()
        tc.client = fake_client
        try:
            return asyncio.run(server._wf_state("web-deadbeef"))
        finally:
            tc.client = orig

    def test_a_transport_failure_is_reported_as_unreachable_not_failed(self):
        # A timeout/unavailable/connection error says nothing about the RUN — the client has to
        # keep polling, so this must not be a terminal state.
        st = self._state(TimeoutError("deadline exceeded"))
        self.assertEqual(st["state"], "unreachable")

    def test_an_authoritative_not_found_is_still_terminal(self):
        # The other half: a wiped dev-server DB must still stop the watch loop, or a reattaching
        # UI spins forever on a run that genuinely no longer exists.
        try:
            from temporalio.service import RPCError, RPCStatusCode
        except Exception:  # noqa: BLE001
            self.skipTest("temporalio not installed")
        st = self._state(RPCError("no such workflow", RPCStatusCode.NOT_FOUND, b""))
        self.assertEqual(st["state"], "failed")

    def test_the_client_keeps_polling_an_unreachable_run(self):
        # The server half is useless if the browser treats the new state as a fall-through into
        # the terminal branch: it must continue the loop and touch no node.
        with open(os.path.join(os.path.dirname(__file__), "web/index.html"),
                  encoding="utf-8", errors="surrogateescape") as f:
            html = f.read()
        i = html.index('st.state==="unreachable"')
        branch = html[i:html.index('st.state==="failed"', i)]
        self.assertIn("continue;", branch, "an unreachable poll must keep the watch loop alive")
        self.assertNotIn("failCurrent", branch)
        self.assertNotIn("clearRun", branch)


class WfTerminateAuditTests(unittest.TestCase):
    """A Temporal TERMINATE delivers no exception into the workflow, so OttoWorkflow's own
    except-and-finalize (which normally writes a terminal audit row) never runs — a killed run
    had NO audit trail at all, even one that already produced a real side effect before being
    killed (user-reported: a run that had already posted to Slack showed no audit row
    whatsoever). _wf_terminate must record it itself, at the one call site that actually knows
    the kill happened."""

    def setUp(self):
        self._orig_audit_db = engine._DB
        engine._DB = os.path.join(tempfile.mkdtemp(prefix="otto-audit-"), "otto.db")

    def tearDown(self):
        engine._DB = self._orig_audit_db

    def test_terminate_writes_a_terminal_audit_row_using_the_runs_own_origin(self):
        import asyncio
        import server
        import temporal_client as tc
        cap = registry.Capability("skill", "slack-maintenance-thread", "posts a thread")
        # The plan_preview accounting row a real in-flight run leaves behind — _run_origin must
        # recover the capability/request from it since the terminated run itself never audits.
        engine._audit("wf-kill-1", "post the report", cap, "plan text", 0.0, outcome="plan_preview")

        terminated = []

        class FakeHandle:
            async def terminate(self, reason=None):
                terminated.append(reason)

        class FakeClient:
            def get_workflow_handle(self, wid):
                return FakeHandle()

        async def fake_client():
            return FakeClient()

        _orig_client = tc.client
        tc.client = fake_client
        try:
            fut = asyncio.run_coroutine_threadsafe(server._wf_terminate("wf-kill-1"), tc._bg_loop())
            fut.result(timeout=10)
        finally:
            tc.client = _orig_client

        self.assertEqual(terminated, ["terminated from the Otto board"])
        rows = [e for e in engine.iter_audit_entries() if e.get("workflow") == "wf-kill-1"]
        terminal = [e for e in rows if e.get("outcome") == "needs_human"]
        self.assertEqual(len(terminal), 1, "terminate must leave exactly one terminal audit row")
        self.assertEqual(terminal[0]["reason"], "terminated")
        self.assertEqual(terminal[0]["capability"], "skill:slack-maintenance-thread")


class TemporalRequiredTests(unittest.TestCase):
    """Temporal is required to SERVE (#278): main() must refuse before binding a port when
    temporalio isn't importable — the direct run path is gone, so starting anyway would be a
    UI whose every submit 503s."""

    def test_main_refuses_to_start_without_temporal(self):
        import server
        orig = server.TEMPORAL_OK
        server.TEMPORAL_OK = False
        try:
            with self.assertRaises(SystemExit) as ctx:
                server.main()
            self.assertIn("Temporal", str(ctx.exception))
            self.assertIn("./install.sh", str(ctx.exception))   # the error must be actionable
        finally:
            server.TEMPORAL_OK = orig


class ReaperSurvivesADisabledBoardTests(unittest.IsolatedAsyncioTestCase):
    """The reaper Temporal Schedule used to be created only while the GitHub board was enabled,
    and DELETED otherwise — so the one install shape that most needs a backstop (no board, runs
    arriving from web/Slack/cron) had none. `reap_stuck`'s second pass sweeps every non-card
    OttoWorkflow and self-gates the card pass on `board_on`, so the schedule has no business
    reading board config at all. Measured live: `/api/health` reported `reaper: {exists: false}`
    on a board-disabled install for as long as the board had been off.

    The off switch is OTTO_REAPER_SECONDS=0, which must still delete it."""

    def setUp(self):
        self.created, self.deleted, self.updated = [], [], []
        test = self

        class FakeHandle:
            def __init__(self, sid): self.sid = sid
            async def describe(self): raise RuntimeError("no such schedule")
            async def delete(self): test.deleted.append(self.sid)
            async def update(self, fn): test.updated.append(self.sid)

        class FakeClient:
            def get_schedule_handle(self, sid): return FakeHandle(sid)
            async def create_schedule(self, sid, sched):
                test.created.append((sid, sched.spec.intervals[0].every.total_seconds()))

        import temporal_client as tc
        self._orig_client = tc.client
        self._orig_secs = config.REAPER_SECONDS

        async def fake_client():
            return FakeClient()
        tc.client = fake_client
        self.tc = tc

    def tearDown(self):
        self.tc.client = self._orig_client
        config.REAPER_SECONDS = self._orig_secs

    async def test_the_reaper_is_scheduled_even_with_the_board_disabled(self):
        import board
        cfg = dict(board.load()); cfg["enabled"] = False
        orig_load = board.load
        board.load = lambda: cfg
        try:
            status = await board._reconcile_reaper_schedule()
        finally:
            board.load = orig_load
        self.assertEqual(self.deleted, [], "a disabled board must not delete the reaper")
        self.assertEqual([sid for sid, _ in self.created], [board.REAPER_SCHED_ID])
        self.assertIn("created", status)

    async def test_zero_seconds_is_still_the_off_switch(self):
        import board
        config.REAPER_SECONDS = 0
        status = await board._reconcile_reaper_schedule()
        self.assertEqual(self.deleted, [board.REAPER_SCHED_ID])
        self.assertEqual(self.created, [])
        self.assertIn("disabled", status)


class NeedsYouLoopSafetyTests(unittest.TestCase):
    """server._needs_you runs ON temporal_client's background loop (the handler wraps it in
    tc.run), so inside it every Temporal touch must be awaited directly — the sync wrappers
    (board.poll_status / board.reaper_status / tc.connected) each re-enter tc.run from the
    loop's own thread, blocking the loop on work only that loop can run. Regression: the first
    /api/needs-you hit deadlocked the background loop and with it every later Temporal call in
    the server. Runs the real coroutine on the real background loop with the Temporal edges
    stubbed; a re-introduced sync wrapper deadlocks and trips the timeout (test fails, not hangs)."""

    def test_needs_you_completes_on_the_background_loop(self):
        import asyncio
        import board
        import server
        import temporal_client as tc

        async def fake_board(limit=40):
            return [{"id": "x", "status": "RUNNING", "phase": "awaiting approval"}]

        async def fake_sched_status():
            return {"exists": True, "paused": False}

        async def fake_client():
            return object()

        orig = (server._board, board._poll_status, board._reaper_status, tc.client)
        server._board, board._poll_status, board._reaper_status, tc.client = (
            fake_board, fake_sched_status, fake_sched_status, fake_client)
        try:
            fut = asyncio.run_coroutine_threadsafe(server._needs_you(), tc._bg_loop())
            d = fut.result(timeout=10)
        finally:
            server._board, board._poll_status, board._reaper_status, tc.client = orig
        self.assertEqual(d["counts"]["awaiting_approval"], 1)
        self.assertTrue(d["health"]["board_poll"]["exists"])
        self.assertTrue(d["health"]["reaper"]["exists"])


class AuditViewFilterTests(unittest.TestCase):
    """/api/audit filters (issue #95): server-side wid/cap/verified filtering over the full
    audit trail, with the capability dropdown fed from the UNFILTERED trail."""

    def setUp(self):
        import tempfile
        import server
        self.server = server
        self.dir = tempfile.mkdtemp(prefix="otto-aview-")
        self._orig = engine._DB
        engine._DB = os.path.join(self.dir, "otto.db")
        rows = [
            {"workflow": "wf-a", "capability": "skill:alpha", "verified": True,
             "cost_usd": 1, "at": "2026-07-01T10:00:00"},
            {"workflow": "wf-a", "capability": "skill:alpha", "verified": False,
             "cost_usd": 1, "at": "2026-07-01T11:00:00"},
            {"workflow": "wf-b", "capability": "agent:beta", "verified": True,
             "cost_usd": 1, "at": "2026-07-02T10:00:00"},
        ]
        for r in rows:
            engine._append_audit(r)

    def tearDown(self):
        import shutil
        engine._DB = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_unfiltered_returns_everything(self):
        v = self.server._audit_view()
        self.assertEqual(v["count"], 3)
        self.assertEqual(v["entries"][0]["workflow"], "wf-b")   # newest first
        self.assertEqual(v["capabilities"], ["agent:beta", "skill:alpha"])

    def test_filters(self):
        self.assertEqual(self.server._audit_view(wid="wf-a")["count"], 2)
        self.assertEqual(self.server._audit_view(cap="agent:beta")["count"], 1)
        self.assertEqual(self.server._audit_view(verified="true")["count"], 2)
        self.assertEqual(self.server._audit_view(verified="false")["count"], 1)
        both = self.server._audit_view(wid="wf-a", verified="true")
        self.assertEqual(both["count"], 1)
        # the dropdown list stays unfiltered so narrowing doesn't hide options
        self.assertEqual(both["capabilities"], ["agent:beta", "skill:alpha"])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class ExecRetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Issue #91: run_capability must NOT be retried at the Temporal layer (maximum_attempts=1)
    — a crashed/dead-worker attempt surfaces as a FAILED attempt into the verify ladder (which
    is bounded and audits every attempt) instead of silently re-running a full claude turn."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "flaky-write", "does a write")
        cap.risk = "write"
        self._orig = {n: getattr(engine, n)
                      for n in ("run_attempt", "verify", "record_attempt", "record_terminal")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.record_terminal = lambda *a, **k: None
        self.calls = []
        tests = self

        def flaky_run_attempt(request, cap, *, attempt=1, critique=None, **kwargs):
            tests.calls.append(attempt)
            if len(tests.calls) == 1:
                raise RuntimeError("worker died mid-attempt")
            return {"workflow": "wf-x", "result": "recovered", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}
        engine.run_attempt = flaky_run_attempt

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def test_failed_exec_activity_becomes_failed_attempt_not_temporal_retry(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, finalize_terminal,
                                record_attempt, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="retq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip,
                                deliver_result, finalize_terminal],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "do the flaky thing", "unattended": True,
                         "auto_approve": True,
                         "cap": {"name": "flaky-write", "kind": "skill", "risk": "write"}},
                        id="ret-" + uuid.uuid4().hex[:8], task_queue="retq")
        # Attempt 1 crashed the ACTIVITY. Temporal must not have replayed it (exactly one call
        # with attempt=1); the verify ladder took the next shot as attempt 2 instead.
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual(out["result"], "recovered")


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowHarnessDeathTests(unittest.IsolatedAsyncioTestCase):
    """The PRODUCTION mirror of `test_core.HarnessDeathLadderTests`. `OttoWorkflow._verify_ladder`
    is a third copy of the ladder that cannot merge into `engine._ladder_core` (workflow code is
    deterministic and calls activities), so the rule has to be pinned on both sides or the copies
    drift — which is exactly how they drifted before.

    A dead activity is not a judgement: it must not spend one of `max_attempts`, and it must not
    drag the final-rung model escalation onto an attempt that produced nothing to escalate."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "hangy", "does a thing")
        cap.risk = "read"
        self._orig = {n: getattr(engine, n)
                      for n in ("run_attempt", "verify", "record_attempt", "record_terminal")}
        self._orig_caps, self._setting = activities._caps, config.setting
        activities._caps = [cap]
        config.setting = lambda name: ({"max_attempts": 3, "max_harness_retries": 2}
                                       .get(name, self._setting(name)))
        engine.record_attempt = lambda *a, **k: None
        engine.record_terminal = lambda *a, **k: None
        self.calls, self.escalate, self.judged = [], [], []
        tests = self

        def dying_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False, **kwargs):
            tests.calls.append(attempt)
            tests.escalate.append(escalate)
            if len(tests.calls) == 1:
                raise RuntimeError("worker died mid-attempt")
            return {"workflow": "wf-x", "result": "real output", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}

        def failing_verify(req, c, result, **k):
            tests.judged.append(result)
            return {"passed": False, "source": "judge", "critique": "not good enough"}
        engine.run_attempt, engine.verify = dying_run_attempt, failing_verify

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps, config.setting = self._orig_caps, self._setting

    async def _run(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, finalize_terminal,
                                record_attempt, record_skip, route_request, snapshot_settings,
                                run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="hdq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip,
                                deliver_result, finalize_terminal],
                    activity_executor=ex,
                ):
                    return await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "do the hangy thing", "unattended": True,
                         "auto_approve": True,
                         "cap": {"name": "hangy", "kind": "skill", "risk": "read"}},
                        id="hd-" + uuid.uuid4().hex[:8], task_queue="hdq")

    async def test_a_dead_activity_does_not_spend_a_judged_rung(self):
        await self._run()
        # attempt 1 died in the harness; a judge must still get its three shots.
        self.assertEqual(self.calls, [1, 2, 3, 4])
        self.assertEqual(len(self.judged), 3)

    async def test_a_dead_activity_does_not_pull_the_model_escalation_forward(self):
        await self._run()
        self.assertEqual(self.escalate, [False, False, False, True])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowSignalTests(unittest.IsolatedAsyncioTestCase):
    """The durable workflow's two human moments as real Temporal SIGNALS: clarification
    answer, then approve/deny of a write. Engine seams are mocked (no Claude)."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "demo-write", "creates something")
        cap.risk = "write"
        self.cap = cap

        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "run_attempt", "verify",
                       "record_attempt", "record_skip", "plan_preview", "critique_plan", "candidate_repo")}
        self._orig_caps = activities._caps
        activities._caps = [cap]                      # so activities._cap(name) resolves it

        self.skips = []
        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: []   # single cohesive task -> no fan-out
        engine.clarify = lambda request, c: "Which environment?"
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. edit a file", "cost": 0, "tokens": None}   # pre-approval preview
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.candidate_repo = lambda request, names: None              # no repo named -> no auto-detect
        engine.verify = lambda request, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.record_skip = lambda request, c, reason="DENIED", **kw: self.skips.append(reason)

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None, wid=None, cwd=None,
                             recall=False, project=None, **kwargs):
            self.last_request = request
            return {"workflow": wid or "wf-test", "result": "ran ok", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}
        engine.run_attempt = fake_run_attempt

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def _until(self, handle, pred, tries=100):
        from workflows import OttoWorkflow
        import asyncio
        for _ in range(tries):
            st = await handle.query(OttoWorkflow.status)
            if pred(st):
                return st
            await asyncio.sleep(0.05)
        raise AssertionError("workflow never reached the expected state")

    async def _drive(self, *, approve):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, detect_repo_changes, plan_capability, plan_swarm,
                                record_attempt, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                snapshot_repos, suggest_repo, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="itq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, clarify_request, plan_capability,
                                suggest_repo, run_capability, resolve_pr_target, check_grounding, verify_capability, record_attempt,
                                record_skip, snapshot_repos, detect_repo_changes],
                    activity_executor=ex,
                ):
                    handle = await env.client.start_workflow(
                        OttoWorkflow.run, {"request": "deploy the service"},
                        id="wf-" + uuid.uuid4().hex[:8], task_queue="itq")
                    # 1) clarification signal
                    st = await self._until(handle, lambda s: s["awaiting_clarification"])
                    self.assertEqual(st["question"], "Which environment?")
                    await handle.signal(OttoWorkflow.provide_clarification, "prod")
                    # 2) write-approval signal
                    await self._until(handle, lambda s: s["awaiting_approval"])
                    await handle.signal(OttoWorkflow.approve, approve)
                    return await handle.result()

    async def test_clarify_then_approve_runs(self):
        out = await self._drive(approve=True)
        self.assertEqual(out["result"], "ran ok")
        self.assertIn("prod", self.last_request)        # the answer folded into the request
        self.assertEqual(self.skips, [])

    async def test_clarify_then_deny_skips(self):
        out = await self._drive(approve=False)
        self.assertTrue(out["result"].startswith("Declined"))
        self.assertEqual(self.skips, ["DENIED"])         # recorded as a skip, nothing ran


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class ResumeWriteGateTests(unittest.IsolatedAsyncioTestCase):
    """A resumed session bound to a READ capability whose follow-up asks for a write
    ("now publish those comments") must still hit the approval gate — it is NOT
    auto-approved just because the original capability was read-only."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "demo-review", "reviews a PR")
        cap.risk = "read"                                # the bound session is read-only
        self.cap = cap
        self._orig = {n: getattr(engine, n) for n in
                      ("run_attempt", "verify", "record_attempt", "record_skip",
                       "followup_write_intent", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]

        self.skips = []
        self.plan_resume = []                            # resume_session seen by the plan preview
        self.intent = {"write": True}                    # the follow-up classifier verdict
        engine.followup_write_intent = lambda message, c, repo=None: self.intent["write"]

        def _plan(request, c, cwd=None, resume_session=None, wid=None, **kw):
            self.plan_resume.append(resume_session)
            return {"plan": "1. post the comments", "cost": 0, "tokens": None}
        engine.plan_preview = _plan
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.verify = lambda request, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.record_skip = lambda request, c, reason="DENIED", **kw: self.skips.append(reason)
        engine.run_attempt = lambda request, cap, **k: {
            "workflow": k.get("wid") or "wf-resume", "result": "posted the comments",
            "cost": 0.0, "session_id": "s2", "model": "m", "attempt": 1}

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def _drive(self, *, write_intent, approve=None):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_followup, plan_capability, record_attempt,
                                record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        self.intent["write"] = write_intent
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="rq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, classify_followup, plan_capability,
                                run_capability, resolve_pr_target, check_grounding, verify_capability, record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "now publish those comments", "resume": "s1",
                         "cap": {"name": "demo-review", "kind": "skill", "risk": "read"}},
                        id="rs-" + uuid.uuid4().hex[:8], task_queue="rq")
                    if approve is not None:
                        for _ in range(100):
                            st = await h.query(OttoWorkflow.status)
                            if st["awaiting_approval"]:
                                break
                            await asyncio.sleep(0.05)
                        else:
                            self.fail("write-intent follow-up never reached awaiting_approval")
                        await h.signal(OttoWorkflow.approve, approve)
                    return await h.result()

    async def test_write_intent_followup_gates_then_runs_on_approve(self):
        out = await self._drive(write_intent=True, approve=True)
        self.assertEqual(out["result"], "posted the comments")
        self.assertEqual(self.skips, [])
        # The plan preview must --resume the bound session so it inherits the conversation
        # context (which PR was reviewed); a context-free preview asked "which PR?" mid-chat.
        self.assertEqual(self.plan_resume, ["s1"])

    async def test_write_intent_followup_denied_skips(self):
        out = await self._drive(write_intent=True, approve=False)
        self.assertTrue(out["result"].startswith("Declined"))
        self.assertEqual(self.skips, ["DENIED"])

    async def test_read_followup_resumes_without_a_gate(self):
        # A plain read follow-up must NOT gate — continuity stays frictionless.
        out = await self._drive(write_intent=False, approve=None)
        self.assertEqual(out["result"], "posted the comments")
        self.assertEqual(self.skips, [])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class DiscussionTurnTests(unittest.IsolatedAsyncioTestCase):
    """The INVERSE of ResumeWriteGateTests, and the thing that makes a ticket conversation a
    conversation. A repo-mode chat binds a WRITE capability for the life of the session, so every
    follow-up in it used to take the full write path: a read-only plan preview (minutes of real
    agentic work, 15min ceiling) and an approval card — to answer "why did you use a mutex there?".

    A turn that asks for no mutation has nothing for the gate to guard, so it drops to read for
    THIS TURN: no preview, no gate, and — the half that matters — no write tools either, because
    the classifier behind the decision is one cheap call and the toolset, not its verdict, is what
    actually stands between a misread follow-up and an ungated write.

    Three properties, in the order they'd break:
      1. a question in a write-bound session neither previews nor gates;
      2. it reaches run_capability with risk "read" (skipping the gate WITH write tools is the one
         combination that must never happen);
      3. a real change request in the same session still gates."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("agent", "sre-minion", "implements a ticket and opens a PR")
        cap.risk = "write"                               # the bound session is a write cap
        self.cap = cap
        self._orig = {n: getattr(engine, n) for n in
                      ("run_attempt", "verify", "record_attempt", "record_skip",
                       "followup_write_intent", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]

        self.previews = []                               # every plan preview that actually ran
        self.risks = []                                  # cap.risk as EXECUTION saw it
        self.skips = []
        self.intent = {"write": False}
        self.classified = []                             # (message, repo) the classifier was asked
        engine.followup_write_intent = lambda message, c, repo=None: (
            self.classified.append((message, repo)) or self.intent["write"])

        def _plan(request, c, cwd=None, resume_session=None, wid=None, **kw):
            self.previews.append(request)
            return {"plan": "1. edit the file", "cost": 0, "tokens": None}
        engine.plan_preview = _plan
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.verify = lambda request, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.record_skip = lambda request, c, reason="DENIED", **kw: self.skips.append(reason)

        def _run(request, cap, **k):
            self.risks.append(cap.risk)
            return {"workflow": k.get("wid") or "wf-disc", "result": "because a channel would "
                    "have serialized the writers", "cost": 0.0, "session_id": "s2", "model": "m",
                    "attempt": 1}
        engine.run_attempt = _run

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def _drive(self, message, *, write_intent, approve=None, params=None):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_followup, plan_capability, record_attempt,
                                record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        self.intent["write"] = write_intent
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="dq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, classify_followup,
                                plan_capability, run_capability, resolve_pr_target, check_grounding, verify_capability, record_attempt,
                                record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": message, "resume": "s1",
                         "cap": {"name": "sre-minion", "kind": "agent", "risk": "write"},
                         **(params or {})},
                        id="dt-" + uuid.uuid4().hex[:8], task_queue="dq")
                    if approve is not None:
                        for _ in range(100):
                            st = await h.query(OttoWorkflow.status)
                            if st["awaiting_approval"]:
                                break
                            await asyncio.sleep(0.05)
                        else:
                            self.fail("a change request in a write session never reached the gate")
                        await h.signal(OttoWorkflow.approve, approve)
                    return await h.result()

    async def test_a_question_in_a_write_session_neither_previews_nor_gates(self):
        out = await self._drive("why did you use a mutex there instead of a channel?",
                                write_intent=False)
        self.assertIn("serialized the writers", out["result"])
        # The whole point: no minutes-long preview pass, and nobody was asked to approve a question.
        self.assertEqual(self.previews, [], "a question paid for a plan preview")
        self.assertEqual(self.skips, [])
        self.assertEqual(out["cap"]["risk"], "read")

    async def test_the_downgraded_turn_actually_loses_its_write_tools(self):
        # Skipping the gate is only safe because the risk really drops — `run_capability`
        # re-resolves the cap by NAME from the registry, where it is still "write", so the turn's
        # risk has to travel in the payload or the run keeps Edit/Write with no approval at all.
        await self._drive("walk me through the diff you pushed", write_intent=False)
        self.assertEqual(self.risks, ["read"],
                         "the discussion turn executed with write tools and no gate")

    async def test_a_real_change_request_still_gates(self):
        out = await self._drive("now add a unit test for that edge case", write_intent=True,
                                approve=True)
        self.assertEqual(self.previews, ["now add a unit test for that edge case"])
        self.assertEqual(self.risks, ["write"])
        self.assertEqual(out["cap"]["risk"], "write")

    async def test_a_denied_change_request_is_still_a_decline(self):
        out = await self._drive("now add a unit test for that edge case", write_intent=True,
                                approve=False)
        self.assertTrue(out["result"].startswith("Declined"))
        self.assertEqual(self.skips, ["DENIED"])

    async def test_pre_authorization_is_not_reinterpreted_as_a_question(self):
        # "Auto approve" is the human at the keyboard pre-authorizing this chat's writes. Running
        # the classifier there could only ever TAKE tools away from a turn they already approved,
        # so it must not run at all — and the turn keeps its write risk.
        out = await self._drive("have a look and tidy up whatever needs it", write_intent=False,
                                params={"approval": "auto"})
        self.assertEqual(self.classified, [], "auto-approve still paid for a write-intent call")
        self.assertEqual(self.risks, ["write"])
        self.assertEqual(out["cap"]["risk"], "write")

    async def test_the_classifier_is_told_which_repo_the_session_is_in(self):
        # The generic prompt ends on "change state outside this machine", and an edit in a local
        # clone is exactly a change that isn't — the one wording that could talk a real edit
        # request into READ now that READ means "no tools".
        await self._drive("what does that helper do?", write_intent=False,
                          params={"repo": "otto", "git_run_id": None})
        self.assertEqual([r for _, r in self.classified], ["otto"])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class UnattendedResumeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """A Slack thread follow-up: an UNATTENDED resume must deliver its answer back to the thread
    (before this, the resume branch returned without ever calling deliver_result, so the follow-up
    was answered only in the audit log), record the turn in the Chat thread, and leave the thread
    continuable for the NEXT reply (session id recorded by deliver_result)."""

    def setUp(self):
        import activities
        import slack
        self.activities, self.slack = activities, slack
        cap = registry.Capability("skill", "answer-thing", "answers questions")
        cap.risk = "read"
        self._orig = {n: getattr(engine, n) for n in
                      ("run_attempt", "record_attempt", "followup_write_intent",
                       "plan_preview", "critique_plan", "record_skip")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self.intent = {"write": False}
        engine.followup_write_intent = lambda message, c, repo=None: self.intent["write"]
        engine.record_attempt = lambda *a, **k: None
        engine.run_attempt = lambda request, cap, **k: {
            "workflow": k.get("wid") or "wf-resume", "result": "the other one is fine too",
            "cost": 0.0, "session_id": "sess-2", "model": "m", "attempt": 1}
        self.delivered = []
        self._orig_deliver = delivery.deliver
        delivery.deliver = lambda reply_to, result, cap=None, run_id=None: (
            self.delivered.append((reply_to, result)) or "posted to slack thread (C7)")
        import chats
        self.chats, self.turns = chats, []
        self._orig_start, self._orig_finish = chats.start_run, chats.finish_run
        chats.start_run = lambda cid, request, **k: self.turns.append(("start", cid, request))
        chats.finish_run = lambda cid, result, **k: self.turns.append(("finish", cid, result))
        self._orig_state = slack._STATE
        self._tmp = tempfile.mkdtemp(prefix="otto-slack-")
        slack._STATE = os.path.join(self._tmp, "slack-state.json")

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        delivery.deliver = self._orig_deliver
        self.chats.start_run, self.chats.finish_run = self._orig_start, self._orig_finish
        self.slack._STATE = self._orig_state
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_followup_is_delivered_recorded_and_stays_continuable(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (classify_followup, deliver_result, notify_human, open_chat,
                                record_attempt, record_chat, run_capability, resolve_pr_target,
            check_grounding, snapshot_settings)
        reply_to = {"kind": "slack_thread", "channel": "C7", "thread_ts": "100.0"}
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="rq", workflows=[OttoWorkflow],
                    activities=[snapshot_settings, classify_followup, run_capability, resolve_pr_target, check_grounding,
                                record_attempt, deliver_result, open_chat, record_chat,
                                notify_human],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "and the other one?", "resume": "sess-1",
                         "cap": {"name": "answer-thing", "kind": "skill", "risk": "read"},
                         "unattended": True, "approval": "ask", "reply_to": reply_to,
                         "chat_key": "slack-C7-100-0", "chat_title": "and the other one?"},
                        id="rs-" + uuid.uuid4().hex[:8], task_queue="rq")
        self.assertEqual(out["result"], "the other one is fine too")
        self.assertEqual(self.delivered, [(reply_to, "the other one is fine too")])
        # The follow-up appends BOTH turns to the original Chat thread (a resume used to skip
        # open_chat, so the sidebar showed answers with no questions).
        self.assertEqual([(k, cid) for k, cid, _ in self.turns],
                         [("start", "slack-C7-100-0"), ("finish", "slack-C7-100-0")])
        # And the thread carries the NEW session, so the next reply continues from here.
        rec = self.slack.conversation_record("C7", "100.0")
        self.assertEqual(rec["session"], "sess-2")
        self.assertEqual(rec["cap"]["name"], "answer-thing")
        self.assertNotIn("pending_at", rec)

    async def test_write_intent_followup_gates_even_unattended(self):
        """Nobody is watching a Slack thread run, and the follow-up is someone ELSE's words — an
        emergent write must park on the Needs-you board, not ride in on the read session."""
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (classify_followup, deliver_result, notify_human, open_chat,
                                plan_capability, record_attempt, record_chat, record_skip,
                                run_capability, resolve_pr_target,
            check_grounding, snapshot_settings)
        self.intent["write"] = True
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. restart it", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.record_skip = lambda request, c, reason="DENIED", **kw: None
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="rq", workflows=[OttoWorkflow],
                    activities=[snapshot_settings, classify_followup, plan_capability,
                                run_capability, resolve_pr_target, check_grounding, record_attempt, record_skip, deliver_result,
                                open_chat, record_chat, notify_human],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "just restart the service then", "resume": "sess-1",
                         "cap": {"name": "answer-thing", "kind": "skill", "risk": "read"},
                         "unattended": True, "approval": "ask",
                         "reply_to": {"kind": "slack_thread", "channel": "C7",
                                      "thread_ts": "100.0"},
                         "chat_key": "slack-C7-100-0"},
                        id="rs-" + uuid.uuid4().hex[:8], task_queue="rq")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("an unattended write follow-up never gated")
                    self.assertEqual(self.delivered, [])     # nothing said in Slack yet
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        self.assertEqual(out["result"], "the other one is fine too")
        self.assertEqual(len(self.delivered), 1)


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class PreAuthorizedGateTests(unittest.IsolatedAsyncioTestCase):
    """`approval == "auto"` means the write was pre-authorized. Who pressed start does not
    change that, so the gate skip must not ALSO require `unattended`.

    It did, and the two halves disagreed on the same runbook: a cron fire passed
    `unattended=True` and ran, while the UI's "Run now" passed `unattended=False`
    (`scheduler.run_now`'s deliberate default — a human is present) and gated, even though the
    runbook's own `auto_approve: true` was carried all the way into the workflow. The operator's
    pre-authorization was read and then ignored, after a full read-only preview pass — up to
    900s of `claude -p` — had already been spent on a plan nobody asked for.

    `unattended` keeps its own, separate meaning: nobody is watching. That is what delivery,
    clarify and verify's dead-end rule read it for, and none of them want it conflated with
    whether a write was signed off in advance."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "refine-tickets", "refines board tickets")
        cap.risk = "write"
        self.cap = cap
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "run_attempt", "verify", "record_attempt",
                       "record_skip", "request_write_intent", "plan_preview", "critique_plan",
                       "candidate_repo")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self.previews = []
        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: []
        engine.clarify = lambda request, c: None
        engine.request_write_intent = lambda request, c: True
        engine.candidate_repo = lambda request, names: None
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.verify = lambda request, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.record_skip = lambda request, c, reason="DENIED", **kw: None

        def preview(request, c, cwd=None, resume_session=None, wid=None, **kw):
            self.previews.append(request)
            return {"plan": "1. refine them", "cost": 0, "tokens": None}
        engine.plan_preview = preview
        engine.run_attempt = lambda request, cap, **k: {
            "workflow": k.get("wid") or "wf-pa", "result": "refined 4 tickets",
            "cost": 0.0, "session_id": "s", "model": "m", "attempt": k.get("attempt", 1)}

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def _drive(self, params, *, approve=None):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, detect_repo_changes,
                                plan_capability, plan_swarm, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, snapshot_repos,
                                suggest_repo, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="paq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, clarify_request,
                                classify_request, plan_capability, suggest_repo, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip, snapshot_repos,
                                detect_repo_changes],
                    activity_executor=ex,
                ):
                    base = {"request": 'Refine the tickets in the "Ready" column',
                            "cap": {"name": "refine-tickets", "kind": "skill", "risk": "write"}}
                    base.update(params)
                    h = await env.client.start_workflow(
                        OttoWorkflow.run, base,
                        id="pa-" + uuid.uuid4().hex[:8], task_queue="paq")
                    if approve is not None:
                        for _ in range(100):
                            st = await h.query(OttoWorkflow.status)
                            if st["awaiting_approval"]:
                                break
                            await asyncio.sleep(0.05)
                        else:
                            self.fail("an un-pre-authorized interactive write never gated")
                        await h.signal(OttoWorkflow.approve, approve)
                    return await h.result()

    async def test_run_now_on_an_auto_approve_runbook_executes_without_gating(self):
        # Exactly what scheduler.run_now builds for a UI "Run now": the runbook's auto_approve,
        # but unattended=False because a human clicked the button.
        out = await self._drive({"unattended": False, "auto_approve": True})
        self.assertEqual(out["result"], "refined 4 tickets")
        self.assertEqual(self.previews, [], "a pre-authorized run must not buy a plan preview")

    async def test_the_cron_fire_of_the_same_runbook_behaves_identically(self):
        # The half that already worked — pinned so the two paths can never diverge again.
        out = await self._drive({"unattended": True, "auto_approve": True})
        self.assertEqual(out["result"], "refined 4 tickets")
        self.assertEqual(self.previews, [])

    async def test_an_interactive_write_with_no_pre_authorization_still_gates(self):
        # The control, and the reason this stays narrow: dropping `unattended` from the skip must
        # not let an ordinary web-chat write through. A browser cannot set `approval` on
        # /api/submit at all, so such a run resolves to "skip" and falls to the gate.
        out = await self._drive({"unattended": False}, approve=True)
        self.assertEqual(out["result"], "refined 4 tickets")
        self.assertEqual(len(self.previews), 1, "the interactive gate still previews the plan")


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class FreshRouteWriteGateTests(unittest.IsolatedAsyncioTestCase):
    """A FRESH request that Router #1 misroutes to a READ-classified capability, but which
    actually asks to mutate something ("create a ticket and add it to the board"), must still
    hit the approval gate instead of auto-running just because the chosen cap reads as read."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "demo-read-cli", "reads CI builds and logs")
        cap.risk = "read"                                # Router #1 picked a read cap
        self.cap = cap
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "run_attempt", "verify", "record_attempt",
                       "record_skip", "request_write_intent", "plan_preview", "critique_plan", "candidate_repo")}
        self._orig_caps = activities._caps
        activities._caps = [cap]

        self.skips = []
        self.intent = {"write": True}                    # the request classifier verdict
        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: []      # single cohesive task -> no fan-out
        engine.clarify = lambda request, c: None         # no clarification needed
        engine.request_write_intent = lambda request, c: self.intent["write"]
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. create the ticket", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.candidate_repo = lambda request, names: None              # no repo named -> no auto-detect
        engine.verify = lambda request, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.record_skip = lambda request, c, reason="DENIED", **kw: self.skips.append(reason)
        engine.run_attempt = lambda request, cap, **k: {
            "workflow": k.get("wid") or "wf-fresh", "result": "created the ticket",
            "cost": 0.0, "session_id": "s", "model": "m", "attempt": k.get("attempt", 1)}

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def _drive(self, *, write_intent, approve=None):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, detect_repo_changes,
                                plan_capability, plan_swarm, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, snapshot_repos, suggest_repo,
                                verify_capability)
        self.intent["write"] = write_intent
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="frq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, clarify_request, classify_request,
                                plan_capability, suggest_repo, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip, snapshot_repos, detect_repo_changes],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "create a ticket for the OOM and add it to the board"},
                        id="fr-" + uuid.uuid4().hex[:8], task_queue="frq")
                    if approve is not None:
                        for _ in range(100):
                            st = await h.query(OttoWorkflow.status)
                            if st["awaiting_approval"]:
                                break
                            await asyncio.sleep(0.05)
                        else:
                            self.fail("misrouted write request never reached awaiting_approval")
                        # The gate must SAY why it fired — a bumped read cap is otherwise
                        # indistinguishable from a genuinely write-classified one.
                        self.assertIn("write-intent guard", st.get("risk_reason") or "")
                        await h.signal(OttoWorkflow.approve, approve)
                    return await h.result()

    async def test_write_intent_request_gates_then_runs_on_approve(self):
        out = await self._drive(write_intent=True, approve=True)
        self.assertEqual(out["result"], "created the ticket")
        self.assertEqual(self.skips, [])

    async def test_write_intent_request_denied_skips(self):
        out = await self._drive(write_intent=True, approve=False)
        self.assertTrue(out["result"].startswith("Declined"))
        self.assertEqual(self.skips, ["DENIED"])

    async def test_genuine_read_request_does_not_gate(self):
        # A real read request routed to a read cap must NOT gate — reads stay frictionless.
        out = await self._drive(write_intent=False, approve=None)
        self.assertEqual(out["result"], "created the ticket")
        self.assertEqual(self.skips, [])

    async def test_assistant_misroute_redirects_to_worker_and_gates(self):
        # Fresh-install failure: a task-shaped request routed to the general ASSISTANT (whose
        # prompt forbids acting) must be redirected to the general worker by the write-intent
        # guard — a bare risk bump would gate a run that then refuses the task.
        assistant, worker = registry._general_assistant(), registry._general_worker()
        self.activities._caps = [assistant, worker]
        engine.plan = lambda request, caps, project_root=None: assistant
        ran = []

        def run_attempt(request, cap, **k):
            ran.append(cap.name)
            return {"workflow": k.get("wid") or "wf-fresh", "result": "implemented it",
                    "cost": 0.0, "session_id": "s", "model": "m", "attempt": k.get("attempt", 1)}
        engine.run_attempt = run_attempt
        out = await self._drive(write_intent=True, approve=True)
        self.assertTrue(out["result"].startswith("implemented it"))
        # No repo-mode engaged -> the result must SAY the change is stranded (no commit, no PR)
        # instead of reading as success — the "worker never opened the PR" confusion.
        self.assertIn("No isolated workspace was engaged", out["result"])
        self.assertEqual(out["cap"]["name"], registry.WORKER_NAME)   # the swap is visible downstream
        self.assertEqual(ran, [registry.WORKER_NAME])                # the worker actually executed
        self.assertEqual(self.skips, [])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class PlanRevisionGateTests(unittest.IsolatedAsyncioTestCase):
    """The approval gate isn't only approve/decline — revise_plan lets the human send free-text
    feedback that gets folded into the request and re-previewed, so the gate shows an updated
    plan instead of forcing a blind decision on the first draft."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("custom", "demo-write-cli", "implements tasks")
        cap.risk = "write"
        self.cap = cap
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "run_attempt", "verify", "record_attempt",
                       "record_skip", "plan_preview", "critique_plan", "candidate_repo")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self.preview_requests = []
        self.run_requests = []

        def fake_preview(request, c, cwd=None, resume_session=None, wid=None, **kw):
            self.preview_requests.append(request)
            return {"plan": f"1. plan version {len(self.preview_requests)}",
                    "cost": 0, "tokens": None}

        def fake_run_attempt(request, cap, **k):
            self.run_requests.append(request)
            return {"workflow": k.get("wid") or "wf-rev", "result": "done", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": k.get("attempt", 1)}

        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: []
        engine.clarify = lambda request, c: None
        engine.plan_preview = fake_preview
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.candidate_repo = lambda request, names: None
        engine.verify = lambda request, c, result, project=None, local=False, unattended=False, **k: {
            "passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.record_skip = lambda request, c, reason="DENIED", **kw: None
        engine.run_attempt = fake_run_attempt

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def _start(self, env, task_queue):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, detect_repo_changes, notify_human,
                                plan_capability, plan_swarm, record_attempt, record_skip,
                                route_request, run_capability, resolve_pr_target,
            check_grounding, snapshot_repos, snapshot_settings,
                                suggest_repo, verify_capability)
        with ThreadPoolExecutor(max_workers=4) as ex:
            worker = Worker(
                env.client, task_queue=task_queue, workflows=[OttoWorkflow],
                activities=[route_request, snapshot_settings, plan_swarm, clarify_request,
                            plan_capability, suggest_repo, run_capability, resolve_pr_target, check_grounding, verify_capability,
                            record_attempt, record_skip, snapshot_repos, detect_repo_changes,
                            notify_human],
                activity_executor=ex)
            async with worker:
                h = await env.client.start_workflow(
                    OttoWorkflow.run, {"request": "clean up the stale alerts"},
                    id="pr-" + uuid.uuid4().hex[:8], task_queue=task_queue)
                yield h

    async def _await_status(self, h, pred, msg):
        import asyncio
        from workflows import OttoWorkflow
        for _ in range(200):
            st = await h.query(OttoWorkflow.status)
            if pred(st):
                return st
            await asyncio.sleep(0.05)
        self.fail(msg)

    async def test_revision_feedback_folds_into_a_new_plan_and_the_final_request(self):
        from workflows import OttoWorkflow
        async with await _time_skipping_env() as env:
            async for h in self._start(env, "prq1"):
                st = await self._await_status(
                    h, lambda st: st["awaiting_approval"], "never reached awaiting_approval")
                self.assertEqual(st["plan"], "1. plan version 1")
                self.assertEqual(st["plan_revisions"], 0)

                await h.signal(OttoWorkflow.revise_plan, "only touch dev, not prod")
                # plan_revisions increments before the re-preview activity resolves — wait for the
                # NEW plan text, not just the counter, to avoid reading it mid-flight.
                st = await self._await_status(
                    h, lambda st: st["plan"] == "1. plan version 2", "revision never landed")
                self.assertEqual(st["plan_revisions"], 1)
                self.assertIn("only touch dev, not prod", self.preview_requests[1])

                await h.signal(OttoWorkflow.approve, True)
                out = await h.result()
                self.assertEqual(out["result"], "done")
                # The feedback binds the real execution too, not just the preview the human saw.
                self.assertIn("only touch dev, not prod", self.run_requests[0])

    async def test_a_revision_in_flight_is_distinguishable_from_a_landed_one(self):
        """`plan_revisions` bumps the instant the signal lands — BEFORE the re-preview runs — so it
        cannot also mean "a new plan is up". Measured on web-3a05328f: the browser polled that
        counter alone, so 1.5s after the click it repainted the PREVIOUS round's plan and cleared
        its "Revising the plan…" note, and a 3.5-minute re-plan read as feedback silently dropped.
        `replanning` is the flag that separates in-flight from landed; without it every client has
        to guess by diffing plan text it has no reason to believe is stable."""
        import threading
        from workflows import OttoWorkflow
        release = threading.Event()
        self.addCleanup(release.set)

        def blocking_preview(request, c, cwd=None, resume_session=None, wid=None, **kw):
            self.preview_requests.append(request)
            n = len(self.preview_requests)
            if n > 1:                       # hold the REVISION round open, not the first draft
                release.wait(20)
            return {"plan": f"1. plan version {n}", "cost": 0, "tokens": None}

        engine.plan_preview = blocking_preview
        async with await _time_skipping_env() as env:
            async for h in self._start(env, "prq4"):
                st = await self._await_status(
                    h, lambda st: st["awaiting_approval"], "never reached awaiting_approval")
                self.assertFalse(st["replanning"], "nothing is being revised at the first gate")

                await h.signal(OttoWorkflow.revise_plan, "only touch dev, not prod")
                st = await self._await_status(
                    h, lambda st: st["plan_revisions"] == 1, "the revision counter never bumped")
                # The counter says round 1 while the plan on screen is still round 0's — exactly
                # the window the UI repainted in.
                self.assertEqual(st["plan"], "1. plan version 1")
                self.assertTrue(st["replanning"],
                                "a bumped counter with the OLD plan must report replanning, or a "
                                "client cannot tell the re-preview is still running")

                release.set()
                st = await self._await_status(
                    h, lambda st: st["plan"] == "1. plan version 2", "revision never landed")
                self.assertFalse(st["replanning"], "the new plan is up — replanning must clear")
                self.assertTrue(st["awaiting_approval"])

                await h.signal(OttoWorkflow.approve, True)
                out = await h.result()
                self.assertEqual(out["result"], "done")

    async def test_declining_after_a_revision_still_skips_cleanly(self):
        from workflows import OttoWorkflow
        async with await _time_skipping_env() as env:
            async for h in self._start(env, "prq2"):
                await self._await_status(
                    h, lambda st: st["awaiting_approval"], "never reached awaiting_approval")
                await h.signal(OttoWorkflow.revise_plan, "double-check the timezone first")
                await self._await_status(
                    h, lambda st: st["plan_revisions"] == 1, "revision never landed")
                await h.signal(OttoWorkflow.approve, False)
                out = await h.result()
                self.assertTrue(out["result"].startswith("Declined"))
                self.assertEqual(self.run_requests, [])   # revising is not approving

    async def test_revisions_beyond_the_budget_are_dropped(self):
        import asyncio
        from workflows import OttoWorkflow
        os.environ["OTTO_MAX_PLAN_REVISIONS"] = "1"
        self.addCleanup(os.environ.pop, "OTTO_MAX_PLAN_REVISIONS", None)
        async with await _time_skipping_env() as env:
            async for h in self._start(env, "prq3"):
                await self._await_status(
                    h, lambda st: st["awaiting_approval"], "never reached awaiting_approval")
                await h.signal(OttoWorkflow.revise_plan, "first change")
                st = await self._await_status(
                    h, lambda st: st["plan_revisions"] == 1, "first revision never landed")
                self.assertEqual(st["max_plan_revisions"], 1)

                # A second revision is over budget — it must be dropped, not silently re-loop
                # forever or crash; the run stays parked on the same (already-revised) plan.
                await h.signal(OttoWorkflow.revise_plan, "second change")
                await asyncio.sleep(0.3)
                st = await h.query(OttoWorkflow.status)
                self.assertEqual(st["plan_revisions"], 1)
                self.assertEqual(len(self.preview_requests), 2)

                await h.signal(OttoWorkflow.approve, True)
                out = await h.result()
                self.assertEqual(out["result"], "done")


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowSwarmTests(unittest.IsolatedAsyncioTestCase):
    """Multi-agent swarm (issue #4): a fresh request decomposes into several independent
    sub-tasks that run as parallel child workflows, each gated on its own, and the results
    merge into one coherent response."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "swarm-write", "does a write")
        cap.risk = "write"
        self.cap = cap
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "merge", "run_attempt", "verify", "record_attempt")}
        self._orig_caps = activities._caps
        activities._caps = [cap]

        # Plan: two independent write sub-tasks, both mapped to the same (write) cap.
        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: [
            {"cap": cap, "request": "sub A"}, {"cap": cap, "request": "sub B"}]
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        self.ran, self.attempt_audiences = [], []

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None, wid=None, cwd=None,
                             recall=False, project=None, audience=None, **kwargs):
            self.ran.append(request)
            self.attempt_audiences.append(audience)
            return {"workflow": wid or "wf-sw", "result": f"did[{request}]", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}
        engine.run_attempt = fake_run_attempt
        self.merge_audience = []
        engine.merge = lambda request, parts, audience=None: (
            self.merge_audience.append(audience)
            or "MERGED:" + "+".join((p.get("result") or "") for p in parts))

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def test_request_fans_out_runs_children_and_merges(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, merge_results, plan_swarm,
                                record_attempt, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="swq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results, clarify_request,
                                classify_request, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "do sub A and sub B", "unattended": True, "auto_approve": True},
                        id="swarm-" + uuid.uuid4().hex[:8], task_queue="swq")
        # Both sub-tasks ran (as separate child workflows) and the results were merged.
        self.assertEqual(sorted(self.ran), ["sub A", "sub B"])
        self.assertEqual(out["cap"]["name"], "swarm")
        self.assertTrue(out["result"].startswith("MERGED:"))
        self.assertIn("did[sub A]", out["result"])
        self.assertIn("did[sub B]", out["result"])
        self.assertEqual(len(out["swarm"]), 2)           # one part per sub-task
        self.assertEqual(self.merge_audience, [None])    # no reply target -> operator report

    async def test_swarm_answering_a_person_shapes_the_MERGE_not_the_children(self):
        """The merge is what gets DELIVERED, so that's where the audience applies. A child's output
        is only ever read by the merge, so it stays report-shaped — giving children the conversational
        contract would produce N chatty half-replies to synthesise instead of one answer."""
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, deliver_result, merge_results,
                                notify_human, plan_swarm, record_attempt, record_chat, record_skip,
                                route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="swaq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results,
                                clarify_request, classify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip, record_chat,
                                deliver_result, notify_human],
                    activity_executor=ex,
                ):
                    await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "do sub A and sub B", "unattended": True, "auto_approve": True,
                         "reply_to": {"kind": "slack_thread", "channel": "C7", "thread_ts": None}},
                        id="swarma-" + uuid.uuid4().hex[:8], task_queue="swaq")
        self.assertEqual(self.merge_audience, ["conversation"])
        # The children ran as ordinary pinned runs with no audience of their own.
        self.assertEqual(sorted(self.ran), ["sub A", "sub B"])
        self.assertEqual(self.attempt_audiences, [None, None])

    async def test_single_task_does_not_fan_out(self):
        # decompose() returning <2 sub-tasks must take the ordinary single-capability path.
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, merge_results, plan_swarm,
                                record_attempt, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        engine.decompose = lambda request, caps, project_root=None: []      # one cohesive task
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="sw1q", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results, clarify_request,
                                classify_request, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "do one thing", "unattended": True, "auto_approve": True},
                        id="one-" + uuid.uuid4().hex[:8], task_queue="sw1q")
        self.assertEqual(out["cap"]["name"], "swarm-write")   # routed to the single cap
        self.assertNotIn("swarm", out)                        # not a swarm result
        self.assertEqual(out["result"], "did[do one thing]")


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowPlanModeTests(unittest.IsolatedAsyncioTestCase):
    """Plan-then-execute on the durable path (design doc 2026-07-16): with PLAN_MODE on, a fresh
    non-repo run whose plan_task_steps yields a multi-step plan runs via the execute_plan branch
    (NOT the verify ladder); a no/single-step plan falls through to the ladder; an incomplete plan
    routes to needs-human. Engine planner + executor are stubbed (the loop itself is covered by the
    pure StepParse/RunPlan tests) — this asserts the workflow wiring + activation gate."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "planme", "reads and reports")
        cap.risk = "read"                                 # read cap -> no approval gate
        self.cap = cap
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "plan_mode_active", "plan_steps", "run_plan",
                       "run_attempt", "verify", "record_attempt")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self._mode = config.PLAN_MODE
        config.PLAN_MODE = "auto-local"                   # workflow-side gate must let the call through
        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: []   # no swarm fan-out
        engine.plan_mode_active = lambda c, requested=False: True
        engine.record_attempt = lambda *a, **k: None
        engine.verify = lambda *a, **k: {"passed": True, "critique": ""}
        self.ladder_ran = []                              # populated only if the ladder is taken
        def fake_run_attempt(request, cap, **k):
            self.ladder_ran.append(request)
            return {"workflow": k.get("wid") or "wf", "result": "LADDER", "cost": 0,
                    "session_id": "s", "model": "m", "attempt": k.get("attempt", 1)}
        engine.run_attempt = fake_run_attempt

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        config.PLAN_MODE = self._mode

    def _steps(self, n=2):
        return [{"id": f"s{i}", "goal": f"step {i}", "context": "", "needs": [],
                 "produces": f"s{i}", "done_when": ""} for i in range(1, n + 1)]

    async def _run(self, steps, plan_result):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, execute_plan, finalize_terminal,
                                plan_swarm, plan_task_steps, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        engine.plan_steps = lambda request, cap, force_claude=True: steps
        engine.run_plan = lambda request, cap, steps, **k: plan_result
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="pmq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, plan_task_steps, execute_plan,
                                clarify_request, classify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip, finalize_terminal],
                    activity_executor=ex,
                ):
                    return await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "a multi-step task", "unattended": True},
                        id="plan-" + uuid.uuid4().hex[:8], task_queue="pmq")

    async def test_multistep_plan_runs_via_execute_plan_not_ladder(self):
        out = await self._run(self._steps(2), {
            "result": "PLAN DONE", "passed": True, "cost": 0, "tokens": {"output": 0},
            "steps_run": 2, "replans": 0, "budget_stop": False})
        self.assertEqual(out["result"], "PLAN DONE")
        self.assertTrue(out["verified"])
        self.assertIsNone(out["needs_human"])
        self.assertEqual(self.ladder_ran, [])             # verify ladder NOT used

    async def test_no_plan_falls_through_to_ladder(self):
        out = await self._run([], None)                   # plan_task_steps -> [] (no plan)
        self.assertEqual(out["result"], "LADDER")
        self.assertEqual(self.ladder_ran, ["a multi-step task"])

    async def test_incomplete_plan_routes_to_needs_human(self):
        out = await self._run(self._steps(2), {
            "result": "partial work", "passed": False, "cost": 0, "tokens": {"output": 0},
            "steps_run": 1, "replans": 2, "budget_stop": False})
        self.assertFalse(out["verified"])
        self.assertEqual(out["needs_human"], {"reason": "verify_exhausted"})
        self.assertIn("Needs human review", out["result"])
        self.assertEqual(self.ladder_ran, [])

    async def test_plan_off_takes_ladder_without_calling_planner(self):
        config.PLAN_MODE = "off"                          # workflow-side gate blocks the plan call
        called = {"plan": False}
        def spy(request, cap, force_claude=True):
            called["plan"] = True
            return self._steps(2)
        # Even though plan_steps WOULD return steps, PLAN_MODE=off must skip the plan branch.
        out = await self._run_off(spy)
        self.assertEqual(out["result"], "LADDER")
        self.assertFalse(called["plan"])

    async def _run_off(self, plan_steps_fn):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, execute_plan, finalize_terminal,
                                plan_swarm, plan_task_steps, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        engine.plan_steps = plan_steps_fn
        engine.run_plan = lambda *a, **k: {"result": "PLAN", "passed": True, "cost": 0,
                                           "tokens": {"output": 0}, "steps_run": 1, "replans": 0,
                                           "budget_stop": False}
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="pmoffq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, plan_task_steps, execute_plan,
                                clarify_request, classify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip, finalize_terminal],
                    activity_executor=ex,
                ):
                    return await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "a multi-step task", "unattended": True},
                        id="planoff-" + uuid.uuid4().hex[:8], task_queue="pmoffq")


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowRunbookTests(unittest.IsolatedAsyncioTestCase):
    """A runbook hands the workflow a graph a human already wrote. Two things must follow: the
    LLM planner is never consulted (its steps ARE the plan), and the runbook's prose rides in as
    `approved_plan` so execution and the verify judge are both bound to it — including on the
    auto-approved path, where nobody is watching."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("agent", "runner", "runs things")
        cap.risk = "read"
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self._orig = {n: getattr(engine, n)
                      for n in ("plan_steps", "run_plan", "verify", "record_attempt")}
        self.planner_called = []
        self.run_plan_args = []
        engine.plan_steps = lambda *a, **k: self.planner_called.append(1) or []
        # record_attempt does real memory extraction (a live gateway call) AND appends to the
        # developer's real data/otto.db audit trail — neither belongs in a unit run.
        engine.record_attempt = lambda *a, **k: None

        def fake_run_plan(request, cap_, steps, **k):
            self.run_plan_args.append({"steps": steps, "replan": k.get("replan"),
                                       "resolve_cap": k.get("resolve_cap")})
            return {"result": "RUNBOOK DONE", "passed": True, "cost": 0,
                    "tokens": {"output": 0}, "steps_run": len(steps), "replans": 0,
                    "budget_stop": False}
        engine.run_plan = fake_run_plan

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps

    async def _run(self, params):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, execute_plan, notify_human,
                                plan_swarm, plan_task_steps, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="rbq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, plan_task_steps,
                                execute_plan, clarify_request, classify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip, notify_human],
                    activity_executor=ex,
                ):
                    return await env.client.execute_workflow(
                        OttoWorkflow.run, params,
                        id="rb-" + uuid.uuid4().hex[:8], task_queue="rbq")

    def _steps(self):
        return [{"id": "s1", "goal": "check expiry", "context": "", "needs": [],
                 "produces": "s1", "done_when": "", "cap": ""}]

    async def test_authored_steps_skip_the_planner_and_disable_replan(self):
        out = await self._run({"request": "rotate certs", "unattended": True,
                               "cap": {"name": "runner", "kind": "agent", "risk": "read"},
                               "steps": self._steps()})
        self.assertEqual(out["result"], "RUNBOOK DONE")
        self.assertEqual(self.planner_called, [])          # its steps ARE the plan
        self.assertEqual(len(self.run_plan_args), 1)
        self.assertEqual([s["id"] for s in self.run_plan_args[0]["steps"]], ["s1"])
        self.assertFalse(self.run_plan_args[0]["replan"])   # never rewrite a human's graph
        self.assertIsNotNone(self.run_plan_args[0]["resolve_cap"])   # per-step caps enabled

    async def test_authored_steps_run_even_with_plan_mode_off(self):
        # plan_mode gates whether Otto should INVENT a plan, which says nothing about whether it
        # should run one a human already wrote.
        old = config.PLAN_MODE
        config.PLAN_MODE = "off"
        try:
            out = await self._run({"request": "rotate certs", "unattended": True,
                                   "cap": {"name": "runner", "kind": "agent", "risk": "read"},
                                   "steps": self._steps()})
        finally:
            config.PLAN_MODE = old
        self.assertEqual(out["result"], "RUNBOOK DONE")
        self.assertEqual(len(self.run_plan_args), 1)

    async def test_the_doc_reaches_the_judge_as_the_approved_plan_with_no_gate(self):
        # An auto-approved runbook (every cron fire) never reaches the approval gate, which is
        # where _plan is normally bound — so the doc must be bound before it.
        seen = {}
        engine.run_plan = self._orig["run_plan"]

        def fake_run_attempt(request, cap, **k):
            seen["approved_plan"] = k.get("approved_plan")
            return {"workflow": k.get("wid") or "w", "result": "R", "cost": 0,
                    "session_id": None, "model": "m", "attempt": 1}

        def fake_verify(request, result, cap, **k):
            seen["verify_plan"] = k.get("approved_plan")
            return {"passed": True, "critique": ""}
        orig_attempt = engine.run_attempt
        engine.run_attempt, engine.verify = fake_run_attempt, fake_verify
        try:
            await self._run({"request": "rotate certs", "unattended": True, "auto_approve": True,
                             "cap": {"name": "runner", "kind": "agent", "risk": "read"},
                             "doc": "## Rollback\nRe-import the previous cert."})
        finally:
            engine.run_attempt = orig_attempt
        self.assertIn("Rollback", seen.get("approved_plan") or "")
        self.assertIn("Rollback", seen.get("verify_plan") or "")


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowRepoModeTests(unittest.IsolatedAsyncioTestCase):
    """Repo-mode (issue #57): a fresh request with `repo` set provisions an isolated workspace,
    runs the capability with cwd pointed at it, then pushes a branch + opens a draft PR. The git
    side is stubbed (covered for real by WorkspaceRoundtripTests); this asserts the orchestration
    — provision, cwd threading, write-gating, finalize, cleanup."""

    def setUp(self):
        import activities
        import workspace
        self.activities, self.workspace = activities, workspace
        cap = registry.Capability("agent", "sre-minion", "implements a github issue")
        cap.risk = "read"                                 # repo-mode must FORCE the write gate
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "run_attempt", "verify", "record_attempt",
                       "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: []
        engine.clarify = lambda request, c: None          # no clarification -> straight to the gate
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. edit the code", "cost": 0, "tokens": None}   # pre-approval preview
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        self.cwds = []

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None,
                             wid=None, cwd=None, recall=False, project=None, **kwargs):
            self.cwds.append(cwd)
            return {"workflow": wid or "wf-repo", "result": "edited the code", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}
        engine.run_attempt = fake_run_attempt

        # Stub the git side (real git is covered offline by WorkspaceRoundtripTests).
        self._orig_ws = {n: getattr(workspace, n) for n in ("provision", "finalize", "cleanup")}
        self.cleaned = []
        workspace.provision = lambda repo, run_id, from_branch=False, branch=None: {
            "path": f"/tmp/ws/{run_id}", "branch": "otto/x", "repo": repo, "origin": "", "head": "h0"}
        workspace.finalize = lambda run_id, title=None, base_head=None, existing_pr=False, branch=None, body=None, **kw: {
            "branch": "otto/x", "pushed": True, "committed": True,
            "pr_url": "https://github.com/o/r/pull/1", "detail": ""}
        workspace.cleanup = lambda run_id: self.cleaned.append(run_id)

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        for n, fn in self._orig_ws.items():
            setattr(self.workspace, n, fn)
        self.activities._caps = self._orig_caps

    async def test_repo_run_gates_provisions_threads_cwd_and_opens_pr(self):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, merge_results, plan_capability, plan_swarm,
                                provision_workspace, resolve_pr_target,
            check_grounding, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="repoq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results, clarify_request,
                                classify_request, plan_capability, provision_workspace, resolve_pr_target, check_grounding,
                                finalize_workspace, cleanup_workspace, run_capability,
                                verify_capability, record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "fix the OOM bug", "repo": "myrepo", "review": False},
                        id="repo-" + uuid.uuid4().hex[:8], task_queue="repoq")
                    # repo-mode is inherently a write -> it must wait at the approval gate even
                    # though the cap is read-classified.
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("repo-mode never reached the write-approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        # cap ran with cwd pointed at the provisioned workspace
        self.assertTrue(self.cwds and self.cwds[0].startswith("/tmp/ws/"))
        # the draft PR url is surfaced in the result, and the workspace was cleaned up
        self.assertIn("https://github.com/o/r/pull/1", out["result"])
        self.assertIn("edited the code", out["result"])
        self.assertEqual(len(self.cleaned), 1)
        self.assertEqual(out["cap"]["risk"], "write")     # forced to write so the gate fires
        self.assertEqual(out["repo"], "myrepo")           # the target repo is surfaced (#59)
        # Every post-RUN moment sits inside a stage span. Without one the board card renders no
        # stage chip at all for the PR push and the delivery tail — the run reads as "running ·
        # try 1" forever, which is the stalled-run look the chip exists to prevent.
        t = out["times"]
        for st in ("RUN", "PR", "DELIVER"):
            self.assertIn(st, t, f"no {st} span recorded — the board can show no stage for it")
            self.assertIsNotNone(t[st]["dur"], f"the {st} span was never closed")
        self.assertGreaterEqual(t["PR"]["start"], t["RUN"]["start"] + t["RUN"]["dur"])
        self.assertGreaterEqual(t["DELIVER"]["start"], t["PR"]["start"] + t["PR"]["dur"])

    async def test_repo_run_with_failed_verify_but_open_pr_completes_not_held(self):
        # A repo-mode run's deliverable is a draft PR, which awaits human review on GitHub. So a
        # failed AUTOMATED verify must NOT hold it as needs-human — the run COMPLETES (card ->
        # Review), verified stays False, and an advisory is surfaced on the result.
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, merge_results, plan_capability, plan_swarm,
                                provision_workspace, resolve_pr_target,
            check_grounding, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, verify_capability)
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {
            "passed": False, "critique": "not convinced"}
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="repofailq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results, clarify_request,
                                classify_request, plan_capability, provision_workspace, resolve_pr_target, check_grounding,
                                finalize_workspace, cleanup_workspace, run_capability,
                                verify_capability, record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "fix the OOM bug", "repo": "myrepo", "review": False},
                        id="repofail-" + uuid.uuid4().hex[:8], task_queue="repofailq")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("repo-mode never reached the write-approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        self.assertIsNone(out["needs_human"])             # NOT held despite a failed verify
        self.assertFalse(out["verified"])                 # honestly reported as unverified
        self.assertIn("https://github.com/o/r/pull/1", out["result"])
        self.assertIn("Automated verification didn't pass", out["result"])

    async def test_repo_hint_auto_engages_repo_mode_for_a_write(self):
        # A board-style unattended WRITE run tied to a registered repo (repo_hint) but WITHOUT an
        # explicit repo-edit label still runs in an ISOLATED clone (+ draft PR) — the live local
        # checkout is never touched. approval="auto" (Ready==approval) so it runs without a gate.
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, merge_results, plan_capability, plan_swarm,
                                provision_workspace, resolve_pr_target,
            check_grounding, record_attempt, record_skip,
                                route_request, snapshot_settings, run_capability, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="hintq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results, clarify_request,
                                classify_request, plan_capability, provision_workspace, resolve_pr_target, check_grounding,
                                finalize_workspace, cleanup_workspace, run_capability,
                                verify_capability, record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "fix the typo", "unattended": True, "approval": "auto",
                         "repo_hint": "myrepo", "review": False,
                         "cap": {"name": "sre-minion", "kind": "agent", "risk": "write"}},
                        id="hint-" + uuid.uuid4().hex[:8], task_queue="hintq")
        self.assertTrue(self.cwds and self.cwds[0].startswith("/tmp/ws/"))   # ran in the clone
        self.assertIn("https://github.com/o/r/pull/1", out["result"])        # draft PR opened
        self.assertEqual(out["repo"], "myrepo")                              # repo-mode engaged
        self.assertEqual(len(self.cleaned), 1)                               # workspace torn down


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowRepoModeResumeTests(unittest.IsolatedAsyncioTestCase):
    """A follow-up to a repo-mode chat (`/api/continue`) must re-provision the workspace rather
    than assume the clone from the original run still exists — it was torn down right after that
    run finished. This re-provisions on the SAME branch (from_branch=True) keyed on the ORIGINAL
    run's id (`git_run_id`, not this new workflow's own id), threads `cwd` into the follow-up
    turn, and pushes any further changes to the SAME PR (existing_pr=True). The git side is
    stubbed (the real git roundtrip is covered by WorkspaceRoundtripTests)."""

    def setUp(self):
        import activities
        import workspace
        self.activities, self.workspace = activities, workspace
        cap = registry.Capability("agent", "sre-minion", "implements a github issue")
        cap.risk = "write"                                # the bound session is already a write
        self._orig = {n: getattr(engine, n) for n in
                      ("run_attempt", "record_attempt", "plan_preview", "critique_plan", "pr_url_from_run")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. fix the build config", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        # Every follow-up here asks for a CHANGE, which is what makes the gate + the workspace
        # re-provisioning the subject of these tests. Pinned rather than left to the real
        # classifier: a resumed write session now consults it on every turn (a question becomes a
        # read-only discussion turn and skips the gate entirely), so an unstubbed seam would both
        # make a live model call from the suite and decide these tests' control flow.
        self._orig["followup_write_intent"] = engine.followup_write_intent
        engine.followup_write_intent = lambda message, c, repo=None: True
        # Branch-recovery seam (recover_pr_branch activity): default to "no PR recoverable" so the
        # existing tests exercise the recorded-branch / otto-default paths deterministically;
        # a test overrides these to drive the PR-recovery path.
        engine.pr_url_from_run = lambda wid: None
        self.recovered = {"branch": None}
        self.calls = []

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None,
                             wid=None, cwd=None, recall=False, project=None, **kwargs):
            self.calls.append({"cwd": cwd, "resume": resume_session})
            return {"workflow": wid or "wf-resume-repo", "result": "fixed the build config",
                    "cost": 0.0, "session_id": "s2", "model": "m", "attempt": attempt}
        engine.run_attempt = fake_run_attempt

        self._orig_ws = {n: getattr(workspace, n) for n in
                         ("provision", "finalize", "cleanup", "pr_branch")}
        self.provisions, self.finalizes, self.cleaned = [], [], []
        workspace.pr_branch = lambda repo, url: self.recovered["branch"]

        def fake_provision(repo, run_id, from_branch=False, branch=None):
            self.provisions.append({"repo": repo, "run_id": run_id, "from_branch": from_branch,
                                    "branch": branch})
            return {"path": f"/tmp/ws/{run_id}", "branch": branch or f"otto/{run_id}",
                    "repo": repo, "origin": "", "head": "h0"}

        def fake_finalize(run_id, title=None, base_head=None, existing_pr=False, branch=None, body=None, **kw):
            self.finalizes.append({"run_id": run_id, "existing_pr": existing_pr, "branch": branch})
            return {"branch": branch or f"otto/{run_id}", "pushed": True, "committed": True,
                    "pr_url": None, "detail": "pushed to existing PR"}
        workspace.provision = fake_provision
        workspace.finalize = fake_finalize
        workspace.cleanup = lambda run_id: self.cleaned.append(run_id)

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        for n, fn in self._orig_ws.items():
            setattr(self.workspace, n, fn)
        self.activities._caps = self._orig_caps

    async def test_followup_reprovisions_original_branch_and_pushes_fix(self):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (classify_followup, cleanup_workspace, clarify_request,
                                finalize_workspace, plan_capability, provision_workspace, resolve_pr_target,
            check_grounding,
                                recover_pr_branch, record_attempt, record_skip, run_capability,
                                verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="rrq", workflows=[OttoWorkflow],
                    activities=[classify_followup, clarify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                recover_pr_branch, run_capability, verify_capability,
                                record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "you picked the wrong build config, fix it", "resume": "sess-1",
                         "cap": {"name": "sre-minion", "kind": "agent", "risk": "write"},
                         "repo": "myrepo", "git_run_id": "web-orig1"},
                        id="rr-" + uuid.uuid4().hex[:8], task_queue="rrq")
                    # bound cap is already a write -> the resumed follow-up still gates.
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("resumed write follow-up never reached the approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        # Re-provisioned on the ORIGINAL run's id, from the existing branch — not a fresh one
        # under this new workflow's own id. No git_branch was passed in (an older-style chat, or
        # a run whose work landed on Otto's own branch), so it falls back to the default.
        self.assertEqual(self.provisions, [{"repo": "myrepo", "run_id": "web-orig1",
                                            "from_branch": True, "branch": None}])
        # The follow-up ran with BOTH cwd (the re-provisioned clone) and the original session id —
        # a genuine resume now has somewhere real to look.
        self.assertEqual(self.calls, [{"cwd": "/tmp/ws/web-orig1", "resume": "sess-1"}])
        # Pushed to the SAME PR (existing_pr=True), kept under the original git identity, then
        # torn back down.
        self.assertEqual(self.finalizes, [{"run_id": "web-orig1", "existing_pr": True,
                                           "branch": "otto/web-orig1"}])
        self.assertEqual(self.cleaned, ["web-orig1"])
        self.assertEqual(out["result"],
                         "fixed the build config\n\nPushed follow-up changes to the existing PR on `otto/web-orig1`.")
        self.assertEqual(out["repo"], "myrepo")
        self.assertEqual(out["git_run_id"], "web-orig1")

    async def test_followup_reprovisions_capability_owned_branch(self):
        # sre-minion drove its own git and opened its PR on branch "22", not `otto/<run_id>`
        # (workspace._agent_pr) — the original run's result carries that branch as `git_branch`,
        # and a follow-up must re-provision THAT branch, not guess the Otto default.
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (classify_followup, cleanup_workspace, clarify_request,
                                finalize_workspace, plan_capability, provision_workspace, resolve_pr_target,
            check_grounding,
                                recover_pr_branch, record_attempt, record_skip, run_capability,
                                verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="rrq2", workflows=[OttoWorkflow],
                    activities=[classify_followup, clarify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                recover_pr_branch, run_capability, verify_capability,
                                record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "one more tweak", "resume": "sess-1",
                         "cap": {"name": "sre-minion", "kind": "agent", "risk": "write"},
                         "repo": "myrepo", "git_run_id": "web-orig1", "git_branch": "22"},
                        id="rr2-" + uuid.uuid4().hex[:8], task_queue="rrq2")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("resumed write follow-up never reached the approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        self.assertEqual(self.provisions, [{"repo": "myrepo", "run_id": "web-orig1",
                                            "from_branch": True, "branch": "22"}])
        self.assertEqual(self.finalizes, [{"run_id": "web-orig1", "existing_pr": True,
                                           "branch": "22"}])
        self.assertEqual(out["git_branch"], "22")

    async def test_followup_recovers_branch_from_pr_when_unrecorded(self):
        # The bug: an older chat / agent-managed run stored NO git_branch, so resume defaulted to
        # the never-pushed otto/<run_id> and dead-ended. Recover the EXACT branch from the PR the
        # original run opened (audit result -> PR URL -> gh) and re-provision THAT instead.
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (classify_followup, cleanup_workspace, clarify_request,
                                finalize_workspace, plan_capability, provision_workspace, resolve_pr_target,
            check_grounding,
                                recover_pr_branch, record_attempt, record_skip, run_capability,
                                verify_capability)
        engine.pr_url_from_run = lambda wid: "https://github.com/o/myrepo/pull/39"
        self.recovered["branch"] = "38"                   # gh resolves the PR to agent branch `38`
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="rrq4", workflows=[OttoWorkflow],
                    activities=[classify_followup, clarify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                recover_pr_branch, run_capability, verify_capability,
                                record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "the PR looks incomplete", "resume": "sess-1",
                         "cap": {"name": "sre-minion", "kind": "agent", "risk": "write"},
                         "repo": "myrepo", "git_run_id": "web-orig1"},   # NO git_branch recorded
                        id="rr4-" + uuid.uuid4().hex[:8], task_queue="rrq4")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("resumed write follow-up never reached the approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        # Re-provisioned on the RECOVERED agent branch, not the never-pushed otto default.
        self.assertEqual(self.provisions, [{"repo": "myrepo", "run_id": "web-orig1",
                                            "from_branch": True, "branch": "38"}])
        self.assertEqual(self.calls, [{"cwd": "/tmp/ws/web-orig1", "resume": "sess-1"}])
        self.assertEqual(self.finalizes, [{"run_id": "web-orig1", "existing_pr": True,
                                           "branch": "38"}])

    async def test_a_followup_that_needs_no_workspace_still_gets_one_when_nothing_was_pushed(self):
        """The tier that was missing. A repo-mode run can legitimately finish with NO commits — the
        approved plan gated implementation on an unanswered question, so the cap only commented
        (ci#66, `web-b97b623a`). `otto/<run>` was then never pushed and no PR exists, so the
        recorded-branch and PR-recovery tiers both find nothing and the chat used to dead-end: you
        could not even ask it why it hadn't implemented anything. Nothing is lost by a clean clone
        here precisely BECAUSE nothing was pushed, and the follow-up only needs the PATH back (that
        is where `claude -p --resume` looks up the session)."""
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (classify_followup, cleanup_workspace, clarify_request,
                                finalize_workspace, plan_capability, provision_workspace, resolve_pr_target,
            check_grounding,
                                recover_pr_branch, record_attempt, record_skip, run_capability,
                                verify_capability)

        def branchless_provision(repo, run_id, from_branch=False, branch=None):
            # Every from_branch attempt fails — the branch is nowhere, because it was never pushed.
            if from_branch:
                raise ValueError(f"fetch of existing branch {branch or run_id} failed: not found")
            self.provisions.append({"repo": repo, "run_id": run_id, "from_branch": from_branch,
                                    "branch": branch})
            return {"path": f"/tmp/ws/{run_id}", "branch": f"otto/{run_id}", "repo": repo,
                    "origin": "", "head": "h0"}
        self.workspace.provision = branchless_provision

        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="rrq4", workflows=[OttoWorkflow],
                    activities=[classify_followup, clarify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                recover_pr_branch, run_capability, verify_capability,
                                record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "have another go at it — implement it properly this time",
                         "resume": "sess-1",
                         "cap": {"name": "sre-minion", "kind": "agent", "risk": "write"},
                         "repo": "myrepo", "git_run_id": "web-orig1"},
                        id="rr4-" + uuid.uuid4().hex[:8], task_queue="rrq4")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("resumed write follow-up never reached the approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        # A clean clone at the ORIGINAL run's path — same path, so the session resolves.
        self.assertEqual(self.provisions, [{"repo": "myrepo", "run_id": "web-orig1",
                                            "from_branch": False, "branch": None}])
        self.assertEqual(self.calls, [{"cwd": "/tmp/ws/web-orig1", "resume": "sess-1"}])
        self.assertNotIn("Can't continue", out["result"])

    async def test_a_merged_prs_followup_dead_ends_rather_than_getting_a_clean_clone(self):
        """The guard on the clean-clone tier. When the original run DID open a PR but its head
        branch is gone (merged and deleted), a fresh default-branch clone would silently discard the
        commits a follow-up might amend — so this case must still dead-end. That distinction is the
        whole reason the tier keys on "was a PR ever opened?" rather than "did checkout fail?", and
        it is why `recover_pr_branch` reports its `pr_url` even when it cannot resolve a branch."""
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (classify_followup, cleanup_workspace, clarify_request,
                                finalize_workspace, plan_capability, provision_workspace, resolve_pr_target,
            check_grounding,
                                recover_pr_branch, record_attempt, record_skip, run_capability,
                                verify_capability)

        engine.pr_url_from_run = lambda wid: "https://github.com/o/myrepo/pull/38"
        self.recovered["branch"] = None                  # PR exists; its head no longer does

        def failing_provision(repo, run_id, from_branch=False, branch=None):
            self.provisions.append({"from_branch": from_branch, "branch": branch})
            raise ValueError("fetch of existing branch failed: not found")
        self.workspace.provision = failing_provision

        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="rrq5", workflows=[OttoWorkflow],
                    activities=[classify_followup, clarify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                recover_pr_branch, run_capability, verify_capability,
                                record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "one more tweak", "resume": "sess-1",
                         "cap": {"name": "sre-minion", "kind": "agent", "risk": "write"},
                         "repo": "myrepo", "git_run_id": "web-orig1"},
                        id="rr5-" + uuid.uuid4().hex[:8], task_queue="rrq5")
                    out = await h.result()
        self.assertIn("Can't continue this conversation", out["result"])
        self.assertEqual(self.calls, [])
        # Never attempted a clean clone — every attempt was for an existing branch.
        self.assertTrue(self.provisions, "expected at least one branch attempt")
        self.assertTrue(all(p["from_branch"] for p in self.provisions),
                        f"a merged PR's follow-up fell through to a clean clone: {self.provisions}")

    async def test_followup_surfaces_error_when_branch_gone_instead_of_blind_resume(self):
        # If re-provisioning fails (the branch was merged/deleted), the workflow must NOT fall
        # back to running the capability with cwd=None — that silently resumes `claude -p` from
        # the wrong directory, can't find the session's history, and returns "(no output)" while
        # wedging the chat into repeating the same doomed attempt forever. It must surface a
        # clear, actionable message instead and never call run_capability at all.
        #
        # The message now arrives WITHOUT the approval gate ever firing (it used to be raised after
        # the human approved): a follow-up that cannot run is not something to ask permission for,
        # and the gate's own plan preview needs the very workspace that is missing.
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (classify_followup, cleanup_workspace, clarify_request,
                                finalize_workspace, plan_capability, provision_workspace, resolve_pr_target,
            check_grounding,
                                recover_pr_branch, record_attempt, record_skip, run_capability,
                                verify_capability)

        def failing_provision(repo, run_id, from_branch=False, branch=None):
            raise ValueError("fetch of existing branch otto/web-orig1 failed: branch not found")
        self.workspace.provision = failing_provision

        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="rrq3", workflows=[OttoWorkflow],
                    activities=[classify_followup, clarify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                recover_pr_branch, run_capability, verify_capability,
                                record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "one more tweak", "resume": "sess-1",
                         "cap": {"name": "sre-minion", "kind": "agent", "risk": "write"},
                         "repo": "myrepo", "git_run_id": "web-orig1"},
                        id="rr3-" + uuid.uuid4().hex[:8], task_queue="rrq3")
                    gated = False
                    for _ in range(20):
                        st = await h.query(OttoWorkflow.status)
                        gated = gated or st["awaiting_approval"]
                        await asyncio.sleep(0.05)
                    out = await h.result()
        self.assertFalse(gated, "an uncontinuable follow-up must not ask for approval first")
        self.assertEqual(self.calls, [])                    # run_capability never called
        self.assertIn("Can't continue this conversation", out["result"])
        self.assertNotEqual(out["result"], "(no output)")
        self.assertEqual(out["session_id"], "sess-1")        # stays bound, no wedged-blank session


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowInteractiveRepoDetectTests(unittest.IsolatedAsyncioTestCase):
    """Interactive auto-detect: a fresh chat WRITE that names a registered repo and edits its
    code auto-engages repo-mode (isolated clone + draft PR) with NO repo picked — so the composer's
    repo picker is an optional override, not a required upfront choice. The detect seams
    (candidate_repo + repo_edit_intent) and the git side are stubbed."""

    def setUp(self):
        import activities
        import workspace
        self.activities, self.workspace = activities, workspace
        cap = registry.Capability("agent", "sre-minion", "implements a github issue")
        cap.risk = "write"
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "run_attempt", "verify", "record_attempt",
                       "plan_preview", "critique_plan", "candidate_repo", "repo_edit_intent")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: []
        engine.clarify = lambda request, c: None
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. edit the code", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        # The request names exactly one registered repo, and it genuinely edits that repo's code.
        engine.candidate_repo = lambda request, names: "myrepo"
        engine.repo_edit_intent = lambda request, repo: True
        self.cwds = []

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None,
                             wid=None, cwd=None, recall=False, project=None, **kwargs):
            self.cwds.append(cwd)
            return {"workflow": wid or "wf-id", "result": "edited the code", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}
        engine.run_attempt = fake_run_attempt

        self._orig_ws = {n: getattr(workspace, n) for n in ("provision", "finalize", "cleanup")}
        self.cleaned = []
        workspace.provision = lambda repo, run_id, from_branch=False, branch=None: {
            "path": f"/tmp/ws/{run_id}", "branch": "otto/x", "repo": repo, "origin": "", "head": "h0"}
        workspace.finalize = lambda run_id, title=None, base_head=None, existing_pr=False, branch=None, body=None, **kw: {
            "branch": "otto/x", "pushed": True, "committed": True,
            "pr_url": "https://github.com/o/r/pull/1", "detail": ""}
        workspace.cleanup = lambda run_id: self.cleaned.append(run_id)

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        for n, fn in self._orig_ws.items():
            setattr(self.workspace, n, fn)
        self.activities._caps = self._orig_caps

    async def test_interactive_write_naming_a_repo_auto_isolates(self):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, merge_results, plan_capability, plan_swarm,
                                provision_workspace, resolve_pr_target,
            check_grounding, record_attempt, record_skip, route_request, snapshot_settings,
                                run_capability, suggest_repo, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=8) as ex:
                async with Worker(
                    env.client, task_queue="detq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results, clarify_request,
                                classify_request, plan_capability, suggest_repo,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                run_capability, verify_capability, record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    # Fresh interactive write, NO repo picked.
                    h = await env.client.start_workflow(
                        OttoWorkflow.run, {"request": "fix the OOM bug in myrepo", "review": False},
                        id="det-" + uuid.uuid4().hex[:8], task_queue="detq")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("auto-detected repo-mode never reached the write-approval gate")
                    self.assertEqual(st["repo"], "myrepo")          # gate transparently shows the clone target
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        self.assertTrue(self.cwds and self.cwds[0].startswith("/tmp/ws/"))   # ran in the clone
        self.assertIn("https://github.com/o/r/pull/1", out["result"])        # draft PR opened
        self.assertEqual(out["repo"], "myrepo")
        self.assertEqual(len(self.cleaned), 1)


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowQALoopTests(unittest.IsolatedAsyncioTestCase):
    """Post-PR QA loop: after the draft PR opens, the QA cap validates it; a FAIL folds its
    findings into a fix on the SAME branch (from_branch provision + existing_pr finalize) and
    re-QAs; a PASS ends the loop. The git + Claude sides are stubbed — this asserts the
    orchestration (QA runs, fix round wiring, verdict surfaced)."""

    def setUp(self):
        import activities
        import workspace
        self.activities, self.workspace = activities, workspace
        import config
        worker_cap = registry.Capability("agent", "sre-minion", "implements a github issue")
        worker_cap.risk = "read"                          # repo-mode forces the write gate
        qa_cap = registry.Capability("agent", config.QA_CAP, "validates a change empirically")
        qa_cap.risk = "write"
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "run_attempt", "verify",
                       "record_attempt", "judge_qa", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [worker_cap, qa_cap]
        engine.plan = lambda request, caps, project_root=None: worker_cap
        engine.decompose = lambda request, caps, project_root=None: []
        engine.clarify = lambda request, c: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. edit the code", "cost": 0, "tokens": None}   # pre-approval preview
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        self.runs = []

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None,
                             wid=None, cwd=None, recall=False, project=None, **kwargs):
            self.runs.append({"cap": cap.name, "critique": critique, "cwd": cwd})
            res = "QA transcript" if cap.name == config.QA_CAP else "edited the code"
            return {"workflow": wid or "w", "result": res, "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}
        engine.run_attempt = fake_run_attempt
        # QA judge: FAIL the first pass, PASS after the fix lands.
        self.verdicts = iter([
            {"verdict": "fail", "critique": "over-mutes beyond its policy"},
            {"verdict": "pass", "critique": ""},
        ])
        engine.judge_qa = lambda request, result, project=None: next(self.verdicts)

        self._orig_ws = {n: getattr(workspace, n) for n in ("provision", "finalize", "cleanup")}
        self.provisions, self.finalizes, self.cleaned = [], [], []

        self.provision_raises = False

        def fake_provision(repo, run_id, from_branch=False, branch=None):
            self.provisions.append({"from_branch": from_branch, "branch": branch})
            if from_branch and self.provision_raises:
                raise ValueError(f"fetch of existing branch {branch} failed: "
                                 f"fatal: couldn't find remote ref {branch}")
            return {"path": f"/tmp/ws/{run_id}", "branch": branch or "otto/x", "repo": repo,
                    "origin": "", "head": "h0"}

        def fake_finalize(run_id, title=None, base_head=None, existing_pr=False, branch=None, body=None, **kw):
            self.finalizes.append({"existing_pr": existing_pr, "branch": branch})
            return {"branch": "otto/x", "pushed": True, "committed": True,
                    "pr_url": (None if existing_pr else "https://github.com/o/r/pull/1"),
                    "detail": ""}
        workspace.provision = fake_provision
        workspace.finalize = fake_finalize
        workspace.cleanup = lambda run_id: self.cleaned.append(run_id)
        # The PR's head branch — what the fix round must check out. Deliberately NOT
        # `otto/<run_id>`: a run amending an existing PR pushes to that PR's branch.
        self._orig_prb = workspace.pr_branch
        workspace.pr_branch = lambda repo, pr_url: "feature/someone-elses"

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        for n, fn in self._orig_ws.items():
            setattr(self.workspace, n, fn)
        self.workspace.pr_branch = self._orig_prb
        self.activities._caps = self._orig_caps

    async def test_qa_fail_then_fix_then_pass(self):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, judge_qa, merge_results, plan_capability,
                                plan_swarm, pr_head_branch, provision_workspace, resolve_pr_target,
            check_grounding, qa_capability,
                                record_attempt,
                                record_skip, route_request, snapshot_settings, run_capability, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=8) as ex:
                async with Worker(
                    env.client, task_queue="qaq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results, clarify_request,
                                classify_request, plan_capability, provision_workspace, resolve_pr_target, check_grounding,
                                finalize_workspace, cleanup_workspace, run_capability,
                                verify_capability, qa_capability, judge_qa, record_attempt,
                                record_skip, pr_head_branch],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "add muting rules", "repo": "myrepo", "qa": True, "review": False},
                        id="qa-" + uuid.uuid4().hex[:8], task_queue="qaq")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("never reached the write-approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        import config
        # QA ran twice (initial FAIL + post-fix PASS).
        qa_runs = [r for r in self.runs if r["cap"] == config.QA_CAP]
        self.assertEqual(len(qa_runs), 2)
        # A fix run of the worker cap happened in between, folding in the QA critique.
        fix_runs = [r for r in self.runs if r["cap"] == "sre-minion" and r["critique"]]
        self.assertTrue(any("over-mutes" in (r["critique"] or "") for r in fix_runs))
        # The fix re-provisioned the EXISTING branch and pushed to the EXISTING PR (no new PR).
        self.assertTrue(any(p["from_branch"] for p in self.provisions))
        self.assertTrue(any(f["existing_pr"] for f in self.finalizes))
        # Final outcome advertises the QA pass.
        self.assertEqual(out["qa"]["state"], "pass")
        self.assertIn("PASS", out["result"])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowReviewLoopTests(unittest.IsolatedAsyncioTestCase):
    """Post-PR code-review loop: after the draft PR opens, the review cap reviews it; FAIL folds
    the findings into a fix on the SAME branch (from_branch provision + existing_pr finalize) and
    re-reviews; a clean PASS ends the loop. Default-ON for the general worker cap — this run does
    NOT pass review:True, proving the worker enables it by itself. Git + Claude sides stubbed."""

    def setUp(self):
        import activities
        import workspace
        self.activities, self.workspace = activities, workspace
        import config
        # Named config.WORKER_CAP so the review loop turns on WITHOUT an explicit review flag.
        worker_cap = registry.Capability("custom", config.WORKER_CAP, "implements a change")
        worker_cap.risk = "read"                          # repo-mode forces the write gate
        review_cap = registry.Capability("skill", config.REVIEW_CAP, "reviews a PR")
        review_cap.risk = "read"
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "run_attempt", "verify",
                       "record_attempt", "judge_review", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [worker_cap, review_cap]
        engine.plan = lambda request, caps, project_root=None: worker_cap
        engine.decompose = lambda request, caps, project_root=None: []
        engine.clarify = lambda request, c: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. edit the code", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        self.runs = []

        self.fix_errors = False           # make the post-review fix round die like a real one

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None,
                             wid=None, cwd=None, recall=False, project=None, **kwargs):
            self.runs.append({"cap": cap.name, "critique": critique, "cwd": cwd,
                              "local_disabled": kwargs.get("local_disabled")})
            res = "review transcript" if cap.name == config.REVIEW_CAP else "edited the code"
            # A fix round is a worker run carrying the reviewer's critique.
            if self.fix_errors and cap.name == config.WORKER_CAP and critique:
                return {"workflow": wid or "w", "result": "(local model produced reasoning but "
                        "no final answer — finish_reason: length)", "cost": 0.0, "is_error": True,
                        "session_id": "s", "model": "m", "attempt": attempt}
            return {"workflow": wid or "w", "result": res, "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt}
        engine.run_attempt = fake_run_attempt
        # Review judge: FAIL (findings) the first pass, clean PASS after the fix lands.
        self.verdicts = iter([
            {"verdict": "fail", "critique": "hardcoded region string in the resource"},
            {"verdict": "pass", "critique": ""},
        ])
        engine.judge_review = lambda request, result, project=None: next(self.verdicts)

        self._orig_ws = {n: getattr(workspace, n) for n in ("provision", "finalize", "cleanup")}
        self.provisions, self.finalizes, self.cleaned = [], [], []

        self.provision_raises = False

        def fake_provision(repo, run_id, from_branch=False, branch=None):
            self.provisions.append({"from_branch": from_branch, "branch": branch})
            if from_branch and self.provision_raises:
                raise ValueError(f"fetch of existing branch {branch} failed: "
                                 f"fatal: couldn't find remote ref {branch}")
            return {"path": f"/tmp/ws/{run_id}", "branch": branch or "otto/x", "repo": repo,
                    "origin": "", "head": "h0"}

        def fake_finalize(run_id, title=None, base_head=None, existing_pr=False, branch=None, body=None, **kw):
            self.finalizes.append({"existing_pr": existing_pr, "branch": branch})
            return {"branch": "otto/x", "pushed": True, "committed": True,
                    "pr_url": (None if existing_pr else "https://github.com/o/r/pull/1"),
                    "detail": ""}
        workspace.provision = fake_provision
        workspace.finalize = fake_finalize
        workspace.cleanup = lambda run_id: self.cleaned.append(run_id)
        # The PR's head branch — what the fix round must check out. Deliberately NOT
        # `otto/<run_id>`: a run amending an existing PR pushes to that PR's branch.
        self._orig_prb = workspace.pr_branch
        workspace.pr_branch = lambda repo, pr_url: "feature/someone-elses"

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        for n, fn in self._orig_ws.items():
            setattr(self.workspace, n, fn)
        self.workspace.pr_branch = self._orig_prb
        self.activities._caps = self._orig_caps

    async def test_review_fail_then_fix_then_pass(self):
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, judge_review, merge_results, plan_capability,
                                plan_swarm, pr_head_branch, provision_workspace, resolve_pr_target,
            check_grounding, record_attempt,
                                record_skip,
                                review_capability, route_request, snapshot_settings, run_capability, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=8) as ex:
                async with Worker(
                    env.client, task_queue="revq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results, clarify_request,
                                classify_request, plan_capability, provision_workspace, resolve_pr_target, check_grounding,
                                finalize_workspace, cleanup_workspace, run_capability,
                                verify_capability, review_capability, judge_review,
                                record_attempt, record_skip, pr_head_branch],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "add a lifecycle block", "repo": "myrepo"},
                        id="rev-" + uuid.uuid4().hex[:8], task_queue="revq")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("never reached the write-approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        import config
        # Review ran twice (initial FAIL + post-fix PASS) — with NO review flag passed.
        review_runs = [r for r in self.runs if r["cap"] == config.REVIEW_CAP]
        self.assertEqual(len(review_runs), 2)
        # A fix run of the worker cap folded in the review critique.
        fix_runs = [r for r in self.runs if r["cap"] == config.WORKER_CAP and r["critique"]]
        self.assertTrue(any("hardcoded region" in (r["critique"] or "") for r in fix_runs))
        # The fix re-provisioned the EXISTING branch and pushed to the EXISTING PR (no new PR).
        self.assertTrue(any(p["from_branch"] for p in self.provisions))
        self.assertTrue(any(f["existing_pr"] for f in self.finalizes))
        # Final outcome advertises the clean review.
        self.assertEqual(out["review"]["state"], "pass")
        self.assertIn("clean", out["result"])
        # The whole review loop — rounds, fix runs and all — is one stage span, so the board
        # card names it instead of showing a bare "running · try 1" for its several minutes.
        self.assertIsNotNone(out["times"].get("REVIEW", {}).get("dur"),
                             "the review loop recorded no closed REVIEW span")
        # ...and that branch is the PR's OWN head, never `otto/<run_id>`: a run asked to amend
        # an existing PR pushes to THAT branch and never pushes its own, so defaulting would
        # fetch a ref that was never created (`web-346d40a5`).
        fixp = [p for p in self.provisions if p["from_branch"]]
        self.assertTrue(all(p["branch"] == "feature/someone-elses" for p in fixp), fixp)
        self.assertTrue(all(f["branch"] == "feature/someone-elses"
                            for f in self.finalizes if f["existing_pr"]), self.finalizes)

    async def test_an_unprovisionable_fix_branch_does_not_fail_the_run(self):
        """By the time review runs, the work is committed, pushed and has a PR. A fix round that
        cannot check the branch out must degrade to inconclusive — letting the activity error
        escape reports a finished run as Failed (`web-346d40a5`: workflow_error, 'Activity task
        failed', after a passing verify)."""
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, judge_review, merge_results, plan_capability,
                                finalize_terminal, plan_swarm, pr_head_branch,
                                provision_workspace, resolve_pr_target,
            check_grounding, record_attempt,
                                record_skip, review_capability, route_request, snapshot_settings,
                                run_capability, verify_capability)
        self.provision_raises = True
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=8) as ex:
                async with Worker(
                    env.client, task_queue="revq2", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results,
                                clarify_request, classify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                run_capability, verify_capability, review_capability,
                                judge_review, record_attempt, record_skip, pr_head_branch,
                                finalize_terminal],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "add a lifecycle block", "repo": "myrepo"},
                        id="rev2-" + uuid.uuid4().hex[:8], task_queue="revq2")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("never reached the write-approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()          # completes — does NOT raise
        self.assertEqual(out["review"]["state"], "inconclusive")
        self.assertIn("could not be checked out", out["review"]["critique"])
        # Blocked for a human (PR stays draft), NOT a dead workflow.
        self.assertEqual(out["needs_human"]["reason"], "review_inconclusive")

    async def test_an_errored_fix_round_stops_instead_of_rereviewing(self):
        """An errored/timed-out fix round commits NOTHING, so re-reviewing walks the same judge
        over the same diff and spends the rest of the budget reaching the same verdict (run
        web-2bd1a194: a 944s fix round died at the local model's output wall with zero commits,
        and the loop re-reviewed anyway). It must stop for a human instead, and the dead round
        must not be pushed, counted, or advertised as a fix that happened."""
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, judge_review, merge_results, plan_capability,
                                finalize_terminal, plan_swarm, pr_head_branch,
                                provision_workspace, resolve_pr_target,
            check_grounding, record_attempt,
                                record_skip, review_capability, route_request, snapshot_settings,
                                run_capability, verify_capability)
        import config
        self.fix_errors = True
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=8) as ex:
                async with Worker(
                    env.client, task_queue="revq3", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results,
                                clarify_request, classify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                run_capability, verify_capability, review_capability,
                                judge_review, record_attempt, record_skip, pr_head_branch,
                                finalize_terminal],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "add a lifecycle block", "repo": "myrepo"},
                        id="rev3-" + uuid.uuid4().hex[:8], task_queue="revq3")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("never reached the write-approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    out = await h.result()
        # The reviewer ran ONCE. Pre-fix it ran again on an unchanged PR (and again, and again).
        review_runs = [r for r in self.runs if r["cap"] == config.REVIEW_CAP]
        self.assertEqual(len(review_runs), 1, self.runs)
        # Stopped for a human, naming the fix failure — not a silent still-FAIL.
        self.assertEqual(out["review"]["state"], "inconclusive")
        self.assertIn("fix round did not finish", out["review"]["critique"])
        self.assertIn("finish_reason: length", out["review"]["critique"])
        self.assertEqual(out["needs_human"]["reason"], "review_inconclusive")
        # The reviewer's ORIGINAL findings still reach the human — the fix failure is prepended
        # to them, not swapped for them.
        self.assertIn("hardcoded region", out["review"]["critique"])
        # Checked AFTER finalize: a round that commits and THEN dies keeps its commits (the
        # dead-end-never-discard rule), and finalize is a no-op when it committed nothing.
        self.assertTrue([f for f in self.finalizes if f["existing_pr"]], self.finalizes)
        # The dead round is not counted, so the summary can't read "clean after 1 fix round".
        self.assertEqual(out["review"]["rounds"], 0)
        # ...and the clone it provisioned is still torn down.
        self.assertTrue(self.cleaned)

    async def test_a_fix_round_never_runs_on_the_local_backend(self):
        """Both post-PR fix loops are one-shot — no retry, no escalation — so `LOCAL_FALLBACK`'s
        promise that a failing local model is covered by Claude has no rung to keep it here.
        The fix round must therefore be dispatched with local_disabled (run web-2bd1a194: 944s
        and 1.68M input tokens against a 22k-line file before the local output wall)."""
        import asyncio
        import uuid
        from workflows import OttoWorkflow
        from activities import (cleanup_workspace, clarify_request, classify_request,
                                finalize_workspace, judge_review, merge_results, plan_capability,
                                plan_swarm, pr_head_branch, provision_workspace, resolve_pr_target,
            check_grounding, record_attempt,
                                record_skip, review_capability, route_request, snapshot_settings,
                                run_capability, verify_capability)
        import config
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=8) as ex:
                async with Worker(
                    env.client, task_queue="revq4", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, merge_results,
                                clarify_request, classify_request, plan_capability,
                                provision_workspace, resolve_pr_target, check_grounding, finalize_workspace, cleanup_workspace,
                                run_capability, verify_capability, review_capability,
                                judge_review, record_attempt, record_skip, pr_head_branch],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "add a lifecycle block", "repo": "myrepo"},
                        id="rev4-" + uuid.uuid4().hex[:8], task_queue="revq4")
                    for _ in range(100):
                        st = await h.query(OttoWorkflow.status)
                        if st["awaiting_approval"]:
                            break
                        await asyncio.sleep(0.05)
                    else:
                        self.fail("never reached the write-approval gate")
                    await h.signal(OttoWorkflow.approve, True)
                    await h.result()
        fix_runs = [r for r in self.runs if r["cap"] == config.WORKER_CAP and r["critique"]]
        self.assertTrue(fix_runs, self.runs)
        self.assertTrue(all(r["local_disabled"] for r in fix_runs), fix_runs)
        # The ORIGINAL execution is untouched — this is a fix-round rule, not a global opt-out
        # of the local backend the operator chose.
        first = [r for r in self.runs if r["cap"] == config.WORKER_CAP and not r["critique"]][0]
        self.assertFalse(first["local_disabled"])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowInPlaceGuardTests(unittest.IsolatedAsyncioTestCase):
    """In-place edit guard (issue #59): an interactive, NON-repo-mode run that mutates a
    registered live checkout (via Bash) is flagged in the result + audited, not silent."""

    def setUp(self):
        import activities
        import workspace
        self.activities, self.workspace = activities, workspace
        cap = registry.Capability("agent", "sre-minion", "does work"); cap.risk = "read"
        self._orig = {n: getattr(engine, n) for n in
                      ("plan", "decompose", "clarify", "request_write_intent",
                       "run_attempt", "verify", "record_attempt")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        engine.plan = lambda request, caps, project_root=None: cap
        engine.decompose = lambda request, caps, project_root=None: []
        engine.clarify = lambda request, c: None
        engine.request_write_intent = lambda request, c: False    # a genuine read -> no gate
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {"passed": True, "critique": ""}
        engine.record_attempt = lambda *a, **k: None
        engine.run_attempt = lambda request, cap, **k: {
            "workflow": k.get("wid") or "w", "result": "did stuff", "cost": 0.0,
            "session_id": "s", "model": "m", "attempt": k.get("attempt", 1)}
        # Simulate: the registered repo's HEAD moves DURING the run (an in-place commit).
        self._snaps = iter([
            {"terraform-modules": {"path": "/r", "head": "aaaaaaa", "dirty": False}},   # before
            {"terraform-modules": {"path": "/r", "head": "bbbbbbb", "dirty": False}},   # after
        ])
        self._orig_snap = workspace.snapshot
        workspace.snapshot = lambda: next(self._snaps)
        self.audited = []
        self._orig_audit = engine.audit_repo_changes
        engine.audit_repo_changes = lambda wid, req, changed: self.audited.append(changed)

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.workspace.snapshot = self._orig_snap
        engine.audit_repo_changes = self._orig_audit
        self.activities._caps = self._orig_caps

    async def test_in_place_edit_is_flagged_and_audited(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, detect_repo_changes,
                                plan_swarm, record_attempt, record_skip, route_request, snapshot_settings,
                                run_capability, resolve_pr_target,
            check_grounding, snapshot_repos, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=6) as ex:
                async with Worker(
                    env.client, task_queue="ipq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, clarify_request, classify_request,
                                run_capability, resolve_pr_target, check_grounding, verify_capability, record_attempt, record_skip,
                                snapshot_repos, detect_repo_changes],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run, {"request": "tweak terraform-modules"},
                        id="ip-" + uuid.uuid4().hex[:8], task_queue="ipq")
        self.assertTrue(out["in_place"])                                  # surfaced in the return
        self.assertEqual(out["in_place"][0]["name"], "terraform-modules")
        self.assertIn("outside an isolated workspace", out["result"])     # flagged in the chat
        self.assertEqual(len(self.audited), 1)                            # and audited


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class ActivityFailFastTests(unittest.IsolatedAsyncioTestCase):
    """An activity the worker never registered (the worker.py-drift bug that hung a run at
    'executing…' forever) must now FAIL the workflow fast, not retry silently. The
    `_RETRY` policy in workflows.py marks NotFoundError non-retryable + bounds attempts."""

    async def test_unregistered_activity_fails_the_run_not_hangs(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, classify_request, record_attempt,
                                record_skip, run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        from temporalio.client import WorkflowFailureError
        # Worker is MISSING plan_swarm (the first activity a fresh request now calls) and
        # route_request. With no retry policy an unregistered activity loops forever (the
        # time-skipping env would hang); the guard makes it fail on the first attempt instead.
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="ffq", workflows=[OttoWorkflow],
                    activities=[clarify_request, classify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip],
                    activity_executor=ex,
                ):
                    h = await env.client.start_workflow(
                        OttoWorkflow.run, {"request": "do something"},
                        id="ff-" + uuid.uuid4().hex[:8], task_queue="ffq")
                    with self.assertRaises(WorkflowFailureError) as ctx:
                        await h.result()
        # The unregistered-activity detail lives in the cause chain (WorkflowFailureError ->
        # ActivityError -> ApplicationError[NotFoundError]).
        chain, exc = [], ctx.exception
        while exc is not None:
            chain.append(str(exc))
            exc = exc.__cause__
        self.assertTrue(any("not registered" in m for m in chain),
                        f"expected an unregistered-activity failure, got: {chain}")


class _FakeSpec:
    def __init__(self, cron, tz):
        self.cron_expressions = list(cron)
        self.time_zone_name = tz


class _FakeHandle:
    def __init__(self, client, sid):
        self.client, self.sid = client, sid

    async def describe(self):
        if self.sid not in self.client.specs:
            raise RuntimeError("not found")
        sched = types.SimpleNamespace(
            spec=self.client.specs[self.sid],
            action=types.SimpleNamespace(args=[self.client.actions.get(self.sid, {})]),
            state=types.SimpleNamespace(paused=self.client.paused.get(self.sid, False)),
            policy=None)
        return types.SimpleNamespace(schedule=sched)

    async def update(self, updater):
        inp = types.SimpleNamespace(description=await self.describe())
        out = updater(inp)
        self.client.specs[self.sid] = out.schedule.spec
        self.client.actions[self.sid] = out.schedule.action.args[0]
        self.client.updated.append(self.sid)

    async def delete(self):
        self.client.specs.pop(self.sid, None)
        self.client.deleted.append(self.sid)

    async def trigger(self, overlap=None):
        self.client.triggered.append((self.sid, overlap))


class _FakeScheduleClient:
    """Just enough of a Temporal client for scheduler._reconcile / _run_now."""
    def __init__(self, specs, actions=None, paused=None):
        self.specs = dict(specs)           # sid -> _FakeSpec currently in "Temporal"
        self.actions = dict(actions or {})  # sid -> workflow-args dict (chat_key etc.)
        self.paused = dict(paused or {})
        self.created, self.updated, self.deleted, self.triggered = [], [], [], []
        self.running = []                  # workflow ids currently RUNNING (overlap guard)
        self.started = []                  # (wid, args) from start_workflow

    def get_schedule_handle(self, sid):
        return _FakeHandle(self, sid)

    async def create_schedule(self, sid, schedule):
        self.created.append(sid)
        self.specs[sid] = schedule.spec
        self.actions[sid] = schedule.action.args[0]

    async def list_schedules(self):
        ids = list(self.specs.keys())

        async def _it():
            for sid in ids:
                yield types.SimpleNamespace(id=sid)
        return _it()

    def list_workflows(self, query):
        ids = list(self.running)

        async def _it():
            for wid in ids:
                yield types.SimpleNamespace(id=wid)
        return _it()

    async def start_workflow(self, fn, args, id=None, task_queue=None):  # noqa: A002
        self.started.append((id, args))
        self.running.append(id)


@unittest.skipUnless(_HAS_TEMPORAL, "scheduler ops need temporalio")
class ScheduleReconcileTests(unittest.IsolatedAsyncioTestCase):
    """The schedule-firing bugs: drifted timezone and orphan/manual duplicate fires."""

    def _patch_client(self, fake):
        import scheduler
        self._orig_client = scheduler.tc.client
        self._orig_tz = scheduler.local_tz_name

        async def _client():
            return fake
        scheduler.tc.client = _client
        scheduler.local_tz_name = lambda: "Pacific/Auckland"
        self.addCleanup(self._restore)

    def _restore(self):
        import scheduler
        scheduler.tc.client = self._orig_client
        scheduler.local_tz_name = self._orig_tz

    async def test_reconcile_fixes_drifted_timezone(self):
        import scheduler
        # A schedule stored UTC (pre-fix) for a "30 17 * * *" cron — fires 12h off.
        fake = _FakeScheduleClient({"otto-aaa": _FakeSpec(["30 17 * * *"], None)})
        self._patch_client(fake)
        await scheduler._reconcile({"otto-aaa": {"request": "daily-summary",
                                                   "cron": "30 17 * * *", "auto_approve": True}})
        self.assertIn("otto-aaa", fake.updated)
        self.assertEqual(fake.specs["otto-aaa"].time_zone_name, "Pacific/Auckland")

    async def test_reconcile_refreshes_action_so_runs_record_chats(self):
        import scheduler
        # Pre-chat-feature schedule: correct spec, but its action lacks chat_key, so runs only
        # hit the audit trail. Reconcile must rebuild the action and restore chat recording.
        fake = _FakeScheduleClient(
            {"otto-bbb": _FakeSpec(["0 9 * * *"], "Pacific/Auckland")},
            actions={"otto-bbb": {"request": "daily-summary", "scheduled": True}})
        self._patch_client(fake)
        await scheduler._reconcile({"otto-bbb": {"request": "daily-summary",
                                                   "cron": "0 9 * * *", "auto_approve": True}})
        self.assertIn("otto-bbb", fake.updated)
        self.assertEqual(fake.actions["otto-bbb"]["chat_key"], "chat-otto-bbb")
        self.assertEqual(fake.actions["otto-bbb"]["chat_labels"], ["scheduled-job"])

    async def test_reconcile_deletes_orphans(self):
        import scheduler
        # otto-orphan is live in Temporal but absent from the store -> duplicate fires.
        fake = _FakeScheduleClient({"otto-keep": _FakeSpec(["0 9 * * *"], "Pacific/Auckland"),
                                    "otto-orphan": _FakeSpec(["*/5 * * * *"], "Pacific/Auckland")})
        self._patch_client(fake)
        await scheduler._reconcile({"otto-keep": {"request": "x", "cron": "0 9 * * *"}})
        self.assertIn("otto-orphan", fake.deleted)
        self.assertNotIn("otto-keep", fake.deleted)

    async def test_reconcile_recreates_missing(self):
        import scheduler
        # In the store but wiped from (in-memory) Temporal -> recreate so it fires again.
        fake = _FakeScheduleClient({})
        self._patch_client(fake)
        await scheduler._reconcile({"otto-ccc": {"request": "daily-summary",
                                                   "cron": "30 17 * * *", "auto_approve": True}})
        self.assertIn("otto-ccc", fake.created)
        self.assertEqual(fake.specs["otto-ccc"].time_zone_name, "Pacific/Auckland")
        self.assertEqual(fake.actions["otto-ccc"]["chat_key"], "chat-otto-ccc")

    async def test_run_now_refuses_to_stack_on_an_in_flight_run(self):
        # "Run now" used to inherit ScheduleOverlapPolicy.SKIP, which is what stopped a second
        # click from starting a concurrent duplicate ("it ran multiple times"). Runs now start
        # directly (a schedule's args are frozen, so it can't carry the operator's parameters),
        # so the same guarantee has to be enforced explicitly.
        import scheduler
        fake = _FakeScheduleClient({})
        fake.running = ["runbook-otto-ddd-aaaaaa"]
        self._patch_client(fake)
        rb = {"name": "d", "request": "r", "cron": "", "params": [], "steps": [], "doc": ""}
        with self.assertRaises(scheduler.AlreadyRunning):
            await scheduler._run_now("otto-ddd", rb, None, False)
        self.assertEqual(fake.started, [])

    async def test_run_now_starts_a_workflow_when_nothing_is_in_flight(self):
        import scheduler
        fake = _FakeScheduleClient({})
        self._patch_client(fake)
        rb = {"name": "d", "request": "check {{env}}", "cron": "", "steps": [], "doc": "",
              "params": [{"name": "env", "label": "env", "default": "stg", "choices": [],
                          "required": True}]}
        wid = await scheduler._run_now("otto-eee", rb, {"env": "prod-a"}, False)
        self.assertTrue(wid.startswith("runbook-otto-eee-"))
        # The operator's value is what runs — the whole reason this isn't ScheduleHandle.trigger().
        self.assertEqual(fake.started[0][1]["request"], "check prod-a")
        self.assertFalse(fake.started[0][1]["unattended"])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowNeedsHumanTests(unittest.IsolatedAsyncioTestCase):
    """No-silent-failure invariant, POSITIVE case: a fresh non-repo run whose automated verify
    NEVER passes must be HELD as needs_human (reason verify_exhausted) — banner prepended, board
    card routed to Blocked (delivery reply_to marked blocked) — never delivered as "done". This is
    the counterpart to WorkflowRepoModeTests.test_repo_run_with_failed_verify_but_open_pr... (the
    PR-exception, where a failed verify is only advisory); with nothing downstream to catch a bad
    result, the run must block. Only the negative (assertIsNone) side was previously covered."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "flaky-report", "produces a report")
        cap.risk = "write"
        self._orig = {n: getattr(engine, n)
                      for n in ("run_attempt", "verify", "record_attempt", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        # Verify never passes — every attempt in the ladder is judged a FAIL.
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {
            "passed": False, "critique": "still missing the numbers"}
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. do the write", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        self.attempts = []

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None,
                             wid=None, cwd=None, recall=False, project=None, **kwargs):
            self.attempts.append(attempt)
            return {"workflow": wid or "wf-nh", "result": "a plausible but wrong answer",
                    "cost": 0.0, "session_id": "s", "model": "m", "attempt": attempt,
                    "tokens": {"input": 1, "output": 1}}
        engine.run_attempt = fake_run_attempt

        self.delivered = []
        self._orig_deliver = delivery.deliver
        delivery.deliver = lambda reply_to, result, cap=None, run_id=None: (
            self.delivered.append((reply_to, result, cap)) or "ok")
        # Silence the best-effort needs-human push (asserted separately in test_gate_pushes_a_
        # notification); leaving notify_human unregistered works too (_notify swallows it) but
        # spews a scary Temporal "activity failed" traceback into the suite output.
        self._orig_notify = delivery.notify
        delivery.notify = lambda *a, **k: True
        # The needs-human finalizer writes a REAL terminal row (engine.record_terminal) — without
        # this redirect these three classes stamp phantom needs-human runs onto the developer's
        # live data/otto.db every time the suite runs.
        self._orig_db = engine._DB
        engine._DB = os.path.join(tempfile.mkdtemp(prefix="otto-nh-"), "otto.db")

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        delivery.deliver = self._orig_deliver
        delivery.notify = self._orig_notify
        engine._DB = self._orig_db

    async def test_verify_exhausted_no_pr_is_held_as_needs_human(self):
        import config
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, finalize_terminal, notify_human,
                                record_attempt, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="nhq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip, deliver_result, finalize_terminal,
                                notify_human],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "summarize last week's incidents", "unattended": True,
                         "auto_approve": True,
                         "cap": {"name": "flaky-report", "kind": "skill", "risk": "write"},
                         "reply_to": {"kind": "github_issue", "repo": "o/r", "issue": 7}},
                        id="nh-" + uuid.uuid4().hex[:8], task_queue="nhq")
        # Ran the FULL bounded ladder (never gave up early), then held.
        self.assertEqual(self.attempts, list(range(1, config.MAX_VERIFY_ATTEMPTS + 1)))
        self.assertEqual(out["needs_human"], {"reason": "verify_exhausted"})
        self.assertFalse(out["verified"])                     # honestly reported as unverified
        self.assertIsNone(out["pr"])
        self.assertTrue(out["result"].startswith("⚠️"))        # unmistakable needs-human banner
        self.assertIn("did not pass automated verification", out["result"])
        # Delivered to the reply target flagged blocked -> the board card routes to Blocked, not Done.
        self.assertEqual(len(self.delivered), 1)
        self.assertTrue(self.delivered[0][0].get("blocked"))
        self.assertFalse(self.delivered[0][0].get("repo_edit"))   # no PR opened
        # ...and it left a DURABLE terminal row. /api/needs-you is built from live Temporal
        # visibility, so a needs-human run that never reaches record_terminal is visible only
        # until history ages out — the audit trail then shows nothing but failed attempts, which
        # is exactly how a verify-exhausted run reads as "it just ran twice" weeks later.
        terminal = [e for e in engine.iter_audit_entries() if e.get("outcome") == "needs_human"]
        self.assertEqual([e.get("reason") for e in terminal], ["verify_exhausted"])
        self.assertTrue(terminal[0].get("needs_human"))
        self.assertEqual(terminal[0].get("capability"), "skill:flaky-report")


class FailureDetailTests(unittest.TestCase):
    """`str(ActivityError)` is the constant string "Activity task failed" — it carries no fact
    about what broke. Recording it stamped that placeholder into the audit row, the Chat thread
    and the owner push at once, and it was the single most common terminal detail in the live
    store (23 rows). The only other copy of the traceback is the worker log, which lives in /tmp
    and is truncated on every restart, so a `workflow_error` was in practice undiagnosable."""

    def test_the_temporal_wrapper_is_unwrapped_to_the_real_cause(self):
        from temporalio import exceptions
        import workflows
        # The exact live shape: an activity died on a KeyError over a settings key the run's
        # snapshot predates (CLAUDE.md's "a snapshot missing a key poisons the run forever").
        inner = exceptions.ApplicationError("'max_plan_revisions'", type="KeyError")
        err = exceptions.ActivityError(
            "Activity task failed", scheduled_event_id=1, started_event_id=2, identity="i",
            activity_type="verify_capability", activity_id="a", retry_state=None)
        err.__cause__ = inner
        detail = workflows._failure_detail(err)
        self.assertIn("KeyError", detail)
        self.assertIn("max_plan_revisions", detail)
        self.assertIn("verify_capability", detail)   # WHICH activity died, not just "an activity"
        self.assertNotEqual(detail, "Activity task failed")

    def test_a_plain_exception_still_names_itself(self):
        import workflows
        self.assertEqual(workflows._failure_detail(ValueError("bad repo hint")),
                         "ValueError: bad repo hint")

    def test_a_long_chain_is_clipped_WITH_a_marker(self):
        import workflows
        detail = workflows._failure_detail(ValueError("x" * 900), limit=40)
        self.assertEqual(len(detail), 40)
        self.assertTrue(detail.endswith("\u2026"))   # never a bare slice


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowFailureDetailTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a run that dies inside an activity must leave a terminal row saying WHAT died.
    Drives the real workflow so the assertion covers the call site, not just the helper."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "flaky-report", "produces a report")
        cap.risk = "read"
        self._orig = {n: getattr(engine, n)
                      for n in ("run_attempt", "verify", "record_attempt", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]

        def exploding_verify(*a, **k):
            raise KeyError("max_plan_revisions")
        engine.verify = exploding_verify
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. do it", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.run_attempt = lambda request, cap, *, attempt=1, wid=None, **kw: {
            "workflow": wid or "wf-detail", "attempt": attempt, "cost": 0.0,
            "result": "a report", "session_id": "s", "model": "m",
            "tokens": {"input": 1, "output": 1}}
        self._orig_notify = delivery.notify
        delivery.notify = lambda *a, **k: True
        self._orig_db = engine._DB
        engine._DB = os.path.join(tempfile.mkdtemp(prefix="otto-detail-"), "otto.db")

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        delivery.notify = self._orig_notify
        engine._DB = self._orig_db

    async def test_a_workflow_error_records_what_actually_broke(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, finalize_terminal, notify_human,
                                record_attempt, record_skip, route_request, snapshot_settings,
                                run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="detailq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip, deliver_result,
                                finalize_terminal, notify_human],
                    activity_executor=ex,
                ):
                    with self.assertRaises(Exception):
                        await env.client.execute_workflow(
                            OttoWorkflow.run,
                            {"request": "summarize last week", "unattended": True,
                             "auto_approve": True,
                             "cap": {"name": "flaky-report", "kind": "skill", "risk": "read"}},
                            id="detail-" + uuid.uuid4().hex[:8], task_queue="detailq")
        rows = [e for e in engine.iter_audit_entries() if e.get("outcome") == "needs_human"]
        self.assertEqual([e.get("reason") for e in rows], ["workflow_error"])
        detail = "".join(str(e.get("result") or "") for e in engine.iter_content_entries())
        self.assertIn("KeyError", detail)
        self.assertIn("max_plan_revisions", detail)
        self.assertIn("verify_capability", detail)
        self.assertNotEqual(detail.strip(), "Activity task failed")


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowStrictLocalTests(unittest.IsolatedAsyncioTestCase):
    """Strict local mode (OTTO_LOCAL_FALLBACK=0) on the TEMPORAL path: the workflow's own ladder
    is a deterministic mirror of engine.execute's, so it needs its own proof that a
    `local_strict_stop` attempt is terminal — ONE attempt, no verify call, needs_human with
    config.STRICT_STOP_REASON, and the loud reason delivered (blocked) rather than a Claude-covered
    result presented as done."""

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "local-report", "produces a report")
        cap.risk = "write"
        self._orig = {n: getattr(engine, n)
                      for n in ("run_attempt", "verify", "record_attempt", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self.verified = []
        engine.verify = lambda *a, **k: self.verified.append(1) or {"passed": True, "critique": None}
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. do it", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        self.attempts = []

        def fake_run_attempt(request, cap, *, attempt=1, wid=None, **kwargs):
            self.attempts.append(attempt)
            return {"workflow": wid or "wf-strict", "attempt": attempt, "cost": 0.0,
                    "result": config.strict_stop_message(
                        "qwen3.6", "the local server rejects tool calls"),
                    "session_id": None, "model": "qwen3.6", "backend": "local",
                    "is_error": True, "local_strict_stop": True,
                    "tokens": {"input": 0, "output": 0}}
        engine.run_attempt = fake_run_attempt

        self.delivered = []
        self._orig_deliver = delivery.deliver
        delivery.deliver = lambda reply_to, result, cap=None, run_id=None: (
            self.delivered.append((reply_to, result, cap)) or "ok")
        self._orig_notify = delivery.notify
        delivery.notify = lambda *a, **k: True
        # The needs-human finalizer writes a REAL terminal row (engine.record_terminal) — without
        # this redirect these three classes stamp phantom needs-human runs onto the developer's
        # live data/otto.db every time the suite runs.
        self._orig_db = engine._DB
        engine._DB = os.path.join(tempfile.mkdtemp(prefix="otto-nh-"), "otto.db")

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        delivery.deliver = self._orig_deliver
        delivery.notify = self._orig_notify
        engine._DB = self._orig_db

    async def test_strict_stop_is_terminal_on_the_first_attempt(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, finalize_terminal, notify_human,
                                record_attempt, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="strictq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip, deliver_result, finalize_terminal,
                                notify_human],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "summarize last week's incidents", "unattended": True,
                         "auto_approve": True,
                         "cap": {"name": "local-report", "kind": "skill", "risk": "write"},
                         "reply_to": {"kind": "github_issue", "repo": "o/r", "issue": 7}},
                        id="strict-" + uuid.uuid4().hex[:8], task_queue="strictq")
        self.assertEqual(self.attempts, [1])          # no further rungs into a dead endpoint
        self.assertEqual(self.verified, [])           # nothing to judge
        self.assertEqual(out["needs_human"], {"reason": config.STRICT_STOP_REASON})
        self.assertFalse(out["verified"])
        self.assertTrue(out["result"].startswith("⛔"))
        self.assertIn("OTTO_LOCAL_FALLBACK=0", out["result"])
        self.assertIn("No Claude tokens were spent", out["result"])
        self.assertEqual(len(self.delivered), 1)
        self.assertTrue(self.delivered[0][0].get("blocked"))   # Blocked column, not Done


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowClaudeAuthWallTests(unittest.IsolatedAsyncioTestCase):
    """The Claude auth wall on the TEMPORAL path. `OttoWorkflow._verify_ladder` is a deliberate
    third mirror of engine._ladder_core (workflow code is deterministic, so it can't merge in), so
    it needs its own proof: ONE attempt, no verify call, needs_human with config.AUTH_STOP_REASON,
    and a delivered body that names the remedy — not three harness deaths and a banner blaming a
    timeout or a dead worker. Engine-side twin: test_core.ClaudeAuthWallTests."""

    AUTH_ERR = "Failed to authenticate: OAuth session expired and could not be refreshed"

    def setUp(self):
        import activities
        self.activities = activities
        cap = registry.Capability("skill", "daily-summary", "posts a summary")
        cap.risk = "write"
        self._orig = {n: getattr(engine, n)
                      for n in ("run_attempt", "verify", "record_attempt", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self.verified = []
        engine.verify = lambda *a, **k: self.verified.append(1) or {"passed": True, "critique": None}
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. do it", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        self.attempts = []

        def fake_run_attempt(request, cap, *, attempt=1, wid=None, **kwargs):
            self.attempts.append(attempt)
            return {"workflow": wid or "wf-auth", "attempt": attempt, "cost": 0.0,
                    "result": self.AUTH_ERR, "session_id": None, "model": "claude-sonnet-5",
                    "backend": "claude", "is_error": True, "auth_stop": True,
                    "tokens": {"input": 0, "output": 0}}
        engine.run_attempt = fake_run_attempt

        self.delivered = []
        self._orig_deliver = delivery.deliver
        delivery.deliver = lambda reply_to, result, cap=None, run_id=None: (
            self.delivered.append((reply_to, result, cap)) or "ok")
        self._orig_notify = delivery.notify
        delivery.notify = lambda *a, **k: True
        self._orig_db = engine._DB
        engine._DB = os.path.join(tempfile.mkdtemp(prefix="otto-auth-"), "otto.db")

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        delivery.deliver = self._orig_deliver
        delivery.notify = self._orig_notify
        engine._DB = self._orig_db

    async def test_an_expired_login_is_terminal_on_the_first_attempt(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, finalize_terminal, notify_human,
                                record_attempt, record_skip, route_request, snapshot_settings,
                                run_capability, resolve_pr_target,
            check_grounding, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="authq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding,
                                verify_capability, record_attempt, record_skip, deliver_result,
                                finalize_terminal, notify_human],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "post the daily summary", "unattended": True,
                         "auto_approve": True,
                         "cap": {"name": "daily-summary", "kind": "skill", "risk": "write"},
                         "reply_to": {"kind": "github_issue", "repo": "o/r", "issue": 7}},
                        id="auth-" + uuid.uuid4().hex[:8], task_queue="authq")
        self.assertEqual(self.attempts, [1])       # not 1 + max_harness_retries against a dead login
        self.assertEqual(self.verified, [])        # nothing ran, so nothing to judge
        self.assertEqual(out["needs_human"], {"reason": config.AUTH_STOP_REASON})
        self.assertFalse(out["verified"])
        # The delivered body must name the culprit AND the remedy — the whole reason this is not
        # filed as harness_exhausted, whose banner blames a timeout or a crashed worker.
        self.assertIn("could not authenticate", out["result"])
        self.assertIn("claude /login", out["result"])
        self.assertIn(self.AUTH_ERR, out["result"])          # the CLI's own words survive
        self.assertNotIn("worker crash", out["result"])
        self.assertEqual(len(self.delivered), 1)
        self.assertTrue(self.delivered[0][0].get("blocked"))  # Blocked column, not Done
        terminal = [e for e in engine.iter_audit_entries() if e.get("outcome") == "needs_human"]
        self.assertEqual([e.get("reason") for e in terminal], [config.AUTH_STOP_REASON])


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class WorkflowBudgetTests(unittest.IsolatedAsyncioTestCase):
    """Per-run cost budget, workflow-level: a HARD token ceiling stops the run BEFORE launching
    the next attempt and routes it to needs_human (reason budget_exceeded), taking precedence over
    verify_exhausted. config.budget_exceeded is unit-tested in test_core; this drives the accumulator
    (_account) + the stop through OttoWorkflow.run itself."""

    def setUp(self):
        import activities
        import config
        self.activities, self.config = activities, config
        cap = registry.Capability("skill", "spendy", "does expensive work")
        cap.risk = "write"
        self._orig = {n: getattr(engine, n)
                      for n in ("run_attempt", "verify", "record_attempt", "plan_preview", "critique_plan")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        # Verify always FAILs so the ladder would keep going if the budget didn't stop it.
        engine.verify = lambda req, c, result, project=None, local=False, unattended=False, **k: {
            "passed": False, "critique": "not good enough"}
        engine.record_attempt = lambda *a, **k: None
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {"plan": "1. spend tokens", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        self.attempts = []

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             extra_tools=None, mcp_config_path=None, resume_session=None,
                             wid=None, cwd=None, recall=False, project=None, **kwargs):
            self.attempts.append(attempt)
            # Each attempt blows well past the hard ceiling set below.
            return {"workflow": wid or "wf-bd", "result": "partial work", "cost": 0.0,
                    "session_id": "s", "model": "m", "attempt": attempt,
                    "tokens": {"input": 10, "output": 1000}}
        engine.run_attempt = fake_run_attempt

        self.delivered = []
        self._orig_deliver = delivery.deliver
        delivery.deliver = lambda reply_to, result, cap=None, run_id=None: (
            self.delivered.append((reply_to, result, cap)) or "ok")

        self._orig_notify = delivery.notify
        delivery.notify = lambda *a, **k: True
        # The needs-human finalizer writes a REAL terminal row (engine.record_terminal) — without
        # this redirect these three classes stamp phantom needs-human runs onto the developer's
        # live data/otto.db every time the suite runs.
        self._orig_db = engine._DB
        engine._DB = os.path.join(tempfile.mkdtemp(prefix="otto-nh-"), "otto.db")
        self._orig_hard = config.BUDGET_HARD_TOKENS
        config.BUDGET_HARD_TOKENS = 100          # attempt 1 (1000 output tokens) blows past it

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        delivery.deliver = self._orig_deliver
        delivery.notify = self._orig_notify
        engine._DB = self._orig_db
        self.config.BUDGET_HARD_TOKENS = self._orig_hard

    async def test_hard_budget_stops_before_next_attempt_and_needs_human(self):
        import uuid
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, finalize_terminal, notify_human,
                                record_attempt, record_skip, route_request, snapshot_settings, run_capability, resolve_pr_target,
            check_grounding,
                                verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="bdq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, clarify_request, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip, deliver_result, finalize_terminal,
                                notify_human],
                    activity_executor=ex,
                ):
                    out = await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "do the expensive thing", "unattended": True,
                         "auto_approve": True,
                         "cap": {"name": "spendy", "kind": "skill", "risk": "write"}},
                        id="bd-" + uuid.uuid4().hex[:8], task_queue="bdq")
        # Hard-stop never fires on attempt 1 (spend starts at 0); attempt 1 runs, then the ceiling
        # trips BEFORE attempt 2 launches — so exactly one attempt ran even though verify FAILed.
        self.assertEqual(self.attempts, [1])
        self.assertEqual(out["needs_human"], {"reason": "budget_exceeded"})
        self.assertFalse(out["verified"])
        self.assertIn("budget", out["result"])                # budget_exceeded banner
        self.assertTrue(out["result"].startswith("⚠️"))


@unittest.skipUnless(_HAS_TEMPORAL, "activities.py imports temporalio at module level")
class BoardPollPickupTests(unittest.TestCase):
    """The board-queue INGRESS orchestration (poll_board activity), with the gh/Temporal seams
    (board.*) mocked: each Ready issue is shaped into params, its pinned cap re-resolved from the
    TRUSTED registry (risk never taken from a label), reply_to enriched with the ids delivery
    needs to move the card, then started under a deterministic `gh-issue-<n>` id and CLAIMED
    (moved out of Ready). A duplicate start (issue already run once ever) is skipped and NOT
    re-claimed. board.issue_to_request itself is unit-tested in test_core — this asserts the
    wiring around it (pickup, claim, idempotency, reply_to enrichment)."""

    def setUp(self):
        import activities
        import board
        self.activities, self.board = activities, board
        # A pinned cap whose real (trusted) risk is write — a label must never be able to soften it.
        cap = registry.Capability("skill", "renew-cert", "renews a certificate")
        cap.risk = "write"
        self._orig_caps = activities._caps
        activities._caps = [cap]

        self._cfg = {"enabled": True,
                     "columns": {"ready": "Ready", "active": "In Progress",
                                 "review": "Review", "done": "Done", "blocked": "Blocked"}}
        self._meta = {"project_id": "PVT", "status_field_id": "FLD",
                      "options": {"In Progress": "opt-ip", "Review": "opt-rv",
                                  "Done": "opt-dn", "Blocked": "opt-bl"}}
        self._orig = {n: getattr(board, n) for n in
                      ("load", "enabled", "project_meta", "list_ready", "issue_to_request",
                       "start_run", "set_status")}
        board.load = lambda: self._cfg
        board.enabled = lambda cfg=None: True
        board.project_meta = lambda cfg: self._meta
        board.list_ready = lambda cfg: [
            {"number": 41, "item_id": "IT41", "repo": "o/r", "title": "renew the cert"},
            {"number": 42, "item_id": "IT42", "repo": "o/r", "title": "already running"}]

        def fake_issue_to_request(issue, cfg):
            # `cap` is a plain NAME here (the label->cap mapping); poll_board resolves it against
            # the trusted registry, so a label can never assert a cap's risk.
            return {"request": f"handle #{issue['number']}", "unattended": True,
                    "approval": "auto", "cap": "renew-cert",
                    "reply_to": {"kind": "github_issue", "repo": issue["repo"],
                                 "number": issue["number"], "item_id": issue["item_id"]}}
        board.issue_to_request = fake_issue_to_request

        self.started, self.claims = [], []
        # #41 starts fresh (True); #42 is a duplicate id already run once ever (False).
        board.start_run = lambda wid, params: (self.started.append((wid, params))
                                               or wid.endswith("-41"))
        board.set_status = lambda cfg, meta, item_id, column: self.claims.append((item_id, column))

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(self.board, n, fn)
        self.activities._caps = self._orig_caps

    def test_ready_issues_are_picked_claimed_and_deduped(self):
        out = self.activities.poll_board({})
        self.assertEqual(out["picked"], [41])          # started fresh
        self.assertEqual(out["skipped"], [42])         # duplicate id -> not re-run
        # Deterministic ids for idempotency; one OttoWorkflow start per issue.
        self.assertEqual([wid for wid, _ in self.started], ["gh-issue-41", "gh-issue-42"])
        # Only the freshly-started card is CLAIMED (moved to In Progress); the duplicate is left.
        self.assertEqual(self.claims, [("IT41", "In Progress")])

    def test_pinned_cap_risk_comes_from_the_trusted_registry_not_the_label(self):
        self.activities.poll_board({})
        _, params41 = self.started[0]
        self.assertEqual(params41["cap"]["risk"], "write")   # registry wins over the label's "read"
        # reply_to is enriched with everything delivery needs to move the card on completion.
        rt = params41["reply_to"]
        self.assertEqual(rt["project_id"], "PVT")
        self.assertEqual(rt["status_field_id"], "FLD")
        self.assertEqual(rt["review_col"], "Review")
        self.assertEqual(rt["done_col"], "Done")
        self.assertEqual(rt["blocked_col"], "Blocked")

    def test_disabled_board_does_nothing(self):
        self.board.enabled = lambda cfg=None: False
        out = self.activities.poll_board({})
        self.assertEqual(out, {"picked": [], "disabled": True})
        self.assertEqual(self.started, [])


@unittest.skipUnless(_HAS_TEMPORAL, "activities.py imports temporalio at module level")
class ReaperSweepTests(unittest.TestCase):
    """The reaper's SWEEP orchestration (reap_stuck activity), with board/Temporal seams mocked:
    an In-Progress card whose workflow DIED or ran past the stuck-TTL is moved to Blocked, stamped
    `needs-human`, and given a terminal audit row; a still-alive card is left untouched. The
    per-workflow state classifier (_reap_state) and the column parsing are covered elsewhere —
    this asserts the sweep ties them together correctly and is conservative about live runs."""

    def setUp(self):
        import activities
        import board
        import delivery
        self.activities, self.board, self.delivery = activities, board, delivery
        self._cfg = {"enabled": True, "columns": {"active": "In Progress", "blocked": "Blocked"}}
        self._meta = {"project_id": "PVT", "status_field_id": "FLD",
                      "options": {"Blocked": "opt-bl"}}
        self._orig_board = {n: getattr(board, n) for n in
                            ("load", "enabled", "project_meta", "list_in_column",
                             "set_status_raw", "add_label")}
        board.load = lambda: self._cfg
        board.enabled = lambda cfg=None: True
        board.project_meta = lambda cfg: self._meta
        board.list_in_column = lambda cfg, key: [
            {"number": 10, "item_id": "IT10", "repo": "o/r"},   # dead
            {"number": 11, "item_id": "IT11", "repo": "o/r"},   # alive -> left
            {"number": 12, "item_id": "IT12", "repo": "o/r"}]   # stale
        self.moves, self.labels = [], []
        board.set_status_raw = lambda pid, fid, item, opt: self.moves.append((item, opt))
        board.add_label = lambda repo, n, label: self.labels.append((n, label))

        # Classify each stuck card's workflow deterministically (real _reap_state hits Temporal).
        self._orig_state = activities._reap_state
        states = {"gh-issue-10": "dead", "gh-issue-11": "alive", "gh-issue-12": "stale"}
        activities._reap_state = lambda wid: states[wid]

        # The general sweep's Temporal listing + audit-trail seams (real ones hit the live
        # Temporal server and the audit DB). Tests set self.wfs / self.audited per case.
        self.wfs, self.audited = [], set()
        self._orig_list = activities._list_otto_workflows
        activities._list_otto_workflows = lambda window_h, limit=500: self.wfs
        self._orig_iter = engine.iter_audit_entries
        engine.iter_audit_entries = lambda: [{"workflow": w, "needs_human": True}
                                             for w in self.audited]
        self._orig_origin = engine.run_origin
        engine.run_origin = lambda wid: (f"req for {wid}", "agent:x", None, True)

        self.terminals = []
        self._orig_terminal = engine.record_terminal
        engine.record_terminal = lambda wid, req, cap, reason=None, detail=None, repo=None: (
            self.terminals.append({"wid": wid, "req": req, "reason": reason, "repo": repo}))
        self.notes = []
        self._orig_notify = delivery.notify
        delivery.notify = lambda title, lines=None, **k: self.notes.append(
            {"title": title, "lines": [*(lines or [])]}) or True

    def tearDown(self):
        for n, fn in self._orig_board.items():
            setattr(self.board, n, fn)
        self.activities._reap_state = self._orig_state
        self.activities._list_otto_workflows = self._orig_list
        engine.iter_audit_entries = self._orig_iter
        engine.run_origin = self._orig_origin
        engine.record_terminal = self._orig_terminal
        self.delivery.notify = self._orig_notify

    def test_dead_and_stale_cards_are_blocked_labelled_and_audited(self):
        out = self.activities.reap_stuck({})
        self.assertEqual(out["reaped"], [10, 12])          # #11 (alive) left alone
        # Both reaped cards moved to the Blocked option; #11 never touched.
        self.assertEqual(self.moves, [("IT10", "opt-bl"), ("IT12", "opt-bl")])
        self.assertEqual(self.labels, [(10, "needs-human"), (12, "needs-human")])
        # Terminal rows carry the RIGHT reason: a crashed workflow vs a TTL timeout.
        by_wid = {t["wid"]: t for t in self.terminals}
        self.assertEqual(by_wid["gh-issue-10"]["reason"], "workflow_dead")
        self.assertEqual(by_wid["gh-issue-12"]["reason"], "stuck_timeout")
        self.assertNotIn("gh-issue-11", by_wid)

    def test_all_alive_reaps_nothing(self):
        self.activities._reap_state = lambda wid: "alive"
        out = self.activities.reap_stuck({})
        self.assertEqual(out["reaped"], [])
        self.assertEqual(self.moves, [])
        self.assertEqual(self.terminals, [])

    def test_general_sweep_covers_non_board_runs(self):
        """The gap the board sweep can't see (issue #275): a web-*/sched-*/slack-* run whose
        workflow died or hung gets a terminal audit row recovering its origin — while a live one,
        an already-audited one, a swarm child, and a board wid are all left alone."""
        self.activities._reap_state = lambda wid: "alive"       # board pass: nothing to reap
        self.wfs = [
            {"wid": "web-abc", "status": "TERMINATED", "age_h": 1},       # swept: killed, no row
            {"wid": "sched-x", "status": "RUNNING", "age_h": 99},         # swept: stale
            {"wid": "slack-C1-1", "status": "TIMED_OUT", "age_h": 2},     # swept: timed out
            {"wid": "web-live", "status": "RUNNING", "age_h": 1},         # alive -> untouched
            {"wid": "web-done", "status": "COMPLETED", "age_h": 1},       # completed -> untouched
            {"wid": "web-old", "status": "FAILED", "age_h": 3},           # audited -> skipped
            {"wid": "web-abc-s2", "status": "TERMINATED", "age_h": 1},    # swarm child -> skipped
            {"wid": "gh-issue-10", "status": "FAILED", "age_h": 1},       # board's, not ours
        ]
        self.audited = {"web-old"}
        out = self.activities.reap_stuck({})
        self.assertEqual(out["swept"], ["web-abc", "sched-x", "slack-C1-1"])
        by_wid = {t["wid"]: t for t in self.terminals}
        self.assertEqual(by_wid["web-abc"]["reason"], "workflow_dead")
        self.assertEqual(by_wid["sched-x"]["reason"], "stuck_timeout")
        self.assertEqual(by_wid["web-abc"]["req"], "req for web-abc")     # origin recovered
        self.assertEqual(set(by_wid), {"web-abc", "sched-x", "slack-C1-1"})

    def test_general_sweep_runs_even_with_the_board_disabled(self):
        self.board.enabled = lambda cfg=None: False
        self.wfs = [{"wid": "web-abc", "status": "TERMINATED", "age_h": 1}]
        out = self.activities.reap_stuck({})
        self.assertEqual(out, {"reaped": [], "swept": ["web-abc"], "disabled": True})

    def test_notification_never_carries_a_raw_slack_wid(self):
        """A slack-* wid embeds the channel id; the ntfy broker must see counts only (same rule
        as privacy.source_line)."""
        self.activities._reap_state = lambda wid: "alive"
        self.wfs = [{"wid": "slack-C0SECRET-17", "status": "TERMINATED", "age_h": 1}]
        self.activities.reap_stuck({})
        self.assertEqual(len(self.notes), 1)
        joined = " ".join(self.notes[0]["lines"])
        self.assertNotIn("C0SECRET", joined)
        self.assertIn("1 slack", joined)

    def test_sweep_is_idempotent_across_passes(self):
        self.activities._reap_state = lambda wid: "alive"
        self.wfs = [{"wid": "web-abc", "status": "TERMINATED", "age_h": 1}]
        self.assertEqual(self.activities.reap_stuck({})["swept"], ["web-abc"])
        self.audited = {"web-abc"}          # the row the first pass just wrote
        self.assertEqual(self.activities.reap_stuck({})["swept"], [])
        self.assertEqual(len(self.terminals), 1)


class SupervisorEnforceKillTests(unittest.TestCase):
    """Supervisor ENFORCE mode (issue #143) through engine.execute: on a fresh run an Abort switch
    is armed and handed to the backend; when the supervisor fires (the attempt comes back
    `(aborted by supervisor: …)` as a FAILED attempt), it audits as supervisor_kill and — the
    load-bearing part — the ladder folds the supervisor's course-correction into the NEXT attempt
    (engine.error_verdict), which then passes. The Abort switch itself and the verdict parser are
    unit-tested in test_core; this asserts the kill→steer WIRING end-to-end. No background threads:
    supervisor.start is faked and _claude simulates the post-kill result the real backend returns."""

    def setUp(self):
        self._noop = lambda *a, **k: None
        self._traces = [(engine, "trace"), (engine, "say"), (gateway, "trace")]
        self._orig_traces = [(m, n, getattr(m, n)) for m, n in self._traces]
        for m, n in self._traces:
            setattr(m, n, self._noop)
        self._tmp = tempfile.mkdtemp(prefix="otto-supk-")
        self._paths = engine._DB
        engine._DB = os.path.join(self._tmp, "otto.db")

        import supervisor
        self.supervisor = supervisor
        # Fake the live watcher: no thread, no gateway call. finish() reports a kill on attempt 1
        # only, so exactly one attempt is judged a supervisor kill.
        self.finishes = []
        self._orig_start = supervisor.start

        class _FakeSup:
            def __init__(self, attempt):
                self.attempt = attempt

            def note(self, event):
                pass

            def finish(_self):
                if _self.attempt == 1:
                    self.finishes.append(1)
                    return {"killed": True, "would_retry": True,
                            "verdicts": [{"at_s": 3, "verdict": "retry",
                                          "critique": "querying the US account; the alert is EU"}]}
                return None
        supervisor.start = (lambda wid, attempt, request, cap, transcript=None, abort=None,
                                   cwd=None, critique=None, **kw: _FakeSup(attempt))

        # _claude: attempt 1 comes back as the post-kill result the real backend returns when the
        # Abort fired mid-stream; attempt 2 runs clean. Capture the prompt + abort per call.
        self._orig_claude = engine._claude
        self.claude_calls = []

        def fake_claude(prompt, allowed_tools=None, model=None, mcp_config_path=None,
                        resume_session=None, system_context=None, timeout=900, cwd=None,
                        transcript=None, on_event=None, abort=None, meta=None,
                        permission_mode=None, **kwargs):
            self.claude_calls.append({"prompt": prompt, "abort": abort})
            if len(self.claude_calls) == 1:
                return {"result": "(aborted by supervisor: querying the US account; the alert is EU)",
                        "is_error": True, "total_cost_usd": 0.0, "session_id": "s"}
            return {"result": "queried the EU account, all clear", "total_cost_usd": 0.0,
                    "session_id": "s"}
        engine._claude = fake_claude

        self._orig_complete = gateway.complete
        gateway.complete = lambda task, prompt: ("PASS" if task == "verify" else "NONE")

        self.cap = registry.Capability("agent", "sre-incident-inspector", "investigates incidents")
        self.cap.risk = "read"

    def tearDown(self):
        self.supervisor.start = self._orig_start
        engine._claude = self._orig_claude
        gateway.complete = self._orig_complete
        engine._DB = self._paths
        shutil.rmtree(self._tmp, ignore_errors=True)
        for m, n, fn in self._orig_traces:
            setattr(m, n, fn)

    def test_kill_becomes_failed_attempt_and_steers_the_next_rung(self):
        import config
        # Guard the test's premise: enforce mode must be the effective config, else no Abort is
        # armed and this asserts nothing. (Default is enforce; be explicit + restore.)
        orig_mode, orig_on = config.SUPERVISE_MODE, config.SUPERVISE
        config.SUPERVISE_MODE, config.SUPERVISE = "enforce", True
        try:
            out = engine.execute("investigate the prod alert", self.cap)
        finally:
            config.SUPERVISE_MODE, config.SUPERVISE = orig_mode, orig_on
        # Two attempts: the killed one, then the steered-and-clean one.
        self.assertEqual(len(self.claude_calls), 2)
        self.assertEqual(self.finishes, [1])
        # Enforce mode ARMED an abort switch on the (fresh) attempt.
        self.assertIsInstance(self.claude_calls[0]["abort"], self.supervisor.Abort)
        # The supervisor's course-correction was folded into attempt 2's prompt (the steer).
        self.assertIn("querying the US account; the alert is EU", self.claude_calls[1]["prompt"])
        self.assertIn("take a different approach", self.claude_calls[1]["prompt"])
        # Final result is the clean second attempt, and it verified.
        self.assertEqual(out["result"], "queried the EU account, all clear")
        self.assertTrue(out["verified"])
        # The kill is recorded as its OWN audit outcome (not needs-human — it feeds the ladder).
        rows = list(engine.iter_audit_entries())
        kills = [r for r in rows if r.get("outcome") == "supervisor_kill"]
        self.assertEqual(len(kills), 1)
        self.assertEqual(kills[0]["reason"], "supervisor_retry")


@unittest.skipUnless(_HAS_TEMPORAL, "needs temporalio")
class RealHeartbeatPlumbingTests(unittest.TestCase):
    """The unit tests for `activities._heartbeating` monkeypatch `activity.heartbeat`, which
    proves the threading and the loop but NOT that a beat reaches temporalio's real machinery.
    That distinction is the whole risk of this change: `heartbeat_timeout` is now declared on
    every long activity, so a beat that silently no-ops does not stall a run — it KILLS a
    healthy one three minutes in. This drives the real context through the real API."""

    def test_a_beat_from_the_helper_thread_reaches_the_real_activity_context(self):
        from temporalio import activity as tact
        from temporalio.testing import ActivityEnvironment
        beats = []
        env = ActivityEnvironment()
        env.on_heartbeat = lambda *d: beats.append(d)

        @tact.defn
        @activities._heartbeats("probe", every_s=0.02)
        def slow(payload: dict) -> str:
            # Blocks exactly the way a real `claude -p` turn does: no yields, no awaits, and
            # nothing inside it ever calls heartbeat itself.
            #
            # Blocks until the beats ARRIVE, not for a fixed window. The original slept 0.2s
            # and demanded 3 beats at a 20ms interval, which is an assertion about how promptly
            # the MACHINE schedules a starved helper thread — a loaded macOS runner delivered 2
            # and failed a working implementation. The property under test is that a beat from
            # this thread reaches the real Temporal API at all; the deadline only stops a truly
            # dead heartbeat from hanging the suite.
            self.assertTrue(tact.in_activity())
            deadline = time.monotonic() + 10
            while len(beats) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            return "done"

        self.assertEqual(env.run(slow, {}), "done")
        self.assertGreaterEqual(len(beats), 3,
                                f"the real API received {len(beats)} beats while a 20ms-interval "
                                "activity blocked for up to 10s — heartbeating is not actually "
                                "reaching Temporal")
        self.assertEqual(beats[0][0], "probe")            # labelled for the Temporal UI
        self.assertTrue(all(isinstance(b[1], int) for b in beats))   # elapsed seconds

    def test_a_naive_thread_would_not_have_worked(self):
        # Attribution, not decoration: proves the copy_context() in `_heartbeating` is the load-
        # bearing part, by showing the obvious implementation raises inside the real context.
        from temporalio import activity as tact
        from temporalio.testing import ActivityEnvironment
        err = []

        @tact.defn
        def naive(payload: dict) -> str:
            def beat():
                try:
                    tact.heartbeat("x")
                except Exception as e:  # noqa: BLE001
                    err.append(type(e).__name__)
            t = threading.Thread(target=beat)
            t.start()
            t.join()
            return "done"

        ActivityEnvironment().run(naive, {})
        self.assertEqual(err, ["RuntimeError"],
                         "a bare thread CAN heartbeat — then copy_context() is dead weight")


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class GateDeadlineAndDenialIdentityTests(unittest.IsolatedAsyncioTestCase):
    """The approval gate's two silent-failure modes, both measured off the live audit trail.

    1. A DENIAL was audited under a freshly minted `wf-<hex>-NNNN` instead of the run's own
       workflow id, so the row correlated with nothing — not the plan preview the human read
       before declining, not the chat, not the board card. Seven real declines were filed that
       way and the runs they belonged to read as abandoned-at-the-gate.
    2. The gate waited on an UNBOUNDED `wait_condition`. That is only safe when the asker can
       see the approval card, and Slack's asker cannot: 7 of 52 Slack runs parked forever,
       answering neither yes nor no. Timing out must DECLINE (never approve — a timeout that
       grants a write is privilege escalation by patience) and must surface.
    """

    def setUp(self):
        import activities
        cap = registry.Capability("skill", "deployer", "deploys things")
        cap.risk = "write"
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self.activities = activities
        self._orig = {n: getattr(engine, n) for n in
                      ("plan_preview", "critique_plan", "run_attempt", "verify", "record_attempt")}
        engine.plan_preview = lambda request, c, cwd=None, resume_session=None, wid=None, **kw: {
            "plan": "1. push the button", "cost": 0, "tokens": None}
        engine.critique_plan = lambda *a, **k: {"concerns": []}
        engine.record_attempt = lambda *a, **k: None
        self.ran = []
        engine.run_attempt = lambda request, cap, **k: (
            self.ran.append(request) or {"workflow": k.get("wid"), "result": "did it", "cost": 0.0,
                                         "session_id": "s", "model": "m", "attempt": 1,
                                         "tokens": {"input": 1, "output": 1}})
        engine.verify = lambda *a, **k: {"passed": True, "critique": ""}
        self.delivered = []
        self._orig_deliver, self._orig_notify = delivery.deliver, delivery.notify
        delivery.deliver = lambda reply_to, result, cap=None, run_id=None: (
            self.delivered.append((reply_to, result)) or "ok")
        delivery.notify = lambda *a, **k: True
        # Never stamp rows on the developer's live otto.db (LiveStoreIsolationTests' rule).
        self._orig_db = engine._DB
        engine._DB = os.path.join(tempfile.mkdtemp(prefix="otto-gate-"), "otto.db")

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        self.activities._caps = self._orig_caps
        delivery.deliver, delivery.notify = self._orig_deliver, self._orig_notify
        engine._DB = self._orig_db

    async def _until(self, handle, pred, tries=200):
        import asyncio
        from workflows import OttoWorkflow
        for _ in range(tries):
            if pred(await handle.query(OttoWorkflow.status)):
                return
            await asyncio.sleep(0.05)
        raise AssertionError("workflow never reached the expected state")

    async def _run(self, *, decide, wid, params=None):
        """Start a gated run; `decide` is None to leave the gate unanswered (the timeout case)."""
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, detect_repo_changes,
                                estop_check, finalize_terminal, notify_human, open_chat,
                                plan_capability, plan_swarm, record_attempt, record_chat,
                                record_skip, route_request, run_capability, resolve_pr_target,
            check_grounding, snapshot_repos,
                                snapshot_settings, suggest_repo, verify_capability)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="gtq", workflows=[OttoWorkflow],
                    activities=[route_request, snapshot_settings, plan_swarm, clarify_request,
                                plan_capability, suggest_repo, run_capability, resolve_pr_target, check_grounding, verify_capability,
                                record_attempt, record_skip, snapshot_repos, detect_repo_changes,
                                finalize_terminal, deliver_result, notify_human, estop_check,
                                open_chat, record_chat],
                    activity_executor=ex,
                ):
                    handle = await env.client.start_workflow(
                        OttoWorkflow.run,
                        {"request": "deploy the service", "unattended": True,
                         # "ask": the unattended-but-gated mode — the Board's Waiting-on-you
                         # state, and the one Slack's write-intent path lands in.
                         "approval": "ask",
                         "cap": {"name": "deployer", "kind": "skill", "risk": "write"},
                         **(params or {})},
                        id=wid, task_queue="gtq")
                    if decide is None:
                        # Leave the gate unanswered and let the env fast-forward past the
                        # deadline. Without one this await never returns — the bug itself.
                        return await handle.result()
                    # Hold the clock still, or time-skipping blows through gate_timeout_h while
                    # this poll is sleeping and the run expires before it can be answered.
                    with env.auto_time_skipping_disabled():
                        await self._until(handle, lambda s: s["awaiting_approval"])
                        await handle.signal(OttoWorkflow.approve, decide)
                        return await handle.result()

    async def test_a_decline_is_audited_under_the_runs_own_workflow_id(self):
        wid = "web-gatedeny01"
        out = await self._run(decide=False, wid=wid)
        self.assertTrue(out["result"].startswith("Declined"))
        self.assertEqual(self.ran, [])
        denied = [e for e in engine.iter_audit_entries() if e.get("outcome") == "denied"]
        self.assertEqual(len(denied), 1)
        # The whole point: a minted wf-<hex>-NNNN here is invisible to every consumer that
        # correlates on the run id (chat, board card, /api/run/detail, the gate's own preview).
        self.assertEqual(denied[0]["workflow"], wid)

    async def test_an_unanswered_gate_expires_declined_and_surfaces(self):
        # Nobody signals. Time-skipping fast-forwards past gate_timeout_h (24h) — without the
        # deadline this call never returns, which is precisely the production symptom.
        wid = "slack-C123-1785395780-677549"
        out = await self._run(
            decide=None, wid=wid,
            params={"reply_to": {"kind": "slack_thread", "channel": "C123", "ts": "1.1"}})
        self.assertEqual(self.ran, [], "an expired gate must never run the write")
        self.assertEqual(out["needs_human"], {"reason": "gate_timeout"})
        self.assertIn("Nobody approved this in time", out["result"])
        # Durable row, so it reaches /api/needs-you and outlives Temporal history.
        terminal = [e for e in engine.iter_audit_entries() if e.get("outcome") == "needs_human"]
        self.assertEqual([e.get("reason") for e in terminal], ["gate_timeout"])
        self.assertEqual(terminal[0]["workflow"], wid)
        # An expired gate is NOT a human decision, so it must not masquerade as one.
        self.assertEqual([e for e in engine.iter_audit_entries()
                          if e.get("outcome") == "denied"], [])
        # ...and the Slack thread that asked is TOLD, instead of getting silence.
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.delivered[0][0]["kind"], "slack_thread")
        self.assertIn("Nobody approved this in time", self.delivered[0][1])

    async def test_zero_restores_the_unbounded_wait(self):
        # The escape hatch must actually disable the deadline rather than shorten it to nothing.
        # Measured, not asserted from the source: with the knob at 0 the run is still parked at
        # the gate after the env has skipped twice the default window.
        import uuid
        from datetime import timedelta as _td
        from workflows import OttoWorkflow
        from activities import (clarify_request, deliver_result, detect_repo_changes,
                                estop_check, finalize_terminal, notify_human, open_chat,
                                plan_capability, plan_swarm, record_attempt, record_chat,
                                record_skip, route_request, run_capability, resolve_pr_target,
            check_grounding, snapshot_repos,
                                snapshot_settings, suggest_repo, verify_capability)
        os.environ["OTTO_GATE_TIMEOUT_H"] = "0"
        try:
            self.assertEqual(config.setting("gate_timeout_h"), 0.0)
            async with await _time_skipping_env() as env:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    async with Worker(
                        env.client, task_queue="gtq0", workflows=[OttoWorkflow],
                        activities=[route_request, snapshot_settings, plan_swarm, clarify_request,
                                    plan_capability, suggest_repo, run_capability, resolve_pr_target, check_grounding,
                                    verify_capability, record_attempt, record_skip, snapshot_repos,
                                    detect_repo_changes, finalize_terminal, deliver_result,
                                    notify_human, estop_check, open_chat, record_chat],
                        activity_executor=ex,
                    ):
                        handle = await env.client.start_workflow(
                            OttoWorkflow.run,
                            {"request": "deploy the service", "unattended": True,
                             "approval": "ask",
                             "cap": {"name": "deployer", "kind": "skill", "risk": "write"}},
                            id="web-gateoff-" + uuid.uuid4().hex[:6], task_queue="gtq0")
                        with env.auto_time_skipping_disabled():
                            await self._until(handle, lambda s: s["awaiting_approval"])
                        await env.sleep(_td(hours=48))
                        st = await handle.query(OttoWorkflow.status)
                        self.assertTrue(st["awaiting_approval"],
                                        "gate_timeout_h=0 still expired the gate")
                        self.assertIsNone(st["terminal"])
                        # Leave nothing running behind the test.
                        await handle.signal(OttoWorkflow.approve, True)
                        out = await handle.result()
            self.assertEqual(out["result"], "did it")
        finally:
            os.environ.pop("OTTO_GATE_TIMEOUT_H", None)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_HAS_TEMPORAL, "temporalio not installed")
class BrainstormRunTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a pinned brainstorm run through the real `OttoWorkflow`.

    The shape guards in `test_pipeline.BrainstormModeTests` assert the code is written the way
    the mode needs; this asserts the run that comes out the other end. Three of the four ways
    this mode dies are only visible here — a judge that still runs, a clarify that still blocks
    the first turn, and an unjudged run that lands in needs-human and renders the chat Blocked.

    `verify_capability` and `clarify_request` are DELIBERATELY not registered on the worker: a
    call to either raises Temporal's non-retryable NotFoundError and fails the workflow, which is
    a far louder assertion than counting calls on a stub that would happily answer them.
    """

    def setUp(self):
        cap = registry.Capability("custom", config.BRAINSTORM_CAP, "thinks an idea through")
        cap.risk = "read"
        cap.route_hidden = True
        self._orig = {n: getattr(engine, n) for n in ("run_attempt", "verify", "record_attempt")}
        self._orig_caps = activities._caps
        activities._caps = [cap]
        self.calls = []

        def fake_run_attempt(request, cap, *, attempt=1, critique=None, escalate=False,
                             wid=None, cwd=None, recall=False, audience=None, **kwargs):
            self.calls.append({"attempt": attempt, "audience": audience, "critique": critique,
                               "escalate": escalate, "risk": cap.risk,
                               "supervise_enforce": kwargs.get("supervise_enforce")})
            return {"workflow": wid or "wf-bs", "result": "Two options, and I'd take the second.",
                    "cost": 0.0, "session_id": "s1", "model": "m", "attempt": attempt,
                    "tokens": {"input": 1, "output": 1}}
        engine.run_attempt = fake_run_attempt
        engine.verify = lambda *a, **k: self.fail("a judge ran on a brainstorm turn")
        engine.record_attempt = lambda *a, **k: None
        self._orig_db = engine._DB
        self._tmp = tempfile.mkdtemp(prefix="otto-bs-")
        engine._DB = os.path.join(self._tmp, "otto.db")

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        activities._caps = self._orig_caps
        engine._DB = self._orig_db
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _run(self, **params):
        import uuid
        from workflows import OttoWorkflow
        from activities import (check_grounding, deliver_result, detect_repo_changes, estop_check,
                                notify_human, record_attempt, record_chat, resolve_pr_target,
                                run_capability, snapshot_repos, snapshot_settings)
        async with await _time_skipping_env() as env:
            with ThreadPoolExecutor(max_workers=4) as ex:
                async with Worker(
                    env.client, task_queue="bsq", workflows=[OttoWorkflow],
                    activities=[snapshot_settings, estop_check, run_capability, record_attempt,
                                resolve_pr_target, check_grounding, snapshot_repos,
                                detect_repo_changes, record_chat, deliver_result, notify_human],
                    activity_executor=ex,
                ):
                    return await env.client.execute_workflow(
                        OttoWorkflow.run,
                        {"request": "should we split the approval gate out of workflows.py?",
                         "cap": {"name": config.BRAINSTORM_CAP, "kind": "custom", "risk": "read"},
                         **params},
                        id="bs-" + uuid.uuid4().hex[:8], task_queue="bsq")

    async def test_one_unjudged_attempt_that_never_blocks(self):
        out = await self._run()
        # Exactly one attempt: no judge, so no critique to retry with and no rung to escalate to.
        self.assertEqual([c["attempt"] for c in self.calls], [1])
        self.assertIsNone(self.calls[0]["critique"])
        self.assertFalse(self.calls[0]["escalate"])
        # The executor was told who is reading, and it is not the operator's report.
        self.assertEqual(self.calls[0]["audience"], contracts.BRAINSTORM_AUDIENCE)
        # The supervisor's kill switch is disarmed — there is no rung for its critique to steer.
        self.assertIs(self.calls[0]["supervise_enforce"], False)
        # Unjudged is NOT unverified: `verified` must be None, or the UI badges a normal
        # conversation as having failed something.
        self.assertIsNone(out["verified"])
        # ...and above all it must not be held. `passed` is False here (there is no verdict), so
        # an unguarded verify_exhausted branch parks the very first reply of every brainstorm on
        # the Needs-you board and renders the chat Blocked.
        self.assertIsNone(out["needs_human"])
        self.assertEqual(out["result"], "Two options, and I'd take the second.")
        self.assertNotIn("⚠", out["result"])
        self.assertEqual(out["cap"]["name"], config.BRAINSTORM_CAP)

    async def test_the_mode_outranks_a_conversational_delivery_target(self):
        """A pinned /brainstorm from Slack is still a brainstorm: the capability decides the
        contract, not the delivery target that would otherwise pick _DIRECT_REPLY_FORMAT."""
        self._orig_deliver = delivery.deliver
        delivery.deliver = lambda *a, **k: "ok"
        try:
            await self._run(unattended=True, approval="auto",
                            reply_to={"kind": "slack_thread", "channel": "C1",
                                      "thread_ts": "1.1", "user": "U1"})
        finally:
            delivery.deliver = self._orig_deliver
        self.assertEqual(self.calls[0]["audience"], contracts.BRAINSTORM_AUDIENCE)

    async def test_the_turn_never_gets_write_tools(self):
        """The gate is skipped, so the TOOLSET is the only guard left — cap.risk must stay read
        all the way into the executor (activities.run_capability re-resolves by name and the
        payload risk only ever narrows)."""
        await self._run()
        self.assertEqual(self.calls[0]["risk"], "read")


class McpNoteEndpointTests(unittest.TestCase):
    """POST /api/mcp/note over real HTTP: the route exists, resolves the server name against
    the trusted all_mcps() set, and persists to policy.json without disturbing the rest."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.httpd = ThreadingTCPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="otto-mcpnote-api-")
        self._path, self._disc = policy._PATH, policy.discover_mcps
        policy._PATH = os.path.join(self._tmp, "policy.json")
        policy.discover_mcps = lambda: ["grafana"]      # never read the developer's ~/.claude.json
        self._pol = dict(self.server.POLICY)
        self.server.POLICY.clear()
        self.server.POLICY.update({"capabilities": {}, "mcps": {"grafana": {"enabled": True}}})
        policy.save(self.server.POLICY)

    def tearDown(self):
        policy._PATH, policy.discover_mcps = self._path, self._disc
        self.server.POLICY.clear()
        self.server.POLICY.update(self._pol)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _post(self, path, body):
        req = urllib.request.Request(self.base + path, method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_note_is_saved_and_read_back_by_the_run_path(self):
        status, out = self._post("/api/mcp/note", {"name": "grafana", "notes": "read-only token"})
        self.assertEqual((status, out["notes"]), (200, "read-only token"))
        self.assertEqual(policy.mcp_notes(), {"grafana": "read-only token"})

    def test_an_unknown_server_is_refused(self):
        """`name` reaches a store key, so it resolves against the discovered set, never the
        client's string."""
        status, out = self._post("/api/mcp/note", {"name": "../../etc", "notes": "x"})
        self.assertEqual(status, 400)
        self.assertIn("unknown", out["error"])

    def test_a_later_policy_save_from_the_panel_keeps_the_note(self):
        """The live POLICY is refreshed by the note write, so the next whole-policy POST (the
        Admin panel's enable/disable) re-attaches it instead of shipping a stale map without."""
        self._post("/api/mcp/note", {"name": "grafana", "notes": "read-only token"})
        status, _ = self._post("/api/policy", {"capabilities": {},
                                               "mcps": {"grafana": {"enabled": False}}})
        self.assertEqual(status, 200)
        saved = policy.load()["mcps"]["grafana"]
        self.assertEqual(saved, {"enabled": False, "notes": "read-only token"})
