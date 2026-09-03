"""Otto unit tests — cost, privacy, memory and report shaping.

Shared fixtures and the reason this suite is split by layer: test_support.py.
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

from test_support import setUpModule  # noqa: F401 - unittest calls it per module
from test_support import (_Cap, _FAKE_MCP_SERVER, _cap_stub, _fake_embed, _patched_registry_dirs, _storage_hammer)  # noqa: F401


class RedactTests(unittest.TestCase):
    """supervisor.redact / compact_event scrub secret-shaped substrings before a transcript
    line reaches the chat's live-progress blob or the supervisor's prompt (issue: a tool_result
    echoing credentials was rendered verbatim in the UI)."""

    def test_secret_kv_and_token_shapes_are_scrubbed(self):
        cases = [
            ('{"api_key": "sk-ABCDEF0123456789ABCDEF"}', "sk-ABCDEF0123456789ABCDEF"),
            ("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY", "wJalr"),
            ("password=hunter2superlong", "hunter2superlong"),
            ("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig", "payload"),
            ("token AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
            ('ghp_0123456789abcdefghijABCDEFGHIJ0123', "ghp_0123456789"),
        ]
        for text, secret in cases:
            out = supervisor.redact(text)
            self.assertNotIn(secret, out, f"leaked in: {text!r} -> {out!r}")
            self.assertIn("[redacted]", out)

    def test_vendor_key_formats_are_scrubbed_as_the_vendors_actually_issue_them(self):
        # Written from the REAL key formats, not from the regex. The old alnum-only body stopped
        # at the hyphen in `sk-ant`, so every Anthropic key — the one credential this tool
        # actually handles — passed straight through all four egresses. The case above used
        # `sk-ABCDEF…`, a fixture shaped to match the pattern, so it could never have caught it.
        for key in ("sk-ant-api03-AbCdEf0123456789GhIjKlMnOpQrStUvWxYz-AA",   # Anthropic
                    "sk-proj-AbCdEf0123456789GhIjKlMnOpQrStUv",               # OpenAI project
                    "sk-AbCdEf0123456789GhIjKlMnOpQr",                        # OpenAI legacy
                    "rk-AbCdEf0123456789GhIjKlMnOpQr"):
            out = supervisor.redact(f"the key is {key} ok")
            self.assertNotIn(key, out, f"leaked: {key}")
            self.assertIn("[redacted]", out)

    def test_a_hyphenated_resource_name_is_not_mistaken_for_a_key(self):
        # Why `sk-(ant|proj)-` is its own pattern instead of widening the legacy charset: `sk-`
        # plus a 16-char hyphenated body also matches ordinary infra names, and mangling those in
        # every reply is what gets a redaction rule weakened back out later.
        for benign in ("deploy sk-cluster-prod-eu-west-1 now",
                       "orion-prod-a sk-node-group-gpu-a10g-large"):
            self.assertEqual(supervisor.redact(benign), benign)

    def test_url_credentials_keep_the_host_readable(self):
        # The point of scrubbing user:pass but KEEPING the host is that the reader still learns
        # WHERE. Consuming the `@` rendered `https://[redacted]db.internal`, which reads as though
        # the host were scrubbed too.
        self.assertEqual(supervisor.redact("https://admin:hunter2@db.internal/x"),
                         "https://[redacted]@db.internal/x")
        self.assertEqual(supervisor.redact("postgres://otto:s3cr3t@rds.aws:5432/db"),
                         "postgres://[redacted]@rds.aws:5432/db")

    def test_benign_ids_survive(self):
        # nrql/json result shapes and ordinary identifiers must not be mangled
        text = '{ "data": { "actor": { "account": { "nrql": { "results": [] } } } } }'
        self.assertEqual(supervisor.redact(text), text)
        self.assertEqual(supervisor.redact("issue #63 branch otto/gh-issue-63"),
                         "issue #63 branch otto/gh-issue-63")

    def test_compact_event_redacts_tool_result(self):
        event = {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": 'API_KEY=sk-ABCDEF0123456789ABCDEF'}]}}
        line = supervisor.compact_event(event)
        self.assertIn("tool_result:", line)
        self.assertNotIn("sk-ABCDEF0123456789ABCDEF", line)
        self.assertIn("[redacted]", line)

    def test_compact_event_redacts_tool_use_input(self):
        event = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "curl -H 'Authorization: Bearer eyJabc.def.ghi'"}}]}}
        line = supervisor.compact_event(event)
        self.assertNotIn("eyJabc.def.ghi", line)

    def test_redact_handles_empty(self):
        self.assertEqual(supervisor.redact(""), "")
        self.assertIsNone(supervisor.redact(None))

    def test_supervisor_redact_is_the_privacy_implementation(self):
        """ONE implementation behind every boundary. A second copy is how one of them silently
        falls behind — the transcript scrub and the egress scrub must never diverge."""
        self.assertIs(supervisor.redact, privacy.redact)

    def test_redaction_is_idempotent(self):
        """Load-bearing: a delivered result passes TWO choke points (delivery.deliver and
        slack.post), so the second pass must not mangle what the first produced."""
        for text in ('{"api_key": "sk-ABCDEF0123456789ABCDEF"}',
                     "password=hunter2superlong and token: abc123def456",
                     "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"):
            once = privacy.redact(text)
            self.assertEqual(privacy.redact(once), once, f"not idempotent: {text!r}")

    def test_pem_private_key_block_is_collapsed(self):
        """Header-only matching leaves the key material itself in the message."""
        pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
               "MIIEowIBAAKCAQEAvxQ1x9keyMaterialHere\nmore/base64+data==\n"
               "-----END RSA PRIVATE KEY-----")
        out = privacy.redact(f"here you go:\n{pem}\ncheers")
        self.assertNotIn("keyMaterialHere", out)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", out)
        self.assertIn("cheers", out)

    def test_credentials_in_a_url_are_stripped_but_the_host_survives(self):
        """The host is the actionable half of "where does this connect"; the password is not."""
        out = privacy.redact("postgres://admin:s3cr3tpw@registry.example.internal:5432/registry")
        self.assertNotIn("s3cr3tpw", out)
        self.assertIn("registry.example.internal", out)

    def test_a_redaction_crash_fails_closed(self):
        """If the scrub itself blows up, the text must NOT pass through unredacted."""
        boom = type("B", (), {"__len__": lambda s: 1,
                              "__bool__": lambda s: True})()
        self.assertEqual(privacy.redact(boom), privacy.REDACTED)

    def test_keep_prefix_indices_track_the_pattern_tuple(self):
        """_KEEP_PREFIX is positional; adding a pattern without updating it silently turns the
        group-1 patterns into whole-match ones (dropping "password: " from the output) or, worse,
        makes a no-group pattern emit a literal r"\\1"."""
        for i in privacy._KEEP_PREFIX:
            self.assertEqual(privacy._SECRET_PATTERNS[i].groups, 1,
                             f"pattern {i} is in _KEEP_PREFIX but has no capture group")
        for i, pat in enumerate(privacy._SECRET_PATTERNS):
            if i not in privacy._KEEP_PREFIX:
                self.assertEqual(pat.groups, 0,
                                 f"pattern {i} has a capture group but is not in _KEEP_PREFIX")


class TldrReportShapeTests(unittest.TestCase):
    """Three texts shape a final report (_REPORT_FORMAT in the system context,
    _SINGLE_TURN_CONTRACT wrapping a subagent, _RESUME_CONTRACT on a follow-up turn), and the
    wrapper is the LAST thing the model reads — so a shape that lives in only one of them is a
    shape that loses. They must share `engine._TLDR_SHAPE` verbatim rather than paraphrase it."""

    CONTRACTS = ("_REPORT_FORMAT", "_SINGLE_TURN_CONTRACT", "_RESUME_CONTRACT")

    def test_every_report_contract_carries_the_shared_shape(self):
        for name in self.CONTRACTS:
            self.assertIn(engine._TLDR_SHAPE, getattr(engine, name),
                          f"{name} must interpolate _TLDR_SHAPE, not restate it")

    def test_no_contract_keeps_the_old_bottom_line_wording(self):
        """"Lead with the bottom line in 1-2 sentences" was satisfied by "The run is blocked, per
        the plan's own hard gate" (web-b97b623a) — true, and useless to the human who had to go
        answer a question on the ticket. A surviving copy would compete with the TLDR shape."""
        for name in self.CONTRACTS:
            self.assertNotIn("bottom line", getattr(engine, name), f"{name} still asks for a "
                             "bare bottom line, which competes with the TLDR shape")

    def test_the_shape_demands_a_tldr_and_a_conditional_next_action(self):
        shape = engine._TLDR_SHAPE
        self.assertIn("**TLDR** — ", shape)
        self.assertIn("**What you need to do** — ", shape)
        # Omitted when nothing is needed: a standing "no action required" footer trains the reader
        # to skip the one line that matters on the runs where there IS an action.
        self.assertIn("Omit that line entirely", shape)
        # The TLDR is for someone who has never heard of Otto's internals.
        for jargon in ("the gate", "the approved plan", "verify"):
            self.assertIn(jargon, shape, "the shape must name the vocabulary it forbids")

    def test_the_capabilitys_own_format_outranks_the_shape(self):
        """Otto's shape and a cap's mandated format are two contracts over the same bytes, and
        `judging.cap_contract_block` already tells the judge the cap's rules win. Without the
        executor being told the same, obeying either one fails: sched-mosaic-9e5e5681 (2026-08-19)
        rendered the TLDR shape on attempt 1 and was failed for breaking the cap's mandated
        sections, rendered the cap's format on attempt 2, and exhausted the ladder."""
        for name in self.CONTRACTS:
            text = getattr(engine, name)
            self.assertIn("pass it through UNCHANGED", text,
                          f"{name} lets Otto's shape override a cap-mandated format, which the "
                          "judge then fails as a contract violation")
        shape = engine._TLDR_SHAPE
        # Actionable from what the caller can actually SEE. A subagent's format rules live inside
        # the subagent, so "obey the capability's format" is an instruction the coordinator cannot
        # follow — measured: it kept emitting the TLDR shape. Keying on the RETURNED text is what
        # it can act on.
        self.assertIn("only what it returns", shape)
        self.assertIn("no TLDR line", shape)
        # And it must not swallow the normal case: raw narration still gets shaped.
        self.assertIn("raw working material", shape)

    def test_the_shape_survives_both_audiences(self):
        """A conversational run gets _DIRECT_REPLY_FORMAT INSTEAD (a Slack reply must not carry a
        "**TLDR**" heading), so the shape must be absent there — the split is deliberate."""
        self.assertNotIn(engine._TLDR_SHAPE, engine._DIRECT_REPLY_FORMAT)
        self.assertIs(engine._output_contract("report"), engine._REPORT_FORMAT)
        self.assertIs(engine._output_contract(engine.CONVERSATION_AUDIENCE),
                      engine._DIRECT_REPLY_FORMAT)


class MemoryTests(unittest.TestCase):
    """Memory must stay relevant: only durable facts are stored, runs that teach nothing
    write no row, and a fact already known is never duplicated (issue #55)."""

    def setUp(self):
        import tempfile
        self._orig_complete = engine.gateway.complete
        self._orig_db = engine._DB
        engine._DB = os.path.join(tempfile.mkdtemp(prefix="otto-mem-"), "otto.db")

    def tearDown(self):
        engine.gateway.complete = self._orig_complete
        engine._DB = self._orig_db

    def _cap(self):
        c = registry.Capability("skill", "deploy-status", "desc")
        c.risk = "read"
        return c

    def test_a_long_fact_is_clipped_on_a_word_boundary_and_marked(self):
        """A bare slice ends a stored fact mid-word, which reads as corrupt data wherever it is
        shown and is indistinguishable from a truncation the model made itself."""
        long = ("The ECR lifecycle rule creates a retention risk because a still-deployed image "
                "on a high-churn repository can fall outside the thirty image window and then be "
                "deleted, so every future retention decision must account for deployment age")
        self.assertGreater(len(long), 200)
        out = memory._clip_fact(long, 200)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("\u2026"), out)
        # the cut landed between words, not inside one
        self.assertTrue(long.startswith(out[:-1]), out)
        self.assertIn(out[:-1].split()[-1], long.split())

    def test_a_short_fact_is_stored_whole_and_unmarked(self):
        short = "The staging AWS account ID is 123456789012."
        self.assertEqual(memory._clip_fact(short, 200), short)

    def test_none_yields_no_facts(self):
        engine.gateway.complete = lambda task, prompt: "NONE"
        self.assertEqual(engine._extract_facts("req", "some result"), [])

    def test_no_output_skips_the_model(self):
        engine.gateway.complete = lambda task, prompt: self.fail("should not call the model")
        self.assertEqual(engine._extract_facts("req", "(no output)"), [])

    def test_parses_and_caps_at_three(self):
        # realistic fact shapes: _is_durable_fact drops one-liners too short to be a real fact
        engine.gateway.complete = lambda task, prompt: (
            "- the dev cluster runs two vllm engines per node\n"
            "- the prod-a gpu ami is built by ci nightly\n"
            "- registry stores its registry db on the shared rds\n"
            "- the reaper sweeps in-progress cards every 300s")
        self.assertEqual(engine._extract_facts("req", "r"),
                         ["the dev cluster runs two vllm engines per node",
                          "the prod-a gpu ami is built by ci nightly",
                          "registry stores its registry db on the shared rds"])

    def test_narration_lines_are_dropped_before_storage(self):
        engine.gateway.complete = lambda task, prompt: (
            "## Memory facts from this run\n"
            "Let me extract the durable facts for the owner.\n"
            "the prod-a vllm deployment is scaffolded but disabled\n"
            "When did the audit occur?")
        self.assertEqual(engine._extract_facts("req", "r"),
                         ["the prod-a vllm deployment is scaffolded but disabled"])

    def test_known_facts_are_shown_to_the_model_and_dropped_from_output(self):
        seen_prompt = {}

        def fake(task, prompt):
            seen_prompt["p"] = prompt
            # first line restates a known fact
            return "prod-a is hosted in the us-east-1 region\nthe turn servers run on dev-a too"

        engine.gateway.complete = fake
        out = engine._extract_facts("req", "r", known=["prod-a is hosted in the us-east-1 region"])
        self.assertIn("prod-a is hosted in the us-east-1 region", seen_prompt["p"])  # given to the model
        self.assertEqual(out, ["the turn servers run on dev-a too"])                 # restated one dropped

    def test_remember_skips_row_when_nothing_durable(self):
        engine._remember(self._cap(), "req", [])
        self.assertEqual(engine.memory_events(), [])                 # no noise row written

    def test_remember_dedupes_against_existing(self):
        cap = self._cap()
        engine._remember(cap, "req-1", ["The dev GPU is idle"])
        engine._remember(cap, "req-2", ["the dev gpu is idle.", "a fresh fact"])  # normalized dup + new
        self.assertEqual(engine.recent_facts(), ["The dev GPU is idle", "a fresh fact"])

    def test_remember_dedupes_within_one_batch(self):
        engine._remember(self._cap(), "req", ["same fact", "same fact", "other"])
        self.assertEqual(engine.recent_facts(), ["same fact", "other"])

    def test_delete_fact_removes_one_and_keeps_its_siblings(self):
        """Forgetting a single wrong fact must not cost the right ones stored beside it — the whole
        point of per-fact delete (clearing the store was previously the only option)."""
        cap = self._cap()
        engine._remember(cap, "r1", ["vllm is not deployed in production",
                                     "the reaper sweeps in-progress cards every 300s"])
        row = engine.memory_events()[0]
        self.assertTrue(engine.delete_fact(row["id"], "vllm is not deployed in production"))
        left = [e["facts"] for e in engine.memory_events()]
        self.assertEqual(left, [["the reaper sweeps in-progress cards every 300s"]])
        self.assertNotIn("vllm is not deployed in production", engine.recent_facts())

    def test_delete_fact_drops_the_row_when_it_empties(self):
        """`_remember` never writes a row with an empty `facts` list, so removing the last fact
        must delete the row rather than invent a shape no reader expects."""
        cap = self._cap()
        engine._remember(cap, "r1", ["vllm is not deployed in production"])
        row = engine.memory_events()[0]
        self.assertTrue(engine.delete_fact(row["id"], "vllm is not deployed in production"))
        self.assertEqual(engine.memory_events(), [])

    def test_delete_fact_matches_on_normalised_text(self):
        """A fact copied out of the UI drifts in whitespace/case; match the way `_remember`
        de-dupes rather than on raw equality."""
        cap = self._cap()
        engine._remember(cap, "r1", ["The prod-a gpu ami is built by ci nightly"])
        row = engine.memory_events()[0]
        self.assertTrue(engine.delete_fact(row["id"], "  the PROD-A gpu ami is built by ci nightly "))
        self.assertEqual(engine.memory_events(), [])

    def test_delete_fact_rejects_unknown_row_or_fact(self):
        cap = self._cap()
        engine._remember(cap, "r1", ["the reaper sweeps in-progress cards every 300s"])
        row = engine.memory_events()[0]
        self.assertFalse(engine.delete_fact(row["id"], "a fact this row never held at all"))
        self.assertFalse(engine.delete_fact(99_999, "the reaper sweeps in-progress cards every 300s"))
        self.assertFalse(engine.delete_fact(None, "the reaper sweeps in-progress cards every 300s"))
        self.assertFalse(engine.delete_fact(row["id"], ""))
        self.assertEqual(len(engine.memory_events()[0]["facts"]), 1)   # nothing collateral

    def test_delete_fact_cannot_reach_a_project_namespace(self):
        """`/api/memory` lists GLOBAL facts only (as `clear_memory` clears global only), so a
        delete driven from that list must not reach into a namespace it never showed."""
        cap = self._cap()
        proj = "/repos/infra"
        engine._remember(cap, "r1", ["the prod-a gpu ami is built by ci nightly"], project=proj)
        row = engine.memory_events(every=True)[0]
        self.assertEqual(row["namespace"], engine._memory_ns(proj))
        self.assertFalse(engine.delete_fact(row["id"], row["facts"][0]))          # global scope: refused
        self.assertTrue(engine.delete_fact(row["id"], row["facts"][0], every=True))


class MemoryGcParseTests(unittest.TestCase):
    """Pure parsers for the memory garbage collector — no LLM call, so these are the cheap,
    exhaustive layer. Both default toward KEEPING an item on anything unparseable: a lost
    eviction candidate costs nothing, a wrongly-evicted fact/approach/rule can't be undone."""

    def test_classification_parses_one_line_per_item(self):
        text = ("1: STALE - contradicted by item 3\n"
                "2: VERIFY - asserts vllm is currently deployed\n"
                "3: KEEP - still accurate")
        out = engine._parse_gc_classification(text, 3)
        self.assertEqual([o["verdict"] for o in out], ["STALE", "VERIFY", "KEEP"])
        self.assertEqual(out[0]["reason"], "contradicted by item 3")

    def test_classification_defaults_every_slot_to_keep(self):
        out = engine._parse_gc_classification("garbage, not the expected format at all", 4)
        self.assertEqual([o["verdict"] for o in out], ["KEEP"] * 4)

    def test_classification_ignores_out_of_range_indices(self):
        out = engine._parse_gc_classification("0: STALE - bad index\n99: STALE - bad index", 2)
        self.assertEqual([o["verdict"] for o in out], ["KEEP", "KEEP"])

    def test_classification_empty_reply_keeps_everything(self):
        out = engine._parse_gc_classification("", 2)
        self.assertEqual([o["verdict"] for o in out], ["KEEP", "KEEP"])

    def test_verify_true_false_unparseable(self):
        self.assertEqual(engine._parse_gc_verify("TRUE\nstill deployed")[0], True)
        self.assertEqual(engine._parse_gc_verify("FALSE\nremoved last quarter")[0], False)
        self.assertIsNone(engine._parse_gc_verify("not sure, couldn't check")[0])
        self.assertIsNone(engine._parse_gc_verify("")[0])


class MemoryGcTests(unittest.TestCase):
    """Integration-shaped tests for the on-demand GC pass (engine.gc_preview/gc_evict): a batched
    classifier flags STALE/VERIFY candidates, VERIFY items get a bounded real tool-verification
    call, and only a human-confirmed subset is ever actually deleted."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="otto-gc-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self._saved = (engine._DB, engine._claude, engine.gateway.complete, gateway._PATH)
        engine._DB = os.path.join(tmp, "otto.db")
        gateway._PATH = os.path.join(tmp, "models.json")
        self.addCleanup(self._restore)

    def _restore(self):
        engine._DB, engine._claude, engine.gateway.complete, gateway._PATH = self._saved

    def _cap(self):
        c = registry.Capability("skill", "deploy-status", "desc")
        c.risk = "read"
        return c

    def test_preview_splits_stale_and_verified_false_into_candidates(self):
        engine._remember(self._cap(), "r1", ["vllm was removed from prod-a last quarter"])
        engine._remember_solution(self._cap(), "restart a stuck pod",
                                   "kubectl delete pod <name> -n <ns>")
        engine.add_behavior("always check the dev cluster before prod")

        def fake_complete(task, prompt):
            self.assertEqual(task, "memory_gc")
            return ("1: STALE - superseded by a newer fact\n"
                    "2: KEEP - a durable approach\n"
                    "3: VERIFY - asserts a standing current-state claim")
        engine.gateway.complete = fake_complete
        engine._claude = lambda *a, **k: {"result": "FALSE\nrule no longer applies", "is_error": False}

        out = engine.gc_preview()
        self.assertEqual(out["scanned"], 3)
        self.assertEqual(out["verify_skipped"], 0)
        stores = sorted(c["store"] for c in out["candidates"])
        self.assertEqual(stores, ["behavior", "fact"])   # STALE fact + VERIFY-then-FALSE rule
        self.assertNotIn("solution", stores)              # the KEEP item never becomes a candidate

    def test_both_gc_model_calls_ride_the_memory_gc_tier(self):
        """GC is the one pass that DELETES the operator's memory, and both its model calls used to
        be invisible in the Admin phase matrix: the batch classifier rode "verify" (so downshifting
        the judge silently downshifted what decides which memories die) and the live tool-check was
        hardcoded to `gateway._default_claude()` — the same invisible-default bug the `preview`
        tier exists to fix. Both must resolve through the `memory_gc` tier."""
        self.assertIn("memory_gc", gateway.TASKS)
        engine._remember(self._cap(), "r1", ["prod-a still runs vllm"])
        seen = {}
        def fake_complete(task, prompt):
            seen["task"] = task
            return "1: VERIFY - current-state claim"
        engine.gateway.complete = fake_complete
        engine._claude = lambda *a, **k: (seen.__setitem__("model", k.get("model")),
                                          {"result": "FALSE\ngone", "is_error": False})[1]
        gateway.save({**gateway.load(), "assign": {**gateway.load()["assign"],
                                                   "memory_gc": "claude-opus", "verify": "claude-haiku"}})
        engine.gc_preview()
        self.assertEqual(seen["task"], "memory_gc", "the batch classifier must not ride the verify tier")
        self.assertEqual(seen["model"], gateway.memory_gc_model_id(),
                         "the live tool-check must read the tier, not a hardcoded default")
        self.assertNotEqual(seen["model"], gateway._default_claude())

    def test_preview_never_evicts_on_an_inconclusive_live_check(self):
        engine.add_behavior("always ask before touching prod")
        engine.gateway.complete = lambda task, prompt: "1: VERIFY - current-state claim"
        engine._claude = lambda *a, **k: {"result": "not sure, the check timed out", "is_error": False}
        out = engine.gc_preview()
        self.assertEqual(out["candidates"], [])            # inconclusive -> kept, never a candidate

    def test_preview_caps_live_verification_calls(self):
        for i in range(3):
            engine.add_behavior(f"standing rule number {i}")
        engine.gateway.complete = lambda task, prompt: "\n".join(
            f"{i + 1}: VERIFY - claim {i}" for i in range(3))
        calls = []
        engine._claude = lambda *a, **k: (calls.append(1), {"result": "FALSE\nno longer true"})[1]
        orig_setting = config.setting
        config.setting = lambda name: 1 if name == "memory_gc_max_verify" else orig_setting(name)
        try:
            out = engine.gc_preview()
        finally:
            config.setting = orig_setting
        self.assertEqual(len(calls), 1)                     # capped, not one per VERIFY item
        self.assertEqual(out["verify_skipped"], 2)
        self.assertEqual(len(out["candidates"]), 1)

    def test_preview_returns_nothing_scanned_with_an_empty_store(self):
        out = engine.gc_preview()
        self.assertEqual(out, {"candidates": [], "scanned": 0, "verify_skipped": 0})

    def test_preview_runs_multiple_batches_concurrently_and_keeps_verdicts_aligned(self):
        """A store bigger than one batch must fan out across a thread pool (issue: a real ~220-item
        store chained 9 sequential `claude -p` calls, several real minutes) — but each batch's
        verdicts must still land on the RIGHT items regardless of which batch finishes first."""
        for i in range(4):
            engine.add_behavior(f"standing rule number {i}")
        orig_setting = config.setting
        config.setting = lambda name: 2 if name == "memory_gc_batch_size" else orig_setting(name)
        seen_batches = []

        def fake_complete(task, prompt):
            # Content-driven, not position-driven: whichever batch happens to contain "rule
            # number 0" replies slower, proving the result's batch order doesn't depend on
            # completion order — and each line's verdict is decided by that item's own text, so
            # a verdict can't land on the wrong item no matter how batches interleave.
            lines = [l for l in prompt.splitlines() if re.match(r"^\d+\.\s*\[behavior\]", l)]
            seen_batches.append(len(lines))
            if any("rule number 0" in l for l in lines):
                time.sleep(0.05)
            out = []
            for l in lines:
                idx = re.match(r"^(\d+)\.", l).group(1)
                verdict = "STALE" if ("rule number 0" in l or "rule number 2" in l) else "KEEP"
                out.append(f"{idx}: {verdict} - because")
            return "\n".join(out)
        engine.gateway.complete = fake_complete
        try:
            out = engine.gc_preview()
        finally:
            config.setting = orig_setting
        self.assertEqual(len(seen_batches), 2)                 # two batches of 2, ran concurrently
        self.assertEqual(out["scanned"], 4)
        stale_texts = {c["text"] for c in out["candidates"]}
        self.assertEqual(stale_texts, {"standing rule number 0", "standing rule number 2"})

    def test_classify_batch_failure_keeps_its_items_instead_of_crashing(self):
        """One batch's model call blowing up (tier down, transient error) must not crash the
        whole scan — its items default to KEEP, same bias as an unparseable reply."""
        engine.add_behavior("a rule")
        engine.gateway.complete = lambda task, prompt: (_ for _ in ()).throw(OSError("tier down"))
        out = engine.gc_preview()          # must not raise
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["scanned"], 1)

    def test_live_verify_failure_is_not_treated_as_false(self):
        """A raised exception during tool-verification (not just an is_error result) must degrade
        to 'can't confirm it's false', never to an eviction."""
        engine.add_behavior("a rule")
        engine.gateway.complete = lambda task, prompt: "1: VERIFY - current-state claim"
        engine._claude = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("subprocess died"))
        out = engine.gc_preview()          # must not raise
        self.assertEqual(out["candidates"], [])

    def test_evict_removes_only_confirmed_candidates_and_audits_once(self):
        engine._remember(self._cap(), "r1", ["fact one", "fact two"])
        engine._remember_solution(self._cap(), "task", "approach text")
        engine.add_behavior("a rule")
        fact_event = engine.memory_events()[0]
        sol = engine.solutions()[0]
        rule = engine.behaviors()[0]

        candidates = [
            {"store": "fact", "id": fact_event["id"], "text": "fact one"},
            {"store": "solution", "id": sol["id"], "text": sol["approach"]},
        ]
        n = engine.gc_evict(candidates)
        self.assertEqual(n, 2)
        self.assertEqual(engine.recent_facts(), ["fact two"])         # sibling fact survives
        self.assertEqual(engine.solutions(), [])
        self.assertEqual(len(engine.behaviors()), 1)                  # untouched — never a candidate

        rows = list(engine.iter_audit_entries())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["capability"], "gc:memory")
        self.assertEqual(rows[0]["evicted"], 2)

    def test_evict_reaches_a_project_namespaced_fact(self):
        """`gc_preview` scans every namespace (issue: `_gc_items` uses `memory_events(every=True)`),
        so eviction of a project fact must use the `every=True` delete path — the default-scoped
        one the Memory tab's per-fact 'forget' button uses would silently no-op here."""
        proj = "/repos/infra"
        engine._remember(self._cap(), "r1", ["the prod-a gpu ami is built nightly"], project=proj)
        row = engine.memory_events(every=True)[0]
        n = engine.gc_evict([{"store": "fact", "id": row["id"], "text": row["facts"][0]}])
        self.assertEqual(n, 1)
        self.assertEqual(engine.memory_events(every=True), [])

    def test_evict_with_no_candidates_writes_no_audit_row(self):
        n = engine.gc_evict([])
        self.assertEqual(n, 0)
        self.assertEqual(list(engine.iter_audit_entries()), [])


class SolutionsTests(unittest.TestCase):
    """Solved-task approaches (issue #66): distilled only on a verified pass, deduped on the
    request, recalled by keyword similarity above a threshold, and bounded."""

    def setUp(self):
        import tempfile
        self._orig_complete = engine.gateway.complete
        self._orig_db = engine._DB
        d = tempfile.mkdtemp(prefix="otto-sol-")
        # one db => facts/solutions are isolated together, so _memory_context stays clean
        engine._DB = os.path.join(d, "otto.db")

    def tearDown(self):
        engine.gateway.complete = self._orig_complete
        engine._DB = self._orig_db

    def _cap(self):
        c = registry.Capability("agent", "sre-minion", "desc")
        c.risk = "write"
        return c

    def test_none_yields_empty(self):
        engine.gateway.complete = lambda task, prompt: "NONE"
        self.assertEqual(engine._extract_solution("req", self._cap(), "some result"), "")

    def test_no_output_skips_the_model(self):
        engine.gateway.complete = lambda task, prompt: self.fail("should not call the model")
        self.assertEqual(engine._extract_solution("req", self._cap(), "(no output)"), "")

    def test_extract_truncates(self):
        engine.gateway.complete = lambda task, prompt: "x" * 999
        self.assertEqual(len(engine._extract_solution("req", self._cap(), "r")), 600)

    def test_remember_skips_empty_approach(self):
        engine._remember_solution(self._cap(), "req", "")
        self.assertEqual(engine.solutions(), [])              # nothing distilled -> no row

    def test_remember_dedupes_same_request(self):
        cap = self._cap()
        engine._remember_solution(cap, "Deploy the api-service service to staging", "first approach")
        engine._remember_solution(cap, "deploy the api-service service to staging.", "second approach")  # normalized dup
        sols = engine.solutions()
        self.assertEqual(len(sols), 1)
        self.assertEqual(sols[0]["approach"], "second approach")   # refreshed in place, not piled up

    def test_remember_bounds_store(self):
        cap, orig = self._cap(), engine._SOLUTIONS_MAX
        engine._SOLUTIONS_MAX = 3
        try:
            for i in range(5):
                engine._remember_solution(cap, f"unique request number {i}", f"approach {i}")
            self.assertEqual(len(engine.solutions()), 3)
        finally:
            engine._SOLUTIONS_MAX = orig

    def test_recall_ranks_by_overlap_and_thresholds(self):
        cap = self._cap()
        engine._remember_solution(cap, "renew the aws client vpn certificate", "vpn approach")
        engine._remember_solution(cap, "investigate slow rds queries on reporting-db", "rds approach")
        hits = engine.recall_solutions("the aws client vpn certificate expired again", limit=2)
        self.assertEqual([h["approach"] for h in hits], ["vpn approach"])  # only the vpn one clears the threshold

    def test_recall_returns_nothing_for_unrelated(self):
        engine._remember_solution(self._cap(), "renew the aws client vpn certificate", "vpn approach")
        self.assertEqual(engine.recall_solutions("write a poem about cats"), [])

    def test_memory_context_injects_matching_solution(self):
        engine._remember_solution(self._cap(), "renew the aws client vpn certificate",
                                  "rotate the cert via endpoint recreation")
        ctx = engine._memory_context("aws client vpn certificate renewal")
        self.assertIn("rotate the cert via endpoint recreation", ctx)
        self.assertIsNone(engine._memory_context())   # no request -> no recall (and no facts here)

    def test_is_durable_fact_rejects_narration(self):
        """Every rejected string below was found in the LIVE fact store on 2026-07-30, filling the
        recall window and crowding out real facts."""
        for junk in ("## Memory facts from this audit",
                     "Let me extract the memory facts, then provide the answer for the owner.",
                     "When did the audit occur?** (specific quarter/year)",
                     "What exactly was the auditor's finding?** (e.g., was it missing scans)",
                     "I don't have access to Slack history or a record of your SOC2 audit details",
                     "Here's the answer to deliver:", "Based on the owner's own vLLM project notes",
                     "**Reply to send:**", "dev-a CI/Terraform parity gaps:**",
                     "---", "NONE", "ok", "", None):
            self.assertFalse(engine._is_durable_fact(junk), f"should be rejected: {junk!r}")

    def test_is_durable_fact_keeps_real_facts(self):
        for good in ("prod-a vLLM serving is scaffolded in draft PR #327 with enabled=false.",
                     "GPU time-slicing is enabled in the dev environment to run two vLLM engines.",
                     "The vllm API endpoint is gated with a bearer-token API key.",
                     "Slack cursors must be 6-decimal timestamps or history returns nothing."):
            self.assertTrue(engine._is_durable_fact(good), f"should be kept: {good!r}")

    def test_failed_run_marks_its_facts_unverified(self):
        """Facts are distilled even from a final attempt that FAILED verification — that is how a
        wrong answer became a durable global fact. Keep them, but label them."""
        cap = self._cap()
        engine._remember(cap, "r1", ["the prod endpoint is infer.example.com"], verified=True)
        engine._remember(cap, "r2", ["vllm is not deployed in production"], verified=False)
        engine._remember(cap, "r3", ["the reaper sweeps in-progress cards"], verified=None)
        rows = engine.memory_events(every=True)
        self.assertNotIn("unverified", rows[0])   # a pass keeps the original row shape
        self.assertTrue(rows[1]["unverified"])
        self.assertNotIn("unverified", rows[2])   # unjudged is not the same as failed
        dated = {d["fact"]: d["unverified"] for d in engine.recent_facts(dated=True)}
        self.assertFalse(dated["the prod endpoint is infer.example.com"])
        self.assertTrue(dated["vllm is not deployed in production"])
        ctx = engine._memory_context()
        self.assertRegex(ctx, r"UNVERIFIED\) vllm is not deployed")
        self.assertIn("never as established fact", ctx)
        self.assertNotIn("UNVERIFIED) the prod endpoint", ctx)

    def test_recall_window_prefers_relevant_facts_over_merely_recent(self):
        """The vLLM miss: the facts that answered the question sat just outside the recency tail
        while the window held unrelated CI/Savings-Plan trivia."""
        cap = self._cap()
        engine._remember(cap, "old", ["prod-a vllm serving is enabled and autostarts gemma-e4b"])
        for i in range(12):
            engine._remember(cap, f"noise{i}", [f"ci agent pool {i} was resized last week"])
        recent = engine.recent_facts(limit=6)
        self.assertNotIn("prod-a vllm serving is enabled and autostarts gemma-e4b", recent)
        relevant = engine.recent_facts(limit=6, request="is vllm serving in prod-a?")
        self.assertIn("prod-a vllm serving is enabled and autostarts gemma-e4b", relevant)
        self.assertEqual(len(relevant), 6)          # window is still filled, not truncated
        # an unrelated request is unaffected by ranking
        self.assertEqual(engine.recent_facts(limit=6, request="write a poem about cats"), recent)

    def test_recent_facts_dated_keeps_order_and_carries_dates(self):
        engine._remember(self._cap(), "r1", ["older fact"])
        engine._remember(self._cap(), "r2", ["newer fact"])
        plain, dated = engine.recent_facts(), engine.recent_facts(dated=True)
        self.assertEqual(plain, ["older fact", "newer fact"])          # selection/order unchanged
        self.assertEqual([d["fact"] for d in dated], plain)
        self.assertTrue(all(d["at"] for d in dated), dated)

    def test_memory_context_dates_facts_and_forbids_unverified_current_state(self):
        """The vLLM miss (2026-07-30): an undated recollection reads as current truth, so the run
        answered "no production deployment" from a stale fact instead of checking the repo."""
        engine._remember(self._cap(), "r", ["vllm is dev-only"])
        ctx = engine._memory_context()
        self.assertIn("vllm is dev-only", ctx)
        self.assertRegex(ctx, r"\(\d{4}-\d{2}-\d{2}\) vllm is dev-only")   # tagged with its date
        low = ctx.lower()
        for phrase in ("not the current state", "override memory", "the newer one wins"):
            self.assertIn(phrase, low, f"missing staleness framing: {phrase}")


class BehaviorTests(unittest.TestCase):
    """Behaviour rules (issue #68): user directives on HOW to work — stored explicitly or via a
    confirmed suggestion, scoped global/per-cap, injected as directives, never touching the gate."""

    def setUp(self):
        import tempfile
        self._orig_complete = engine.gateway.complete
        self._orig_db = engine._DB
        d = tempfile.mkdtemp(prefix="otto-bhv-")
        # one db => rules/facts/solutions are all isolated together, so context stays clean
        engine._DB = os.path.join(d, "otto.db")

    def tearDown(self):
        engine.gateway.complete = self._orig_complete
        engine._DB = self._orig_db

    def _cap(self, name="sre-minion", kind="agent", risk="read"):
        c = registry.Capability(kind, name, "desc")
        c.risk = risk
        return c

    def test_add_and_list(self):
        rec = engine.add_behavior("Always run the tests before opening a PR", "global")
        self.assertTrue(rec and rec["id"])
        self.assertEqual(len(engine.behaviors()), 1)

    def test_add_requires_text(self):
        self.assertIsNone(engine.add_behavior("   ", "global"))
        self.assertEqual(engine.behaviors(), [])

    def test_dedupes_on_rule_and_scope(self):
        engine.add_behavior("Ask before touching prod", "global")
        engine.add_behavior("ask  before   touching prod.", "global")        # normalized dup, same scope
        self.assertEqual(len(engine.behaviors()), 1)
        engine.add_behavior("Ask before touching prod", "agent:sre-minion")  # same rule, other scope -> kept
        self.assertEqual(len(engine.behaviors()), 2)

    def test_bounds_store(self):
        orig = engine._BEHAVIORS_MAX
        engine._BEHAVIORS_MAX = 3
        try:
            for i in range(5):
                engine.add_behavior(f"rule number {i}", "global")
            self.assertEqual(len(engine.behaviors()), 3)
        finally:
            engine._BEHAVIORS_MAX = orig

    def test_scoping(self):
        engine.add_behavior("global rule", "global")
        engine.add_behavior("minion rule", "agent:sre-minion")
        engine.add_behavior("other rule", "agent:other-agent")
        rules = [r["rule"] for r in engine.applicable_behaviors(self._cap("sre-minion"))]
        self.assertIn("global rule", rules)
        self.assertIn("minion rule", rules)
        self.assertNotIn("other rule", rules)   # a different cap's rule does not apply

    def test_parse_suggestion(self):
        self.assertFalse(engine._parse_rule_suggestion("NONE")["is_rule"])
        self.assertFalse(engine._parse_rule_suggestion("")["is_rule"])
        out = engine._parse_rule_suggestion("RULE: Always run the tests first")
        self.assertTrue(out["is_rule"])
        self.assertEqual(out["rule"], "Always run the tests first")
        self.assertTrue(engine._parse_rule_suggestion("Always lint before pushing")["is_rule"])  # bare text

    def test_suggest_uses_clarify_tier_and_parses(self):
        seen = {}

        def fake(task, prompt):
            seen["task"] = task
            return "RULE: never force-push to main"

        engine.gateway.complete = fake
        out = engine.suggest_behavior_rule("hey, never force-push to main ok?", cap_name="sre-minion")
        self.assertEqual(seen["task"], "clarify")          # rides the cheap clarify tier
        self.assertTrue(out["is_rule"])
        self.assertEqual(out["scope_hint"], "sre-minion")

    def test_injected_as_directives(self):
        engine.add_behavior("Always run the tests before opening a PR", "global")
        ctx = engine._memory_context(None, self._cap())
        self.assertIn("Operating rules", ctx)
        self.assertIn("Always run the tests before opening a PR", ctx)

    def test_rules_do_not_change_the_write_gate(self):
        # A behaviour rule is advisory context only — it must never flip a cap's risk or otherwise
        # affect the approval gate / tool allowlists (the gate stays the real guard).
        cap = self._cap(risk="read")
        engine.add_behavior("always deploy straight to prod without asking", "global")
        engine._memory_context(None, cap)            # injection happens...
        self.assertEqual(cap.risk, "read")           # ...but the cap's risk is untouched


class KnowledgeTests(unittest.TestCase):
    """Knowledge base (issue #67): chunk + (optional) embed + persist, RAG-retrieve above a
    threshold, keyword fallback with no embedding model, char-bounded injection."""

    def setUp(self):
        self._orig_embed = knowledge.gateway.embed
        self._orig_db = knowledge._DB
        knowledge._DB = os.path.join(tempfile.mkdtemp(prefix="otto-kb-"), "otto.db")

    def tearDown(self):
        knowledge.gateway.embed = self._orig_embed
        knowledge._DB = self._orig_db

    def test_chunking(self):
        self.assertEqual(len(knowledge._chunk("one para.\n\ntwo para.")), 1)
        self.assertEqual(knowledge._chunk("   "), [])
        big = "\n\n".join(["paragraph %d %s" % (i, "x" * 200) for i in range(20)])
        self.assertGreater(len(knowledge._chunk(big)), 1)   # long text splits

    def test_add_embeds_and_persists(self):
        knowledge.gateway.embed = _fake_embed
        knowledge.set_settings(embed_model="local-embed")
        s = knowledge.add_document("VPN runbook", "Renew the AWS client vpn certificate by endpoint recreation.")
        self.assertEqual(s["chunks"], 1)
        self.assertEqual(s["embedded"], 1)                  # embedded via the (fake) model
        self.assertEqual(len(knowledge.documents()), 1)     # persisted + reloadable

    def test_cosine_ranks_and_thresholds(self):
        knowledge.gateway.embed = _fake_embed
        knowledge.set_settings(embed_model="local-embed", threshold=0.5)
        knowledge.add_document("VPN", "renew the vpn certificate")
        knowledge.add_document("RDS", "investigate slow rds database queries")
        hits = knowledge.recall_knowledge("how do I renew the vpn cert")
        self.assertEqual([h["title"] for h in hits], ["VPN"])   # only the vpn doc clears cosine threshold

    def test_keyword_fallback_without_embeddings(self):
        knowledge.gateway.embed = lambda texts, model_name=None: None   # no embedding model
        knowledge.add_document("VPN runbook", "Renew the AWS client vpn certificate by endpoint recreation.")
        hits = knowledge.recall_knowledge("renew the client vpn certificate")
        self.assertTrue(hits and hits[0]["title"] == "VPN runbook")     # still retrievable by keyword
        self.assertEqual(knowledge.recall_knowledge("write a poem about cats"), [])   # unrelated -> nothing

    def test_reembed_recovers_docs_added_while_model_unreachable(self):
        # A doc added while the embed model was unreachable stores null vectors and can only be
        # retrieved by keyword (the failure the user hit: "New Relic" never matched "newrelic_operator").
        knowledge.set_settings(embed_model="local-embed")
        knowledge.gateway.embed = lambda texts, model_name=None: None      # model unreachable at add-time
        knowledge.add_document("RDS", "investigate slow rds database queries")
        self.assertEqual(knowledge.documents()[0]["embedded"], 0)          # nothing embedded

        knowledge.gateway.embed = _fake_embed                             # model comes back
        res = knowledge.reembed_all()
        self.assertEqual(res["embedded"], res["chunks"])                   # every chunk now embedded
        self.assertEqual(knowledge.documents()[0]["embedded"], res["chunks"])
        # and cosine retrieval now works where keyword would have missed
        hits = knowledge.recall_knowledge("problems with the database", threshold=0.5)
        self.assertEqual([h["title"] for h in hits], ["RDS"])

    def test_reembed_noop_without_model(self):
        knowledge.gateway.embed = _fake_embed
        knowledge.add_document("d", "some content about vpn")
        knowledge.set_settings(embed_model="")                            # keyword mode
        res = knowledge.reembed_all()
        self.assertEqual((res["embedded"], res["model"]), (0, None))       # no model -> no-op

    def test_context_block_is_char_bounded(self):
        knowledge.gateway.embed = lambda texts, model_name=None: None
        knowledge.add_document("big", "vpn certificate renewal " * 400)   # huge matching doc
        block = knowledge.context_block("vpn certificate renewal")
        self.assertIsNotNone(block)
        # _MAX_INJECT_CHARS caps the SNIPPET text only; the fixed header plus each hit's
        # "[title]\n" wrapper sit outside it, so allow the header + a small constant for those.
        self.assertLessEqual(len(block),
                             knowledge._MAX_INJECT_CHARS + len(knowledge._KB_HEADER) + 120)

    def test_empty_kb_and_blank_query(self):
        self.assertEqual(knowledge.recall_knowledge("anything"), [])      # empty KB
        knowledge.gateway.embed = lambda texts, model_name=None: None
        knowledge.add_document("d", "some content here about vpn")
        self.assertEqual(knowledge.recall_knowledge(""), [])              # blank query
        self.assertIsNone(knowledge.context_block(""))

    def test_injected_into_memory_context_only_with_request(self):
        # isolate the other stores so only knowledge is in play
        import tempfile as _t
        d = _t.mkdtemp(prefix="otto-kb-mem-")
        o_db = engine._DB
        engine._DB = os.path.join(d, "otto.db")
        try:
            knowledge.gateway.embed = lambda texts, model_name=None: None
            knowledge.add_document("VPN", "renew the client vpn certificate by endpoint recreation")
            ctx = engine._memory_context("how to renew the client vpn certificate")
            self.assertIn("Reference material", ctx)
            self.assertIn("possibly stale", ctx)   # a loaded doc is never framed as authoritative
            self.assertIsNone(engine._memory_context())   # no request (e.g. resume) -> no retrieval
        finally:
            engine._DB = o_db


class DeliveryTests(unittest.TestCase):
    def test_no_target(self):
        self.assertEqual(delivery.deliver(None, "r"), "no reply target")

    def test_unsupported_kind(self):
        self.assertIn("unsupported", delivery.deliver({"kind": "smoke-signal"}, "r"))

    def test_webhook_requires_url(self):
        self.assertIn("missing", delivery.deliver({"kind": "webhook"}, "r"))

    def test_github_issue_handled_and_never_raises(self):
        # No repo/number and no gh available -> graceful status string, not "unsupported", no raise.
        out = delivery.deliver({"kind": "github_issue"}, "the result")
        self.assertNotIn("unsupported", out)
        self.assertIsInstance(out, str)


class DeliveryBlockedTests(unittest.TestCase):
    """delivery._github_issue: blocked routing, idempotency marker, and body cap."""

    def _reply(self, **over):
        r = {"kind": "github_issue", "repo": "acme/w", "number": 7, "item_id": "I7",
             "project_id": "P", "status_field_id": "F",
             "status_options": {"Review": "rv", "Done": "dn", "Blocked": "bl"},
             "review_col": "Review", "done_col": "Done", "blocked_col": "Blocked"}
        r.update(over)
        return r

    def test_blocked_run_moves_to_blocked_column(self):
        import board
        moves = []
        orig_c, orig_m, orig_has = board.comment, board.set_status_raw, board.has_comment_marker
        board.comment = lambda repo, n, body: True
        board.has_comment_marker = lambda repo, n, marker: False
        board.set_status_raw = lambda pid, fid, iid, oid: moves.append(oid) or True
        try:
            out = delivery.deliver(self._reply(blocked=True, repo_edit=True), "r", run_id="gh-issue-7")
            self.assertEqual(moves, ["bl"])          # Blocked wins even when a PR (repo_edit) exists
            self.assertIn("Blocked", out)
        finally:
            board.comment, board.set_status_raw, board.has_comment_marker = orig_c, orig_m, orig_has

    def test_idempotent_marker_skips_duplicate_comment(self):
        import board
        posted = []
        orig_c, orig_m, orig_has = board.comment, board.set_status_raw, board.has_comment_marker
        board.comment = lambda repo, n, body: posted.append(body) or True
        board.has_comment_marker = lambda repo, n, marker: True    # already delivered on a retry
        board.set_status_raw = lambda pid, fid, iid, oid: True
        try:
            delivery.deliver(self._reply(), "the result", run_id="gh-issue-7")
            self.assertEqual(posted, [])             # not re-posted
        finally:
            board.comment, board.set_status_raw, board.has_comment_marker = orig_c, orig_m, orig_has

    def test_oversize_body_truncated(self):
        import board
        posted = []
        orig_c, orig_m, orig_has = board.comment, board.set_status_raw, board.has_comment_marker
        board.comment = lambda repo, n, body: posted.append(body) or True
        board.has_comment_marker = lambda repo, n, marker: False
        board.set_status_raw = lambda pid, fid, iid, oid: True
        try:
            delivery.deliver(self._reply(), "x" * 60_000)
            self.assertLessEqual(len(posted[0]), delivery._MAX_COMMENT + 200)
            self.assertIn("truncated", posted[0])
        finally:
            board.comment, board.set_status_raw, board.has_comment_marker = orig_c, orig_m, orig_has


class NtfyTests(unittest.TestCase):
    """delivery.notify (issue #92): pushes to the owner's ntfy topic on human-blocking
    transitions. Off (and silent) without OTTO_NTFY_TOPIC; NEVER raises on failure."""

    def setUp(self):
        import tempfile
        import types
        self._topic, self._url = config.NTFY_TOPIC, config.NTFY_URL
        self._on_complete = config.NTFY_ON_COMPLETE
        self._detail = config.NTFY_DETAIL
        self._actions_on = config.NTFY_ACTIONS
        self._urllib = delivery.urllib
        # A FRESH push store per test. The dedupe window is real state shared by every push in
        # the process, so without this one test's "Approval needed: x" silently swallows the
        # next test's identical one — the same way two runs would collide if the key ignored the
        # run id.
        self._state = delivery._STATE
        delivery._STATE = os.path.join(tempfile.mkdtemp(prefix="otto-ntfy-"), "notify-state.json")
        self.posts = []
        tests = self

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            tests.posts.append(json.loads(req.data))
            return _FakeResp()

        delivery.urllib = types.SimpleNamespace(
            request=types.SimpleNamespace(Request=self._urllib.request.Request,
                                          urlopen=fake_urlopen))

    def tearDown(self):
        config.NTFY_TOPIC, config.NTFY_URL = self._topic, self._url
        config.NTFY_ON_COMPLETE = self._on_complete
        config.NTFY_DETAIL = self._detail
        config.NTFY_ACTIONS = self._actions_on
        delivery.urllib = self._urllib
        delivery._STATE = self._state

    def test_noop_without_topic(self):
        config.NTFY_TOPIC = ""
        self.assertFalse(delivery.notify("t", lines=["b"]))
        self.assertEqual(self.posts, [])

    def test_posts_json_payload(self):
        config.NTFY_TOPIC = "my-secret-topic"
        self.assertTrue(delivery.notify("Approval needed: x", lines=["sre-minion · write"],
                                        tags=["warning"]))
        self.assertEqual(len(self.posts), 1)
        p = self.posts[0]
        self.assertEqual(p["topic"], "my-secret-topic")
        self.assertEqual(p["title"], "Approval needed: x")
        self.assertEqual(p["message"], "sre-minion · write")
        self.assertEqual(p["tags"], ["warning"])
        self.assertTrue(p["click"])                     # always carries a click-through URL

    def test_complete_kind_dropped_by_default(self):
        """Clean-finish pushes are opt-in (OTTO_NTFY_ON_COMPLETE): with a topic set but the
        flag off, kind="complete" is dropped — human-blocking pushes still go through."""
        config.NTFY_TOPIC = "my-secret-topic"
        config.NTFY_ON_COMPLETE = False
        self.assertFalse(delivery.notify("Otto finished: x", lines=["req"], kind="complete"))
        self.assertEqual(self.posts, [])
        self.assertTrue(delivery.notify("Approval needed: x", lines=["req"]))  # blocking: unaffected
        self.assertEqual(len(self.posts), 1)

    def test_complete_kind_sent_when_opted_in(self):
        config.NTFY_TOPIC = "my-secret-topic"
        config.NTFY_ON_COMPLETE = True
        self.assertTrue(delivery.notify("Otto finished: x", lines=["req"],
                                        kind="complete", priority="default"))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["title"], "Otto finished: x")
        self.assertEqual(self.posts[0]["priority"], 3)      # lower than the blocking pushes

    def test_never_raises_on_failure(self):
        import types
        config.NTFY_TOPIC = "t"

        def boom(req, timeout=None):
            raise OSError("network down")
        delivery.urllib = types.SimpleNamespace(
            request=types.SimpleNamespace(Request=self._urllib.request.Request, urlopen=boom))
        self.assertFalse(delivery.notify("t", lines=["b"]))   # False, not an exception

    def test_wid_appended_to_message(self):
        """When a workflow id is provided, append it as final line (issue #202)."""
        config.NTFY_TOPIC = "my-secret-topic"
        self.assertTrue(delivery.notify("Approval needed: x", lines=["sre-minion · write"],
                                        wid="web-test-1"))
        self.assertEqual(len(self.posts), 1)
        p = self.posts[0]
        self.assertIn("run: web-test-1", p["message"])
        self.assertEqual(p["message"], "sre-minion · write\n\nrun: web-test-1")

    def test_message_without_wid_unchanged(self):
        """When wid is not provided, message should be unchanged."""
        config.NTFY_TOPIC = "my-secret-topic"
        self.assertTrue(delivery.notify("Approval needed: x", lines=["sre-minion · write"]))
        self.assertEqual(len(self.posts), 1)
        p = self.posts[0]
        self.assertEqual(p["message"], "sre-minion · write")
        self.assertNotIn("run:", p["message"])

    # --- privacy: what may leave the machine on a public topic ------------------

    def test_request_content_is_dropped_unless_opted_in(self):
        """THE control. ntfy.sh is a third-party broker whose topic name is its only credential,
        so `detail` (the request/ticket/message text) never rides along by default — the run id
        plus the click URL take the owner to it in the local UI instead."""
        config.NTFY_TOPIC = "my-secret-topic"
        config.NTFY_DETAIL = False
        secret_ask = "Rotate the registry prod DB password, the old one is in vault"
        self.assertTrue(delivery.notify("Approval needed: sre-minion",
                                        lines=["sre-minion · write", "repo: infra"],
                                        detail=secret_ask, wid="web-1"))
        msg = self.posts[0]["message"]
        self.assertNotIn("registry", msg)
        self.assertNotIn("vault", msg)
        # …but it is still actionable: which cap, which repo, which run.
        self.assertIn("sre-minion · write", msg)
        self.assertIn("repo: infra", msg)
        self.assertIn("run: web-1", msg)

    def test_detail_included_and_clipped_when_opted_in(self):
        config.NTFY_TOPIC = "my-secret-topic"
        config.NTFY_DETAIL = True
        self.assertTrue(delivery.notify("t", lines=["cap"], detail="x" * 5000))
        msg = self.posts[0]["message"]
        self.assertIn("x" * 50, msg)
        self.assertLessEqual(len(msg), 800)

    def test_secrets_are_scrubbed_from_every_field(self):
        """The floor under the content rule: even opted-in detail, and even a title or a
        metadata line, is redacted — a capability name or a terminal `detail` is caller text."""
        config.NTFY_TOPIC = "my-secret-topic"
        config.NTFY_DETAIL = True
        self.assertTrue(delivery.notify(
            "failed: ghp_0123456789abcdefghijABCDEFGHIJ0123",
            lines=["token=supersecretvalue123"],
            detail="AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"))
        p = self.posts[0]
        self.assertNotIn("ghp_0123456789", p["title"])
        self.assertNotIn("supersecretvalue123", p["message"])
        self.assertNotIn("wJalrXUtnFEMIK", p["message"])

    def test_an_empty_push_still_says_something(self):
        """With no lines, no detail and no wid there'd be an empty message; ntfy renders that
        as a blank notification, so fall back to a pointer rather than sending nothing useful."""
        config.NTFY_TOPIC = "my-secret-topic"
        self.assertTrue(delivery.notify("Otto needs you", detail="private text"))
        self.assertEqual(self.posts[0]["message"], "(open Otto for details)")

    def test_free_text_body_parameter_no_longer_exists(self):
        """The old signature took the request as a positional `body`, which is how request[:250]
        of every run reached ntfy.sh. Everything after the title is keyword-only now, so that
        shape fails loudly instead of leaking quietly."""
        config.NTFY_TOPIC = "my-secret-topic"
        with self.assertRaises(TypeError):
            delivery.notify("Approval needed: x", "the raw request text")
        self.assertEqual(self.posts, [])

    # --- deep link -------------------------------------------------------------------------
    def test_a_push_about_a_run_deep_links_to_that_run(self):
        """The push's whole job is to get the owner to the run. Landing them on the home tab
        makes them hunt for the run they were just told about."""
        config.NTFY_TOPIC = "my-secret-topic"
        delivery.notify("Approval needed: x", lines=["cap"], wid="web-abc123")
        self.assertTrue(self.posts[0]["click"].endswith("#run=web-abc123"))

    def test_a_push_about_nothing_in_particular_lands_on_the_home_page(self):
        """The reaper's digest is about a SWEEP, not a run — a deep link would have to pick one
        of the runs it surfaced arbitrarily."""
        config.NTFY_TOPIC = "my-secret-topic"
        delivery.notify("Otto reaper: 3 stuck run(s) surfaced", lines=["Runs: 3 web"])
        self.assertNotIn("#run=", self.posts[0]["click"])

    def test_an_explicit_click_still_wins(self):
        config.NTFY_TOPIC = "my-secret-topic"
        delivery.notify("t", lines=["x"], wid="web-1", click="https://example.test/somewhere")
        self.assertEqual(self.posts[0]["click"], "https://example.test/somewhere")

    # --- dedupe ----------------------------------------------------------------------------
    def test_an_identical_push_within_the_window_is_dropped(self):
        """The blocking pushes are retried by Temporal now; a retry after a LOST result would
        otherwise ring the phone twice for one gate."""
        config.NTFY_TOPIC = "my-secret-topic"
        self.assertTrue(delivery.notify("Approval needed: x", lines=["cap"], wid="web-1"))
        self.assertFalse(delivery.notify("Approval needed: x", lines=["cap"], wid="web-1"))
        self.assertEqual(len(self.posts), 1)

    def test_the_same_title_for_a_DIFFERENT_run_is_not_a_duplicate(self):
        """Two runs of the same capability produce the same title. Keying on the title alone
        would silence the second run's gate entirely."""
        config.NTFY_TOPIC = "my-secret-topic"
        self.assertTrue(delivery.notify("Approval needed: sre-minion", lines=["c"], wid="web-1"))
        self.assertTrue(delivery.notify("Approval needed: sre-minion", lines=["c"], wid="web-2"))
        self.assertEqual(len(self.posts), 2)

    def test_a_FAILED_push_does_not_suppress_its_own_retry(self):
        """The dedupe claim is taken before the HTTP call, so it has to be released when that
        call fails — otherwise the guard swallows exactly the retry it was added alongside, and
        a transient ntfy failure silently costs the whole run."""
        import types
        config.NTFY_TOPIC = "my-secret-topic"
        calls = []

        def boom(req, timeout=None):
            calls.append(1)
            raise OSError("ntfy unreachable")

        delivery.urllib = types.SimpleNamespace(
            request=types.SimpleNamespace(Request=self._urllib.request.Request, urlopen=boom))
        self.assertFalse(delivery.notify("Approval needed: x", lines=["c"], wid="web-1"))
        self.assertFalse(delivery.notify("Approval needed: x", lines=["c"], wid="web-1"))
        self.assertEqual(len(calls), 2, "the retry was swallowed by the dedupe guard")

    # --- push health -----------------------------------------------------------------------
    def test_a_failed_blocking_push_is_recorded(self):
        """A push that silently fails is worse than none: the run parks at its gate, the phone
        stays quiet, and the gate deadline declines it a day later with nothing saying why."""
        import types
        config.NTFY_TOPIC = "my-secret-topic"

        def boom(req, timeout=None):
            raise OSError("ntfy unreachable")

        delivery.urllib = types.SimpleNamespace(
            request=types.SimpleNamespace(Request=self._urllib.request.Request, urlopen=boom))
        delivery.notify("Approval needed: x", lines=["c"], wid="web-1", kind="approval")
        health = delivery.health()
        self.assertFalse(health["ok"])
        self.assertIn("unreachable", health["error"])
        # ... and a later success clears it, so the badge tracks the CURRENT state.
        delivery.urllib = self._urllib
        delivery.notify("Approval needed: y", lines=["c"], wid="web-2", kind="approval")
        self.assertTrue(delivery.health()["ok"])

    def test_health_is_empty_when_pushes_are_off(self):
        config.NTFY_TOPIC = ""
        self.assertEqual(delivery.health(), {})

    # --- action tokens ---------------------------------------------------------------------
    def test_an_action_token_is_single_use(self):
        """The token rides on a third-party broker, so it is the narrowest grant that still
        works: one run, one use. A leaked topic must not re-approve a second gate the same run
        reaches after a plan revision."""
        token = delivery.mint_action_token("web-1")
        self.assertEqual(delivery.redeem_action_token(token), "web-1")
        self.assertIsNone(delivery.redeem_action_token(token))

    def test_an_expired_token_is_refused(self):
        token = delivery.mint_action_token("web-1", ttl_s=-1)
        self.assertIsNone(delivery.redeem_action_token(token))

    def test_an_unknown_token_is_refused(self):
        self.assertIsNone(delivery.redeem_action_token("not-a-real-token"))
        self.assertIsNone(delivery.redeem_action_token(""))

    def test_action_buttons_are_off_unless_opted_in(self):
        """They are only reachable through a tunnel, and the grant they carry is only as private
        as the topic name — so the default install must not ship them."""
        config.NTFY_TOPIC = "my-secret-topic"
        config.NTFY_ACTIONS = False
        delivery.notify("Approval needed: x", lines=["c"], wid="web-1",
                        actions=delivery.gate_actions("tok"))
        self.assertNotIn("actions", self.posts[0])
        config.NTFY_ACTIONS = True
        delivery.notify("Approval needed: x", lines=["c"], wid="web-2",
                        actions=delivery.gate_actions("tok"))
        acts = self.posts[1]["actions"]
        self.assertEqual([a["label"] for a in acts], ["Approve", "Deny"])
        self.assertTrue(all(a["url"].endswith("/api/gate/tok") for a in acts))
        # The run id is never in the URL — the token is what names the run, server-side.
        self.assertTrue(all("web-2" not in a["url"] for a in acts))


class NotificationSourceTests(unittest.TestCase):
    """privacy.source_line / context_lines: the always-sent half of a push. Names the run
    coarsely enough to be safe on a topic anyone who guesses the name can read."""

    def test_slack_source_is_not_identified_by_channel(self):
        """A channel id is a direct pointer into a private DM. "Slack" plus the run id already
        gets the owner to the right conversation in the local UI."""
        line = privacy.source_line({"kind": "slack_thread", "channel": "D06AHB0LZ3Q",
                                    "thread_ts": "1754300000.000100"})
        self.assertEqual(line, "source: Slack message")
        self.assertNotIn("D06AHB0LZ3Q", line)

    def test_github_issue_is_named(self):
        """The actionable half — and a repo name is a far smaller exposure than a ticket body."""
        self.assertEqual(
            privacy.source_line({"kind": "github_issue", "repo": "acme-corp/platform", "number": 342}),
            "source: GitHub issue acme-corp/platform#342")

    def test_webhook_url_is_never_included(self):
        """A webhook URL routinely carries a token in its path or query."""
        line = privacy.source_line({"kind": "webhook",
                                    "url": "https://hooks.example.com/t/sekrit-token-abc"})
        self.assertEqual(line, "source: webhook")
        self.assertNotIn("sekrit", line)

    def test_scheduled_run_without_a_reply_target(self):
        self.assertEqual(privacy.source_line(None, unattended=True), "source: scheduled run")
        self.assertIsNone(privacy.source_line(None))

    def test_context_lines_carry_only_ottos_own_vocabulary(self):
        lines = privacy.context_lines(cap={"name": "sre-minion", "risk": "write"}, repo="infra",
                                      reply_to={"kind": "github_issue", "repo": "acme-corp/platform",
                                                "number": 342},
                                      extra=["2 plan concern(s)"])
        self.assertEqual(lines, ["sre-minion · write", "repo: infra",
                                 "source: GitHub issue acme-corp/platform#342", "2 plan concern(s)"])

    def test_context_lines_redact_caller_supplied_extras(self):
        lines = privacy.context_lines(extra=["failed: token=hunter2superlong"])
        self.assertNotIn("hunter2superlong", lines[0])


class ScorecardTests(unittest.TestCase):
    """Pure per-capability aggregation over a fixture audit trail (issue #102, no LLM/IO)."""

    def _row(self, wid, cap, attempt, verified, model, cost=0.0, out=0,
             at="2026-07-15T10:00:00", fallback_from=None):
        r = {"workflow": wid, "capability": cap, "attempt": attempt, "verified": verified,
             "model": model, "cost_usd": cost, "at": at, "tokens": {"output": out}}
        if fallback_from:
            r["fallback_from"] = fallback_from
        return r

    def test_pass_rate_and_attempts_to_pass(self):
        # w1: passes on attempt 1 (clean). w2: fails a1, passes a2 (one retry).
        rows = [
            self._row("w1", "agent:sre-qa", 1, True, "claude-sonnet", cost=0.1, out=100),
            self._row("w2", "agent:sre-qa", 1, False, "claude-sonnet", cost=0.1, out=50),
            self._row("w2", "agent:sre-qa", 2, True, "claude-sonnet", cost=0.2, out=200),
        ]
        card = {c["capability"]: c for c in engine.scorecard(rows)}["agent:sre-qa"]
        self.assertEqual(card["runs"], 2)
        self.assertEqual(card["pass_rate"], 1.0)
        self.assertEqual(card["name"], "sre-qa")
        self.assertEqual(card["avg_attempts"], 1.5)          # (1 + 2) / 2
        self.assertEqual(card["avg_attempts_to_pass"], 1.5)  # passed on a1 and a2
        self.assertEqual(card["avg_cost_usd"], 0.2)          # (0.1 + 0.3) / 2
        self.assertEqual(card["avg_output_tokens"], 175)     # (100 + 250) / 2

    def test_escalation_and_fallback_flags(self):
        # Final attempt escalates to a different (stronger) model → escalated run.
        # A separate run's chosen local model fell back to Claude → fell_back run.
        rows = [
            self._row("w1", "agent:worker", 1, False, "claude-sonnet"),
            self._row("w1", "agent:worker", 2, True, "claude-opus"),
            self._row("w2", "agent:worker", 1, True, "claude-opus", fallback_from="qwen3.6"),
        ]
        card = engine.scorecard(rows)[0]
        self.assertEqual(card["runs"], 2)
        self.assertEqual(card["escalation_rate"], 0.5)   # only w1 changed model
        self.assertEqual(card["fallback_rate"], 0.5)     # only w2 fell back

    def test_failing_run_lowers_pass_rate(self):
        rows = [
            self._row("w1", "agent:x", 1, True, "m"),
            self._row("w2", "agent:x", 1, False, "m"),
            self._row("w2", "agent:x", 2, False, "m"),  # exhausted the ladder, never passed
        ]
        card = engine.scorecard(rows)[0]
        self.assertEqual(card["runs"], 2)
        self.assertEqual(card["pass_rate"], 0.5)
        self.assertEqual(card["avg_attempts_to_pass"], 1.0)  # only w1 passed, on attempt 1

    def test_a_post_PR_review_round_is_not_a_capability_failure(self):
        # The post-PR review loop audits each round as an attempt of the REVIEW capability, with
        # `verified` set only on an outright clean PASS. But judge_review's verdict is about THE
        # PR, and a review that correctly raises must-fix findings is a SUCCESSFUL review — so
        # counted as a verify verdict it books the reviewer as failing every time Otto's own PR
        # needed a fix. Measured on the live trail: 49 of github-pr-review's 100 recorded runs
        # were review rounds, and excluding them moved its pass rate 0.64 -> 0.78. It could never
        # self-correct either: a review round raises no needs-you card, so no human can Accept
        # one and `false_fails` stays 0, pointing the blame at the capability.
        rev = self._row("w1-rev0", "skill:github-pr-review", 1, False, "claude-sonnet")
        rev["verdict_source"] = "review"
        rows = [self._row("w1", "skill:github-pr-review", 1, True, "claude-sonnet"), rev]
        card = engine.scorecard(rows)[0]
        self.assertEqual(card["runs"], 1)          # the real task only
        self.assertEqual(card["pass_rate"], 1.0)
        self.assertEqual(card["used"], 2)          # but the round DID run, and still counts

    def test_a_legacy_post_PR_round_is_recognized_by_its_workflow_id(self):
        # The loops stamped no `verdict_source` until this change, so the 49 historical rounds
        # have no field to read. The correlate that survives on the row itself is the wid the
        # loop mints ("<run>-rev<N>" / "-qa<N>") — same retroactive trick as the supervisor-kill
        # set, and the same rule: only where the evidence is on the row.
        rows = [self._row("w1", "skill:github-pr-review", 1, True, "claude-sonnet"),
                self._row("w1-rev0", "skill:github-pr-review", 1, False, "claude-sonnet"),
                self._row("w1-rev1", "skill:github-pr-review", 2, False, "claude-sonnet"),
                self._row("w2-qa0", "agent:sre-qa", 1, False, "claude-sonnet")]
        cards = {c["capability"]: c for c in engine.scorecard(rows)}
        self.assertEqual(cards["skill:github-pr-review"]["runs"], 1)
        self.assertEqual(cards["skill:github-pr-review"]["pass_rate"], 1.0)
        self.assertEqual(cards["agent:sre-qa"]["runs"], 0)      # nothing judged it
        self.assertEqual(cards["agent:sre-qa"]["pass_rate"], None)

    def test_both_post_PR_loops_stamp_their_verdict_source(self):
        # The wid rule above is the RETROACTIVE half; this is the root fix. Each loop builds its
        # own verdict dict for record_attempt (judge_review/judge_qa return {verdict, critique},
        # not a source), so a source omitted here lands as a sourceless row that reads as a
        # verify verdict about the capability. Asserted on the source because the loops are
        # workflow code — there is no way to call one without a Temporal worker, and the mistake
        # is a missing key in a literal.
        body = open("workflows.py").read()
        for marker, source in (('"[review] ', "review"), ('"[QA] ', "qa")):
            i = body.index(marker)
            call = body[i:body.index('"remember": False', i)]
            self.assertIn('"passed": verdict["verdict"] == "pass"', call)
            self.assertIn(f'"source": "{source}"', call,
                          f'the {source} loop must stamp verdict_source, or its rounds are '
                          f'counted as capability failures')

    def test_a_fix_round_wid_is_not_mistaken_for_a_review_round(self):
        # "-revfix<N>" / "-fix<N>" are the FIX runs of the same loops. They record no verdict at
        # all, so they never reach the verdict filter — and the wid rule must not widen to catch
        # a real run whose id merely ends in digits.
        rows = [self._row("w1-revfix1", "custom:worker", 1, True, "claude-sonnet"),
                self._row("wf-0001-0002", "custom:worker", 1, True, "claude-sonnet")]
        card = engine.scorecard(rows)[0]
        self.assertEqual(card["runs"], 2)

    def test_human_accepted_run_counts_as_a_false_fail_not_a_cap_failure(self):
        # The Needs-you Accept button: the judges failed the run, a human overrode them. It stays
        # a miss for pass_rate (nothing passed verification) but is attributable to the JUDGE —
        # a low pass_rate with high false_fails means fix the verify/supervisor prompt, not the cap.
        rows = [
            self._row("w1", "agent:x", 1, True, "m"),
            self._row("w2", "agent:x", 1, False, "m"),
            self._row("w2", "agent:x", 2, False, "m"),
            {"workflow": "w2", "capability": "agent:x", "outcome": "human_accepted"},
        ]
        card = engine.scorecard(rows)[0]
        self.assertEqual(card["pass_rate"], 0.5)      # unchanged — it did not pass verification
        self.assertEqual(card["accepted"], 1)
        self.assertEqual(card["false_fails"], 1.0)    # the ONE failed run was a judge error
        self.assertTrue(next(r for r in card["recent"] if not r["passed"])["accepted"])

    def test_accepting_a_run_that_already_passed_is_not_a_false_fail(self):
        rows = [
            self._row("w1", "agent:x", 1, True, "m"),
            {"workflow": "w1", "capability": "agent:x", "outcome": "human_accepted"},
        ]
        card = engine.scorecard(rows)[0]
        self.assertEqual(card["accepted"], 0)
        self.assertEqual(card["false_fails"], 0.0)    # no failed runs at all -> no division by zero

    def test_unjudged_runs_are_excluded(self):
        # A resume/continuation attempt carries verified=None and must not count as a judged run.
        rows = [
            self._row("w1", "agent:x", 1, None, "m"),   # resume — no verdict
            self._row("w2", "agent:x", 1, True, "m"),
        ]
        card = engine.scorecard(rows)[0]
        self.assertEqual(card["runs"], 1)               # only w2
        self.assertEqual(card["used"], 2)               # but BOTH count as uses

    def test_supervisor_kill_is_not_a_verdict_about_the_capability(self):
        # A supervisor kill is recorded verified=False so the ladder can steer on it, but Otto
        # stopped the run — no judge read any output. Counting it made the cap wear Otto's own
        # intervention as a failure (44 of 291 recorded verify failures over the live trail).
        rows = [
            self._row("w1", "agent:x", 1, False, "m"),
            {"workflow": "w1", "capability": "agent:x", "attempt": 1,
             "outcome": "supervisor_kill"},
            self._row("w1", "agent:x", 2, True, "m"),
            self._row("w2", "agent:x", 1, False, "m"),
            {"workflow": "w2", "capability": "agent:x", "attempt": 1,
             "outcome": "supervisor_kill"},
        ]
        card = engine.scorecard(rows)[0]
        # w1 is a judged run (a2 passed). w2 was ONLY ever killed — no judgement exists, so it
        # cannot count as a run the capability failed.
        self.assertEqual(card["runs"], 1)
        self.assertEqual(card["pass_rate"], 1.0)
        self.assertEqual(card["used"], 2)          # both still count as uses

    def test_verdict_source_marks_a_harness_death_as_unjudged(self):
        # A timeout leaves no supervisor_kill row to correlate against, so the fix for those is
        # the explicit source recorded at write time.
        rows = [
            dict(self._row("w1", "agent:x", 1, False, "m"), verdict_source="harness"),
            dict(self._row("w2", "agent:x", 1, False, "m"), verdict_source="judge"),
        ]
        card = engine.scorecard(rows)[0]
        self.assertEqual(card["runs"], 1)          # only the judged one
        self.assertEqual(card["pass_rate"], 0.0)   # and it genuinely failed
        self.assertEqual(card["used"], 2)

    def test_rows_without_a_source_are_still_treated_as_judged(self):
        # Every row written before verdict_source existed carries none. They were judge verdicts
        # (that was the only kind recorded), so history must not silently re-rate itself.
        rows = [self._row("w1", "agent:x", 1, False, "m"),
                self._row("w2", "agent:x", 1, True, "m")]
        card = engine.scorecard(rows)[0]
        self.assertEqual((card["runs"], card["pass_rate"]), (2, 0.5))

    def test_used_counts_distinct_runs_not_attempts(self):
        # "How many times has this cap been used" is runs, not attempts — a 3-attempt ladder is
        # one use. Judged and unjudged runs both count.
        rows = [
            self._row("w1", "agent:x", 1, False, "m"),
            self._row("w1", "agent:x", 2, False, "m"),
            self._row("w1", "agent:x", 3, True, "m"),
            self._row("w2", "agent:x", 1, True, "m"),
            self._row("w3", "agent:y", 1, True, "m"),
        ]
        cards = {c["capability"]: c for c in engine.scorecard(rows)}
        self.assertEqual(cards["agent:x"]["used"], 2)
        self.assertEqual(cards["agent:y"]["used"], 1)

    def test_a_cap_whose_runs_were_all_unjudged_still_gets_a_used_count(self):
        # Every run a resume: it has no reliability figures at all, but it HAS been used — so it
        # must not vanish from the table, and its empty figures must be None rather than 0%
        # (a 0 pass rate reads as "this capability fails").
        rows = [self._row("w1", "agent:x", 1, None, "m", at="2026-07-15T10:00:00"),
                self._row("w2", "agent:x", 1, None, "m", at="2026-07-16T10:00:00")]
        card = engine.scorecard(rows)[0]
        self.assertEqual((card["used"], card["runs"]), (2, 0))
        self.assertIsNone(card["pass_rate"])
        self.assertIsNone(card["avg_attempts"])
        self.assertEqual(card["last_at"], "2026-07-16T10:00:00")   # last USED, judged or not

    def test_non_attempt_rows_ignored(self):
        # Terminal/guard rows carry no `attempt` and must be skipped entirely.
        rows = [
            {"workflow": "w1", "capability": "guard:in-place-edit", "outcome": "ran"},
            self._row("w2", "agent:x", 1, True, "m"),
        ]
        cards = {c["capability"]: c for c in engine.scorecard(rows)}
        self.assertNotIn("guard:in-place-edit", cards)
        self.assertIn("agent:x", cards)

