"""Otto unit tests — config, docs ratchets, routing and capabilities.

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
from unittest import mock
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


class AuthoredPlanTests(unittest.TestCase):
    """engine.run_plan executing a graph a HUMAN wrote rather than one plan_steps invented."""

    def setUp(self):
        # Stub the synthesis call like RunPlanTests does — otherwise every case here makes a real
        # gateway call to fold the step outputs together.
        self._complete, self._trace = engine.gateway.complete, engine.trace
        engine.gateway.complete = lambda task, prompt: "SYNTH"
        engine.trace = lambda *a, **k: None

    def tearDown(self):
        engine.gateway.complete, engine.trace = self._complete, self._trace

    def _cap(self, name="exec", risk="read"):
        c = registry.Capability("agent", name, "executes")
        c.risk = risk
        return c

    def _steps(self, *specs):
        return [{"id": sid, "goal": f"goal {sid}", "context": "", "needs": list(needs),
                 "produces": sid, "done_when": "", "cap": cap}
                for sid, cap, needs in specs]

    def _ladder(self, record, passed=True):
        def fake(request, cap, wid, **k):
            record.append((wid, cap.name, k.get("write_escalate")))
            return {"result": f"out-{cap.name}", "passed": passed, "critique": "nope",
                    "cost": 0, "tokens_out": 0, "attempts": 1}
        return fake

    def test_each_step_runs_on_its_own_capability(self):
        seen = []
        caps = {"vpn": self._cap("vpn"), "k8s": self._cap("k8s")}
        orig = engine._run_ladder
        engine._run_ladder = self._ladder(seen)
        try:
            engine.run_plan("task", self._cap("default"),
                            self._steps(("s1", "vpn", ()), ("s2", "k8s", ("s1",))),
                            wid="w1", replan=False, resolve_cap=lambda n: caps.get(n))
        finally:
            engine._run_ladder = orig
        self.assertEqual([c for _, c, _ in seen], ["vpn", "k8s"])

    def test_a_step_with_no_cap_of_its_own_runs_on_the_runs_cap(self):
        seen = []
        orig = engine._run_ladder
        engine._run_ladder = self._ladder(seen)
        try:
            engine.run_plan("task", self._cap("default"), self._steps(("s1", "", ())),
                            wid="w1", replan=False, resolve_cap=lambda n: None)
        finally:
            engine._run_ladder = orig
        self.assertEqual([c for _, c, _ in seen], ["default"])

    def test_an_unresolvable_step_cap_aborts_before_anything_runs(self):
        # Failing at step 7 of 8 has already spent the money and half-applied the work.
        seen = []
        orig = engine._run_ladder
        engine._run_ladder = self._ladder(seen)
        try:
            with self.assertRaises(ValueError) as cm:
                engine.run_plan("task", self._cap(),
                                self._steps(("s1", "", ()), ("s2", "ghost", ("s1",))),
                                wid="w1", replan=False, resolve_cap=lambda n: None)
        finally:
            engine._run_ladder = orig
        self.assertIn("ghost", str(cm.exception))
        self.assertEqual(seen, [])                 # nothing ran

    def test_a_human_authored_graph_is_never_rewritten_by_a_replan(self):
        # The steps ARE the intent; substituting LLM-invented ones would deliver something the
        # author never approved.
        replans = []
        orig_ladder, orig_replan = engine._run_ladder, engine.replan_steps
        engine._run_ladder = self._ladder([], passed=False)
        engine.replan_steps = lambda *a, **k: replans.append(a) or []
        try:
            out = engine.run_plan("task", self._cap(), self._steps(("s1", "", ()), ("s2", "", ("s1",))),
                                  wid="w1", replan=False)
        finally:
            engine._run_ladder, engine.replan_steps = orig_ladder, orig_replan
        self.assertEqual(replans, [])              # the planner was never consulted
        self.assertFalse(out["passed"])            # it stops for a human instead
        self.assertEqual(out["replans"], 0)

    def test_an_llm_authored_plan_still_replans(self):
        # The counterpart: the existing behaviour must be untouched when nobody authored it.
        calls = []
        orig_ladder, orig_replan = engine._run_ladder, engine.replan_steps
        engine._run_ladder = self._ladder([], passed=False)
        engine.replan_steps = lambda *a, **k: calls.append(1) or []
        try:
            engine.run_plan("task", self._cap(), self._steps(("s1", "", ()), ("s2", "", ("s1",))),
                            wid="w1")
        finally:
            engine._run_ladder, engine.replan_steps = orig_ladder, orig_replan
        self.assertEqual(len(calls), 1)

    def test_escalation_is_on_exactly_when_replan_is_off(self):
        # With no tail repair coming, escalating the model IS the recovery — and vice versa, an
        # LLM plan keeps its steps local and re-plans instead (issue #172).
        for replan, expected in ((False, True), (True, False)):
            seen = []
            orig = engine._run_ladder
            engine._run_ladder = self._ladder(seen)
            try:
                engine.run_plan("t", self._cap(), self._steps(("s1", "", ())), wid="w1",
                                replan=replan)
            finally:
                engine._run_ladder = orig
            self.assertEqual(seen[0][2], expected, f"replan={replan}")


class RiskTests(unittest.TestCase):
    def test_known_overrides(self):
        self.assertEqual(registry.classify("sre-minion", ""), "write")
        self.assertEqual(registry.classify("board-status", ""), "read")

    def test_write_hint(self):
        self.assertEqual(registry.classify("xyz", "create a new thing"), "write")

    def test_read_hint(self):
        self.assertEqual(registry.classify("xyz", "status overview report"), "read")

    def test_unknown_defaults_to_write(self):
        # neutral, unrecognised -> gated (safe default)
        self.assertEqual(registry.classify("frobnicate", "does a thing"), "write")


class ScoreTests(unittest.TestCase):
    def test_pasted_url_tokens_do_not_inflate_score(self):
        # A pasted build URL's path segments must not be counted as keyword hits, or a
        # topic-matching read cap gets unfairly boosted over the cap that does the action.
        cap = registry.Capability("skill", "terraform-reviewer",
                                  "reviews terraform infrastructure plan changes")
        with_url = ("create a ticket "
                    "https://ci.example.com/buildConfiguration/Deploy_Infrastructure_TerraformPlan")
        without = "create a ticket"
        self.assertEqual(cap.score(with_url), cap.score(without))

    def test_real_words_still_score(self):
        cap = registry.Capability("skill", "x", "terraform infrastructure plan review")
        self.assertGreater(cap.score("review the terraform plan"), 0)


class FrontmatterTests(unittest.TestCase):
    def test_folded_description(self):
        text = "---\nname: foo\ndescription: >\n  hello\n  world\nmodel: sonnet\n---\nbody"
        fm = registry._parse_frontmatter(text)
        self.assertEqual(fm["name"], "foo")
        self.assertIn("hello world", fm["description"])

    def test_no_frontmatter(self):
        self.assertEqual(registry._parse_frontmatter("just text"), {})


class AssistantWriteRedirectTests(unittest.TestCase):
    """The write-intent guard on the general assistant must REDIRECT to the general worker,
    not just bump risk — the assistant's prompt forbids acting, so a gated assistant run would
    still refuse the task (the fresh-install 'asked for permission conversationally' failure)."""

    def _caps(self, worker_enabled=True):
        a, w = registry._general_assistant(), registry._general_worker()
        w.enabled = worker_enabled
        return a, w, [a, w]

    def test_assistant_redirects_to_enabled_worker(self):
        a, w, caps = self._caps()
        self.assertIs(engine.assistant_write_redirect(a, caps), w)

    def test_non_assistant_read_cap_keeps_plain_risk_bump(self):
        _, _, caps = self._caps()
        cli = registry.Capability("skill", "ci-cli", "reads builds")
        self.assertIsNone(engine.assistant_write_redirect(cli, caps))

    def test_no_redirect_when_worker_disabled_or_missing(self):
        a, _, caps = self._caps(worker_enabled=False)
        self.assertIsNone(engine.assistant_write_redirect(a, caps))
        self.assertIsNone(engine.assistant_write_redirect(a, [a]))
        self.assertIsNone(engine.assistant_write_redirect(None, caps))


class DoctorTests(unittest.TestCase):
    """Environment doctor (portability): the catalogue/config checks are pure — they must flag
    the silent fresh-install degradations (stock-only catalogue, unresolvable loop caps)."""

    def _cap(self, name, source="builtin", enabled=True, general=False):
        c = registry.Capability("agent", name, "does things")
        c.source, c.enabled, c.general = source, enabled, general
        return c

    def _full(self):
        import doctor
        loops = [self._cap(n, source="stock") for n in
                 (config.QA_CAP, config.REVIEW_CAP, config.WORKER_CAP)]
        return doctor, loops

    def test_catalogue_empty_fails_and_stock_only_warns(self):
        doctor, loops = self._full()
        self.assertEqual(doctor.check_catalogue([])["status"], "fail")
        stock_only = loops + [self._cap("assistant", source="stock", general=True)]
        c = doctor.check_catalogue(stock_only)
        self.assertEqual(c["status"], "warn")
        self.assertIn("~/.claude", c["hint"])            # the fix is actionable, not just a flag
        with_user = stock_only + [self._cap("sre-minion")]
        self.assertEqual(doctor.check_catalogue(with_user)["status"], "ok")

    def test_config_caps_resolve_or_warn(self):
        doctor, loops = self._full()
        self.assertEqual(doctor.check_config_caps(loops)["status"], "ok")
        missing = [c for c in loops if c.name != config.REVIEW_CAP]
        c = doctor.check_config_caps(missing)
        self.assertEqual(c["status"], "warn")
        self.assertIn(config.REVIEW_CAP, c["detail"])
        disabled = list(loops)
        disabled[0].enabled = False
        self.assertEqual(doctor.check_config_caps(disabled)["status"], "warn")

    def test_models_check_probes_only_used_local_models(self):
        import doctor
        probed = []

        class FakeGateway:
            @staticmethod
            def load():
                return {"pool": [{"name": "claude-sonnet", "provider": "claude"},
                                 {"name": "qwen", "provider": "openai", "base_url": "http://x"},
                                 {"name": "idle-local", "provider": "openai", "base_url": "http://y"}],
                        "assign": {"routing": "qwen", "execution": "claude-sonnet"},
                        "cap_local_exec": {}}

            @staticmethod
            def exec_model_entry(cap_name=None, cfg=None):
                return {"name": "claude-sonnet", "provider": "claude"}

            @staticmethod
            def test_model(name):
                probed.append(name)
                return {"ok": False, "detail": "connection refused"}
        c = doctor.check_models(FakeGateway)
        self.assertEqual(probed, ["qwen"])               # idle-local isn't assigned anywhere
        self.assertEqual(c["status"], "warn")
        self.assertIn("qwen", c["detail"])

    def test_summary_counts(self):
        import doctor
        checks = [doctor._check("a", "ok", ""), doctor._check("b", "warn", ""),
                  doctor._check("c", "fail", "")]
        self.assertEqual(doctor.summary(checks), {"fails": 1, "warns": 1})

    def test_exec_tool_call_probe_flags_a_rejecting_local_server(self):
        # The Mistral-24b-on-vLLM failure: reachable server, tools param rejected -> every
        # execution silently on Claude. The doctor must say so; a Claude exec never probes.
        import doctor
        probed = []
        orig = doctor._probe_tool_calls
        doctor._probe_tool_calls = lambda m, gw: (probed.append(m["name"]) or
                                                  (False, "--enable-auto-tool-choice missing"))

        class LocalExec:
            @staticmethod
            def load():
                return {}

            @staticmethod
            def exec_model_entry(cap_name=None, cfg=None):
                return {"name": "mistral-24b", "provider": "openai", "base_url": "http://x",
                        "model": "mistral"}

        class ClaudeExec(LocalExec):
            @staticmethod
            def exec_model_entry(cap_name=None, cfg=None):
                return {"name": "claude-sonnet", "provider": "claude"}
        try:
            c = doctor.check_exec_tool_calls(LocalExec)
            self.assertEqual(c["status"], "warn")
            self.assertIn("re-dispatches to Claude", c["detail"])
            self.assertIn("--enable-auto-tool-choice", c["hint"])
            self.assertEqual(probed, ["mistral-24b"])
            self.assertEqual(doctor.check_exec_tool_calls(ClaudeExec)["status"], "ok")
            self.assertEqual(probed, ["mistral-24b"])      # claude exec never probes
        finally:
            doctor._probe_tool_calls = orig


class StockCapabilityTests(unittest.TestCase):
    """Capabilities bundled with Otto (capabilities/*.md), loaded as source='stock'."""

    def test_bundled_files_discovered_as_stock_custom_caps(self):
        stock = {name: (desc, body, path, tier)
                 for name, desc, body, path, tier, _k in registry.stock_caps()}
        # The bundled tier must be present with a real body and description.
        for name in ("product-manager", "qa-tester", "code-reviewer"):
            self.assertIn(name, stock, f"{name}.md should ship in capabilities/bundled/")
            desc, body, path, tier = stock[name]
            self.assertEqual(tier, "bundled")
            self.assertTrue(desc.strip(), "description parsed from frontmatter")
            self.assertNotIn("---", body[:4], "frontmatter must be stripped from the body")
            self.assertTrue(body.strip())
        # The optional catalog is discovered too, tagged with its tier.
        for name in ("researcher", "technical-writer"):
            self.assertIn(name, stock, f"{name}.md should ship in capabilities/optional/")
            self.assertEqual(stock[name][3], "optional")

    def test_load_tags_stock_caps_and_makes_them_gated_custom(self):
        by_name = {c.name: c for c in registry.load()}
        for name in ("product-manager", "qa-tester", "technical-writer"):
            c = by_name[name]
            self.assertEqual(c.source, "stock")
            self.assertEqual(c.kind, "custom")            # runs as an inlined prompt, not a subagent
            self.assertEqual(c.risk, "write")             # all mutate → the approval gate fires
            self.assertIn("{request}", c.prompt or "")    # invocation substitutes the request
            self.assertTrue(c.path)                        # local runtime can inline the source
        self.assertEqual(by_name["code-reviewer"].risk, "read")   # strict read-only reviewer

    def test_product_manager_is_scoped_to_management_not_implementation(self):
        # Both the router-facing description AND the executing prompt must carry the scope
        # boundary — the description steers Router #1, the body stops a misrouted PM from
        # implementing the ticket itself (PR #194/#195).
        stock = {name: (desc, body) for name, desc, body, _p, _t, _k in registry.stock_caps()}
        desc, body = stock["product-manager"]
        self.assertIn("never implements", desc.lower())
        self.assertIn("you never implement them", body.lower())
        self.assertIn("never run `git`", body.lower())

    def test_review_cap_default_resolves_to_a_bundled_cap(self):
        # Portability: the review loop's default must be self-contained on a fresh install,
        # never a user ~/.claude cap (the old github-pr-review default was unresolvable there).
        by_name = {c.name: c for c in registry.load()}
        self.assertIn(config.REVIEW_CAP, by_name)
        self.assertEqual(by_name[config.REVIEW_CAP].source, "stock")
        self.assertEqual(by_name[config.REVIEW_CAP].tier, "bundled")

    def test_optional_tier_defaults_disabled_until_policy_enables(self):
        caps = [c for c in registry.load() if c.source == "stock"]
        registry.apply_policy(caps, {})
        by_name = {c.name: c for c in caps}
        self.assertFalse(by_name["researcher"].enabled)            # opt-in catalog: default OFF
        self.assertTrue(by_name["code-reviewer"].enabled)          # bundled: default ON
        self.assertTrue(by_name["qa-tester"].enabled)
        # A plain `enabled` policy override (the Admin toggle) opts an optional cap in.
        registry.apply_policy(caps, {"capabilities": {"researcher": {"enabled": True}}})
        self.assertTrue(by_name["researcher"].enabled)

    def test_user_home_cap_of_same_name_wins_over_stock(self):
        # Precedence: a user ~/.claude cap shadows the bundled stock copy (first-wins de-dupe).
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, "qa-tester.md"), "w") as f:
            f.write("---\nname: qa-tester\ndescription: user override\n---\nbody\n")
        empty = os.path.join(tmp, "none")
        with _patched_registry_dirs(agents=tmp, skills=empty, plugins=empty, custom=empty, projects=empty):
            by_name = {c.name: c for c in registry.load()}
        c = by_name["qa-tester"]
        self.assertEqual(c.kind, "agent")                  # the user's agent wins…
        self.assertNotEqual(c.source, "stock")             # …not the bundled stock cap
        # product-manager has no user override, so the stock copy still surfaces.
        self.assertEqual(by_name["product-manager"].source, "stock")


class ClarifyParseTests(unittest.TestCase):
    """Pure parse of the clarify-tier reply (no LLM). Biased toward proceeding so a weak local
    model on the clarify tier doesn't dead-end a clear request in awaiting_clarification."""

    def test_ok_means_no_question(self):
        self.assertIsNone(engine._parse_clarification("OK"))
        self.assertIsNone(engine._parse_clarification("ok, this is clear"))
        self.assertIsNone(engine._parse_clarification(""))
        self.assertIsNone(engine._parse_clarification(None))

    def test_declarative_affirmations_proceed(self):
        # No '?' -> not a real question, even if the model didn't say the literal OK.
        self.assertIsNone(engine._parse_clarification("The request is clear enough to proceed."))
        self.assertIsNone(engine._parse_clarification("No clarification needed."))
        self.assertIsNone(engine._parse_clarification("None."))

    def test_strips_leaked_reasoning(self):
        # qwen/gemma reasoning leak: <think> block then an OK verdict must still proceed.
        self.assertIsNone(engine._parse_clarification(
            "<think>The user gave a PR URL, that's enough.</think>OK"))
        # …and a leaked think block with no real question proceeds too.
        self.assertIsNone(engine._parse_clarification(
            "<think>Hmm, should I ask which files?</think>The request looks complete."))

    def test_real_question_is_kept(self):
        q = engine._parse_clarification("Which environment should I target, dev or staging?")
        self.assertEqual(q, "Which environment should I target, dev or staging?")
        # …even after a reasoning block is stripped.
        self.assertEqual(
            engine._parse_clarification("<think>ambiguous env</think>Which environment?"),
            "Which environment?")

    def test_unwraps_fenced_reply(self):
        self.assertIsNone(engine._parse_clarification("```\nOK\n```"))


class DecomposeTests(unittest.TestCase):
    """The planner end-to-end with the model call stubbed — maps indices back to caps,
    dedupes, and only fans out at 2+ independent sub-tasks."""

    def setUp(self):
        self._orig = engine.gateway.complete
        self._trace = engine.trace
        engine.trace = lambda *a, **k: None

    def tearDown(self):
        engine.gateway.complete = self._orig
        engine.trace = self._trace

    def _caps(self):
        return [registry.Capability("skill", "ci-cli", "read CI builds and logs"),
                registry.Capability("skill", "github-issue", "create a GitHub issue"),
                registry.Capability("skill", "slack-maintenance-thread", "post to Slack")]

    def test_fans_out_into_mapped_caps(self):
        engine.gateway.complete = lambda task, prompt: "0: check the build\n1: open a ticket"
        tasks = engine.decompose("check the build and open a ticket", self._caps())
        self.assertEqual([t["cap"].name for t in tasks], ["ci-cli", "github-issue"])
        self.assertEqual(tasks[0]["request"], "check the build")

    def test_single_task_returns_empty(self):
        engine.gateway.complete = lambda task, prompt: "SINGLE"
        self.assertEqual(engine.decompose("just check the build", self._caps()), [])

    def test_a_lone_subtask_is_not_a_swarm(self):
        # One parseable line isn't a fan-out — fall back to the single-capability path.
        engine.gateway.complete = lambda task, prompt: "1: open a ticket"
        self.assertEqual(engine.decompose("open a ticket", self._caps()), [])

    def test_dedupes_identical_subtasks(self):
        engine.gateway.complete = lambda task, prompt: "1: open a ticket\n1: open a ticket\n2: post to slack"
        tasks = engine.decompose("x", self._caps())
        self.assertEqual([t["cap"].name for t in tasks], ["github-issue", "slack-maintenance-thread"])

    def test_no_fanout_when_catalogue_too_small(self):
        engine.gateway.complete = lambda task, prompt: "0: a\n0: b"
        self.assertEqual(engine.decompose("x", self._caps()[:1]), [])


class MergeTests(unittest.TestCase):
    """Swarm result synthesis (the model call stubbed)."""

    def setUp(self):
        self._orig = engine.gateway.complete
        self._trace = engine.trace
        engine.trace = lambda *a, **k: None

    def tearDown(self):
        engine.gateway.complete = self._orig
        engine.trace = self._trace

    def test_single_part_passes_through_without_a_call(self):
        engine.gateway.complete = lambda task, prompt: self.fail("should not call the model")
        self.assertEqual(engine.merge("req", [{"cap": "x", "request": "y", "result": "done"}]), "done")

    def test_empty_parts(self):
        self.assertEqual(engine.merge("req", []), "(no sub-task results)")

    def test_synthesizes_multiple_parts(self):
        seen = {}
        def fake(task, prompt):
            seen["task"], seen["prompt"] = task, prompt
            return "combined answer"
        engine.gateway.complete = fake
        out = engine.merge("do A and B", [{"cap": "ca", "request": "A", "result": "ra"},
                                          {"cap": "cb", "request": "B", "result": "rb"}])
        self.assertEqual(out, "combined answer")
        self.assertEqual(seen["task"], "verify")        # merge runs on the verify tier
        self.assertIn("ra", seen["prompt"]); self.assertIn("rb", seen["prompt"])


class RunPlanTests(unittest.TestCase):
    """The sequential plan executor with the per-step ladder, re-plan, and synthesis stubbed:
    runs steps in order, threads each output forward, re-plans a failed step (design #3), and
    stops (incomplete) only when a failure can't be repaired within the re-plan budget."""

    def setUp(self):
        self._ladder = engine._run_ladder
        self._replan = engine.replan_steps
        self._complete = engine.gateway.complete
        self._trace = engine.trace
        self._max = config.PLAN_MAX_REPLANS
        self._par = config.PLAN_MAX_PARALLEL
        engine.trace = lambda *a, **k: None
        engine.gateway.complete = lambda task, prompt: "SYNTH: " + prompt[-40:]

    def tearDown(self):
        engine._run_ladder = self._ladder
        engine.replan_steps = self._replan
        engine.gateway.complete = self._complete
        engine.trace = self._trace
        config.PLAN_MAX_REPLANS = self._max
        config.PLAN_MAX_PARALLEL = self._par

    def _cap(self):
        return registry.Capability("agent", "worker", "impl")

    def _step(self, sid, needs=None):
        return {"id": sid, "goal": f"do {sid}", "context": "", "needs": needs or [],
                "produces": sid, "done_when": ""}

    def _steps(self):
        return [self._step("s1"), self._step("s2", needs=["s1"])]

    def test_runs_in_order_and_threads_output(self):
        seen = []
        def fake_ladder(req, cap, wid, recall=False, project=None, remember=True,
                        write_escalate=True, memory_enabled=True, model_override=None):
            seen.append((wid, req, write_escalate))
            return {"result": f"result-of-{wid}", "passed": True, "critique": None,
                    "cost": 0, "tokens_out": 0, "attempts": 1}
        engine._run_ladder = fake_ladder
        out = engine.run_plan("big task", self._cap(), self._steps(), wid="w1")
        self.assertEqual([w for w, _, _ in seen], ["w1-s1", "w1-s2"])  # in order, step-scoped wids
        self.assertIn("result-of-w1-s1", seen[1][1])                 # s2's prompt carries s1's output
        # Plan-mode steps stay LOCAL (re-plan on exhaustion, not escalate execution) — issue #172.
        self.assertTrue(all(we is False for _, _, we in seen))
        self.assertTrue(out["result"].startswith("SYNTH:"))
        self.assertTrue(out["passed"])                               # both steps passed
        self.assertEqual(out["steps_run"], 2)

    def test_replans_and_recovers_from_a_failed_step(self):
        # s2 fails its ladder; every other step (incl. the repaired r1) passes.
        engine._run_ladder = lambda req, cap, wid, **k: {
            "result": "r", "passed": not wid.endswith("s2"),
            "critique": "too big", "cost": 0, "tokens_out": 0, "attempts": 3}
        replans = {"n": 0}
        def fake_replan(request, cap, done, failed, critique, pending):
            replans["n"] += 1
            return [self._step("r1")]        # a repaired tail that will pass
        engine.replan_steps = fake_replan
        out = engine.run_plan("t", self._cap(), self._steps(), wid="w1")
        self.assertEqual(replans["n"], 1)
        self.assertNotIn("incomplete", out["result"].lower())   # s2 superseded by r1 -> not incomplete
        self.assertTrue(out["passed"])

    def test_stops_incomplete_when_replan_budget_spent(self):
        config.PLAN_MAX_REPLANS = 2
        engine._run_ladder = lambda req, cap, wid, **k: {
            "result": "r", "passed": False, "critique": "nope",
            "cost": 0, "tokens_out": 0, "attempts": 3}      # everything fails
        calls = {"n": 0}
        def fake_replan(*a, **k):
            calls["n"] += 1
            return [self._step(f"r{calls['n']}")]           # always offers a (doomed) repair
        engine.replan_steps = fake_replan
        out = engine.run_plan("t", self._cap(), self._steps(), wid="w1")
        self.assertEqual(calls["n"], 2)                     # re-planned exactly PLAN_MAX_REPLANS times
        self.assertIn("incomplete", out["result"].lower())
        self.assertFalse(out["passed"])

    def test_stops_incomplete_when_replan_cannot_salvage(self):
        engine._run_ladder = lambda req, cap, wid, **k: {
            "result": "r", "passed": wid.endswith("s1"), "critique": "x",
            "cost": 0, "tokens_out": 0, "attempts": 3}
        engine.replan_steps = lambda *a, **k: []            # planner gives up
        out = engine.run_plan("t", self._cap(), self._steps(), wid="w1")
        self.assertIn("incomplete", out["result"].lower())
        self.assertFalse(out["passed"])

    def test_independent_steps_run_concurrently_in_one_wave(self):
        # Two steps with no interdependency form one dependency wave and must run AT THE SAME TIME:
        # a barrier of 2 (short timeout) only clears if both ladders are in-flight together.
        import threading
        config.PLAN_MAX_PARALLEL = 3
        active, lock, barrier = {"now": 0, "max": 0}, threading.Lock(), threading.Barrier(2, timeout=5)

        def fake_ladder(req, cap, wid, **k):
            with lock:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            with lock:
                active["now"] -= 1
            return {"result": f"r-{wid}", "passed": True, "critique": None,
                    "cost": 1, "tokens_out": 2, "attempts": 1}
        engine._run_ladder = fake_ladder
        out = engine.run_plan("t", self._cap(), [self._step("a"), self._step("b")], wid="w1")
        self.assertEqual(active["max"], 2)             # both were in-flight simultaneously
        self.assertTrue(out["passed"])
        self.assertEqual(out["steps_run"], 2)
        self.assertEqual(out["cost"], 2)               # cost/tokens accumulate across the wave
        self.assertEqual(out["tokens"]["output"], 4)

    def test_max_parallel_one_is_fully_sequential(self):
        # PLAN_MAX_PARALLEL=1 collapses every wave to a single step (the old behaviour / escape hatch).
        config.PLAN_MAX_PARALLEL = 1
        order = []
        engine._run_ladder = lambda req, cap, wid, **k: (order.append(wid) or {
            "result": "r", "passed": True, "critique": None, "cost": 0, "tokens_out": 0, "attempts": 1})
        engine.run_plan("t", self._cap(), [self._step("a"), self._step("b")], wid="w1")
        self.assertEqual(order, ["w1-a", "w1-b"])      # one at a time, in plan order

    def test_diamond_threads_both_parents_into_the_join(self):
        # s1 -> {s2, s3} -> s4: the join step must receive BOTH parents' outputs, proving wave
        # selection walks the DAG (not a flat list) and results integrate in plan order.
        config.PLAN_MAX_PARALLEL = 3
        seen = {}
        def fake_ladder(req, cap, wid, **k):
            seen[wid] = req
            return {"result": f"out-{wid.split('-')[-1]}", "passed": True, "critique": None,
                    "cost": 0, "tokens_out": 0, "attempts": 1}
        engine._run_ladder = fake_ladder
        steps = [self._step("s1"), self._step("s2", needs=["s1"]),
                 self._step("s3", needs=["s1"]), self._step("s4", needs=["s2", "s3"])]
        out = engine.run_plan("t", self._cap(), steps, wid="w1")
        self.assertEqual(out["steps_run"], 4)
        self.assertTrue(out["passed"])
        self.assertIn("out-s2", seen["w1-s4"])
        self.assertIn("out-s3", seen["w1-s4"])

    def test_multi_failure_wave_stops_for_a_human(self):
        # Two independent steps failing in the SAME wave is unrepairable-in-parallel -> needs-human,
        # and the single-failure re-plan path must NOT be entered.
        config.PLAN_MAX_PARALLEL = 3
        engine._run_ladder = lambda req, cap, wid, **k: {
            "result": "r", "passed": False, "critique": "no", "cost": 0, "tokens_out": 0, "attempts": 3}
        replans = {"n": 0}
        engine.replan_steps = lambda *a, **k: replans.__setitem__("n", replans["n"] + 1) or [self._step("r1")]
        out = engine.run_plan("t", self._cap(), [self._step("a"), self._step("b")], wid="w1")
        self.assertEqual(replans["n"], 0)              # never re-planned a multi-failure wave
        self.assertFalse(out["passed"])
        self.assertIn("incomplete", out["result"].lower())


class InvocationTests(unittest.TestCase):
    def _cap(self, kind, name, prompt=None):
        c = registry.Capability(kind, name, "desc")
        c.prompt = prompt
        return c

    def test_skill(self):
        self.assertEqual(engine._invocation(self._cap("skill", "board-status"), "hi"),
                         "/board-status hi")

    def test_agent(self):
        inv = engine._invocation(self._cap("agent", "sre-minion"), "do it")
        self.assertIn("sre-minion subagent", inv)
        self.assertIn("do it", inv)

    def test_agent_invocation_forbids_background_framing(self):
        # Regression: the coordinator once reported a finished subagent run as "running in the
        # background … I'll relay when it completes", freezing the chat on a false in-progress
        # status. The single-turn contract must ride along on every agent invocation.
        inv = engine._invocation(self._cap("agent", "sre-minion"), "do it")
        self.assertIn("single-turn", inv)
        self.assertIn("background", inv)
        # Skills are not coordinator-wrapped, so they stay a plain slash invocation.
        self.assertNotIn("single-turn", engine._invocation(self._cap("skill", "board-status"), "hi"))

    def test_custom_template(self):
        inv = engine._invocation(self._cap("custom", "sum", "Summarize:\n{request}"), "PR 1")
        self.assertEqual(inv, "Summarize:\nPR 1")

    def test_custom_append_when_no_placeholder(self):
        inv = engine._invocation(self._cap("custom", "sum", "Summarize this"), "PR 1")
        self.assertIn("Summarize this", inv)
        self.assertIn("PR 1", inv)


class BudgetTests(unittest.TestCase):
    """Per-run cost budget predicate (config.budget_exceeded). 0 knobs disable a meter."""

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(config, k, v)

    def setUp(self):
        self._saved = {k: getattr(config, k) for k in
                       ("BUDGET_SOFT_TOKENS", "BUDGET_HARD_TOKENS", "BUDGET_SOFT_USD", "BUDGET_HARD_USD")}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)

    def test_disabled_by_default_zero(self):
        self._set(BUDGET_HARD_TOKENS=0, BUDGET_SOFT_TOKENS=0, BUDGET_HARD_USD=0, BUDGET_SOFT_USD=0)
        self.assertFalse(config.budget_exceeded(10**9, 10**9, hard=True))
        self.assertFalse(config.budget_exceeded(10**9, 10**9, hard=False))

    def test_hard_token_ceiling(self):
        self._set(BUDGET_HARD_TOKENS=1000, BUDGET_SOFT_TOKENS=500, BUDGET_HARD_USD=0, BUDGET_SOFT_USD=0)
        self.assertFalse(config.budget_exceeded(999, 0, hard=True))
        self.assertTrue(config.budget_exceeded(1000, 0, hard=True))
        self.assertTrue(config.budget_exceeded(600, 0, hard=False))    # soft threshold

    def test_usd_meter(self):
        self._set(BUDGET_HARD_TOKENS=0, BUDGET_SOFT_TOKENS=0, BUDGET_HARD_USD=5.0, BUDGET_SOFT_USD=0)
        self.assertTrue(config.budget_exceeded(0, 5.0, hard=True))
        self.assertFalse(config.budget_exceeded(0, 4.99, hard=True))


class LocalInvocationTests(unittest.TestCase):
    """_local_invocation: the local runtime can't resolve /skill or subagents, so the cap's
    own markdown is inlined (frontmatter stripped); custom caps keep their prompt."""

    def test_skill_markdown_inlined_without_frontmatter(self):
        tmp = tempfile.mkdtemp(prefix="otto-linv-")
        try:
            path = os.path.join(tmp, "SKILL.md")
            with open(path, "w") as f:
                f.write("---\nname: foo\ndescription: d\n---\nAlways check the board first.")
            cap = registry.Capability("skill", "foo", "d")
            cap.path = path
            inv = engine._local_invocation(cap, "do the thing")
            self.assertIn("Always check the board first.", inv)
            self.assertNotIn("name: foo", inv)                        # frontmatter stripped
            self.assertIn("do the thing", inv)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_custom_cap_uses_its_prompt(self):
        cap = registry.Capability("custom", "sum", "d")
        cap.prompt = "Summarize: {request}"
        self.assertEqual(engine._local_invocation(cap, "x"), "Summarize: x")

    def test_missing_source_falls_back_to_description(self):
        cap = registry.Capability("agent", "ghost", "investigates ghosts")
        cap.path = "/nope/agent.md"
        self.assertIn("investigates ghosts", engine._local_invocation(cap, "boo"))


class RuntimeSettingsStoreTests(unittest.TestCase):
    """UI-editable runtime settings (config._SETTING_SPECS): resolution is env > store > code
    default. Env winning is load-bearing — .env/systemd stays the escape hatch, and Admin renders an
    env-pinned knob as locked instead of offering a control whose clicks would be discarded."""

    def setUp(self):
        self._path = config._SETTINGS_PATH
        self._dir = tempfile.mkdtemp(prefix="otto-settings-")
        config._SETTINGS_PATH = os.path.join(self._dir, "settings.json")
        config._store_memo["stamp"] = None

    def tearDown(self):
        config._SETTINGS_PATH = self._path
        config._store_memo["stamp"] = None
        shutil.rmtree(self._dir, ignore_errors=True)
        os.environ.pop("OTTO_MAX_ATTEMPTS", None)

    def test_default_then_store_then_env(self):
        self.assertEqual(config.setting("max_attempts"), config.MAX_VERIFY_ATTEMPTS)
        config.save_settings({"max_attempts": 7})
        self.assertEqual(config.setting("max_attempts"), 7)          # store beats default
        os.environ["OTTO_MAX_ATTEMPTS"] = "2"
        self.assertEqual(config.setting("max_attempts"), 2)          # env beats store

    def test_bool_and_choice_coercion(self):
        config.save_settings({"local_fallback": False, "plan_mode": "auto-local"})
        self.assertIs(config.setting("local_fallback"), False)
        self.assertEqual(config.setting("plan_mode"), "auto-local")

    def test_invalid_values_and_unknown_keys_are_dropped(self):
        config.save_settings({"plan_mode": "bogus", "max_attempts": "abc", "not_a_setting": 1})
        self.assertEqual(config.setting("plan_mode"), config.PLAN_MODE)      # unchanged
        self.assertEqual(config.setting("max_attempts"), config.MAX_VERIFY_ATTEMPTS)
        self.assertEqual(storage.read_json(config._settings_path(), {}), {})

    def test_setting_back_to_the_default_removes_the_key(self):
        # The store is a DIFF against the defaults, so a later default change isn't silently pinned.
        config.save_settings({"max_attempts": 7})
        self.assertIn("max_attempts", storage.read_json(config._settings_path(), {}))
        config.save_settings({"max_attempts": config.MAX_VERIFY_ATTEMPTS})
        self.assertNotIn("max_attempts", storage.read_json(config._settings_path(), {}))

    def test_corrupt_store_degrades_to_defaults(self):
        with open(config._settings_path(), "w") as f:
            f.write("{ not json")
        config._store_memo["stamp"] = None
        self.assertEqual(config.setting("max_attempts"), config.MAX_VERIFY_ATTEMPTS)

    def test_settings_all_reports_provenance(self):
        os.environ["OTTO_MAX_ATTEMPTS"] = "2"
        allv = config.settings_all()
        self.assertTrue(allv["max_attempts"]["env_pinned"])
        self.assertFalse(allv["plan_mode"]["env_pinned"])
        self.assertEqual(allv["max_attempts"]["env"], "OTTO_MAX_ATTEMPTS")

    def test_snapshot_covers_every_spec_key(self):
        # The workflow indexes the snapshot directly (self._settings["max_attempts"]), so a spec
        # entry missing from the snapshot would be a KeyError mid-run.
        self.assertEqual(set(config.settings_snapshot()), set(config._SETTING_SPECS))
        self.assertEqual(set(config.SETTINGS_FALLBACK), set(config._SETTING_SPECS))

    def test_budget_exceeded_prefers_the_snapshot_over_the_live_store(self):
        # Deterministic workflow code passes a snapshot; a later store edit must NOT change the
        # verdict it computes on replay.
        snap = dict(config.SETTINGS_FALLBACK, budget_hard_tokens=100)
        self.assertTrue(config.budget_exceeded(150, 0, hard=True, snapshot=snap))
        config.save_settings({"budget_hard_tokens": 10_000})
        self.assertTrue(config.budget_exceeded(150, 0, hard=True, snapshot=snap))   # snapshot wins
        self.assertFalse(config.budget_exceeded(150, 0, hard=True))                 # live differs


class SettingsSnapshotAccessTests(unittest.TestCase):
    """Reading the per-run settings snapshot must tolerate a MISSING key. The snapshot is the
    snapshot_settings activity's recorded result, replayed verbatim from history, so a run already
    in flight when a new spec key ships has a snapshot without it — bare indexing then KeyErrors on
    every replay forever and no worker restart recovers it (web-95917757 crash-looped on exactly
    this). test_snapshot_covers_every_spec_key above only proves a FRESH snapshot is complete,
    which is structurally unable to catch the in-flight case."""

    def _src(self, name):
        with open(os.path.join(os.path.dirname(__file__), name), encoding="utf-8") as f:
            return f.read()

    def test_workflow_code_never_bare_indexes_the_snapshot(self):
        # The bug class is a NEW setting read added the obvious way. One accessor (_setting) is
        # the root fix; this keeps it the only one.
        # Parsed, not grepped: prose in a comment or docstring naming the anti-pattern must not
        # trip it, and a real read must not hide behind one.
        import ast
        tree = ast.parse(self._src("workflows.py"))
        reads = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute)
                 and n.value.attr == "_settings"]
        self.assertEqual([n.lineno for n in reads], [],
                         "bare snapshot indexing in workflows.py — use self._setting()")
        # The accessor itself is the ONE sanctioned .get, and it must carry the code default.
        gets = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == "_settings"]
        self.assertEqual(len(gets), 1, "snapshot reads must funnel through one accessor")
        self.assertEqual(len(gets[0].args), 2, "snapshot .get without a fallback default")

    def test_budget_exceeded_tolerates_a_snapshot_missing_the_budget_keys(self):
        # config.budget_exceeded is called from workflow code with the snapshot, so it carries the
        # same constraint as _setting — it used to bare-index `snapshot[k]`.
        self.assertFalse(config.budget_exceeded(10**9, 10**6, hard=True, snapshot={}))
        self.assertFalse(config.budget_exceeded(10**9, 10**6, hard=False, snapshot={}))
        partial = {"budget_hard_tokens": 100}          # an older snapshot: one key, not four
        self.assertTrue(config.budget_exceeded(150, 0, hard=True, snapshot=partial))

    def test_the_accessor_falls_back_to_the_code_default(self):
        try:
            from workflows import OttoWorkflow
        except ImportError:
            self.skipTest("temporalio not installed")
        wf = OttoWorkflow()      # __init__ + _setting touch no workflow API, so this is safe here
        wf._settings = {}                              # a snapshot predating every key
        for name, default in config.SETTINGS_FALLBACK.items():
            self.assertEqual(wf._setting(name), default)
        wf._settings = {"max_attempts": 7}             # a present key still wins
        self.assertEqual(wf._setting("max_attempts"), 7)
        self.assertEqual(wf._setting("plan_mode"), config.SETTINGS_FALLBACK["plan_mode"])


class AdminBadgeSourcesTests(unittest.TestCase):
    """The Admin-tab warning badge counts BOTH broken MCP servers and failing LLM models. Same
    shape of bug as GateStateForwardingTests below: a source the server computes but the poll
    never reads is invisible, because a missing key looks exactly like "nothing wrong". Greps the
    two ends against each other rather than re-testing the logic."""

    def _html(self):
        with open(os.path.join(os.path.dirname(__file__), "web", "index.html"),
                  encoding="utf-8", errors="surrogateescape") as f:
            return f.read()

    def test_the_poll_reads_every_source_the_health_endpoint_publishes(self):
        with open(os.path.join(os.path.dirname(__file__), "server.py"), encoding="utf-8") as f:
            server_src = f.read()
        i = server_src.index('elif self.path == "/api/health"')
        payload = server_src[i:server_src.index("elif", i + 10)]
        poll = re.search(r"async function pollAdminBadge\(\).*?\n}", self._html(), re.S).group(0)
        for key in ("mcp", "models"):
            self.assertIn(f'"{key}": {{"unhealthy"', payload,
                          f"/api/health must publish {key}.unhealthy for the badge")
            self.assertIn(f"(d.{key}||{{}}).unhealthy", poll,
                          f"the badge poll ignores {key}.unhealthy — a broken {key} would be silent")

    def test_the_badge_names_which_source_needs_attention(self):
        # One badge, two causes: the title has to say which, or the number is a dead end.
        setter = re.search(r"function setAdminBadge\(.*?\n}", self._html(), re.S).group(0)
        self.assertIn("MCP server", setter)
        self.assertIn("LLM model", setter)


class QAVerdictTests(unittest.TestCase):
    """Pure parse of the post-PR QA judge's reply into pass/fail/inconclusive (no LLM)."""

    def test_pass(self):
        v = engine._parse_qa_verdict("PASS")
        self.assertEqual(v["verdict"], "pass")
        self.assertEqual(v["critique"], "")

    def test_pass_with_trailing_text(self):
        v = engine._parse_qa_verdict("pass — proven in staging, torn down clean")
        self.assertEqual(v["verdict"], "pass")

    def test_fail_carries_critique(self):
        v = engine._parse_qa_verdict("FAIL\nMuting rule over-mutes beyond its policy.")
        self.assertEqual(v["verdict"], "fail")
        self.assertIn("over-mutes", v["critique"])

    def test_fail_without_detail_still_has_critique(self):
        v = engine._parse_qa_verdict("FAIL")
        self.assertEqual(v["verdict"], "fail")
        self.assertTrue(v["critique"])         # the fix round needs something to act on

    def test_inconclusive_word(self):
        v = engine._parse_qa_verdict("INCONCLUSIVE\nNR never opened the issue in time.")
        self.assertEqual(v["verdict"], "inconclusive")
        self.assertIn("never opened", v["critique"])

    def test_unparseable_defaults_to_inconclusive(self):
        # An unrecognised reply must NOT read as PASS, and must NOT loop forever (inconclusive
        # stops the loop and surfaces for a human).
        v = engine._parse_qa_verdict("the test agent rambled without a verdict line")
        self.assertEqual(v["verdict"], "inconclusive")

    def test_empty_defaults_to_inconclusive(self):
        self.assertEqual(engine._parse_qa_verdict("")["verdict"], "inconclusive")

    def test_leaked_reasoning_with_stray_close_tag_reads_verdict(self):
        # Same local-model leak as verify: strip the stray </think> so the real verdict leads.
        leaked = ("Reasoning about whether the PR is safe to merge.\n"
                  "The staging test passed and torn down clean.\n</think>\n\nPASS")
        self.assertEqual(engine._parse_qa_verdict(leaked)["verdict"], "pass")

    def test_unfenced_ramble_stays_inconclusive_not_promoted(self):
        # No tag to strip: an empirical-QA ramble must default to INCONCLUSIVE (human), never
        # be heuristically promoted to PASS the way verify recovers a tail token.
        v = engine._parse_qa_verdict("Weighing the evidence.\nProbably fine.\nPASS")
        self.assertEqual(v["verdict"], "inconclusive")


class QARequestTests(unittest.TestCase):
    """The QA instruction handed to the QA cap is pure and references the PR + a parseable verdict."""

    def test_includes_pr_repo_and_verdict_contract(self):
        req = engine.qa_review_request("https://github.com/o/r/pull/7", "terraform-modules",
                                       "Add muting rules to the newrelic module")
        self.assertIn("https://github.com/o/r/pull/7", req)
        self.assertIn("terraform-modules", req)
        self.assertIn("muting rules", req)
        self.assertIn("PASS", req)
        self.assertIn("FAIL", req)
        self.assertIn("INCONCLUSIVE", req)
        self.assertIn("dev/staging", req)

    def test_omits_repo_clause_when_none(self):
        req = engine.qa_review_request("https://github.com/o/r/pull/7", None, "do a thing")
        self.assertIn("pull/7", req)
        self.assertNotIn("in repo ``", req)


class FollowupIntentTests(unittest.TestCase):
    """Pure parse of the resumed-follow-up write-intent classifier (no LLM)."""

    def test_read_is_not_a_write(self):
        self.assertFalse(engine._parse_write_intent("READ"))
        self.assertFalse(engine._parse_write_intent("read — just summarising"))

    def test_write_is_a_write(self):
        self.assertTrue(engine._parse_write_intent("WRITE"))
        self.assertTrue(engine._parse_write_intent("write the comments"))

    def test_unrecognised_defaults_to_write(self):
        # Safe default: an un-gated mutation is worse than an extra approval prompt.
        self.assertTrue(engine._parse_write_intent(""))
        self.assertTrue(engine._parse_write_intent("hmm, not sure"))


class ResumeGuardTests(unittest.TestCase):
    """The verify-less resume/follow-up path's only content check (engine.guard_resume_result):
    a model-agnostic shape guard keeping a leaked/duplicated local turn out of the chat (the
    follow-up that skips both verify and the supervisor — run web-96799819)."""

    PARA = ("PR #277 enables NVIDIA GPU time-slicing on the aws-gpu-llm node group so two vLLM "
            "engines co-schedule on a single 96GB g7e card. Blast radius is medium since it "
            "touches GPU scheduling for the serving pair. The KEDA ScaledObjects are commented "
            "out rather than deleted, so the change is reversible. The main risk is an incorrect "
            "VRAM fraction that sends the pods into a crash loop on start of the deployment. ")

    def test_duplicated_blob_is_detected(self):
        self.assertTrue(engine._is_duplicated(self.PARA * 4))    # opening re-emitted each restart
        self.assertFalse(engine._is_duplicated(self.PARA))       # a single copy is fine

    def test_normal_answer_is_not_flagged(self):
        self.assertFalse(engine._is_duplicated(
            "The change looks good. One nit: rename the variable for clarity. Approving."))
        # A long, STRUCTURED but genuine answer (an enumerated review — distinct content per line)
        # must not trip: the guard keys on a verbatim repeated OPENING, not on structural regularity.
        self.assertFalse(engine._is_duplicated(
            " ".join(f"finding{i}: distinct point number {i} about the diff." for i in range(300))))

    def test_guard_replaces_a_loop_blob(self):
        out = engine.guard_resume_result(self.PARA * 4)
        self.assertIn("looped", out)
        self.assertLess(len(out), 400)                           # the blob is gone

    def test_guard_passes_a_clean_answer_through(self):
        ans = "Approve with one suggestion: fix the duplicate --max-num-seqs flag before merge."
        self.assertEqual(engine.guard_resume_result(ans), ans)

    def test_guard_strips_a_leaked_think_fence(self):
        self.assertEqual(
            engine.guard_resume_result("<think>let me look at the diff</think>Looks good to me."),
            "Looks good to me.")


class CandidateRepoTests(unittest.TestCase):
    """Pure repo name-matching that gates interactive auto-engage of repo-mode (no LLM)."""

    REPOS = ["webapp", "infra", "aws-cost-report", "ci"]

    def test_single_named_repo_matches(self):
        self.assertEqual(engine.candidate_repo("fix the OOM bug in webapp", self.REPOS), "webapp")
        self.assertEqual(engine.candidate_repo("update the aws-cost-report README", self.REPOS),
                         "aws-cost-report")

    def test_no_repo_named_is_none(self):
        self.assertIsNone(engine.candidate_repo("what's deployed in prod?", self.REPOS))
        self.assertIsNone(engine.candidate_repo("", self.REPOS))

    def test_ambiguous_multiple_repos_is_none(self):
        # Two distinct registered repos named -> let the user pick, don't guess.
        self.assertIsNone(engine.candidate_repo("sync infra config into webapp", self.REPOS))

    def test_only_whole_token_matches(self):
        # 'infra' must not match inside 'infrastructure' (substring false-positive guard).
        self.assertIsNone(engine.candidate_repo("plan our cloud infrastructure", self.REPOS))


class PluginSkillTests(unittest.TestCase):
    """Discovery of skills bundled in installed Claude Code plugins."""

    def setUp(self):
        self._orig = registry.PLUGINS_FILE

    def tearDown(self):
        registry.PLUGINS_FILE = self._orig

    def _manifest(self):
        root = tempfile.mkdtemp(prefix="otto-plugins-")
        inst = os.path.join(root, "cache", "mp", "myplugin", "1.2.3")
        os.makedirs(os.path.join(inst, "skills", "foo"))
        os.makedirs(os.path.join(inst, "skills", "grp", "bar"))   # nested skill
        with open(os.path.join(inst, "skills", "foo", "SKILL.md"), "w") as f:
            f.write("---\nname: foo\ndescription: does foo\n---\nbody")
        with open(os.path.join(inst, "skills", "grp", "bar", "SKILL.md"), "w") as f:
            f.write("---\nname: bar\ndescription: does bar\n---\nbody")
        path = os.path.join(root, "installed_plugins.json")
        with open(path, "w") as f:
            json.dump({"plugins": {"myplugin@mp": [{"installPath": inst}]}}, f)
        return path

    def test_discovers_namespaced_including_nested(self):
        registry.PLUGINS_FILE = self._manifest()
        got = {n: d for n, d, _, _ in registry.plugin_skills()}
        self.assertEqual(got.get("myplugin:foo"), "does foo")
        self.assertIn("myplugin:bar", got)            # found in a skills/ sub-dir
        self.assertTrue(all(":" in n for n in got))   # every plugin skill is namespaced

    def test_missing_manifest_is_empty(self):
        registry.PLUGINS_FILE = "/nope/installed_plugins.json"
        self.assertEqual(list(registry.plugin_skills()), [])

    def test_project_scoped_installs_are_skipped(self):
        """A project-scoped plugin is only loaded by `claude -p` when it runs from THAT repo, and
        Otto's cwd is its own directory (or a provisioned clone) — so `/<plugin>:<skill>` is an
        Unknown command there. Offering one to the router is a trap: on 2026-07-31 a Slack run
        routed to `acme-ci:tc-doctor` (scope "project") and burned all three attempts plus
        an Opus escalation on `Unknown command`."""
        path = self._manifest()
        with open(path) as f:
            man = json.load(f)
        inst = man["plugins"]["myplugin@mp"][0]["installPath"]
        with open(path, "w") as f:
            json.dump({"plugins": {
                "myplugin@mp": [{"installPath": inst, "scope": "project",
                                 "projectPath": "/home/me/repositories/mobile"}],
            }}, f)
        registry.PLUGINS_FILE = path
        self.assertEqual(list(registry.plugin_skills()), [])

    def test_a_plugin_installed_at_both_scopes_still_surfaces(self):
        path = self._manifest()
        with open(path) as f:
            inst = json.load(f)["plugins"]["myplugin@mp"][0]["installPath"]
        with open(path, "w") as f:
            json.dump({"plugins": {"myplugin@mp": [
                {"installPath": inst, "scope": "project", "projectPath": "/repos/mobile"},
                {"installPath": inst, "scope": "user"},
            ]}}, f)
        registry.PLUGINS_FILE = path
        self.assertIn("myplugin:foo", {n for n, _, _, _ in registry.plugin_skills()})

    def test_an_install_without_a_scope_key_is_treated_as_user(self):
        # Older manifests omit `scope`; dropping those would silently delete working capabilities.
        registry.PLUGINS_FILE = self._manifest()
        self.assertIn("myplugin:foo", {n for n, _, _, _ in registry.plugin_skills()})

    def test_plugin_skill_invocation_is_namespaced_slash_command(self):
        c = registry.Capability("skill", "myplugin:foo", "d")
        self.assertEqual(engine._invocation(c, "go"), "/myplugin:foo go")


class ConfigTests(unittest.TestCase):
    def test_write_is_superset_of_read(self):
        self.assertTrue(set(config.READ_TOOLS).issubset(set(config.WRITE_TOOLS)))
        self.assertIn("Edit", config.WRITE_TOOLS)
        self.assertNotIn("Edit", config.READ_TOOLS)

    def test_is_no_reply_tolerates_wrappers_but_demands_the_whole_output(self):
        """Note the asymmetry this is tuned for: a false positive silently swallows an answer
        somebody is waiting for, a miss just posts a reply we'd rather have skipped."""
        for yes in ("NO_REPLY", " NO_REPLY ", "\nNO_REPLY\n", "no_reply", "`NO_REPLY`",
                    "**NO_REPLY**", "_NO_REPLY_", '"NO_REPLY"', "NO_REPLY.", "NO_REPLY!",
                    "```NO_REPLY```"):
            self.assertTrue(config.is_no_reply(yes), repr(yes))
        for no in ("", None, "NO_REPLY (nothing to add)", "I'll send NO_REPLY", "no reply",
                   "NOREPLY", "Nothing to reply here", "NO_REPLY\n\nbut also: the build is red",
                   "Sure — the build is green."):
            self.assertFalse(config.is_no_reply(no), repr(no))


class DataDirIgnoredTests(unittest.TestCase):
    """`data/` is runtime state that must never be committed, but .gitignore enumerates its
    subpaths one by one — so every new store added under DATA_DIR is a chance to forget one.
    `data/workspaces/` was forgotten, and each entry there is a full clone WITH its own .git,
    so an in-flight repo-mode run showed up as an untracked embedded repo in `git status`.

    Enumerating the literals out of the source (rather than listing them here) is what makes
    this a root fix: a store added tomorrow is covered without touching this test."""

    ROOT = os.path.dirname(os.path.abspath(__file__))

    def _data_paths(self):
        pat = re.compile(r'os\.path\.join\(\s*(?:config\.)?DATA_DIR\s*,\s*"([^"/]+)"')
        found = set()
        for name in sorted(glob.glob(os.path.join(self.ROOT, "*.py"))):
            with open(name, encoding="utf-8") as f:
                found.update(pat.findall(f.read()))
        # A glob is a PATTERN over data/, not a store written under it (file_safety builds the
        # deny rule this way) — `git check-ignore data/**` answers about a path that never
        # exists, so it would report a miss forever.
        return {p for p in found if "*" not in p}

    def test_every_runtime_store_under_data_is_gitignored(self):
        paths = self._data_paths()
        self.assertGreater(len(paths), 10, "the DATA_DIR literal scan found suspiciously little")
        missed = []
        for p in sorted(paths):
            rc = subprocess.run(["git", "check-ignore", "-q", os.path.join("data", p)],
                                cwd=self.ROOT, capture_output=True).returncode
            if rc != 0:
                missed.append(f"data/{p}")
        self.assertEqual(missed, [],
                         f"these runtime paths are written under data/ but are NOT gitignored: "
                         f"{missed}. Add them to .gitignore — data/ is never committed.")

    def test_no_data_rule_is_directory_only(self):
        """The test above asks `git check-ignore` about the LIVE worktree, where a store that
        already exists is matched by a `data/x/` rule and reads as covered. On a fresh clone
        nothing under data/ exists yet, the directory-only rules match nothing, and the first
        run drops untracked state into `git status` — the one moment this list exists for.

        A trailing slash is the whole difference, so assert on the spelling rather than trying
        to reproduce a clean checkout."""
        with open(os.path.join(self.ROOT, ".gitignore"), encoding="utf-8") as f:
            bad = [ln.strip() for ln in f
                   if ln.strip().startswith("data/") and ln.strip().endswith("/")]
        self.assertEqual(bad, [], f"these data/ rules only match an EXISTING directory: {bad}. "
                                  f"Drop the trailing slash so they also match a fresh clone.")


class OpenSourceHygieneTests(unittest.TestCase):
    """No tracked file may name the operator, their employer, or that employer's systems.

    Otto is published as open source, so every tracked byte is public. The pre-publication
    scrub found ~140 such references — a bundled capability hardcoded to one company's GitHub
    org and project-board node ids, regression fixtures describing a real production mesh by
    hostname, and a person's name as a shipped default. None of it was secret; all of it made
    the project read as somebody's internal tool and disclosed more than it meant to.

    A grep is the only thing that catches the NEXT one. The comments in this codebase are
    load-bearing — they cite the real incident behind each guard — so the pressure to paste a
    real hostname or ticket ref back in is constant and the result looks perfectly normal in
    review. Rewrite the example, keep the reasoning.

    Needles are assembled from halves so this file does not match its own list."""

    ROOT = os.path.dirname(os.path.abspath(__file__))

    # (first half, second half) — joined at runtime. Add to this list, never a bare literal.
    FORBIDDEN = [
        ("une", "eq"), ("face", "me.atlassian"), ("rip", "ley"), ("chew", "y"),
        ("mage", "ling"), ("dh", "op"), ("team", "city"), ("ren", "ny"),
        ("mat", "ias"), ("flow", "ise"), ("bish", "op"), ("zsca", "ler"),
        ("harbor.", "prd"), ("prd-", "nv"), ("prd-", "ie"), ("dev-", "nv"),
        ("ent-", "nv"), ("ent-", "ie"), ("U06AHB", "0LZ3Q"), ("1581974", "74045"),
        ("PVT_", "kw"), ("PVTSSF", "_"), ("/home/matias", "-sosa"),
    ]

    # The copyright line is the ONE place the author's own name belongs — a licence naming
    # nobody grants nothing. Everywhere else, a person's name is a leak.
    EXEMPT = {"LICENSE"}

    def test_no_tracked_file_names_a_real_person_org_or_host(self):
        ls = subprocess.run(["git", "ls-files", "-z"], cwd=self.ROOT,
                            capture_output=True, text=True)
        if ls.returncode != 0:
            self.skipTest("not a git checkout")
        needles = [a + b for a, b in self.FORBIDDEN]
        hits = []
        for rel in ls.stdout.split("\0"):
            if not rel:
                continue
            path = os.path.join(self.ROOT, rel)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    body = f.read().lower()
            except OSError:                     # a staged deletion, a submodule
                continue
            if os.path.abspath(path) == os.path.abspath(__file__):
                continue                        # the needle table itself
            if rel in self.EXEMPT:
                continue
            for n in needles:
                if n in body:
                    hits.append(f"{rel}: {n}")
        self.assertEqual(hits, [],
                         "these tracked files name a real person, employer or internal host, and "
                         f"this repo is PUBLIC: {hits}. Rewrite the example onto the fictional "
                         "acme-corp / example.com fleet — the reasoning in the comment is worth "
                         "keeping, the real name never is.")


class TemporalPinTests(unittest.TestCase):
    """`requirements.txt` pinned `temporalio==1.8.0` while every run and all 940 tests were
    executing against 1.30.0 — the SDK number had been copied from the Temporal CLI's pin in
    install.sh, whose scheme is unrelated. Nothing failed (1.8.0 still passes the suite), which
    is exactly why it survived: a wrong pin is only felt by whoever rebuilds the venv during a
    recovery, and then it's silent drift, not an error."""

    ROOT = os.path.dirname(os.path.abspath(__file__))

    def _pin(self):
        with open(os.path.join(self.ROOT, "requirements.txt"), encoding="utf-8") as f:
            m = re.search(r"^temporalio==([\w.]+)\s*$", f.read(), re.M)
        self.assertIsNotNone(m, "temporalio must be pinned with == in requirements.txt")
        return m.group(1)

    def test_the_pin_is_the_version_the_suite_actually_runs_against(self):
        try:
            import temporalio
        except ImportError:
            self.skipTest("temporalio not installed")
        installed = getattr(temporalio, "__version__", None) or \
            __import__("importlib.metadata", fromlist=["version"]).version("temporalio")
        self.assertEqual(
            self._pin(), installed,
            f"requirements.txt pins temporalio=={self._pin()} but this venv runs {installed}. "
            f"A rebuilt venv would not be the environment these tests passed in — bump the pin "
            f"to the tested version (NOT to the Temporal CLI's version, a separate scheme).")

    def _cli_pin(self):
        with open(os.path.join(self.ROOT, "install.sh"), encoding="utf-8") as f:
            sh = f.read()
        m = re.search(r'^TEMPORAL_CLI_VERSION="([\w.]+)"', sh, re.M)
        self.assertIsNotNone(m, "install.sh must pin the Temporal CLI version")
        # Declaring the version is only a pin if the install command actually uses it —
        # a literal left behind below drifts from the declaration in total silence.
        self.assertIn('--version "$TEMPORAL_CLI_VERSION"', sh,
                      "install.sh declares TEMPORAL_CLI_VERSION but installs with a literal — "
                      "the declaration and the installed version would drift in silence")
        return m.group(1)

    def test_the_cli_pin_is_not_copied_from_the_sdk_pin(self):
        """install.sh's CLI version and the SDK pin are independent schemes. They are read from
        two files, so the only way they match is someone having copied one onto the other."""
        self.assertNotEqual(self._cli_pin(), self._pin(),
                            "the Temporal CLI pin equals the temporalio SDK pin — the schemes are "
                            "unrelated, so this is the copied-number bug, not a coincidence")


class NotificationContentTests(unittest.TestCase):
    """The rule that request/ticket/message CONTENT reaches a push only through `detail` is
    enforced at the SIGNATURE (delivery.notify has no body param) — but a call site can still
    smuggle content into `title` or `note`, and that would be invisible. So grep the real call
    sites, the same way GateStateForwardingTests greps the gate whitelist."""

    # Every way a push is raised. Written out rather than pattern-matched on a bare `notify(`
    # suffix: the first version of this used a `(?<![\w.])` lookbehind, which excluded exactly
    # `self._notify(` and `delivery.notify(` — i.e. ALL of them — so the test greped nothing and
    # passed vacuously through two deliberate mutations. Hence test_the_grep_is_not_vacuous.
    _CALL_RE = re.compile(r"(?:self\._notify|delivery\.notify)\(")

    def _notify_calls(self, path):
        """Every push call in `path`, as source text, with parens balanced."""
        src = open(path, encoding="utf-8").read()
        calls, i = [], 0
        while True:
            m = self._CALL_RE.search(src, i)
            if not m:
                return calls
            depth, j = 1, m.end()
            while j < len(src) and depth:
                depth += (src[j] == "(") - (src[j] == ")")
                j += 1
            calls.append(src[m.end():j - 1])
            i = j

    @staticmethod
    def _args(call):
        """Split a call's source into top-level arguments. Splitting on a bare comma breaks
        inside a nested call — `notify(payload.get("title", ""), lines=…)` reads as a positional
        second arg of `""` — which is a false POSITIVE, and a guard that cries wolf gets deleted."""
        args, depth, buf = [], 0, ""
        for ch in call:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                args.append(buf.strip())
                buf = ""
                continue
            buf += ch
        if buf.strip():
            args.append(buf.strip())
        return args

    def test_the_grep_is_not_vacuous(self):
        """A source-greping guard that matches nothing is indistinguishable from a passing one."""
        counts = {p: len(self._notify_calls(p)) for p in ("workflows.py", "activities.py")}
        self.assertGreaterEqual(counts["workflows.py"], 4, counts)
        self.assertGreaterEqual(counts["activities.py"], 2, counts)

    def test_no_call_site_puts_request_text_in_the_always_sent_half(self):
        """`request`, `result` and a clarification `question` are content. They may appear in a
        call only as the value of `detail=` — never in the title or a metadata line, which are
        sent whatever OTTO_NTFY_DETAIL says."""
        for path in ("workflows.py", "activities.py"):
            for call in self._notify_calls(path):
                before = call[:call.index("detail=")] if "detail=" in call else call
                for token in ("request", "clar[", "['question']", '["question"]', "result"):
                    self.assertNotIn(
                        token, before,
                        f"{path}: content {token!r} reaches the always-sent half of "
                        f"notify({' '.join(call.split())[:100]}…) — put it in detail=")

    def test_every_notify_call_site_was_migrated_off_the_positional_body(self):
        """A second positional arg is the OLD content-leaking shape (`notify(title,
        request[:250])`). delivery.notify makes it a TypeError; this catches it at review time,
        and covers self._notify too."""
        for path in ("workflows.py", "activities.py"):
            for call in self._notify_calls(path):
                args = self._args(" ".join(call.split()))
                if len(args) < 2:
                    continue
                second = args[1]
                self.assertTrue(
                    second.startswith("*") or re.match(r"[\w]+\s*=", second),
                    f"{path}: positional second arg {second!r} in "
                    f"notify({' '.join(call.split())[:100]}…) — content risk")


class WidAllocationTests(unittest.TestCase):
    """Locally-minted run ids must be process-unique: a bare counter collided across the
    server and worker processes (both restart at 0), so two unrelated runs could mint the
    same "wf-0001" — overwriting each other's transcripts and producing ambiguous audit rows."""

    def test_wids_are_namespaced_and_monotonic(self):
        a, b = engine._next_wid(), engine._next_wid()
        self.assertTrue(a.startswith(f"wf-{engine._RUN_NS}-"), a)
        self.assertNotEqual(a, b)
        self.assertEqual(len(engine._RUN_NS), 6)    # uuid-derived per-process namespace


class StorageTests(unittest.TestCase):
    """storage.py (issue #88): server.py and worker.py are separate processes doing
    read-modify-write on the same data/*.json files — mutate_json must serialize them
    (no lost updates) and land writes atomically (a reader never sees a torn file)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="otto-storage-")
        self.path = os.path.join(self.dir, "store.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_read_json_tolerant(self):
        self.assertEqual(storage.read_json(self.path, []), [])           # missing -> default
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(storage.read_json(self.path, {"d": 1}), {"d": 1})  # invalid -> default

    def test_mutate_roundtrip(self):
        out = storage.mutate_json(self.path, lambda d: d + ["a"], default=[])
        self.assertEqual(out, ["a"])
        out = storage.mutate_json(self.path, lambda d: d + ["b"], default=[])
        self.assertEqual(out, ["a", "b"])
        self.assertEqual(storage.read_json(self.path, None), ["a", "b"])

    def test_unchanged_skips_write(self):
        # "nothing new to remember" must not even create the data file
        got = storage.mutate_json(self.path, lambda d: storage.UNCHANGED, default=["seed"])
        self.assertEqual(got, ["seed"])
        self.assertFalse(os.path.exists(self.path))

    def test_write_json_overwrites(self):
        storage.write_json(self.path, {"a": 1})
        storage.write_json(self.path, {"b": 2})
        self.assertEqual(storage.read_json(self.path, None), {"b": 2})

    def test_no_lost_updates_across_processes(self):
        # The real failure mode: concurrent Temporal activities + HTTP threads racing on one
        # store. Without the lock, most of these appends would overwrite each other.
        import multiprocessing
        workers, iterations = 4, 25
        procs = [multiprocessing.Process(target=_storage_hammer, args=(self.path, w, iterations))
                 for w in range(workers)]
        for p in procs:
            p.start()
        # while writers hammer, the file must ALWAYS parse (atomic replace: old or new,
        # never torn). read raw — read_json's tolerance would mask a torn file here.
        deadline_guard = 0
        while any(p.is_alive() for p in procs):
            try:
                with open(self.path) as f:
                    json.load(f)
            except FileNotFoundError:
                pass                                # not created yet — fine
            deadline_guard += 1
            if deadline_guard > 100_000:            # safety valve, never expected
                break
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0)
        data = storage.read_json(self.path, [])
        self.assertEqual(len(data), workers * iterations)                # zero lost updates
        self.assertEqual(len({tuple(e) for e in data}), workers * iterations)


class CapabilityTableColumnsTests(unittest.TestCase):
    """The capabilities table is `table-layout: fixed`, so its <colgroup>, <thead> and every row
    must agree on the column count — one missing <td> silently shifts every control right of it
    into the wrong column. Greps the three ends against each other."""

    def _html(self):
        with open(os.path.join(os.path.dirname(__file__), "web", "index.html"),
                  encoding="utf-8", errors="surrogateescape") as f:
            return f.read()

    def test_colgroup_header_and_row_have_the_same_number_of_cells(self):
        html = self._html()
        cols = len(re.findall(r"<col\b", re.search(r"const CAP_COLS=`(.*?)`", html, re.S).group(1)))
        head = len(re.findall(r"<th\b", re.search(r"const CAP_HEAD=`(.*?)`", html, re.S).group(1)))
        row = len(re.findall(r"<td\b", re.search(r"const capRowHtml=c=>`(.*?)`;", html, re.S).group(1)))
        self.assertGreaterEqual(row, 6, "grep found no cells — the assertion below is vacuous")
        self.assertEqual((cols, head), (row, row), "capability table columns are out of step")

    def test_every_informational_column_can_be_shed_on_a_narrow_window(self):
        # A fixed-layout table OVERFLOWS rather than shrinking, so each non-interactive column
        # needs its own breakpoint. Risk/On/actions stay — they're the controls.
        html = self._html()
        for col in ("c-used", "c-score", "c-exec"):
            self.assertRegex(html, r"@media \(max-width: \d+px\) \{ \.captable \." + col + r" \{ display: none",
                             f"{col} has no narrow-window breakpoint")

    def test_the_truncated_description_carries_a_title(self):
        # The cell's fixed width ellipsises the description, so without a title= the tail is
        # unreadable with no way to reveal it.
        row = re.search(r"const capRowHtml=c=>`(.*?)`;", self._html(), re.S).group(1)
        self.assertRegex(row, r'<small title="\$\{esc\(c\.description\)\}"')


class ComposerOverrideForwardingTests(unittest.TestCase):
    """The composer's Model picker is only honoured on the path that reads it. A handoff and a
    resume are both driven from the same visible composer, so an ingress that drops the override
    reads as the picker being broken — measured on web-b488e127, which ran claude-sonnet-5 while
    the picker said Deepseek Flash. Same shape as the gate-whitelist test: catch the dropped hop."""

    def _src(self, name):
        with open(os.path.join(os.path.dirname(__file__), name),
                  encoding="utf-8", errors="surrogateescape") as f:
            return f.read()

    def _call(self, src, marker):
        i = src.index(marker)
        return src[i:src.index("}))", i) + 3]

    def test_the_handoff_resubmit_carries_the_whole_composer(self):
        # A handoff IS a fresh submit — it re-enters /api/submit, so every setting a normal
        # submit sends must ride along or the visible composer silently doesn't apply.
        call = self._call(self._src("web/index.html"), 'api("/api/submit",{request:task')
        for field in ("model_override", "memory_enabled", "repo", "qa", "plan_mode", "auto_approve"):
            self.assertIn(field, call, f"the handoff re-submit drops {field}")

    def test_the_handoff_and_rebind_resubmits_carry_the_auto_approve_toggle(self):
        # Dropping it re-gates a run the human already pre-authorized on screen — the opposite
        # failure to the model picker's, and just as invisible.
        src = self._src("web/index.html")
        call = self._call(src, 'api("/api/submit",{request:task+carryContextForSubmit(true)')
        self.assertIn("auto_approve: selectedAutoApprove()", call,
                      "the model-rebind re-submit drops the approval toggle")

    def test_the_resume_call_carries_the_model_override(self):
        # `_line`, not `_call`: this assertion passed with the field DELETED, because `_call`'s
        # window ran past the one-line fetch into the rebind re-submit below it, which carries
        # a model_override of its own. A guard that cannot fail is not a guard.
        line = self._line(self._src("web/index.html"), 'api("/api/continue",{session_id')
        self.assertIn("model_override: selectedModelOverride()", line,
                      "/api/continue is called without the composer's model pick")

    def _line(self, src, marker):
        """The single SOURCE LINE holding a call — the tight window. `_call` slices to the next
        `}))`, which for the one-line /api/continue fetch (it ends `}); }`) runs on into the
        NEXT call and reads its arguments as this one's: a field dropped from /api/continue is
        then still found, in the rebind re-submit below it. Measured by deleting it."""
        i = src.index(marker)
        return src[src.rindex("\n", 0, i) + 1:src.index("\n", i)]

    def test_every_composer_resubmit_carries_the_effort_pick(self):
        # Effort is one more composer control on the same three paths the model pick rides. A
        # dropped hop is invisible: the run completes, at the wrong effort, reporting success.
        src = self._src("web/index.html")
        for marker, what in (
                ('api("/api/submit",{request:task+carryContextForSubmit(true)', "model-rebind"),
                ('api("/api/submit",{request:task', "handoff")):
            self.assertIn("effort:", self._call(src, marker),
                          f"the {what} re-submit drops the effort pick")
        self.assertIn("effort: selectedEffort()",
                      self._line(src, 'api("/api/continue",{session_id'),
                      "the resume call drops the effort pick")

    def test_api_continue_validates_and_forwards_the_effort_pick(self):
        # Same grep-shaped guard as the model override, for the same reason: the Temporal branch
        # of /api/continue can't be driven from a unit test.
        src = self._src("server.py")
        i = src.index('params = {"request": body["message"], "resume": body["session_id"]')
        branch = src[i:i + 2400]
        self.assertIn('params["effort"] = effort', branch,
                      "/api/continue accepts an effort pick but never forwards it")
        self.assertIn("config.effort_level(body.get(\"effort\"))", branch,
                      "the effort pick reaches the workflow unvalidated")

    def test_api_continue_validates_and_forwards_the_override(self):
        # Grepped for the same reason as the git-identity fallback: the Temporal branch of
        # /api/continue can't be driven from a unit test.
        src = self._src("server.py")
        i = src.index('params = {"request": body["message"], "resume": body["session_id"]')
        branch = src[i:i + 1200]
        self.assertIn('params["model_override"] = model_override', branch,
                      "/api/continue accepts the override but never forwards it")
        self.assertIn("gateway.resolve_model(model_override)", branch,
                      "the override reaches the workflow unvalidated")


class ResumeModelBackendTests(unittest.TestCase):
    """A resumed session is bound to whichever backend minted its id, but the model ENTRY is
    resolved independently (override > cap_exec > phase assignment) — so whatever comes out must
    be re-checked against that binding or a local model id reaches `claude -p`, or a Claude pool
    entry reaches the local runtime, neither of which can serve it. Every case must land on a
    model the session's own backend can actually run."""

    def _cap(self):
        import types
        return types.SimpleNamespace(name="demo", kind="skill", risk="read", description="d",
                                     tool_free=False, cwd=None, mcp_config=None)

    def _swap(self, obj, name, value):
        prev = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, prev)

    _POOL = {"local-flash": {"name": "local-flash", "provider": "openai",
                             "model": "deepseek-v4-flash"},
             "local-other": {"name": "local-other", "provider": "openai", "model": "qwen-3"},
             "claude-tier": {"name": "claude-tier", "provider": "claude",
                             "model": "claude-sonnet-5"}}

    def _run(self, session, override, admin, recorded=None):
        seen, done = {}, {"result": "ok", "cost": 0, "tokens": None, "session_id": session,
                          "is_error": False}

        def _fake_claude(prompt, **k):
            seen.update(backend="claude", model=k.get("model"))
            return done

        def _fake_local(prompt, **k):
            seen.update(backend="local", model=k["model_entry"]["name"])
            return done

        pool = self._POOL
        self._swap(engine.gateway, "resolve_model", pool.get)
        self._swap(engine.gateway, "load", lambda: {"pool": list(pool.values())})
        self._swap(engine.gateway, "exec_model_entry", lambda c: pool[admin])
        self._swap(engine.gateway, "exec_model_id", lambda c: pool[admin]["model"])
        self._swap(engine.local_runtime, "is_local_session",
                   lambda s: str(s).startswith("local-"))
        self._swap(engine.local_runtime, "session_model", lambda s: recorded)
        self._swap(engine, "_claude", _fake_claude)
        self._swap(engine.local_runtime, "run_json", _fake_local)
        engine.run_attempt("go", self._cap(), resume_session=session, wid="w1",
                           model_override=override)
        return seen

    def test_a_claude_session_never_runs_a_local_model_id(self):
        # Unguarded this hands "deepseek-v4-flash" to `claude -p`, which cannot serve it.
        seen = self._run("claude-sess-1", "local-flash", "claude-tier")
        self.assertEqual((seen["backend"], seen["model"]), ("claude", "claude-sonnet-5"),
                         "a local model id leaked into the Claude backend on a resume")

    def test_a_local_session_never_runs_a_claude_pool_entry(self):
        # The mirror, and reachable with NO override at all — a local session resumed while the
        # phase assignment points at Claude handed the Claude entry to the local runtime, whose
        # first act is to look for a `base_url` that a Claude entry does not have.
        seen = self._run("local-sess-1", None, "claude-tier")
        self.assertEqual((seen["backend"], seen["model"]), ("local", "local-flash"),
                         "a Claude pool entry leaked into the local runtime on a resume")

    def test_the_session_own_model_wins_over_any_other_local_entry(self):
        # Which local model is not arbitrary when the session recorded one — resuming a
        # deepseek conversation on qwen silently changes who is answering mid-thread.
        seen = self._run("local-sess-1", None, "claude-tier", recorded="local-other")
        self.assertEqual((seen["backend"], seen["model"]), ("local", "local-other"))

    def test_a_pre_recording_session_is_still_resumable(self):
        # No model recorded (a session file written before it was stored) must fall back to any
        # entry on the same backend, not dead-end — local history is plain, portable messages.
        seen = self._run("local-sess-1", None, "claude-tier", recorded=None)
        self.assertEqual(seen["backend"], "local")

    def test_a_same_backend_override_still_applies(self):
        # Only a CROSS-backend pick is overruled — same-backend, the user's choice still wins.
        seen = self._run("local-sess-1", "local-other", "local-flash")
        self.assertEqual((seen["backend"], seen["model"]), ("local", "local-other"))


class ResumeModelRebindTests(unittest.TestCase):
    """Honouring the backend binding is only half the answer: silently resuming on the OLD model
    is what the user actually saw ("it carries on using that model rather than using the
    override"). A pick the session cannot serve must END the session and run fresh on the chosen
    model, visibly — never be quietly discarded."""

    def _src(self, name):
        with open(os.path.join(os.path.dirname(__file__), name),
                  encoding="utf-8", errors="surrogateescape") as f:
            return f.read()

    def test_api_continue_rebinds_a_cross_backend_pick(self):
        src = self._src("server.py")
        i = src.index('params = {"request": body["message"], "resume": body["session_id"]')
        branch = src[i:i + 2000]
        self.assertIn("local_runtime.is_local_session(", branch,
                      "/api/continue resumes without checking the session's backend")
        self.assertIn('"rebind"', branch,
                      "a cross-backend pick is swallowed instead of rebinding to a fresh run")

    def test_the_client_carries_the_conversation_into_the_rebound_run(self):
        # A rebind leaves a live session, so the history that resume carried implicitly has to be
        # passed explicitly — otherwise switching model silently forgets the conversation.
        html = self._src("web/index.html")
        branch = html[html.index("if(out && out.rebind){"):html.index("if(out && out.handoff){")]
        self.assertIn("carryContextForSubmit(true)", branch,
                      "the rebound run drops the conversation")
        self.assertIn("model_override: out.rebind.model", branch)
        self.assertIn("currentSession=null", branch, "the dead session is left bound")
        self.assertNotIn("suppressCarry=true", branch, "suppressCarry would void the carry")

    def test_the_switch_is_stated_on_screen(self):
        html = self._src("web/index.html")
        branch = html[html.index("if(out && out.rebind){"):html.index("if(out && out.handoff){")]
        self.assertIn("recordMsg(", branch, "the session ends with no visible explanation")

    def test_the_carry_states_what_to_do_with_itself(self):
        # A bare "for context" label leaves the run free to re-check and then quietly serve a
        # figure different from the one above it (judged a fabricated "live" pull on
        # web-50af486b), or to skip the check and assert nothing changed. Measured on the local
        # model: 1/5 replies reconciled the conflict under the bare label, 5/5 under this text.
        html = self._src("web/index.html")
        body = html[html.index("function carryContextForSubmit("):]
        block = body[:body.index("\n}")]
        self.assertIn("re-checked with tools", block, "the carry doesn't require a re-check")
        self.assertIn("say what changed", block, "the carry doesn't require reconciliation")

    def test_carry_context_honours_the_force_flag(self):
        # The guard exists because resume carries history implicitly; the rebind path is the one
        # case where a session is live AND the carry is still needed.
        html = self._src("web/index.html")
        body = html[html.index("function carryContextForSubmit("):]
        self.assertIn("(currentSession && !force)", body[:200],
                      "carryContextForSubmit still returns '' for every live session")


class SetupWizardTests(unittest.TestCase):
    """The guided first run (`./install.sh --guided`). Everything asserted here is a way the
    wizard could damage an install it was supposed to improve: block an unattended installer,
    clobber a configured value, or widen the permissions on the file holding the Slack token."""

    def setUp(self):
        import setup_wizard
        self.sw = setup_wizard
        self.tmp = tempfile.mkdtemp()
        self._orig_env_path = self.sw.ENV_PATH
        self.sw.ENV_PATH = os.path.join(self.tmp, ".env")
        self._env = {k: os.environ.get(k) for k in
                     ("OTTO_SECRET_COMMAND", "OTTO_NTFY_TOPIC", "OTTO_EVENT_SECRET",
                      "OTTO_SLACK_USER_TOKEN")}
        for k in self._env:
            os.environ.pop(k, None)
        # Restored in tearDown: a stubbed ask/confirm leaking into a later test would answer its
        # prompts for it, which is exactly the shape of a guard that proves nothing.
        self._prompts = {n: getattr(self.sw, n) for n in ("ask", "confirm", "secret_input")}
        config.secret_reset()

    def tearDown(self):
        for n, f in self._prompts.items():
            setattr(self.sw, n, f)
        self.sw.ENV_PATH = self._orig_env_path
        for k, v in self._env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        config.secret_reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, text):
        with open(self.sw.ENV_PATH, "w") as f:
            f.write(text)

    # --- the unattended path -------------------------------------------------------------

    def test_without_a_tty_it_is_a_no_op_that_exits_clean(self):
        """install.sh is the unattended path (CI, a piped installer, a re-run after git pull).
        A prompt there blocks forever on input nobody will send."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = self.sw.main([])
        self.assertEqual(rc, 0)
        self.assertIn("skipped (not a terminal)", out.getvalue())
        self.assertFalse(os.path.exists(self.sw.ENV_PATH), "it wrote .env with nobody watching")

    def test_a_piped_stdout_counts_as_non_interactive(self):
        """`./install.sh --guided | tee log` leaves stdin a TTY while the prompts go to a pipe —
        checking stdin alone would print questions into the log and then hang."""
        real = sys.stdin
        try:
            sys.stdin = type("T", (), {"isatty": staticmethod(lambda: True)})()
            self.assertFalse(self.sw.interactive())   # stdout under the test runner is not a tty
        finally:
            sys.stdin = real

    def test_install_sh_guided_flag_is_wired_and_never_fails_the_install(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")) as f:
            sh = f.read()
        self.assertIn("--guided) GUIDED=1", sh)
        self.assertRegex(sh, r'setup_wizard\.py"? \|\| true',
                         "a declined prompt or a ^C must not fail an otherwise-good install")
        self.assertIn("chmod 600 .env", sh)

    # --- never overwrite -----------------------------------------------------------------

    def test_an_already_set_key_is_reported_not_re_asked(self):
        """The wizard is the RECOVERY tool too — re-running it after a partial setup must offer
        only what is still missing, and must never re-prompt for a working token."""
        os.environ["OTTO_EVENT_SECRET"] = "already-configured"
        config.secret_reset()
        self.sw.ask = lambda *a, **k: self.fail("prompted for a key that was already set")
        self.sw.confirm = lambda *a, **k: self.fail("prompted for a key that was already set")
        with contextlib.redirect_stdout(io.StringIO()):
            title, status = self.sw.step_event_secret()
        self.assertEqual(status, "ok")

    def test_set_env_replaces_in_place_and_keeps_the_documentation(self):
        """.env is seeded from .env.example, which is mostly the docs for each knob. A rewrite
        would be correct and useless — the operator loses every explanation."""
        self._write_env("# what this does\n# OTTO_NTFY_TOPIC=\n\nPORT=8765\n")
        self.sw.set_env("OTTO_NTFY_TOPIC", "abc123")
        text = self.sw.env_text()
        self.assertIn("# what this does", text)
        self.assertIn("OTTO_NTFY_TOPIC=abc123", text)
        self.assertNotIn("# OTTO_NTFY_TOPIC=", text)   # uncommented in place, not appended
        self.assertIn("PORT=8765", text)
        self.assertEqual(text.count("OTTO_NTFY_TOPIC"), 1)

    def test_set_env_overwrites_a_set_key_exactly_once(self):
        self._write_env("A=1\nOTTO_EVENT_SECRET=old\nB=2\n")
        self.sw.set_env("OTTO_EVENT_SECRET", "new")
        text = self.sw.env_text()
        self.assertIn("OTTO_EVENT_SECRET=new", text)
        self.assertNotIn("old", text)
        self.assertEqual(text.splitlines(), ["A=1", "OTTO_EVENT_SECRET=new", "B=2"])

    def test_set_env_appends_a_key_the_file_has_never_heard_of(self):
        self._write_env("A=1\n")
        self.sw.set_env("OTTO_NEW_KEY", "v")
        self.assertIn("OTTO_NEW_KEY=v", self.sw.env_text())
        self.assertIn("A=1", self.sw.env_text())

    def test_env_value_does_not_read_a_commented_line_as_set(self):
        """`# OTTO_NTFY_TOPIC=` is the .env.example template, i.e. UNSET. Reading it as set would
        make the wizard skip every gap on a freshly seeded .env."""
        self._write_env("# OTTO_NTFY_TOPIC=placeholder\nOTTO_EVENT_SECRET=real\n")
        self.assertEqual(self.sw.env_value("OTTO_NTFY_TOPIC"), "")
        self.assertEqual(self.sw.env_value("OTTO_EVENT_SECRET"), "real")

    # --- permissions ---------------------------------------------------------------------

    def test_env_is_never_world_readable_after_a_write(self):
        """It holds an xoxp- token that reads and posts as the operator."""
        self._write_env("A=1\n")
        os.chmod(self.sw.ENV_PATH, 0o644)
        self.sw.set_env("OTTO_SLACK_USER_TOKEN", "xoxp-secret")
        self.assertEqual(os.stat(self.sw.ENV_PATH).st_mode & 0o777, 0o600)

    def test_the_replacement_file_is_locked_down_before_it_becomes_dot_env(self):
        """chmod AFTER os.replace leaves a window where .env is world-readable under the default
        umask — so the temp file must be 600 before the rename, not after."""
        import ast
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "setup_wizard.py")) as f:
            tree = ast.parse(f.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "set_env")
        calls = [n.func.attr for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("chmod", "replace")]
        self.assertEqual(calls, ["chmod", "replace"])

    # --- secrets never echoed ------------------------------------------------------------

    def test_a_pasted_token_is_read_without_echo(self):
        """A token typed into a shared terminal stays in scrollback and in the terminal's own
        buffer — getpass is what keeps it off the screen."""
        import ast
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "setup_wizard.py")) as f:
            src = f.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "secret_input")
        self.assertIn("getpass", ast.dump(fn))
        # …and the Slack step must use it rather than plain input()
        slack_fn = next(n for n in ast.walk(ast.parse(src))
                        if isinstance(n, ast.FunctionDef) and n.name == "step_slack_token")
        names = [n.func.id for n in ast.walk(slack_fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("secret_input", names)
        self.assertNotIn("input", names)

    def test_the_generated_secret_is_not_printed_back(self):
        self._write_env("")
        self.sw.confirm = lambda *a, **k: True
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.sw.step_ntfy_topic()
        written = self.sw.env_value("OTTO_NTFY_TOPIC")
        self.assertTrue(written and len(written) >= 24)
        self.assertNotIn(written, out.getvalue())


class ResidentRuleGuardTests(unittest.TestCase):
    """Deterministic guards for CLAUDE.md's resident rules, paying down `RuleEnforcementTests`.

    Why these and not the judge: `conventions.select_rules` fits ~8 rules into one 2,000-char
    judging prompt against ~97 distilled from this repo, so on any given run roughly 90% of the
    rules are not in the judge's prompt AT ALL — before sampling variance. Prose enforcement is
    capped by that arithmetic; a test is not. Every rule that can be a grep should be one."""

    ROOT = os.path.dirname(os.path.abspath(__file__))

    def _src(self, name):
        with open(os.path.join(self.ROOT, name)) as fh:
            return fh.read()

    def test_secret_command_is_never_a_runtime_setting(self):
        """CLAUDE.md: `OTTO_SECRET_COMMAND` is env-only, never in `_SETTING_SPECS`. It is a
        shell command; settable over this unauthenticated API it leaves `_csrf_ok` as the only
        thing between a page the operator visits and code execution as them."""
        for key in config._SETTING_SPECS:
            self.assertNotIn("secret_command", key.lower())
            self.assertNotIn("OTTO_SECRET_COMMAND", str(config._SETTING_SPECS[key]))

    def test_workflow_code_never_reads_the_mutable_settings_store(self):
        """CLAUDE.md: workflow code must never call `config.setting()` — the store is mutable,
        so a replay could branch differently than the history recorded. `OttoWorkflow` takes ONE
        snapshot via `snapshot_settings` and reads it through `self._setting(...)`."""
        tree = ast.parse(self._src("workflows.py"))
        bad = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "setting" and getattr(n.func.value, "id", "") == "config"]
        self.assertEqual([], bad, "config.setting() in workflow code breaks replay determinism")

    def test_no_module_opens_its_own_sqlite_connection(self):
        """CLAUDE.md: SQLite stores go through `storage.sqlite_connect` + `storage.tx`.
        Connections are not thread-safe and `server.py` is threaded; a read-modify-write outside
        `BEGIN IMMEDIATE` lets concurrent writers upgrade-fail and silently lose writes."""
        offenders = []
        for path in sorted(glob.glob(os.path.join(self.ROOT, "*.py"))):
            name = os.path.basename(path)
            if name == "storage.py" or name.startswith(("test_", "regress")):
                continue
            with open(path) as fh:
                for i, line in enumerate(fh, 1):
                    if "sqlite3.connect(" in line:
                        offenders.append(f"{name}:{i}")
        self.assertEqual([], offenders, "use storage.sqlite_connect + storage.tx")

    def test_nothing_under_data_is_committed_except_the_gitkeep(self):
        """CLAUDE.md: `data/` is gitignored RUNTIME state. It holds the audit trail, plaintext
        API keys in models.json, transcripts and chat history — committing any of it publishes
        the operator's secrets and their run history to the repo."""
        tracked = subprocess.run(["git", "ls-files", "data/"], cwd=self.ROOT,
                                 capture_output=True, text=True, timeout=30).stdout.split()
        self.assertEqual(["data/.gitkeep"], sorted(tracked),
                         "data/ is runtime state - only .gitkeep belongs in git")

    def test_the_api_key_is_never_required_only_used_for_discovery(self):
        """CLAUDE.md: auth is the Claude subscription via `claude -p` — never require an API
        key. `ANTHROPIC_API_KEY` only auto-discovers the cloud model list, so it must resolve
        through `config.secret` and never gate a run."""
        gw = self._src("gateway.py")
        self.assertIn('config.secret("ANTHROPIC_API_KEY")', gw,
                      "the key must resolve through the secret helper, not os.environ")
        for name in ("engine.py", "claude_cli.py", "workflows.py", "activities.py"):
            self.assertNotIn("ANTHROPIC_API_KEY", self._src(name),
                             f"{name} must not reference the API key - runs use claude -p")


class RuleEnforcementTests(unittest.TestCase):
    """Every rule in the docs must name a guard test, or be listed here as a known gap.

    `ClaudeMdBudgetTests` ratchets how many BYTES the docs may spend; nothing ratcheted
    whether the code still obeys them. 119 of 164 rules named no test, and one of them —
    "JSON state writes go through storage.mutate_json" — was violated in four modules for
    months: `gateway.save`/`policy.save` wrote their stores with a plain `open(path, "w")`,
    losing 20 of 24 concurrent writes and serving a torn read as the DEFAULT config. The
    rule was written, correct and load-bearing. Nothing could execute it.

    So this is the same ratchet applied to enforcement instead of size. UNGUARDED is seeded
    with every rule that has no guard today, which makes the suite green on day one and the
    debt visible in one constant. It only ever SHRINKS: a NEW unguarded rule fails
    `test_no_new_rule_lands_without_a_guard`, and guarding an old one fails
    `test_the_known_gap_list_has_no_stale_entries` until its entry is deleted.

    Not every rule can be a grep — "a plan is approved in deploy order" is a judgement. Those
    stay here permanently, and that is fine: the point is that dropping a rule into the docs
    is a deliberate act with a visible cost, not a free assertion nobody checks."""

    ROOT = os.path.dirname(os.path.abspath(__file__))
    DOCS = ["CLAUDE.md"] + sorted(glob.glob(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".claude", "rules", "*.md")))
    _TOKEN = re.compile(r"`([^`]+)`")
    _TITLE = re.compile(r"^- \*\*(.+?)\*\*")

    # Rules with no guard test, as of the commit that added this class. SHRINKS ONLY.
    UNGUARDED = frozenset({
        # --- CLAUDE.md (8) ---
        "CLAUDE.md:Five ingresses normalize into one OttoWorkflow.",
        "CLAUDE.md:Global pause",
        "CLAUDE.md:Restart the worker after changing any module it imports",
        "CLAUDE.md:Run ids never collide across processes",
        "CLAUDE.md:Run regress.py before and after editing any prompt",
        "CLAUDE.md:Runtime settings store",
        "CLAUDE.md:Tests are ./.venv/bin/python -m unittest, never bare python3",
        "CLAUDE.md:This repo IS the live service's cwd.",
        # --- engine-core.md (2) ---
        "engine-core.md:One store alias, engine._DB (=config.DB_PATH)",
        "engine-core.md:engine.py is a FACADE, and callers keep addressing engine.X",
        # --- gateway-backends.md (27) ---
        "gateway-backends.md:--setting-sources user for any run with no cwd of its own",
        "gateway-backends.md:--strict-mcp-config only on tool-free calls",
        "gateway-backends.md:A Read(//path/",
        "gateway-backends.md:A cap failing on a local model is latched off it ACROSS runs",
        "gateway-backends.md:A cap needing a connector must not run locally",
        "gateway-backends.md:A context compaction must reach the caller or it is re-paid every turn",
        "gateway-backends.md:A deny rule covers rm through Bash, not just writes",
        "gateway-backends.md:A local resume saves IN PLACE",
        "gateway-backends.md:A path deny rule has exactly one working spelling: Edit(//abs/",
        "gateway-backends.md:A resume follows the SESSION's backend, never the phase model",
        "gateway-backends.md:A wall crosses to the engine as wall_reason (a plain string), never a ",
        "gateway-backends.md:Claude fallback is a flag",
        "gateway-backends.md:Every REGISTERED repo's live checkout is write-denied by default",
        "gateway-backends.md:Every local-endpoint request takes its headers from gateway.request_he",
        "gateway-backends.md:Measure the harness before blaming the model.",
        "gateway-backends.md:Model health is real-outcome-first",
        "gateway-backends.md:No fallback lands on haiku",
        "gateway-backends.md:One wall clock for both backends",
        "gateway-backends.md:Servable/unservable is the whole design",
        "gateway-backends.md:Still missing",
        "gateway-backends.md:The LOCAL backend bypasses claude -p's permission system entirely",
        "gateway-backends.md:This is ONE Admin control, not three",
        "gateway-backends.md:Three claude -p failures are WALLS, not harness deaths",
        "gateway-backends.md:Two bounds, not one",
        "gateway-backends.md:Which 400 is an overflow is error_classifier.classify, not _context_fi",
        "gateway-backends.md:Which failures are walls is error_classifier.classify, not an if-chain",
        "gateway-backends.md:discover_models groups by root, not one row per id",
        # --- ingress.md (15) ---
        "ingress.md:\"Run now\" starts a workflow directly, not ScheduleHandle.trigger()",
        "ingress.md:A Schedule fires from the Temporal server, so it has no in-process ste",
        "ingress.md:A cron and a required param with no default are mutually exclusive",
        "ingress.md:A cursor must be a Slack ts",
        "ingress.md:A human-authored graph is never re-planned",
        "ingress.md:A new task in an old conversation is handed off, not resumed",
        "ingress.md:A paused Slack poll must skip slack.poll entirely",
        "ingress.md:A pleasantry never starts a run",
        "ingress.md:A runbook's doc IS its approved plan",
        "ingress.md:A thread Otto replied in is watched",
        "ingress.md:Continuity is per-conversation",
        "ingress.md:Downtime guard",
        "ingress.md:First sight of a channel isn't its first message",
        "ingress.md:Per-step caps resolve up front or the plan never starts",
        "ingress.md:The store keeps a cap NAME, never its risk",
        # --- memory-privacy.md (18) ---
        "memory-privacy.md:A READ run has no cwd anchor and can pick the wrong sibling clone",
        "memory-privacy.md:A cap that prescribes its own output format outranks _TLDR_SHAPE",
        "memory-privacy.md:A conversational run can choose to say nothing",
        "memory-privacy.md:A fact from a verify-failed run is still stored, but labelled unverifi",
        "memory-privacy.md:A retry must never narrate itself",
        "memory-privacy.md:A single fact can be forgotten",
        "memory-privacy.md:A third-party Slack reader is not the owner",
        "memory-privacy.md:A verdict records WHICH judge reached it",
        "memory-privacy.md:Content minimization on push",
        "memory-privacy.md:Extraction rejects narration",
        "memory-privacy.md:Facts are dated and explicitly not authoritative",
        "memory-privacy.md:GC is two staged passes",
        "memory-privacy.md:Memory GC",
        "memory-privacy.md:Stale secondary sources are the standing failure mode for question-sha",
        "memory-privacy.md:The assistant cap's prompt and the memory-context header must agree",
        "memory-privacy.md:Who reads the result picks the contract",
        "memory-privacy.md:Write each pattern's test from the vendor's REAL key format, never fro",
        "memory-privacy.md:privacy.redact",
        # --- repo-work.md (17) ---
        "repo-work.md:A TERMINATE/CANCEL/TIMED_OUT delivers no exception into the workflow",
        "repo-work.md:A board card's Chat link needs a chat_key, and not every run gets one.",
        "repo-work.md:A cap's own PR wins even when Otto's branch also has work",
        "repo-work.md:A colleague's PR is never a target",
        "repo-work.md:A fix round never runs on the LOCAL backend",
        "repo-work.md:A post-PR fix round checks out the PR's head branch, not otto/<run_id>",
        "repo-work.md:A resume needs the workspace for its path, not its branch",
        "repo-work.md:A retry must inherit the dead run's reply_to",
        "repo-work.md:A retry must reattach to the dying run's OWN chat thread.",
        "repo-work.md:A retry that already reached RUN",
        "repo-work.md:Accept and dismiss are opposite verdicts.",
        "repo-work.md:An errored fix round ENDS the loop",
        "repo-work.md:Registered checkouts refresh via git fetch, never git pull",
        "repo-work.md:Repo-mode's PR base needs the same refresh",
        "repo-work.md:The Reaper is the backstop",
        "repo-work.md:The approved plan reaches the PR as a comment, never a committed file",
        "repo-work.md:The whole ladder is dead code if the chat never recorded repo/git_run_",
        # --- routing-capabilities.md (12) ---
        "routing-capabilities.md:A missing tool inside an agent cap is a frontmatter problem, not a hea",
        "routing-capabilities.md:A wrong route is usually retrieval, not the model",
        "routing-capabilities.md:Assistant redirect",
        "routing-capabilities.md:Clarify parse biases toward proceeding",
        "routing-capabilities.md:Classifier prompt-injection fence",
        "routing-capabilities.md:Follow-up handoff",
        "routing-capabilities.md:Fresh-route write gate",
        "routing-capabilities.md:Only user-scoped plugin installs are discovered",
        "routing-capabilities.md:Project capabilities",
        "routing-capabilities.md:Rank against the catalogue, never per-cap",
        "routing-capabilities.md:The follow-up classifier is read BOTH ways",
        "routing-capabilities.md:The listing is numbered from 1 and the LAST integer in the reply wins",
        # --- run-pipeline.md (10) ---
        "run-pipeline.md:\"Request changes\" (revise_plan)",
        "run-pipeline.md:A decline is audited under the run's OWN wid",
        "run-pipeline.md:A plan is enumerated in edit order but must be approved in deploy orde",
        "run-pipeline.md:A snapshot missing a key poisons the run forever",
        "run-pipeline.md:All FOUR judges route through it",
        "run-pipeline.md:Repo-conventions injection",
        "run-pipeline.md:The supervisor must see the verifier's critique",
        "run-pipeline.md:Unattended dead-end rule",
        "run-pipeline.md:conventions._SOURCES is the whole input set.",
        "run-pipeline.md:engine.critique_plan",
        # --- ui.md (4) ---
        "ui.md:An in-flight flag for a long action lives OUTSIDE the render",
        "ui.md:CSS.escape() is for identifiers, never inside a quoted attribute selec",
        "ui.md:Verifying UI changes headlessly",
        "ui.md:web/index.html contains NUL bytes",
    })

    @classmethod
    def _rules(cls):
        """(key, file, text) for every rule bullet, keyed on `<file>:<bold title>`."""
        for path in cls.DOCS:
            name = os.path.basename(path)
            with open(path if os.path.isabs(path) else os.path.join(cls.ROOT, path)) as fh:
                for line in fh:
                    text = line.strip()
                    if not text.startswith("- **"):
                        continue
                    m = cls._TITLE.match(text)
                    title = re.sub(r"\s+", " ", m.group(1) if m else text)
                    yield f"{name}:{title.replace('`', '').strip()[:70]}", name, text

    @classmethod
    def _names_a_guard(cls, text):
        # A citation is a backticked test class (`GroundingTests`, `test_core.PrTargetTests`),
        # a test method (`test_x`), or the elided form the docs use for long method names
        # (`…one_status_read_serves_both_consumers`). A run id like `web-2bd1a194` is not one.
        return any("Tests" in t or "test_" in t or (t.startswith("\u2026") and t.count("_") >= 2)
                   for t in cls._TOKEN.findall(text))

    def test_no_new_rule_lands_without_a_guard(self):
        new = sorted(k for k, _f, t in self._rules()
                     if not self._names_a_guard(t) and k not in self.UNGUARDED)
        self.assertEqual([], new, "\n\nRule(s) with no guard test. Either name one in the rule "
                                  "(the docs' own convention), or — if it is a judgement no grep "
                                  "can make — add the key above to UNGUARDED:\n  "
                         + "\n  ".join(new))

    def test_the_known_gap_list_has_no_stale_entries(self):
        # The ratchet's teeth: guarding a rule must DELETE its entry, or the debt count lies.
        live = {k for k, _f, t in self._rules() if not self._names_a_guard(t)}
        stale = sorted(self.UNGUARDED - live)
        self.assertEqual([], stale, "\n\nUNGUARDED entries that are no longer unguarded rules "
                                    "(guarded now, reworded, or deleted) - remove them:\n  "
                         + "\n  ".join(stale))

    def test_every_cited_guard_test_exists(self):
        # A rule naming a test that was renamed or deleted reads as enforced and is not.
        body = ""
        for m in sorted(glob.glob(os.path.join(self.ROOT, "test_*.py"))):
            with open(m) as fh:
                body += fh.read()
        missing = set()
        for _k, name, text in self._rules():
            for tok in self._TOKEN.findall(text):
                if "Tests" not in tok:
                    continue
                cited = tok.split(".")[-1]
                if not re.search(r"class %s\b" % re.escape(cited), body):
                    missing.add(f"{name} cites {cited}")
        self.assertEqual(set(), missing)


class ThemeUiTests(unittest.TestCase):
    """Admin -> Appearance -> Theme, in `web/index.html`.

    A palette is a wall of hex values with no behaviour of its own, so the failure mode is
    silence: a theme that half-applies still renders, just with one token inherited from a
    palette it was never meant to mix with. These pin the three couplings a later edit breaks
    without any visible error."""

    def _html(self):
        with open(os.path.join(os.path.dirname(__file__), "web", "index.html"),
                  encoding="utf-8", errors="surrogateescape") as f:
            return f.read()

    _PALETTE = re.compile(r"^\s*(:root, \[data-theme=\"[a-z-]+\"\]|:root\[data-theme="
                          r"\"[a-z-]+\"\], \[data-theme=\"[a-z-]+\"\]) \{(.*?)^  \}",
                          re.S | re.M)

    def _palettes(self):
        """{theme id: set of token names} for every palette block in the stylesheet."""
        out = {}
        for sel, body in self._PALETTE.findall(self._html()):
            name = re.search(r'\[data-theme="([a-z-]+)"\]', sel).group(1)
            out[name] = set(re.findall(r"(--[a-z0-9-]+)\s*:", body))
        return out

    def test_every_palette_defines_the_same_tokens(self):
        """A token added to one palette and not the others is the silent half-apply: the theme
        that lacks it inherits the DEFAULT palette's value, so a dark theme quietly paints one
        surface in cream. Compared against the default rather than a list written here, so
        adding a token to the palette is what forces the other four to be filled in."""
        pals = self._palettes()
        self.assertIn("chocolate-truffle", pals)
        base = pals["chocolate-truffle"]
        self.assertGreater(len(base), 20)
        for name, tokens in pals.items():
            self.assertEqual(base, tokens,
                             f"theme {name} does not define the same tokens as the default: "
                             f"missing {sorted(base - tokens)}, extra {sorted(tokens - base)}")

    def test_the_picker_and_the_stylesheet_list_the_same_themes(self):
        """THEMES drives the picker; the CSS blocks are what a pick actually applies. A name in
        one and not the other is either a card that changes nothing, or a palette nobody can
        reach."""
        html = self._html()
        registry = re.search(r"const THEMES=\[(.*?)\n\];", html, re.S).group(1)
        listed = set(re.findall(r'\{id:"([a-z-]+)"', registry))
        self.assertEqual(listed, set(self._palettes()))

    def test_every_palette_is_double_scoped(self):
        """Each block must match BOTH the root and any element carrying `data-theme`: the
        picker's cards preview a palette by wearing its attribute, so a root-only selector
        renders all five swatches in whatever theme the page already has. Enforced by the
        regex above, which only matches the two-selector form — this asserts the count so a
        theme dropped from the picker can't pass by simply not matching."""
        self.assertEqual(len(self._palettes()), 5)

    def test_the_theme_is_applied_before_the_first_paint(self):
        """The boot has to run in <head>, ahead of <body>: applied from the main script instead,
        every reload of a dark theme flashes the default cream ground first."""
        html = self._html()
        head = html.split("</head>", 1)[0]
        self.assertIn("window.applyTheme(window.currentTheme());", head)
        # and it must be the browser's own choice, not a server setting shared by every viewer
        self.assertIn('window.OTTO_THEME_KEY = "otto.theme"', head)


class AutoApproveToggleTests(unittest.TestCase):
    """The composer's "Auto approve" toggle pre-authorizes a chat's writes. It is the ONE way a
    browser can reach approval "auto", so the plumbing is guarded end to end: the client must send
    a boolean (never the `approval` string, which would hand a browser the unattended
    "ask"/"skip" modes too), the server must map it on BOTH ingresses a chat uses, and the
    composer must say which mode is live rather than leaving it to a toggle's colour."""

    def _src(self, name):
        with open(os.path.join(os.path.dirname(__file__), name),
                  encoding="utf-8", errors="surrogateescape") as f:
            return f.read()

    def test_the_toggle_defaults_to_off(self):
        html = self._src("web/index.html")
        i = html.index('id="autoapprove"')
        self.assertNotIn("checked", html[html.rindex("<input", 0, i):html.index(">", i)],
                         "the approval gate ships pre-authorized")
        self.assertIn("function selectedAutoApprove()", html)

    def test_both_chat_ingresses_forward_the_toggle(self):
        html = self._src("web/index.html")
        for marker in ('api("/api/submit",{request:req', 'api("/api/continue",{session_id'):
            call = html[html.index(marker):html.index("}))", html.index(marker)) + 3]
            self.assertIn("auto_approve: selectedAutoApprove()", call,
                          f"{marker} drops the approval toggle")

    def test_the_server_maps_a_boolean_and_never_takes_approval_from_the_body(self):
        src = self._src("server.py")
        # Anchored on the mapping, not its indentation — pinning leading spaces re-breaks
        # this whenever a handler moves (it did, when the POST chain became a dispatch table).
        pairs = re.findall(r'if body\.get\("auto_approve"\):\s*\n\s*params\["approval"\] = "auto"', src)
        self.assertEqual(2, len(pairs),
                         "/api/submit and /api/continue must both map the toggle")
        self.assertNotIn('body.get("approval")', src,
                         "approval must never be read straight off a request body")

    def test_the_composer_hint_states_which_mode_is_live(self):
        html = self._src("web/index.html")
        self.assertIn('id="gatehint"', html)
        self.assertIn("function applyApprovalHint()", html)
        self.assertIn('e.target.id==="autoapprove") applyApprovalHint()', html,
                      "nothing repaints the hint when the toggle flips")


class DiscussionTurnNoteTests(unittest.TestCase):
    """The note that makes a downgraded turn legible to the model. Its whole job is the case the
    classifier gets WRONG: without it a misread "add a test for that" reaches a model holding no
    Edit/Write and comes back as a tool-permission error, which reads as Otto being broken."""

    def test_absent_unless_the_turn_was_downgraded(self):
        self.assertIsNone(engine._discussion_note(False))

    def test_it_states_the_limit_and_the_way_out(self):
        note = engine._discussion_note(True)
        self.assertIn("read-only", note)
        # Describe the change rather than attempt it — the useful half of a misread turn.
        self.assertIn("DESCRIBE", note)
        # The recovery line: say it read as a question, don't surface a tool error.
        self.assertIn("asking again", note)
        self.assertNotIn("tool error", note.replace("do not report a tool error", ""))

    def test_a_resumed_discussion_turn_carries_it(self):
        # It has to reach the actual invocation, not just exist — the resume branch builds its
        # own sysctx and dropped everything but _RESUME_CONTRACT before this.
        seen = {}
        cap = registry.Capability("agent", "sre-minion", "implements a ticket")
        cap.risk = "read"
        orig = engine._claude
        try:
            def fake(prompt, **kw):
                seen["sys"] = kw.get("system_context") or ""
                return {"result": "ok", "cost": 0, "session_id": "s"}
            engine._claude = fake
            engine.run_attempt("why a mutex?", cap, resume_session="s1", discussion=True,
                               wid="wf-note")
        finally:
            engine._claude = orig
        self.assertIn("THIS TURN IS A CONVERSATION", seen.get("sys", ""))


class WorkerBranchEscapeHatchTests(unittest.TestCase):
    """The worker contract forbids git. Without an escape hatch that leaves a run handed the
    wrong branch with no legal move — and a capable model then deadlocks instead of failing
    (`web-d2438694`: 24 minutes and 784k input tokens spent reading Otto's own database and
    transcripts looking for a sanctioned way to deliver a change it could not reach)."""

    def test_the_contract_still_forbids_git(self):
        p = registry._general_worker().prompt
        self.assertIn("Do NOT manage git yourself", p)

    def test_reporting_a_wrong_branch_is_declared_a_successful_outcome(self):
        p = registry._general_worker().prompt.lower()
        self.assertIn("not in your working directory", p)
        self.assertIn("legitimate outcome", p)

    def test_the_two_wrong_moves_are_forbidden_by_name(self):
        p = registry._general_worker().prompt.lower()
        self.assertIn("switch branches", p)
        self.assertIn("substitute the nearest", p)
        self.assertIn("never inspect the platform's own internals", p)


class SystemContextTranscriptTests(unittest.TestCase):
    """Everything Otto tells a run that is not the request travels `system_context` — the
    approved plan, the workspace-mismatch note, the output contract, recalled memory. None of it
    was recorded, so a transcript could not answer "what was this model actually told?"."""

    def test_the_meta_line_records_it(self):
        src = open("claude_cli.py").read()
        self.assertIn('"system_context": system_context', src)

    def test_it_sits_next_to_the_prompt_not_inside_it(self):
        """They are separate arguments; folding one into the other would change what the model
        receives, not just what is logged."""
        i = src = open("claude_cli.py").read()
        meta = src[src.index('"type": "otto-meta"'):][:400]
        self.assertIn('"prompt": prompt', meta)
        self.assertIn('"system_context": system_context', meta)


class ResumeGroundingTests(unittest.TestCase):
    """A resume picks its branch from what the CHAT recorded, never from what the message asks
    about — so "ci#106 got the following review, pls fix the findings" re-checked out the
    original run's branch and worked on a tree with none of #106's code in it (`web-a6122d6c`)."""

    SRC = None

    def setUp(self):
        if ResumeGroundingTests.SRC is None:
            ResumeGroundingTests.SRC = open("workflows.py").read()
        self.src = ResumeGroundingTests.SRC

    def test_the_resume_path_computes_grounding(self):
        """Fresh-path-only grounding left the resume — the path most likely to be on a stale
        branch — with nothing checking it at all."""
        i = self.src.index("async def _resume_workspace")
        body = self.src[i:i + 6000]
        self.assertIn("check_grounding", body)
        self.assertIn("request=request", self.src)

    def test_it_repoints_only_on_a_demonstrated_mismatch(self):
        """A follow-up merely MENTIONING a PR ("like we did in #106") must not swap the tree
        under an ongoing session — repointing is a repair for a failed grounding check, not an
        inference about intent."""
        i = self.src.index("# REPAIR TIER")
        body = self.src[i:i + 2500]
        gate = body.index("if self._grounding:")
        resolve = body.index("resolve_pr_target")
        self.assertLess(gate, resolve, "the PR lookup must be gated on the mismatch, not run first")

    def test_it_reclears_the_note_after_a_successful_repoint(self):
        """Carrying the old note onto the branch that fixed it tells the run its tree is wrong
        when it no longer is."""
        i = self.src.index("# REPAIR TIER")
        body = self.src[i:i + 2500]
        self.assertEqual(body.count("check_grounding"), 2,
                         "expected a check before the repoint and a re-check after it")

    def test_the_note_reaches_the_resumed_turn(self):
        """Computed and dropped is the classic shape here — and it matters more on a resume: the
        session's history holds the ORIGINAL system prompt, so the worker contract's
        'if the code isn't here, say so' clause is not in scope for this message."""
        i = self.src.index('run_capability, {"request": request, "name": cap["name"], "resume": resume')
        self.assertIn('"grounding": self._grounding', self.src[i:i + 1400])
        eng = open("engine.py").read()
        j = eng.index('sysctx = "\\n\\n".join(filter(None, [')
        self.assertIn("_grounding_note(grounding)", eng[j:j + 900])



if __name__ == "__main__":
    unittest.main()


class RuntimeSettingsUiCoverageTests(unittest.TestCase):
    """Admin renders every runtime setting generically, so a NEW spec key needs no UI work to
    appear — which is exactly why it silently appears WRONG: unnamed in `SETTING_HELP` it renders
    as its raw store key with no explanation, and unlisted in `SETTING_GROUPS` it lands in
    "Other", away from the knob it belongs beside. `web/index.html` says as much in a comment
    over `SETTING_GROUPS` ("a hardcoded list must never be able to silently drop a setting the
    server grew") and nothing enforced it. Both directions matter: a stale entry for a deleted
    setting is dead copy that reads as a live knob."""

    def _blocks(self):
        with open(os.path.join(os.path.dirname(__file__), "web/index.html"),
                  encoding="utf-8", errors="surrogateescape") as f:
            src = f.read()
        i = src.index("const SETTING_GROUPS=[")
        return (src[src.index("const SETTING_HELP={"):i], src[i:src.index("];", i)])

    def test_every_setting_has_a_label_and_a_group_and_no_stale_entries(self):
        help_blk, grp_blk = self._blocks()
        keys = set(config._SETTING_SPECS)
        helped = set(re.findall(r"^  (\w+):\[", help_blk, re.M))
        grouped = set(re.findall(r'"([a-z][a-z0-9_]*)"', grp_blk))
        self.assertEqual(set(), keys - helped, "setting(s) with no SETTING_HELP label/help")
        self.assertEqual(set(), keys - grouped, "setting(s) in no SETTING_GROUPS group")
        self.assertEqual(set(), helped - keys, "SETTING_HELP entries for settings that are gone")
        self.assertEqual(set(), grouped - keys, "SETTING_GROUPS entries for settings that are gone")


class EffortPlumbingTests(unittest.TestCase):
    """Effort has to reach the model on EVERY path a run can take, or the setting is a placebo on
    whichever path was missed — and the miss is silent, because a run at the default effort looks
    exactly like a run that honoured the pick.

    The hops: composer/Admin -> workflow input -> the plan preview and all four run_capability
    callsites (fresh ladder, resume, QA-fix, review-fix) -> activity payload -> run_attempt ->
    each backend. Guarded structurally, since none of these can be driven end-to-end from a unit
    test."""

    def _src(self, name):
        with open(os.path.join(os.path.dirname(__file__), name), encoding="utf-8") as f:
            return f.read()

    def _payload_keys(self, tree, activity):
        """The literal dict keys of every execute_activity(<activity>, {...}) call."""
        import ast
        out = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "execute_activity"
                    and node.args and getattr(node.args[0], "id", None) == activity):
                continue
            for arg in node.args[1:]:
                if isinstance(arg, ast.Dict):
                    out.append((node.lineno, {k.value for k in arg.keys
                                              if isinstance(k, ast.Constant)}))
        return out

    def test_every_execution_and_preview_payload_carries_the_effort(self):
        import ast
        tree = ast.parse(self._src("workflows.py"))
        # 5 run_capability callsites: three ladder rungs' worth (fresh, fix rounds), the resumed
        # turn, and the unjudged brainstorm turn. Effort is per-TURN, so every one carries it.
        for activity, expected in (("run_capability", 5), ("plan_capability", 1)):
            payloads = self._payload_keys(tree, activity)
            self.assertEqual(len(payloads), expected,
                             f"{activity} callsite count changed — this test is stale")
            for lineno, keys in payloads:
                self.assertIn("effort", keys,
                              f"{activity} payload at workflows.py:{lineno} drops effort")

    def test_the_workflow_reads_the_default_from_its_snapshot_not_the_live_store(self):
        # A mutable store read inside workflow code sends a replay down a different branch than
        # history recorded — and effort is read once per run, so the divergence is permanent.
        import ast
        tree = ast.parse(self._src("workflows.py"))
        live = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "setting"
                and getattr(n.func.value, "id", None) == "config"]
        self.assertEqual(live, [], f"config.setting() called in workflow code at {live}")
        self.assertIn(
            'self._effort = config.resolve_effort(params.get("effort"), self._setting("effort"))',
            self._src("workflows.py"),
            "the workflow no longer resolves effort as composer-pick > snapshot default")
        self.assertEqual(config.resolve_effort("max", "low"), "max", "the pick must win")
        self.assertEqual(config.resolve_effort(None, "low"), "low", "the Admin default must apply")
        self.assertEqual(config.resolve_effort("turbo", "bogus"), "default",
                         "two unusable values must fall through to the sentinel")

    def test_the_effort_binding_happens_after_the_snapshot_activity(self):
        # Bound BEFORE the snapshot, `_setting` returns the import-time code default and the
        # Admin value is silently ignored on every run — the exact bug this ordering prevents.
        # Compared by position INSIDE _run_impl, so moving either statement is what trips it.
        import ast
        tree = ast.parse(self._src("workflows.py"))
        impl = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_run_impl")
        snap = [n.lineno for n in ast.walk(impl) if isinstance(n, ast.Call)
                and n.args and getattr(n.args[0], "id", None) == "snapshot_settings"]
        bind = [n.lineno for n in ast.walk(impl) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "_bind_composer"]
        self.assertEqual(len(snap), 1, "snapshot_settings callsite changed — test is stale")
        self.assertEqual(len(bind), 1, "_bind_composer callsite changed — test is stale")
        self.assertLess(snap[0], bind[0],
                        "the composer binding runs before the settings snapshot, so the Admin "
                        "effort default falls back to the code default on every run")

    def test_run_attempt_forwards_effort_to_both_backends(self):
        import ast, inspect, textwrap, engine
        tree = ast.parse(textwrap.dedent(inspect.getsource(engine.run_attempt)))
        for target in ("_claude", "run_json"):
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and (getattr(n.func, "id", None) == target
                          or getattr(n.func, "attr", None) == target)]
            self.assertTrue(calls, f"no {target}() call in run_attempt — test is stale")
            for call in calls:
                self.assertIn("effort", [kw.arg for kw in call.keywords],
                              f"run_attempt calls {target} without effort=")

    def test_the_activity_boundary_forwards_effort(self):
        src = self._src("activities.py")
        self.assertEqual(src.count('payload.get("effort")'), 2,
                         "run_capability and plan_capability must each forward effort")

    def test_the_setting_is_a_choice_over_exactly_the_cli_levels(self):
        # The store holds a NAME the CLI must accept. `claude -p` only WARNS on an unknown value,
        # so a spec that drifts from EFFORT_LEVELS yields runs at the default effort forever.
        _env, kind, const = config._SETTING_SPECS["effort"]
        self.assertEqual(kind, "choice:default," + ",".join(config.EFFORT_LEVELS))
        self.assertEqual(getattr(config, const), config.EFFORT)
        self.assertIsNone(config.effort_level("default"),
                          "'default' must mean 'send no flag', not a level")


class ClaudeSeamSignatureTests(unittest.TestCase):
    """`engine._claude` is the seam every test MOCKS (`_fake_claude(prompt, **k)`), so a kwarg
    the real function never grew is invisible to the whole suite: #376 added `steer=` to
    `claude_cli.run_json` and to `run_attempt`'s callsite, skipped the seam between them, and
    every execution attempt died `TypeError: _claude() got an unexpected keyword argument
    'steer'` in seconds — three rungs, then harness_exhausted. Assert the REAL signatures agree
    and that each kwarg is actually forwarded."""

    def test_every_run_attempt_kwarg_exists_on_the_real_claude_seam(self):
        import ast, inspect, textwrap, engine
        seam = set(inspect.signature(engine._claude).parameters)
        tree = ast.parse(textwrap.dedent(inspect.getsource(engine.run_attempt)))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_claude"]
        self.assertTrue(calls, "no _claude() call found in run_attempt — test is stale")
        for call in calls:
            for kw in call.keywords:
                self.assertIn(kw.arg, seam,
                              f"run_attempt passes {kw.arg}= to _claude, which does not accept it")

    def test_claude_seam_forwards_its_kwargs_to_run_json(self):
        import inspect, engine, claude_cli
        seam = set(inspect.signature(engine._claude).parameters) - {"prompt"}
        target = set(inspect.signature(claude_cli.run_json).parameters)
        self.assertTrue(seam <= target, f"_claude accepts {seam - target}, run_json does not")
        seen = {}

        def _spy(prompt, **kw):
            seen.update(kw)
            return {"result": "ok"}

        with mock.patch.object(claude_cli, "run_json", _spy):
            engine._claude("p", steer="SENTINEL", abort="A", model="m")
        self.assertEqual(seen.get("steer"), "SENTINEL",
                         "_claude accepted steer= but dropped it before run_json")
