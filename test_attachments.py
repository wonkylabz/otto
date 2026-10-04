"""Chat attachments (#161): the store, the per-run read grant, what a run and its judge are told,
the HTTP routes, chat persistence and the composer wiring."""
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from socketserver import ThreadingTCPServer
from unittest import mock

import attachments
import chats
import config
import engine
import error_classifier
import file_safety
import gateway
import judging
import registry
import server
from test_support import setUpModule, ui_src, workflow_src  # noqa: F401 - unittest calls it per module

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class AttachmentStoreTests(unittest.TestCase):
    def test_a_stored_file_resolves_by_id_and_stays_in_its_own_directory(self):
        meta = attachments.store("../../etc/passwd", b"root:x")
        self.assertEqual(meta["name"], "passwd")
        self.assertEqual(os.path.dirname(os.path.dirname(meta["path"])), attachments.upload_dir())
        self.assertEqual(attachments.get(meta["id"])["path"], meta["path"])
        self.assertNotIn("path", attachments.public(meta))

    def test_resolve_reports_unknown_and_malformed_ids_instead_of_dropping_them(self):
        meta = attachments.store("a.txt", b"x")
        metas, missing = attachments.resolve([meta["id"], "att-0000000000000000", "../x"])
        self.assertEqual([m["id"] for m in metas], [meta["id"]])
        self.assertEqual(missing, ["att-0000000000000000", "../x"])

    def test_an_oversized_or_empty_file_is_refused(self):
        with mock.patch.dict(os.environ, {"OTTO_ATTACHMENT_MAX_MB": "1"}):
            with self.assertRaises(ValueError):
                attachments.store("big.bin", b"x" * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            attachments.store("empty.txt", b"")

    def test_sweep_drops_only_uploads_past_the_ttl(self):
        old, new = attachments.store("old.txt", b"o"), attachments.store("new.txt", b"n")
        past = time.time() - 3 * 3600
        os.utime(os.path.dirname(old["path"]), (past, past))
        attachments.sweep(ttl_h=1)
        self.assertIsNone(attachments.get(old["id"]))
        self.assertIsNotNone(attachments.get(new["id"]))


class UploadGrantTests(unittest.TestCase):
    """A run reads only the uploads it was handed — a Slack colleague's run must not reach the
    owner's screenshots. Fails closed: outside a grant, every upload is denied."""

    def test_no_grant_denies_every_upload_to_every_run_including_otto_cwd(self):
        a = attachments.store("a.png", PNG)
        otto_root = os.path.dirname(config.DATA_DIR.rstrip("/"))
        self.assertTrue(file_safety.is_read_denied(a["path"]))
        self.assertTrue(file_safety.is_read_denied(a["path"], allow_cwd=otto_root))

    def test_a_grant_opens_its_own_upload_and_no_other(self):
        a, b = attachments.store("a.png", PNG), attachments.store("b.png", PNG)
        with file_safety.upload_grant(attachments.granted_dirs([a])):
            self.assertFalse(file_safety.is_read_denied(a["path"]))
            self.assertTrue(file_safety.is_read_denied(b["path"]))
            rules = file_safety.settings_arg()
            self.assertIn(os.path.dirname(b["path"]).lstrip("/"), rules)
        self.assertTrue(file_safety.is_read_denied(a["path"]), "the grant outlived its run")

    def test_uploads_are_write_denied_even_to_the_run_that_owns_them(self):
        a = attachments.store("a.txt", b"x")
        with file_safety.upload_grant(attachments.granted_dirs([a])):
            self.assertTrue(file_safety.is_denied(a["path"]))


class RunContextTests(unittest.TestCase):
    def setUp(self):
        self._saved = (engine._claude, gateway.exec_model_entry, gateway.local_execute,
                       engine.trace, engine.say, config.SUPERVISE)
        config.SUPERVISE = False
        engine.trace = engine.say = lambda *a, **k: None
        gateway.exec_model_entry = lambda cap_name=None, cfg=None: {"provider": "claude",
                                                                    "name": "claude-x"}
        self.local_calls, self.claude_calls = [], []
        gateway.local_execute = lambda *a, **k: self.local_calls.append(a)
        engine._claude = lambda prompt, **kw: (self.claude_calls.append(kw) or
                                               {"result": "done", "total_cost_usd": 0,
                                                "session_id": "s", "usage": {}})
        self.cap = registry.Capability("custom", "briefing", "morning briefing")
        self.cap.risk, self.cap.tool_free = "read", True

    def tearDown(self):
        (engine._claude, gateway.exec_model_entry, gateway.local_execute,
         engine.trace, engine.say, config.SUPERVISE) = self._saved

    def test_the_run_is_told_the_path_and_skips_the_tool_free_rung(self):
        meta = attachments.store("shot.png", PNG)
        engine.run_attempt("what is this error?", self.cap, wid="w1", attachments=[meta])
        self.assertEqual(self.local_calls, [], "a tool-free completion cannot open the file")
        ctx = self.claude_calls[-1]["system_context"]
        self.assertIn(meta["path"], ctx)
        self.assertIn("never describe what an image you did not see", ctx)

    def test_without_attachments_nothing_changes(self):
        engine.run_attempt("brief me", self.cap, wid="w2")
        self.assertEqual(len(self.local_calls), 1)

    def test_a_resumed_turn_carries_its_own_attachments(self):
        meta = attachments.store("log.txt", b"boom")
        engine.run_attempt("and this one?", self.cap, wid="w3", resume_session="s",
                           attachments=[meta])
        self.assertIn(meta["path"], self.claude_calls[-1]["system_context"])

    def test_the_judge_is_told_it_cannot_open_the_files(self):
        meta = attachments.store("shot.png", PNG)
        seen = []
        with mock.patch.object(judging, "confirm_adverse",
                               lambda task, prompt, *a, **k: seen.append(prompt) or
                               {"passed": True, "critique": ""}):
            judging.verify("what is this?", self.cap, "a 500 from nginx", attachments=[meta])
        self.assertIn("shot.png", seen[0])
        self.assertIn("you cannot", seen[0])
        self.assertNotIn(meta["path"], seen[0], "the judge has no tools; a path is noise")

    def test_every_run_payload_carries_the_attachments(self):
        src = workflow_src()
        # clarify, plan preview, resume, fresh run, ladder run, verify, swarm child
        self.assertGreaterEqual(src.count('"attachments": self._attachments'), 7)

    def test_clarify_knows_the_files_are_the_target(self):
        seen = []
        with mock.patch.object(gateway, "complete", lambda task, prompt, **k: seen.append(prompt) or "OK"):
            engine.clarify("what's going on here?", self.cap,
                           attachments=[{"name": "shot.png"}])
            engine.clarify("what's going on here?", self.cap)
        self.assertIn("shot.png", seen[0])
        self.assertNotIn("attached", seen[1])


def _req(base, path, data, headers):
    r = urllib.request.Request(base + path, method="POST", data=data, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers), e.read()


class AttachmentHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cap = registry.Capability("skill", "demo-read", "a read-only status report")
        cap.risk = "read"
        cls._saved = (server.CAPS, server._wf_start, server.TEMPORAL_OK)
        server.CAPS, server.TEMPORAL_OK = [cap], True
        cls.started = []

        async def fake_start(wid, params):
            cls.started.append(params)
        server._wf_start = fake_start
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
        server.CAPS, server._wf_start, server.TEMPORAL_OK = cls._saved

    def _upload(self, name, data):
        st, _, body = _req(self.base, "/api/attachments", data,
                           {"Content-Type": "application/octet-stream",
                            "X-Filename": urllib.request.quote(name)})
        return st, json.loads(body or b"{}")

    def _submit(self, body):
        st, _, out = _req(self.base, "/api/submit", json.dumps(body).encode(),
                          {"Content-Type": "application/json"})
        return st, json.loads(out or b"{}")

    def test_upload_then_submit_hands_the_run_trusted_paths(self):
        st, meta = self._upload("screen shot.png", PNG)
        self.assertEqual(st, 200)
        self.assertNotIn("path", meta)
        st, _ = self._submit({"request": "what is this?", "attachments": [meta["id"]]})
        self.assertEqual(st, 200)
        got = self.started[-1]["attachments"]
        self.assertEqual(got[0]["path"], attachments.get(meta["id"])["path"])

    def test_a_client_can_never_name_a_path(self):
        st, out = self._submit({"request": "x", "attachments": [{"path": "/etc/shadow"}]})
        self.assertEqual(st, 400)
        st, out = self._submit({"request": "x", "attachments": ["att-0000000000000000"]})
        self.assertEqual(st, 400)
        self.assertIn("expired or unknown", out["error"])

    def test_the_count_and_size_limits_are_enforced_server_side(self):
        with mock.patch.dict(os.environ, {"OTTO_ATTACHMENT_MAX_COUNT": "1",
                                          "OTTO_ATTACHMENT_MAX_MB": "1"}):
            ids = [self._upload(f"{i}.txt", b"x")[1]["id"] for i in range(2)]
            st, out = self._submit({"request": "x", "attachments": ids})
            self.assertEqual(st, 400)
            st, _ = self._upload("big.bin", b"x" * (1024 * 1024 + 1))
            self.assertEqual(st, 413)

    def test_an_image_renders_inline_and_anything_else_downloads_as_bytes(self):
        _, img = self._upload("a.png", PNG)
        _, page = self._upload("evil.html", b"<script>alert(1)</script>")
        with urllib.request.urlopen(self.base + "/api/attachments/" + img["id"]) as r:
            self.assertEqual(r.headers["Content-Type"], "image/png")
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        with urllib.request.urlopen(self.base + "/api/attachments/" + page["id"]) as r:
            self.assertEqual(r.headers["Content-Type"], "application/octet-stream")
            self.assertTrue(r.headers["Content-Disposition"].startswith("attachment"))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/api/attachments/../otto.db")

    def test_a_non_ascii_name_downloads_instead_of_crashing_the_headers(self):
        _, meta = self._upload("错误截图.png", PNG)
        with urllib.request.urlopen(self.base + "/api/attachments/" + meta["id"]) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("filename*=UTF-8''%E9%94%99", r.headers["Content-Disposition"])

    def test_a_follow_up_may_read_what_earlier_turns_attached_but_is_told_only_its_own(self):
        _, old = self._upload("log.txt", b"line 900: boom")
        _, new = self._upload("shot.png", PNG)
        chats.save({"id": "c-resume", "session_id": "sess-att", "messages": [
            {"role": "user", "text": "look", "attachments": [old]},
            {"role": "otto", "text": "a boom"}]})
        st, _, _ = _req(self.base, "/api/continue", json.dumps(
            {"cap": {"name": "demo-read"}, "session_id": "sess-att", "message": "and this?",
             "attachments": [new["id"]]}).encode(), {"Content-Type": "application/json"})
        self.assertEqual(st, 200)
        params = self.started[-1]
        self.assertEqual([a["id"] for a in params["attachments"]], [new["id"]])
        self.assertEqual([a["id"] for a in params["prior_attachments"]], [old["id"]])

    def test_a_cross_site_upload_is_refused(self):
        st, _, _ = _req(self.base, "/api/attachments", b"x",
                        {"Origin": "https://evil.example", "X-Filename": "a.txt"})
        self.assertEqual(st, 403)


class ChatPersistenceTests(unittest.TestCase):
    def test_a_message_keeps_display_fields_only_and_drops_a_bad_id(self):
        chats.save({"id": "c-att", "messages": [{"role": "user", "text": "see", "attachments": [
            {"id": "att-0123456789abcdef", "name": "a.png", "type": "image/png", "size": 9,
             "path": "/etc/shadow"},
            {"id": "../../x", "name": "b"}]}]})
        m = chats.get("c-att")["messages"][0]
        self.assertEqual(m["attachments"], [{"id": "att-0123456789abcdef", "name": "a.png",
                                             "type": "image/png", "size": 9}])

    def test_a_message_without_attachments_round_trips_unchanged(self):
        chats.save({"id": "c-plain", "messages": [{"role": "user", "text": "hi", "ts": "t"}]})
        self.assertEqual(chats.get("c-plain")["messages"], [{"role": "user", "text": "hi", "ts": "t"}])


class ComposerWiringTests(unittest.TestCase):
    def test_attach_js_loads_before_chat_js_and_every_send_path_carries_ids(self):
        src = ui_src()   # assets re-inlined in document order
        self.assertLess(src.index("Chat attachments (#161). Each file uploads"),
                        src.index("Chat: the composer, the live run watcher"))
        # fresh submit, continue, rebind, handoff
        self.assertGreaterEqual(src.count("attachments: attIds") + src.count(
            "attachments: (atts||[]).map(a=>a.id)"), 4)
        self.assertIn("postFile(\"/api/attachments\"", src)


if __name__ == "__main__":
    unittest.main()


class LocalVisionTests(unittest.TestCase):
    """#162: a `vision` local entry gets attached images as `image_url` parts on its own turn;
    nothing persisted ever holds the bytes, and an endpoint refusing them is a wall."""

    VISION = {"name": "vl", "provider": "openai", "base_url": "http://x/v1", "model": "q",
              "vision": True}
    # Measured against vLLM serving a text-only model (2026-10-01).
    VLLM_REFUSAL = ('{"error":{"message":"QuantTrio/Qwen3-Coder-30B-A3B-Instruct-AWQ is not a '
                    'multimodal model","type":"BadRequestError","param":null,"code":400}}')

    def setUp(self):
        import local_runtime
        self.lr = local_runtime
        self.tmp = tempfile.mkdtemp(prefix="otto-vision-")
        # These drive the tool LOOP; confinement has its own tests (LocalExecShellConfinementTests).
        # CI has no usable bwrap, where every run here would otherwise hit the #208 wall.
        _p = mock.patch.object(local_runtime, "ALLOW_UNGUARDED", True)
        _p.start()
        self.addCleanup(_p.stop)
        self._saved = (local_runtime._post, local_runtime.SESSIONS, gateway._STATS_PATH)
        local_runtime.SESSIONS = os.path.join(self.tmp, "sessions")
        gateway._STATS_PATH = os.path.join(self.tmp, "gateway-stats.json")
        self.bodies = []
        self.png = attachments.store("shot.png", PNG)
        self.jpg = attachments.store("photo.jpg", b"\xff\xd8\xff" + b"\x00" * 32)
        self.txt = attachments.store("log.txt", b"boom")

    def tearDown(self):
        self.lr._post, self.lr.SESSIONS, gateway._STATS_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _answer(self, m, body, timeout):
        self.bodies.append(json.loads(json.dumps(body)))
        return {"choices": [{"message": {"role": "assistant", "content": "a red square"}}],
                "usage": {}}

    def _run(self, entry, atts, **kw):
        self.lr._post = kw.pop("post", self._answer)
        return self.lr.run_json("what is this?", allowed_tools=config.READ_TOOLS,
                                model_entry=entry, cwd=self.tmp, attachments=atts, **kw)

    def _user(self, body):
        return [m for m in body["messages"] if m["role"] == "user"][0]["content"]

    def test_a_vision_entry_gets_one_image_part_per_image_and_none_for_other_files(self):
        self._run(self.VISION, [self.png, self.jpg, self.txt])
        parts = self._user(self.bodies[0])
        self.assertEqual(parts[0], {"type": "text", "text": "what is this?"})
        urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith("data:image/png;base64,"))
        self.assertTrue(urls[1].startswith("data:image/jpeg;base64,"))

    def test_without_the_flag_the_body_is_text_only(self):
        for entry in ({**self.VISION, "vision": False}, {k: v for k, v in self.VISION.items()
                                                          if k != "vision"}):
            self.bodies = []
            self._run(entry, [self.png])
            self.assertEqual(self._user(self.bodies[0]), "what is this?")

    def test_vision_is_a_local_runtime_flag_only(self):
        self.assertFalse(gateway.supports_vision({"provider": "claude", "vision": True}))
        self.assertFalse(gateway.supports_vision({"provider": "codex", "vision": True}))
        self.assertFalse(gateway.supports_vision({"provider": "openai"}))
        self.assertTrue(gateway.supports_vision(self.VISION))

    def test_no_image_bytes_reach_the_session_or_the_transcript(self):
        tr = os.path.join(self.tmp, "t.jsonl")
        out = self._run(self.VISION, [self.png], transcript=tr)
        b64 = self._user(self.bodies[0])[1]["image_url"]["url"].split(",", 1)[1]
        with open(self.lr.session_path(out["session_id"])) as f:
            stored = f.read()
        with open(tr) as f:
            transcript = f.read()
        for text in (stored, transcript):
            self.assertNotIn(b64, text)
            self.assertNotIn("base64", text)
            self.assertIn(f"[image: shot.png, image/png, {len(PNG)} bytes]", text)

    def test_a_resume_never_re_sends_an_earlier_turns_image(self):
        out = self._run(self.VISION, [self.png])
        self.bodies = []
        self._run(self.VISION, None, resume_session=out["session_id"])
        self.assertNotIn("image_url", json.dumps(self.bodies[0]))

    def test_an_endpoint_refusing_images_is_a_wall_and_lights_the_badge(self):
        v = error_classifier.classify(400, self.VLLM_REFUSAL)
        self.assertEqual(v.reason, error_classifier.Reason.images_unsupported)
        self.assertTrue(v.is_wall)

        def refuse(m, body, timeout):
            raise urllib.error.HTTPError("u", 400, "Bad Request", {},
                                         io.BytesIO(self.VLLM_REFUSAL.encode()))
        out = self._run(self.VISION, [self.png], post=refuse)
        self.assertEqual(out["wall_reason"], "images_unsupported")
        self.assertFalse(gateway.model_health()["vl"]["ok"], "a mis-set flag must light the badge")
        self.assertIn("Vision", error_classifier.wall_message("images_unsupported"))

    def test_the_engine_hands_attachments_to_the_local_runtime_and_redispatches_a_wall(self):
        saved = (gateway.exec_model_entry, gateway.exec_model_id, engine._claude,
                 self.lr.run_json, config.SUPERVISE, engine.trace, engine.say)
        seen, claude = [], []
        try:
            config.SUPERVISE = False
            engine.trace = engine.say = lambda *a, **k: None
            gateway.exec_model_entry = lambda cap_name=None, cfg=None: dict(self.VISION)
            gateway.exec_model_id = lambda cap_name=None: "claude-haiku"
            self.lr.run_json = lambda *a, **k: seen.append(k) or {
                "result": "(local runtime error)", "is_error": True, "total_cost_usd": 0,
                "session_id": None, "usage": {}, "wall_reason": "images_unsupported"}
            engine._claude = lambda prompt, **kw: claude.append(kw) or {
                "result": "claude read it", "total_cost_usd": 0, "session_id": "s", "usage": {}}
            cap = registry.Capability("custom", "briefing", "morning briefing")
            cap.risk = "read"
            att = engine.run_attempt("what is this?", cap, wid="w-vis", attachments=[self.png])
        finally:
            (gateway.exec_model_entry, gateway.exec_model_id, engine._claude, self.lr.run_json,
             config.SUPERVISE, engine.trace, engine.say) = saved
        self.assertEqual(seen[0]["attachments"], [self.png])
        self.assertTrue(att["local_incapable"])
        self.assertEqual(att["backend"], "claude")
        self.assertIn(self.png["path"], claude[0]["system_context"], "Claude reads it itself")

    def test_the_local_plan_preview_gets_the_attachments_too(self):
        import plans
        seen = []
        saved = self.lr.run_json
        self.lr.run_json = lambda *a, **k: seen.append(k) or {
            "result": "", "is_error": False, "session_id": None, "usage": {}}
        try:
            plans._local_preview("plan it", None, self.tmp, entry=dict(self.VISION),
                                 attachments=[self.png])
        finally:
            self.lr.run_json = saved
        self.assertEqual(seen[0]["attachments"], [self.png])

    def test_admin_has_a_vision_switch_for_local_rows_only(self):
        ui = ui_src()
        self.assertIn('data-vision="${esc(p.name)}"', ui)
        row = re.search(r'<td class="c-vision">\$\{\((.*?)\)\?', ui).group(1)
        self.assertEqual(row, "p.provider!=='claude'&&p.provider!=='codex'")
        self.assertIn("if(inp.checked) p.vision=true; else delete p.vision;", ui)
