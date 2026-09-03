"""Otto unit tests — plan gate, ladder, verify and supervisor.

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


class PlanPreviewAuditTests(unittest.TestCase):
    """The pre-approval preview is a full CLAUDE agentic pass with no local path — its spend
    must land in the audit trail (it was invisible: 'phases are local, where do my Claude
    credits go?')."""

    def test_preview_writes_a_plan_preview_audit_row(self):
        tmp = tempfile.mkdtemp(prefix="otto-prev-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        saved = (engine._DB, engine._claude)
        engine._DB = os.path.join(tmp, "otto.db")
        engine._claude = lambda *a, **k: {
            "result": "1. read the ticket\n2. edit server.py", "total_cost_usd": 0.42,
            "usage": {"input_tokens": 5, "output_tokens": 100, "cache_read_input_tokens": 9000}}
        try:
            cap = registry.Capability("custom", "worker", "implements tasks")
            preview = engine.plan_preview("fix the bug", cap, wid="web-test1")
            self.assertTrue(preview["plan"].startswith("1."))
            self.assertEqual(preview["cost"], 0.42)
            self.assertEqual(preview["tokens"]["output"], 100)
            rows = list(engine.iter_audit_entries())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "plan_preview")
            self.assertEqual(rows[0]["workflow"], "web-test1")
            self.assertEqual(rows[0]["cost_usd"], 0.42)
            self.assertEqual(rows[0]["tokens"]["output"], 100)
        finally:
            engine._DB, engine._claude = saved


class PlanParseTests(unittest.TestCase):
    """Pure parse of the swarm planner's reply (no LLM) — issue #4."""

    def test_single_means_no_fanout(self):
        self.assertEqual(engine._parse_plan("SINGLE", 5), [])
        self.assertEqual(engine._parse_plan("", 5), [])

    def test_parses_numbered_subtasks(self):
        plan = engine._parse_plan("2: check the failing build\n5: open a ticket for it", 6)
        self.assertEqual(plan, [{"index": 2, "subtask": "check the failing build"},
                                {"index": 5, "subtask": "open a ticket for it"}])

    def test_tolerates_punctuation_and_brackets(self):
        plan = engine._parse_plan("[0] do A\n1) do B\n3 - do C", 4)
        self.assertEqual([p["index"] for p in plan], [0, 1, 3])
        self.assertEqual(plan[0]["subtask"], "do A")

    def test_drops_out_of_range_and_garbage_lines(self):
        plan = engine._parse_plan("9: out of range\nnot a task line\n1: keep me", 3)
        self.assertEqual(plan, [{"index": 1, "subtask": "keep me"}])


class StepParseTests(unittest.TestCase):
    """The pure plan-then-execute parser (engine._parse_steps / _toposort): normalize the
    strong planner's JSON into an ordered, dependency-sane step chain, or [] when unusable."""

    def _steps(self, objs):
        return engine._parse_steps(json.dumps(objs))

    def test_parses_and_normalizes_minimal_steps(self):
        steps = self._steps([{"id": "s1", "goal": "read the file"},
                             {"id": "s2", "goal": "edit the file", "needs": ["s1"]}])
        self.assertEqual([s["id"] for s in steps], ["s1", "s2"])
        self.assertEqual(steps[0]["needs"], [])
        self.assertEqual(steps[1]["needs"], ["s1"])
        self.assertEqual(steps[0]["produces"], "s1")   # defaults to the id

    def test_unparseable_or_empty_yields_no_plan(self):
        self.assertEqual(engine._parse_steps("not json at all"), [])
        self.assertEqual(engine._parse_steps(""), [])
        self.assertEqual(engine._parse_steps("[]"), [])

    def test_tolerates_json_fence_and_surrounding_prose(self):
        text = "Sure, here is the plan:\n```json\n[{\"goal\":\"do A\"},{\"goal\":\"do B\"}]\n```\nHope that helps."
        steps = engine._parse_steps(text)
        self.assertEqual([s["goal"] for s in steps], ["do A", "do B"])
        self.assertEqual([s["id"] for s in steps], ["s1", "s2"])   # ids synthesized when absent

    def test_accepts_steps_object_wrapper(self):
        steps = engine._parse_steps('{"steps":[{"goal":"a"},{"goal":"b"}]}')
        self.assertEqual([s["goal"] for s in steps], ["a", "b"])

    def test_strips_leaked_reasoning_before_json(self):
        text = "<think>let me plan this carefully</think>[{\"goal\":\"a\"},{\"goal\":\"b\"}]"
        self.assertEqual([s["goal"] for s in engine._parse_steps(text)], ["a", "b"])

    def test_drops_goalless_and_nondict_items(self):
        steps = self._steps([{"id": "s1", "goal": "keep"}, {"id": "s2", "goal": ""},
                             "garbage", {"id": "s3", "goal": "also keep"}])
        self.assertEqual([s["id"] for s in steps], ["s1", "s3"])

    def test_drops_dangling_and_self_needs(self):
        steps = self._steps([{"id": "s1", "goal": "a", "needs": ["s1", "nope"]},
                             {"id": "s2", "goal": "b", "needs": ["s1"]}])
        self.assertEqual(steps[0]["needs"], [])       # self + unknown dropped
        self.assertEqual(steps[1]["needs"], ["s1"])

    def test_reorders_into_dependency_order(self):
        # Declared out of order: s2 depends on s1 but comes first.
        steps = self._steps([{"id": "s2", "goal": "b", "needs": ["s1"]},
                             {"id": "s1", "goal": "a"}])
        self.assertEqual([s["id"] for s in steps], ["s1", "s2"])

    def test_cycle_yields_no_plan(self):
        steps = self._steps([{"id": "s1", "goal": "a", "needs": ["s2"]},
                             {"id": "s2", "goal": "b", "needs": ["s1"]}])
        self.assertEqual(steps, [])

    def test_duplicate_ids_dropped(self):
        steps = self._steps([{"id": "s1", "goal": "a"}, {"id": "s1", "goal": "dup"},
                             {"id": "s2", "goal": "b"}])
        self.assertEqual([s["id"] for s in steps], ["s1", "s2"])
        self.assertEqual(steps[0]["goal"], "a")

    def test_capped_at_max_steps(self):
        steps = engine._parse_steps(
            json.dumps([{"goal": f"g{i}"} for i in range(50)]), max_steps=5)
        self.assertEqual(len(steps), 5)

    def test_available_ids_are_valid_needs_targets(self):
        # A re-plan tail may reference an ALREADY-COMPLETED step id in `needs` (kept, not dropped).
        steps = engine._parse_steps(
            '[{"id":"r1","goal":"reuse done work","needs":["s1"]}]', available={"s1"})
        self.assertEqual(steps[0]["needs"], ["s1"])

    def test_new_step_shadowing_a_completed_id_is_dropped(self):
        steps = engine._parse_steps(
            '[{"id":"s1","goal":"shadow"},{"id":"r1","goal":"keep"}]', available={"s1"})
        self.assertEqual([s["id"] for s in steps], ["r1"])


class PlanStepsTests(unittest.TestCase):
    """engine.plan_steps end-to-end with the strong planner call stubbed: pins to Claude,
    returns the ordered chain, and falls through ([]) on a single-step / failed plan."""

    def setUp(self):
        self._orig = engine.gateway.plan_complete
        self._trace = engine.trace
        engine.trace = lambda *a, **k: None

    def tearDown(self):
        engine.gateway.plan_complete = self._orig
        engine.trace = self._trace

    def _cap(self):
        cap = registry.Capability("agent", "worker", "general implementer")
        cap.risk = "write"
        return cap

    def test_uses_strong_planner_and_returns_chain(self):
        seen = {}
        def fake(prompt):
            seen["prompt"] = prompt
            return '[{"id":"s1","goal":"read"},{"id":"s2","goal":"edit","needs":["s1"]}]'
        engine.gateway.plan_complete = fake
        steps = engine.plan_steps("refactor the module", self._cap())
        self.assertEqual([s["id"] for s in steps], ["s1", "s2"])
        self.assertIn("worker", seen["prompt"])        # executor described to the planner
        self.assertIn("Edit", seen["prompt"])          # write cap -> WRITE_TOOLS listed

    def test_single_step_plan_falls_through(self):
        engine.gateway.plan_complete = lambda prompt: '[{"goal":"one shot"}]'
        self.assertEqual(engine.plan_steps("tiny task", self._cap()), [])

    def test_planner_failure_falls_through(self):
        def boom(prompt):
            raise RuntimeError("claude down")
        engine.gateway.plan_complete = boom
        self.assertEqual(engine.plan_steps("x", self._cap()), [])

    def test_replan_returns_repaired_tail_referencing_done_work(self):
        seen = {}
        def fake(prompt):
            seen["prompt"] = prompt
            return '[{"id":"r1","goal":"smaller piece","needs":["s1"]}]'
        engine.gateway.plan_complete = fake
        done = [{"step": {"id": "s1", "goal": "did this"}, "result": "OUT1"}]
        failed = {"id": "s2", "goal": "too big"}
        tail = engine.replan_steps("task", self._cap(), done, failed, "it was too big", [])
        self.assertEqual([s["id"] for s in tail], ["r1"])
        self.assertEqual(tail[0]["needs"], ["s1"])          # completed id survived as a valid need
        self.assertIn("it was too big", seen["prompt"])     # critique threaded to the planner
        self.assertIn("OUT1", seen["prompt"])               # completed output available to reference

    def test_replan_failure_returns_empty(self):
        def boom(prompt):
            raise RuntimeError("down")
        engine.gateway.plan_complete = boom
        self.assertEqual(
            engine.replan_steps("t", self._cap(), [], {"id": "s1", "goal": "g"}, "c", []), [])


class StepPromptTests(unittest.TestCase):
    """Dependency-scoped injection (design decision #1): a step sees the goal header, its own
    goal/context, and ONLY the outputs of the steps it declared in `needs` — bounded."""

    def test_injects_only_declared_needs(self):
        step = {"id": "s3", "goal": "combine", "context": "be terse",
                "needs": ["s1"], "produces": "combo", "done_when": "one file written"}
        store = {"s1": "OUTPUT-ONE", "s2": "OUTPUT-TWO-unrelated"}
        p = engine._step_prompt("the big task", step, store)
        self.assertIn("the big task", p)            # goal header always present
        self.assertIn("combine", p)                 # this step's goal
        self.assertIn("be terse", p)                # context
        self.assertIn("one file written", p)        # done_when
        self.assertIn("OUTPUT-ONE", p)              # declared need injected
        self.assertNotIn("OUTPUT-TWO", p)           # undeclared prior output NOT injected

    def test_injected_artifact_is_truncated(self):
        big = "X" * 5000
        p = engine._step_prompt("t", {"id": "s2", "goal": "g", "context": "", "needs": ["s1"],
                                      "produces": "s2", "done_when": ""}, {"s1": big})
        self.assertLessEqual(p.count("X"), config.PLAN_ARTIFACT_CHARS)


class StepInputTruncationTests(unittest.TestCase):
    """An unmarked cut in a prompt that FEEDS work manufactures the defect it is then failed for.

    The rule already existed for prompts that JUDGE (`plans._clipped`), but the three places that
    hand a model prior work — a step's declared `needs`, the re-plan digest, the final synthesis —
    were bare slices. A step told "Output of prior step s4 (use this)" received a Helm release
    table cut mid-row with nothing saying so, so it either published a confident total over the
    rows it could see or refused the step outright; verify then failed it, and the retry could not
    fix a truncation Otto itself introduced. Marked cuts + a bound that clears a real artifact."""

    STEP = {"id": "s2", "goal": "g", "context": "", "needs": ["s1"], "produces": "s2",
            "done_when": ""}

    def test_a_cut_step_input_is_marked_as_cut(self):
        p = engine._step_prompt("t", self.STEP, {"s1": "ROW\n" * 4000})
        self.assertIn("CUT at", p, "a bare slice reads as the prior step's whole output")
        self.assertIn(str(config.PLAN_ARTIFACT_CHARS), p)

    def test_the_marker_forbids_building_a_total_on_the_hole(self):
        # The opposite instruction to the judge's marker: a judge must ignore what it cannot see,
        # an executor must NOT quietly compute over it. Both failure modes were observed.
        p = engine._step_prompt("t", self.STEP, {"s1": "ROW\n" * 4000})
        self.assertRegex(p, r"do not present a total, a diff or a complete list")
        self.assertIn("UNKNOWN, not absent", p)

    def test_an_uncut_step_input_carries_no_marker(self):
        # Marker noise on every step would train the model to ignore it.
        p = engine._step_prompt("t", self.STEP, {"s1": "short output"})
        self.assertIn("short output", p)
        self.assertNotIn("CUT at", p)

    def test_the_bound_clears_a_realistic_inventory_artifact(self):
        # 1500 chars did not fit one Helm release table, which is precisely the artifact plan-mode
        # fans out to produce. Rows are sized as the real ones were — fully-qualified release and
        # namespace plus chart version, status and timestamp — because a toy 40-char row fits the
        # OLD bound too and would prove nothing.
        table = "".join(
            f"| webapp-helm-signalling-service-{i:02d} | webapp-{i:02d} | signalling-service-1.14.{i} "
            f"| deployed | 2026-08-01 12:03:41 |\n" for i in range(20))
        self.assertGreater(len(table), 1500, "the fixture must exceed the bound this fixes")
        p = engine._step_prompt("t", self.STEP, {"s1": table})
        self.assertIn("signalling-service-19", p,
                      "a normal inventory step's deliverable must not be cut")
        self.assertNotIn("CUT at", p)

    def test_a_cut_replan_digest_is_marked(self):
        line = engine._step_digest({"id": "s1", "goal": "g"}, result="Y" * 40000)
        self.assertIn("CUT at", line)

    def test_a_percent_sign_in_a_step_survives_the_synthesis(self):
        # Threading the clipped body in via %-formatting looked equivalent and was not: a step
        # goal or output legitimately contains "%" ("cut spend by 20%"), and %-formatting a
        # string that has already interpolated it raises. Caught pre-merge, pinned here.
        import plans
        orig = plans.gateway.complete
        plans.gateway.complete = lambda tier, prompt, **kw: "merged"
        try:
            out = plans._synthesize_plan("t", [
                {"step": {"id": "s1", "goal": "cut GPU spend by 20% in prod-a"},
                 "result": "saved 15% (%s of budget)", "passed": True},
                {"step": {"id": "s2", "goal": "b"}, "result": "ok", "passed": True}])
        finally:
            plans.gateway.complete = orig
        self.assertEqual(out, "merged")

    def test_a_cut_step_output_is_marked_in_the_final_synthesis(self):
        # A silently-cut step output drops real outcomes (IDs, links, statuses) out of the answer
        # the user reads, with nothing to say the synthesis was working from a fragment.
        import plans
        seen = {}
        orig = plans.gateway.complete
        plans.gateway.complete = lambda tier, prompt, **kw: seen.setdefault("p", prompt) or "merged"
        try:
            plans._synthesize_plan("t", [
                {"step": {"id": "s1", "goal": "a"}, "result": "A" * 40000, "passed": True},
                {"step": {"id": "s2", "goal": "b"}, "result": "short", "passed": True}])
        finally:
            plans.gateway.complete = orig
        self.assertIn("CUT at", seen["p"])


class PlanModeGateTests(unittest.TestCase):
    """config.PLAN_MODE gating of plan_mode_active."""

    def setUp(self):
        self._mode = config.PLAN_MODE
        self._entry = engine.gateway.exec_model_entry

    def tearDown(self):
        config.PLAN_MODE = self._mode
        engine.gateway.exec_model_entry = self._entry

    def _cap(self):
        return registry.Capability("agent", "worker", "impl")

    def _set_executor(self, provider):
        engine.gateway.exec_model_entry = lambda name=None, cfg=None: {"provider": provider, "name": "m"}

    def test_off_never_engages(self):
        config.PLAN_MODE = "off"
        self._set_executor("local")
        self.assertFalse(engine.plan_mode_active(self._cap(), requested=True))

    def test_opt_in_only_when_requested(self):
        config.PLAN_MODE = "opt-in"
        self._set_executor("local")
        self.assertTrue(engine.plan_mode_active(self._cap(), requested=True))
        self.assertFalse(engine.plan_mode_active(self._cap(), requested=False))

    def test_auto_local_engages_on_local_executor(self):
        config.PLAN_MODE = "auto-local"
        self._set_executor("local")
        self.assertTrue(engine.plan_mode_active(self._cap()))
        self._set_executor("claude")
        self.assertFalse(engine.plan_mode_active(self._cap()))   # invisible on a Claude executor


class ConventionsTests(unittest.TestCase):
    """Repo-conventions digest (PR #251 post-mortem): the judged phases automatically see
    the target repo's own CLAUDE.md hard rules — always on, zero-config — with precedence
    over the request itself. Distillation is cached on the source file's mtime+size."""

    def setUp(self):
        import conventions
        self.conventions = conventions
        self.tmp = tempfile.mkdtemp(prefix="otto-conv-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".claude"))
        self._store = conventions._STORE
        conventions._STORE = os.path.join(self.tmp, "conventions.json")
        self._complete = gateway.complete
        self._trace = engine.trace
        engine.trace = lambda *a, **k: None

    def tearDown(self):
        self.conventions._STORE = self._store
        gateway.complete = self._complete
        engine.trace = self._trace
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_claude_md(self, text):
        with open(os.path.join(self.repo, ".claude", "CLAUDE.md"), "w") as f:
            f.write(text)

    def test_heuristic_extracts_constraint_lines(self):
        rules = self.conventions._heuristic_rules(
            "# Guide\nThe stack uses Terraform.\n"
            "Never declare random_password in a module.\n"
            "- Always run the tests before opening a PR.\n"
            "Never declare random_password in a module.\n")
        self.assertEqual(rules, ["Never declare random_password in a module.",
                                 "Always run the tests before opening a PR."])

    def test_digest_distills_once_and_caches_on_mtime(self):
        self._write_claude_md("Secrets doc.\nNever generate credentials in Terraform.\n")
        calls = []
        gateway.complete = lambda task, prompt: (calls.append(task),
                                                 "Never generate credentials in Terraform")[1]
        self.assertEqual(self.conventions.digest(self.repo),
                         ["Never generate credentials in Terraform"])
        self.assertEqual(self.conventions.digest(self.repo),
                         ["Never generate credentials in Terraform"])
        self.assertEqual(len(calls), 1, "second digest() must come from the cache")
        # Changing the file (size changes too) invalidates the cache.
        self._write_claude_md("Secrets doc, revised.\nNever generate credentials in Terraform.\n")
        self.conventions.digest(self.repo)
        self.assertEqual(len(calls), 2)

    def test_repo_without_claude_md_yields_nothing_and_no_calls(self):
        gateway.complete = lambda task, prompt: self.fail("must not call the model")
        self.assertEqual(self.conventions.digest(self.repo), [])
        self.assertIsNone(self.conventions.judge_block(self.repo))

    def test_gateway_failure_falls_back_to_heuristic(self):
        self._write_claude_md("Never push directly to main.\nSome prose.\n")

        def _boom(task, prompt):
            raise RuntimeError("gateway down")
        gateway.complete = _boom
        self.assertEqual(self.conventions.digest(self.repo), ["Never push directly to main."])

    def test_verify_injects_conventions_with_precedence(self):
        self._write_claude_md("Never generate credentials in Terraform.\n")
        prompts = {}

        def _fake(task, prompt):
            prompts.setdefault(task, []).append(prompt)
            return ("Never generate credentials in Terraform" if task == "memory" else "PASS")
        gateway.complete = _fake
        cap = registry.Capability("agent", "sre-minion", "implements issues")
        verdict = engine.verify("do the ticket", cap, "done", project=self.repo)
        self.assertTrue(verdict["passed"])
        prompt = prompts["verify"][0]
        self.assertIn("REPO CONVENTIONS", prompt)
        self.assertIn("Never generate credentials in Terraform", prompt)
        self.assertIn("OVERRIDE the request", prompt)
        # QA judge gets the same block.
        engine.judge_qa("do the ticket", "qa transcript", project=self.repo)
        self.assertIn("REPO CONVENTIONS", prompts["verify"][1])


class ConventionsDigestBudgetTests(unittest.TestCase):
    """How much of a repo's rule corpus reaches ONE judging prompt.

    The bound was a fixed 2_000 chars, chosen when the cheap tier might be a small local
    model. Measured on this repo's own 224 extracted rules it admitted 20 of them, and the
    survivors were whatever ranked top for the request rather than what the change touched:
    the judge failed a COMPLIANT result on 4 runs in 5, each time citing a rule that result
    could not have violated, while the real violation in a second candidate went unnamed on
    5 of 5. At a budget fitting the whole corpus the same judge was right 10 times out of 10.

    So the bound belongs to the CORPUS, not the model — the judge tier here serves a 1M-token
    window, and 24_000 chars is well under a percent of it. It is a runtime setting because
    the corpus grows: a repo outgrowing the default is an Admin edit, not a deploy, and a
    genuinely small-context judge is the case for lowering it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="otto-digestbudget-")
        self._path = config._SETTINGS_PATH
        config._SETTINGS_PATH = os.path.join(self.tmp, "settings.json")

    def tearDown(self):
        config._SETTINGS_PATH = self._path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _corpus(self, n=224, width=87):
        """This repo's own measured shape: 224 rules averaging ~87 chars after distillation."""
        return [(f"Rule {i:03d}: never write module_{i} state without going through the helper."
                 .ljust(width))[:width] for i in range(n)]

    def test_the_default_budget_fits_a_corpus_the_size_of_this_repos_own(self):
        rules = self._corpus()
        kept, dropped = conventions.select_rules(rules, "change how module_7 writes state")
        self.assertGreaterEqual(
            len(kept), int(len(rules) * 0.95),
            "the default budget must carry a corpus this repo's own size; at the old 2_000 it "
            "carried 20 of 224 and the judge cited whatever ranked top instead of the violation")
        self.assertEqual(dropped, len(rules) - len(kept))

    def test_the_budget_is_a_runtime_setting_not_a_constant(self):
        rules = self._corpus()
        config.save_settings({"conventions_digest_chars": 2000})
        narrow, _ = conventions.select_rules(rules, "change how module_7 writes state")
        config.save_settings({"conventions_digest_chars": 24000})
        wide, _ = conventions.select_rules(rules, "change how module_7 writes state")
        self.assertLess(len(narrow), len(wide),
                        "select_rules must read the live setting, not a frozen constant")

    def test_an_explicit_budget_argument_still_wins(self):
        # The callers that pass one (tests, and any future per-model ceiling) must not be
        # silently overridden by the store.
        kept, _ = conventions.select_rules(self._corpus(), "module_7", budget=200)
        self.assertLess(len(kept), 5)


class ConventionsNoiseTests(unittest.TestCase):
    """A repo's CLAUDE.md states operator instructions as hard imperatives ("restart the
    worker after changing any module it imports"), and the old extraction took them as rules.

    The judge sees only the run's final text output, so no diff can ever satisfy one — it is
    a defect the judge invents and then fails compliant work for, and the critique it writes
    steers the retry at nothing. Measured on this repo before the fix: that single rule was
    top-ranked for an unrelated request and produced FAIL on 4 of 5 runs, violating and
    compliant alike, citing it every time; the real violation in the same output was never
    named. Same failure `verify`'s tool-grant block exists to prevent (`JudgeToolGrantTests`),
    reached through a different input.

    The prompt does the semantic sort, because phrasing varies too much for a pattern. The
    DROP is `_judgeable`, in code, so these assert the exclusion without a model in the loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="otto-convnoise-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".claude"))
        self._store = conventions._STORE
        conventions._STORE = os.path.join(self.tmp, "conventions.json")
        self._complete = gateway.complete
        with open(os.path.join(self.repo, ".claude", "CLAUDE.md"), "w") as f:
            f.write("- **Never write a store with open()** - use storage.mutate_json.\n"
                    "- **Restart the worker after changing any module it imports.**\n")

    def tearDown(self):
        conventions._STORE = self._store
        gateway.complete = self._complete
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reply(self, text):
        gateway.complete = lambda task, prompt: text

    def test_an_operator_labelled_rule_never_reaches_the_digest(self):
        self._reply("CODE: JSON writes must go through storage.mutate_json\n"
                    "OPERATOR: restart the worker after changing any module it imports")
        rules = conventions.digest(self.repo)
        self.assertTrue(any("mutate_json" in r for r in rules),
                        "the code-checkable rule must survive")
        self.assertFalse(any("restart the worker" in r.lower() for r in rules),
                         "an operator instruction cannot be satisfied by any result and "
                         "must never reach a judging prompt")

    def test_the_kind_label_is_stripped_from_a_kept_rule(self):
        # A judge shown "CODE: never do X" reads the label as part of the convention.
        self._reply("CODE: JSON writes must go through storage.mutate_json")
        rules = conventions.digest(self.repo)
        self.assertEqual(rules, ["JSON writes must go through storage.mutate_json"])

    def test_an_unlabelled_line_is_kept_not_dropped(self):
        # The model ignoring the format is a formatting failure, not a verdict that every
        # rule is operator-only. This module's contract is that the digest degrades rather
        # than disappears, so a format wobble must not cost the repo its enforcement.
        self._reply("JSON writes must go through storage.mutate_json\n"
                    "Never commit anything under data/")
        rules = conventions.digest(self.repo)
        self.assertEqual(len(rules), 2, "unlabelled lines are kept, not dropped")

    def test_the_operator_label_is_matched_however_the_model_dresses_it(self):
        for line in ("OPERATOR: run the suite", "operator: run the suite",
                     "**OPERATOR**: run the suite", "Operator : run the suite"):
            self.assertEqual(conventions._judgeable(line), "", f"not dropped: {line!r}")


class ConventionsBudgetTests(unittest.TestCase):
    """When a repo has more hard rules than fit one judging prompt, WHICH ones reach the judge
    must follow the request. It used to follow document order, silently: what got enforced was
    decided by where a section sat in CLAUDE.md, and reordering the file swapped the enforced
    set out from under it (measured on this repo: ~100 rules extracted, 26 kept, 1 surviving a
    pure documentation edit). Ranked against the request now, and the trim is declared — same
    contract as `mcp_client.Pool.trimmed`.

    Trimming is no longer the steady state (`ConventionsDigestBudgetTests` sizes the budget to
    hold a corpus this repo's own size whole), so these force it: it is what a bigger repo, or
    a smaller judge model, still degrades into."""

    def setUp(self):
        import conventions
        self.conventions = conventions
        self.tmp = tempfile.mkdtemp(prefix="otto-convbudget-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".claude"))
        self._store = conventions._STORE
        conventions._STORE = os.path.join(self.tmp, "conventions.json")
        self._complete = gateway.complete
        self._settings = config._SETTINGS_PATH
        config._SETTINGS_PATH = os.path.join(self.tmp, "settings.json")

    def tearDown(self):
        self.conventions._STORE = self._store
        gateway.complete = self._complete
        config._SETTINGS_PATH = self._settings
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_claude_md(self, text):
        with open(os.path.join(self.repo, ".claude", "CLAUDE.md"), "w") as f:
            f.write(text)

    def _many_rules(self, n=200):
        return [f"Never use the deprecated pattern number {i} anywhere in this repository."
                for i in range(n)]

    def test_the_budget_keeps_what_the_request_is_about_not_what_came_first(self):
        # The relevant rule is LAST in the document — under document-order trimming it could
        # never reach the judge, which is exactly how "never leak credentials" fell out.
        rules = [f"Never use pattern {i} in the module." for i in range(40)]
        rules.append("Never generate credentials with random_password in Terraform.")
        kept, dropped = self.conventions.select_rules(
            rules, "add a terraform module that needs credentials", budget=200)
        self.assertGreater(dropped, 0, "the budget must actually bind for this to prove anything")
        self.assertIn("Never generate credentials with random_password in Terraform.", kept)

    def test_the_cache_holds_the_whole_extraction_not_a_trimmed_view(self):
        # Trimming at extraction time bakes one arbitrary subset into the cache forever; the
        # request that decides which rules matter isn't known until a run happens.
        self._write_claude_md("\n".join(self._many_rules()))
        gateway.complete = lambda task, prompt: (_ for _ in ()).throw(RuntimeError("gateway down"))
        rules = self.conventions.digest(self.repo)
        self.assertGreater(len(rules), 100,
                           "digest() must cache every extracted rule, not 2k chars' worth")

    def test_a_trimmed_block_declares_the_trim(self):
        # The default budget now holds a corpus this repo's size whole, so a trim has to be
        # FORCED to test it. It stays reachable — a bigger repo, or a smaller judge model —
        # and a trim that doesn't declare itself is the same lie it always was.
        self._write_claude_md("\n".join(self._many_rules()))
        gateway.complete = lambda task, prompt: (_ for _ in ()).throw(RuntimeError("gateway down"))
        config.save_settings({"conventions_digest_chars": 2000})
        block = self.conventions.judge_block(self.repo, "pattern 7")
        self.assertIn("NOT shown", block)
        self.assertIn("never infer from a rule's absence", block.lower())
        self.assertIn("OVERRIDE the request", block)

    def test_an_untrimmed_block_says_nothing_about_dropping(self):
        # Every clean run must not grow a caveat, or the caveat stops being read.
        self._write_claude_md("Never push directly to main.\n")
        gateway.complete = lambda task, prompt: (_ for _ in ()).throw(RuntimeError("gateway down"))
        block = self.conventions.judge_block(self.repo, "ship the feature")
        self.assertIn("Never push directly to main.", block)
        self.assertNotIn("NOT shown", block)

    def test_selection_is_stable_for_the_same_request(self):
        rules = self._many_rules(100)
        first, _ = self.conventions.select_rules(rules, "pattern 42 deprecated", budget=300)
        second, _ = self.conventions.select_rules(rules, "pattern 42 deprecated", budget=300)
        self.assertEqual(first, second, "two runs of one request must enforce the same rules")

    def test_a_pre_fix_cache_entry_is_re_derived_rather_than_served(self):
        # A v1 entry holds the old truncated digest under a fingerprint that still matches, so
        # without a version check the fix would never take effect on an existing install.
        self._write_claude_md("Never generate credentials in Terraform.\n")
        calls = []
        gateway.complete = lambda task, prompt: (
            calls.append(task), "Never generate credentials in Terraform")[1]
        self.conventions.digest(self.repo)
        self.assertEqual(len(calls), 1)

        def _downgrade(store):
            store[self.repo].pop("v", None)
            store[self.repo]["rules"] = ["stale truncated rule"]
            return store
        storage.mutate_json(self.conventions._STORE, _downgrade, {})
        self.assertEqual(self.conventions.digest(self.repo),
                         ["Never generate credentials in Terraform"])
        self.assertEqual(len(calls), 2, "a v1 entry must be re-derived, not served")

    def test_status_never_derives(self):
        # status() backs an Admin fetch inside loadAdmin's Promise.all, so a model call here
        # would become the panel's load time — once per repo, on every open.
        self._write_claude_md("Never push directly to main.\n")
        gateway.complete = lambda task, prompt: self.fail("status() must not derive")
        s = self.conventions.status(self.repo)
        self.assertEqual(s["state"], "none")
        self.assertEqual(s["count"], 0)

    def test_status_reports_absent_none_fresh_and_stale(self):
        self.assertEqual(self.conventions.status(self.repo)["state"], "absent")
        self._write_claude_md("Never push directly to main.\n")
        self.assertEqual(self.conventions.status(self.repo)["state"], "none")
        gateway.complete = lambda task, prompt: "Never push directly to main."
        self.conventions.digest(self.repo)
        s = self.conventions.status(self.repo)
        self.assertEqual(s["state"], "fresh")
        self.assertEqual(s["count"], 1)
        self._write_claude_md("Never push directly to main.\nNever force-push a shared branch.\n")
        self.assertEqual(self.conventions.status(self.repo)["state"], "stale")

    def test_the_stat_only_fingerprint_matches_the_one_derivation_records(self):
        # They are computed in two places; if they drift, every status() reads "stale" forever
        # and the UI cries wolf on repos that are perfectly current.
        self._write_claude_md("Never push directly to main.\n")
        _, recorded = self.conventions._read_sources(self.repo)
        self.assertEqual(self.conventions._fingerprint(self.repo), recorded)

    def test_refresh_re_derives_even_when_the_cache_is_fresh(self):
        self._write_claude_md("Never push directly to main.\n")
        calls = []
        gateway.complete = lambda task, prompt: (calls.append(task), "Never push directly to main.")[1]
        self.conventions.digest(self.repo)
        self.assertEqual(len(calls), 1)
        self.conventions.digest(self.repo)
        self.assertEqual(len(calls), 1, "unchanged file must stay cached")
        s = self.conventions.refresh(self.repo)
        self.assertEqual(len(calls), 2, "refresh must ignore a fresh cache")
        self.assertEqual(s["state"], "fresh")

    def test_every_judge_passes_the_request_so_ranking_has_something_to_rank_on(self):
        # judge_block(project) with no request silently reverts to document order — the bug.
        import judging
        import plans
        for mod in (judging, plans):
            src = inspect.getsource(mod)
            self.assertNotIn("judge_block(project)", src,
                             f"{mod.__name__} must pass the request into judge_block")

    def test_verify_without_project_is_unchanged(self):
        def _fake(task, prompt):
            self.assertNotEqual(task, "memory", "no project -> no distillation call")
            self.assertNotIn("REPO CONVENTIONS", prompt)
            return "PASS"
        gateway.complete = _fake
        cap = registry.Capability("agent", "sre-minion", "implements issues")
        self.assertTrue(engine.verify("do the ticket", cap, "done")["passed"])


class VerifyLocalAttemptTests(unittest.TestCase):
    """The verifier must know a LOCAL attempt had no tools — the standard prompt vouches for
    real tool access, which would launder tool-shaped output a local model fabricated."""

    def setUp(self):
        self._complete = gateway.complete
        self.prompts = []
        gateway.complete = lambda task, prompt: (self.prompts.append(prompt), "PASS")[1]
        self.cap = registry.Capability("custom", "summarize", "summarize text")
        self.cap.risk = "read"

    def tearDown(self):
        gateway.complete = self._complete

    def test_local_attempt_judged_as_no_tool(self):
        engine.verify("summarize this", self.cap, "a summary", local=True)
        self.assertIn("NO tool access", self.prompts[0])
        self.assertNotIn("real tool access", self.prompts[0])

    def test_claude_attempt_keeps_the_tool_grant(self):
        engine.verify("summarize this", self.cap, "a summary")
        self.assertIn("real tool access", self.prompts[0])

    def test_judge_told_reasoning_is_not_a_result(self):
        # Defence-in-depth (run web-ccbb5378): the judge must FAIL a chain-of-thought blob even
        # if the runtime's own reasoning guard let it through.
        engine.verify("do x", self.cap, "some output")
        self.assertIn("chain-of-thought", self.prompts[0])
        self.assertIn("finished RESULT is required", self.prompts[0])


class VerifyUnattendedTests(unittest.TestCase):
    """An unattended run's judge must FAIL a report whose bottom line is a question to the
    user — nobody is present to answer it (the daily-summary blocked-Write dead-end,
    2026-07-28: "Want me to retry, or should I skip it?" was delivered as the result and
    verify passed it)."""

    def setUp(self):
        self._complete = gateway.complete
        self.prompts = []
        gateway.complete = lambda task, prompt: (self.prompts.append(prompt), "PASS")[1]
        self.cap = registry.Capability("custom", "summarize", "summarize text")
        self.cap.risk = "read"

    def tearDown(self):
        gateway.complete = self._complete

    def test_unattended_judge_gets_the_dead_end_rule(self):
        engine.verify("daily summary", self.cap, "Want me to retry?", unattended=True)
        self.assertIn("UNATTENDED", self.prompts[0])
        self.assertIn("dead end", self.prompts[0])

    def test_interactive_judge_does_not(self):
        # A question in an interactive run is legit — the session continues.
        engine.verify("daily summary", self.cap, "a summary")
        self.assertNotIn("UNATTENDED", self.prompts[0])

    def test_slack_audience_replaces_the_dead_end_rule(self):
        """The dead-end rule's premise is false on Slack: the reader is present and a thread reply
        resumes the session, so a short question back is a complete answer. Without this carve-out
        the two 2026-07-31 fixes fight — "Dammit" and "nope" each burned 2-3 attempts and an Opus
        escalation because every attempt ended by asking and the judge failed it for asking."""
        engine.verify("a slack message", self.cap, "Hey — what's up?",
                      unattended=True, audience="conversation")
        p = self.prompts[0]
        self.assertIn("VERBATIM", p)
        self.assertIn("question back to them PASSES", p)
        self.assertNotIn("dead end by construction", p)     # the unattended rule is REPLACED
        # …and it judges the failure that actually happened instead: leaked scaffolding.
        self.assertIn("here's the reply to send", p.lower())


class JudgeConfirmationTests(unittest.TestCase):
    """`claude -p` exposes no temperature/top-p/seed (65 flags, none for sampling), so a judge on
    the Claude backend is sampled and cannot be pinned the way the OpenAI-compatible path already
    is. Measured on ONE fixed, complete, CORRECT output: 12 PASS / 8 FAIL over 20 judgements, while
    a clearly-bad output failed 0/5 — the instability sits on GOOD results, where a wrong verdict
    is most expensive. A false FAIL is not free: it burns a retry, and on a write cap the retry is
    what widens blast radius. So an adverse verdict must reproduce before it is acted on — measured
    A/B on that input, false FAIL 5/10 → 2/10 for 2.20 judge calls per verdict instead of 1.00. It
    reduces the flip, it does not remove it: at p≈0.5 unanimity-of-3 still lets ~12% through."""

    def setUp(self):
        self._complete = gateway.complete
        self.replies = []
        self.calls = []
        gateway.complete = lambda task, prompt: (self.calls.append(task),
                                                 self.replies.pop(0))[1]
        self.cap = registry.Capability("custom", "summarize", "summarize text")
        self.cap.risk = "read"
        os.environ["OTTO_JUDGE_CONFIRMATIONS"] = "3"
        self.addCleanup(os.environ.pop, "OTTO_JUDGE_CONFIRMATIONS", None)

    def tearDown(self):
        gateway.complete = self._complete

    def test_a_pass_costs_exactly_one_sample(self):
        # The common case must pay nothing extra, or confirmation is a tax on every good run.
        self.replies = ["PASS"]
        self.assertTrue(engine.verify("do it", self.cap, "done")["passed"])
        self.assertEqual(len(self.calls), 1)

    def test_a_fail_that_does_not_reproduce_is_not_acted_on(self):
        # The measured failure mode: one unlucky sample condemning a correct run.
        self.replies = ["FAIL\nnot thorough enough", "PASS"]
        v = engine.verify("do it", self.cap, "done")
        self.assertTrue(v["passed"], "a FAIL contradicted on re-sampling must not stand")
        self.assertEqual(len(self.calls), 2, "and it must stop as soon as it is contradicted")

    def test_a_fail_that_reproduces_stands_with_its_critique(self):
        self.replies = ["FAIL\nmissing the numbers", "FAIL\nstill missing them", "FAIL\nno numbers"]
        v = engine.verify("do it", self.cap, "done")
        self.assertFalse(v["passed"])
        self.assertIn("missing the numbers", v["critique"],
                      "the retry is steered by the FIRST critique, as before")
        self.assertEqual(len(self.calls), 3)

    def test_the_late_contradiction_still_wins(self):
        self.replies = ["FAIL\na", "PASS"]
        self.assertTrue(engine.verify("do it", self.cap, "done")["passed"])

    def test_setting_one_restores_single_sample_judging(self):
        os.environ["OTTO_JUDGE_CONFIRMATIONS"] = "1"
        self.replies = ["FAIL\nnope"]
        self.assertFalse(engine.verify("do it", self.cap, "done")["passed"])
        self.assertEqual(len(self.calls), 1)

    def test_a_corrupt_setting_falls_back_to_the_code_default(self):
        # config._coerce rejects the junk value, so the CODE default (3) applies — a corrupt
        # store entry must not silently switch judging back to one sample.
        os.environ["OTTO_JUDGE_CONFIRMATIONS"] = "not-a-number"
        self.replies = ["FAIL\na", "FAIL\nb", "FAIL\nc"]
        self.assertFalse(engine.verify("do it", self.cap, "done")["passed"])
        self.assertEqual(len(self.calls), 3)

    def test_a_junk_tries_argument_degrades_to_one_sample(self):
        self.replies = ["FAIL\nnope"]
        v = judging.confirm_adverse("verify", "p", judging._parse_verdict,
                                    lambda x: not x["passed"], tries="junk")
        self.assertFalse(v["passed"])
        self.assertEqual(len(self.calls), 1)

    def test_the_supervisor_kill_must_reproduce_too(self):
        # A RETRY kills a run mid-flight from a PARTIAL transcript — strictly worse placed than
        # verify, so it gets the same contract.
        sup = supervisor.Supervisor("w", 1, "do it", "summarize", "summarize text")
        self.replies = ["RETRY: it is off course", "CONTINUE"]
        self.assertEqual(sup._judge("prompt")["verdict"], "continue")
        self.replies = ["RETRY: off course", "RETRY: still off", "RETRY: off"]
        self.assertEqual(sup._judge("prompt")["verdict"], "retry")

    def test_a_supervisor_continue_costs_one_sample(self):
        sup = supervisor.Supervisor("w", 1, "do it", "summarize", "summarize text")
        self.replies = ["CONTINUE"]
        self.assertEqual(sup._judge("prompt")["verdict"], "continue")
        self.assertEqual(len(self.calls), 1)


class JudgeToolGrantTests(unittest.TestCase):
    """The judge sees no transcript, so `verify` names the tool grant to stop it inferring "no
    tool access". It named `config.READ_TOOLS` as if that were the whole set — but it is only the
    `--allowedTools` floor: an `agent` cap's own `tools:` frontmatter and every inherited MCP
    server sit on top of it. Reading the floor as exhaustive, the judge failed two correct runs
    for fabrication (sched-mosaic-9e5e5681 2026-08-19, runbook-mosaic-3f7943f2 2026-08-14) whose
    transcripts contain the successful `mcp__claude_ai_Google_Calendar__list_events`,
    `mcp__claude_ai_Gmail__search_threads`, `mcp__claude_ai_Slack__*` and `mcp__claude_ai_Notion__*`
    calls it declared impossible."""

    CONNECTOR_TOOLS = ["mcp__claude_ai_Google_Calendar__list_events",
                       "mcp__claude_ai_Gmail__search_threads",
                       "mcp__claude_ai_Slack__slack_search_public_and_private"]

    def setUp(self):
        self._complete = gateway.complete
        self.prompts = []
        gateway.complete = lambda task, prompt: (self.prompts.append(prompt), "PASS")[1]
        self.cap = registry.Capability("agent", "sre-secretary", "morning briefing agent")
        self.cap.risk = "read"

    def tearDown(self):
        gateway.complete = self._complete

    def test_a_tool_the_attempt_actually_called_is_named_in_the_grant(self):
        engine.verify("catch me up", self.cap, "Calendar: standup 09:05.",
                      tools_used=self.CONNECTOR_TOOLS)
        p = self.prompts[0]
        for tool in self.CONNECTOR_TOOLS:
            self.assertIn(tool, p,
                          "the judge called this exact tool's real output fabricated because the "
                          "grant it was shown did not mention it")

    def test_the_grant_is_never_presented_as_the_complete_set(self):
        # Naming a closed list is what licenses "that source had no tool path" — the unsound
        # inference. Even with nothing observed, the floor must read as a floor.
        engine.verify("catch me up", self.cap, "A briefing.")
        p = self.prompts[0]
        self.assertIn("at least", p)
        self.assertIn("is a FLOOR", p)
        self.assertIn("NEVER reason that a source could not have been reached", p)

    def test_the_floor_is_kept_even_when_nothing_observed_used_it(self):
        # A tool granted and never called is still a tool the attempt had.
        engine.verify("catch me up", self.cap, "A briefing.", tools_used=["Bash"])
        for tool in config.READ_TOOLS:
            self.assertIn(tool, self.prompts[0])

    def test_a_write_cap_keeps_its_write_floor(self):
        self.cap.risk = "write"
        engine.verify("do it", self.cap, "done", tools_used=["mcp__grafana__query_prometheus"])
        p = self.prompts[0]
        self.assertIn("Edit", p)
        self.assertIn("mcp__grafana__query_prometheus", p)

    def test_the_local_tool_free_framing_is_untouched(self):
        # The inverse case: a local attempt genuinely had NO tools, and crediting tool-shaped
        # output there is the bug this must not undo.
        engine.verify("catch me up", self.cap, "A briefing.", local=True)
        p = self.prompts[0]
        self.assertIn("NO tool access at all", p)
        self.assertNotIn("is a FLOOR", p)

    def test_a_tool_that_only_ever_failed_is_reported_as_refused(self):
        """The mirror error. An ungranted claude.ai connector answers "you haven't granted it
        yet", so the source really was unreachable — but naming the tool in the grant made the
        judge read a truthful "blocked" as an invented excuse and FAIL 3/3 (probe-e2e-0001)."""
        engine.verify("catch me up", self.cap, "Gmail source unavailable.",
                      tools_used=["Bash"],
                      tools_failed=["mcp__claude_ai_Gmail__search_threads"])
        p = self.prompts[0]
        self.assertIn("returned nothing usable", p)
        self.assertIn("mcp__claude_ai_Gmail__search_threads", p)
        self.assertIn("telling the TRUTH", p)
        # ...and it must not also license claimed data from a tool that never answered.
        self.assertIn("do not credit any data it claims to have gotten FROM them", p)

    def test_a_tool_that_worked_once_is_never_called_refused(self):
        # One success proves availability; a later transient error must not retract it.
        engine.verify("catch me up", self.cap, "A briefing.",
                      tools_used=["mcp__claude_ai_Gmail__search_threads"],
                      tools_failed=["mcp__claude_ai_Gmail__search_threads"])
        self.assertNotIn("returned nothing usable", self.prompts[0])

    def test_no_refusals_adds_no_note(self):
        engine.verify("catch me up", self.cap, "A briefing.", tools_used=["Bash"])
        self.assertNotIn("returned nothing usable", self.prompts[0])

    def test_grant_list_degrades_on_junk(self):
        self.assertEqual(judging._grant_list(self.cap, None), sorted(set(config.READ_TOOLS)))
        self.assertEqual(judging._grant_list(self.cap, [None, ""]),
                         sorted(set(config.READ_TOOLS)))


class StockReviewCapContractTests(unittest.TestCase):
    """The stock `code-reviewer` must fit `_CAP_CONTRACT_CHARS` whole.

    `cap_contract_block` does not error when a cap overruns the judge budget — it RANKS the
    sections against the request, keeps what fits, and states how many it dropped. So an
    over-budget review cap means the judge enforces a different subset of the reviewer's
    contract for every PR, and the rule that happens not to fit is invisible. This one is the
    review loop's default, its contract is shown to the judge as supreme, and it is the only
    bundled cap that fits (product-manager, qa-tester and sre-pm are all deliberately over and
    accept per-request ranking). Adding to it is fine; adding without trimming is not."""

    PATH = "capabilities/bundled/code-reviewer.md"

    class _Cap:
        kind, name, risk = "skill", "code-reviewer", "read"
        prompt = None
        path = "capabilities/bundled/code-reviewer.md"

    def test_the_whole_contract_reaches_every_judge(self):
        body = judging._cap_text(self._Cap())
        self.assertLessEqual(
            len(body), judging._CAP_CONTRACT_CHARS,
            f"{self.PATH} is {len(body)} chars against a {judging._CAP_CONTRACT_CHARS}-char "
            "judge budget. Nothing raises — the judge just silently stops seeing some of the "
            "reviewer's own rules, differently per PR. Trim the cap, don't raise the budget "
            "(every OTHER cap's judge prompt pays for that too).")
        for request in ("review this pull request", "a terraform IAM change", None):
            block = judging.cap_contract_block(self._Cap(), request)
            self.assertNotIn("not shown here", block,
                             f"a section was dropped for request={request!r}")

    def test_the_read_only_and_diff_source_rules_survive_any_edit(self):
        # The two rules that are not recoverable from the code: the loop pre-authorizes this cap
        # (nothing gates it), and `judging.review_request` forbids reading a local checkout — a
        # cap telling the reviewer to open local files contradicts the platform instruction it is
        # handed alongside, and the cap's contract is what the judge treats as supreme.
        body = judging._cap_text(self._Cap()).lower()
        self.assertIn("read-only, always", body)
        self.assertIn("only when the diff itself is local", body)

    def test_the_verdict_sentinels_match_what_the_loop_asks_for(self):
        # `review_request` asks for exactly PASS / CHANGES / INCONCLUSIVE and `judge_review`
        # parses that vocabulary. A cap ending on its own words (an "Approve / Request changes"
        # verdict, say) leaves the judge inferring one from prose.
        body = judging._cap_text(self._Cap())
        asked = judging.review_request("https://x/pull/1", "r", "do it")
        for sentinel in ("PASS", "CHANGES", "INCONCLUSIVE"):
            self.assertIn(sentinel, asked)
            self.assertIn(f"`{sentinel}`", body)


class JudgeCapContractTests(unittest.TestCase):
    """The judge sees a capability through `name (description[:160])` alone, which cannot tell a
    rule the cap is REQUIRED to follow from one the output invented — so it judged the raw request
    as the whole contract. Measured (sched-mosaic-b643e7fd, 2026-08-18): product-manager refines
    Ready tickets "assigned to the authenticated user"; the judge failed a correct zero-write run
    for narrowing scope "without any evidence this assignee restriction is an actual documented
    rule of the capability", and told the retry to refine every ticket "regardless of assignee".
    The retry obeyed and wrote to 13 tickets belonging to other people."""

    CONTRACT = ("You are a ticket refiner.\nNever open pull requests.\n\n"
                "# Capability 1: Epics\nCreate epics and wire sub-issues. Label inheritance is "
                "mandatory for every child issue created under a parent epic on the board.\n\n"
                "# Capability 2: Rollover\nSwap the cycle label each quarter, preserving Status "
                "on every board item so columns never move during a rollover.\n\n"
                "# Capability 3: Refine\nRefine every board ticket in the Ready column and "
                "assigned to the authenticated user, filtering on assignee == $ME.\n")

    def setUp(self):
        self._complete = gateway.complete
        self.prompts = []
        gateway.complete = lambda task, prompt: (self.prompts.append(prompt), "PASS")[1]
        self.cap = registry.Capability("custom", "product-manager", "manages a GitHub board")
        self.cap.risk = "write"
        self.cap.prompt = self.CONTRACT

    def tearDown(self):
        gateway.complete = self._complete

    def test_the_judge_is_shown_the_rule_it_used_to_invent(self):
        engine.verify("Refine the tickets in Ready", self.cap, "Refined my Ready tickets.")
        p = self.prompts[0]
        self.assertIn("CAPABILITY CONTRACT", p)
        self.assertIn("assignee == $ME", p,
                      "the judge must see the scope rule, or it fails the run for obeying it")

    def test_the_block_forbids_overriding_the_contract_in_a_critique(self):
        # A contract with no precedence rule is just more context — the judge has to be told the
        # limit is correct AND that it must not order the retry to widen the blast radius.
        block = judging.cap_contract_block(self.cap, "refine")
        self.assertIn("is CORRECT even where", block)
        self.assertIn("never tell the next attempt to override", block)
        self.assertIn("widen the set of things it changes", block)

    def test_sections_are_ranked_against_the_request_not_taken_in_document_order(self):
        # Capability 3 is LAST; a document-order trim keeps 1 and 2 and drops exactly the section
        # the run is about — which is how a reordered file silently changes what is enforced.
        budget = len(self.CONTRACT.split("# Capability")[0]) + 260
        block = judging.cap_contract_block(self.cap, "refine ready tickets by assignee", budget)
        self.assertIn("assignee == $ME", block)
        self.assertNotIn("Swap the cycle label", block)

    def test_the_preamble_is_always_kept(self):
        # A cap's global "always holds" rules sit above every section, so they are never the part
        # that loses the ranking.
        block = judging.cap_contract_block(self.cap, "roll the quarter over", 400)
        self.assertIn("Never open pull requests.", block)

    def test_a_trim_states_what_it_dropped(self):
        block = judging.cap_contract_block(self.cap, "refine ready tickets", 400)
        self.assertIn("not shown here", block)
        self.assertIn("absence is not permission", block,
                      "silently trimming reads as 'that was the whole contract' — the exact "
                      "inference behind the incident")

    def test_a_cap_with_no_readable_contract_degrades_to_nothing(self):
        bare = registry.Capability("agent", "ghost", "a cap whose file moved")
        bare.path = "/nonexistent/ghost.md"
        # No CAPABILITY CONTRACT fence — nothing was readable. The write-gate note still shows:
        # it's Otto's own annotation, independent of whether the cap's own file could be read.
        block = judging.cap_contract_block(bare, "anything")
        self.assertNotIn("CAPABILITY CONTRACT", block)
        self.assertIn("WRITE ALREADY AUTHORIZED", block)
        engine.verify("do a thing", bare, "did the thing")     # must not raise
        self.assertNotIn("CAPABILITY CONTRACT", self.prompts[0])

    def test_frontmatter_is_stripped_from_a_path_read_cap(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "a.md")
        with open(path, "w") as f:
            f.write("---\nname: a\ndescription: x\n---\n\n# Rules\nNever touch prod.\n")
        cap = registry.Capability("agent", "a", "x")
        cap.path = path
        block = judging.cap_contract_block(cap, "prod")
        self.assertIn("Never touch prod.", block)
        self.assertNotIn("description: x", block)


class LadderJudgeContextTests(unittest.TestCase):
    """Both synchronous ladders must hand the judge the SAME context the workflow does.

    They were written as two copies of one loop and had already drifted at exactly this call:
    `execute` passed `local=` without `project=`, `_run_ladder` passed `project=` without
    `local=`. The second half was a live defect — `_run_ladder` runs every plan-mode step, and
    with `local` defaulting to False the judge was told "this capability actually ran with real
    tool access ... do NOT assume tool-shaped output is fabricated" about a tool-free local
    attempt, which is the inverse of the truth and laundered fabricated command output straight
    through verify. Both now share one body (`engine._ladder_core`); this pins the contract so a
    future edit to either adapter can't quietly drop an argument again."""

    def setUp(self):
        self.seen = []
        self._verify, self._record = engine.verify, engine.record_attempt
        self._attempt, self._resolve = engine.run_attempt, engine._resolve_project
        engine.verify = lambda req, cap, res, **kw: (self.seen.append(kw) or
                                                     {"passed": True, "critique": None})
        engine.record_attempt = lambda *a, **k: None
        engine._resolve_project = lambda cap, repo=None: "/repos/infra"
        engine.run_attempt = lambda *a, **k: {
            "workflow": "w1", "result": "done", "cost": 0.0, "session_id": "s1", "attempt": 1,
            "is_error": False, "tokens": {"output": 1}, "model": "m", "local": True,
            "write_local": False}
        self.cap = registry.Capability("agent", "c", "d")
        self.cap.risk = "write"

    def tearDown(self):
        engine.verify, engine.record_attempt = self._verify, self._record
        engine.run_attempt, engine._resolve_project = self._attempt, self._resolve

    def test_execute_tells_the_judge_both_the_repo_and_the_backend(self):
        engine.execute("go", self.cap)
        self.assertEqual(self.seen[0].get("project"), "/repos/infra")
        self.assertIs(self.seen[0].get("local"), True)

    def test_plan_step_ladder_tells_the_judge_both_too(self):
        # The live half: without `local`, a fabricating local model passes laundered.
        engine._run_ladder("go", self.cap, "w1", project="/repos/infra")
        self.assertEqual(self.seen[0].get("project"), "/repos/infra")
        self.assertIs(self.seen[0].get("local"), True)

    def test_a_claude_backed_attempt_is_not_mislabelled_local(self):
        # The flag must track the attempt, not be hardcoded on: mislabelling a real tool-using
        # run as tool-free makes the judge reject genuine command output as fabricated.
        engine.run_attempt = lambda *a, **k: {
            "workflow": "w1", "result": "done", "cost": 0.0, "session_id": "s1", "attempt": 1,
            "is_error": False, "tokens": {"output": 1}, "model": "m", "local": False,
            "write_local": False}
        engine.execute("go", self.cap)
        engine._run_ladder("go", self.cap, "w1", project="/repos/infra")
        self.assertEqual([kw.get("local") for kw in self.seen], [False, False])


class HarnessDeathLadderTests(unittest.TestCase):
    """A HARNESS death (timeout, worker crash, activity failure) is not a judgement — no judge
    read any output — so it must not spend a rung of `max_attempts`.

    It used to: the loop was `for attempt in range(1, n + 1)`, so one timeout both shortened the
    real ladder to two judged shots AND dragged the final-rung model escalation forward onto an
    attempt that had produced nothing to escalate about. Measured over the trail, 21% of recorded
    verify failures were harness deaths. They now draw on their own bounded budget
    (`max_harness_retries`). A SUPERVISOR kill is deliberately NOT free: enforce-mode is an
    intentional intervention that `MAX_VERIFY_ATTEMPTS` is documented to bound.

    Mirrors `OttoWorkflow._verify_ladder` — see `test_integration.WorkflowHarnessDeathTests`."""

    ERR = "(execution activity failed — worker died or attempt timed out)"
    KILL = "(aborted by supervisor: went off-course)"

    def setUp(self):
        self.escalate, self.judged, self.script = [], [], []
        self._verify, self._record = engine.verify, engine.record_attempt
        self._attempt, self._resolve = engine.run_attempt, engine._resolve_project
        self._setting, self._sup = config.setting, config.SUPERVISE
        config.SUPERVISE = False
        engine.trace = engine.say = lambda *a, **k: None
        engine.record_attempt = lambda *a, **k: None
        engine._resolve_project = lambda cap, repo=None: None
        self.knobs = {"max_attempts": 3, "max_harness_retries": 2}
        config.setting = lambda name: self.knobs.get(name, self._setting(name))

        def fake_attempt(request, cap, **kw):
            self.escalate.append(kw.get("escalate"))
            res = self.script[len(self.escalate) - 1] if len(self.escalate) <= len(self.script) \
                else "real output"
            return {"workflow": "w1", "result": res, "cost": 0.0, "session_id": "s1",
                    "attempt": kw.get("attempt"), "is_error": res in (self.ERR, self.KILL),
                    "tokens": {"output": 1}, "model": "m", "local": False, "write_local": False}

        def fake_verify(request, cap, result, **kw):
            self.judged.append(result)
            return {"passed": False, "source": "judge", "critique": "not good enough"}
        engine.run_attempt, engine.verify = fake_attempt, fake_verify
        self.cap = registry.Capability("agent", "c", "d")
        self.cap.risk = "read"

    def tearDown(self):
        engine.verify, engine.record_attempt = self._verify, self._record
        engine.run_attempt, engine._resolve_project = self._attempt, self._resolve
        config.setting, config.SUPERVISE = self._setting, self._sup

    def test_a_harness_death_does_not_spend_a_judged_rung(self):
        # One timeout, then three attempts a judge actually reads. max_attempts=3 must still buy
        # THREE judged shots, so the run takes four physical attempts.
        self.script = [self.ERR]
        out = engine._ladder_core("go", self.cap, "w1", recall=False, project=None)
        self.assertEqual(len(self.escalate), 4, "the timeout ate one of the three judged rungs")
        self.assertEqual(len(self.judged), 3, "a judge must still get three shots")
        self.assertFalse(out["harness_stop"])

    def test_a_harness_death_does_not_pull_the_model_escalation_forward(self):
        # `escalate=True` is the expensive top-tier rung. It belongs on the last JUDGED attempt,
        # never on the one that merely follows a timeout — escalating cannot fix a crash.
        self.script = [self.ERR]
        engine._ladder_core("go", self.cap, "w1", recall=False, project=None)
        self.assertEqual(self.escalate, [False, False, False, True])

    def test_harness_retries_are_bounded_so_a_hanging_cap_cannot_loop_forever(self):
        # Every attempt dies in the harness: 1 + max_harness_retries physical attempts, no more,
        # and nothing is ever judged. The budget is deliberately NOT equal to max_attempts here —
        # at 2-and-3 the old and new loops run the same number of attempts and the assertion
        # would pass either way, proving nothing.
        self.knobs["max_harness_retries"] = 1
        self.script = [self.ERR] * 20
        out = engine._ladder_core("go", self.cap, "w1", recall=False, project=None)
        self.assertEqual(len(self.escalate), 2, "a hanging cap must stop at 1 + the budget")
        self.assertEqual(self.judged, [], "a harness death must never reach the judge")
        self.assertTrue(out["harness_stop"])

    def test_a_run_killed_only_by_the_harness_is_labelled_as_such_not_verify_exhausted(self):
        # "verify_exhausted" on a run no judge ever saw is the mislabel that makes Otto's own
        # failures read as the capability's on the needs-you dashboard.
        self.script = [self.ERR] * 20
        out = engine.execute("go", self.cap)
        self.assertEqual(out["needs_human"], {"reason": "harness_exhausted"})

    def test_a_supervisor_kill_still_spends_a_judged_rung(self):
        # Asymmetric on purpose: a kill is Otto deciding to stop and restart with steering, which
        # MAX_VERIFY_ATTEMPTS bounds by design. Free kills would let enforce-mode run unbounded.
        self.knobs["max_harness_retries"] = 9    # a free kill would run 10+ attempts
        self.script = [self.KILL] * 20
        out = engine._ladder_core("go", self.cap, "w1", recall=False, project=None)
        self.assertEqual(len(self.escalate), 3)
        self.assertFalse(out.get("harness_stop"))

    def test_an_ordinary_judged_ladder_is_unchanged(self):
        # Control: with no harness death the loop must behave exactly as before — three attempts,
        # escalation on the last.
        out = engine._ladder_core("go", self.cap, "w1", recall=False, project=None)
        self.assertEqual(len(self.escalate), 3)
        self.assertEqual(self.escalate, [False, False, True])
        self.assertEqual(len(self.judged), 3)
        self.assertFalse(out["passed"])


class PlanPreviewPermissionTests(unittest.TestCase):
    """The read-only PLAN preview must run under `--permission-mode plan`: without it, its
    SCOPED Bash allows (Bash(gh issue view:*) …) trip Claude Code's command classifier, which
    gates any NETWORK command to an interactive approval a headless `claude -p` can't satisfy —
    so `gh issue view` died and the planner fell back to 'I could not read issue #N'. Plan mode
    lets the scoped read-only network commands run while forbidding every mutation."""

    def setUp(self):
        import tempfile
        self._claude, self._exec_id = engine._claude, gateway.exec_model_id
        engine.trace = engine.say = lambda *a, **k: None
        gateway.exec_model_id = lambda cap_name=None: "claude-test"
        self._tmp = tempfile.mkdtemp(prefix="otto-planprev-")
        self._audit_db, engine._DB = engine._DB, os.path.join(self._tmp, "otto.db")
        self.kw = {}

        def fake_claude(prompt, **kw):
            self.kw = kw
            return {"result": "1. gh issue view 272\n2. edit the module", "total_cost_usd": 0}
        engine._claude = fake_claude
        self.cap = registry.Capability("agent", "sre-minion", "implements a ticket")
        self.cap.risk = "write"

    def tearDown(self):
        engine._claude, gateway.exec_model_id = self._claude, self._exec_id
        engine._DB = self._audit_db
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_failed_pass_yields_no_plan_rather_than_its_error_sentinel(self):
        # claude_cli reports a timeout/crash/abort as is_error plus a sentinel result. Those used
        # to reach the gate rendered under "Planned operations", where "(timed out)" reads as a
        # plan rather than as a preview that never happened. Observed on a real 480s-budget infra
        # preview. "" is the documented fallback: the gate shows the invocation instead.
        for sentinel in ("(timed out)", "(aborted by supervisor: off-course)", "auth error\ntail"):
            engine._claude = lambda p, **kw: {"result": sentinel, "is_error": True,
                                              "total_cost_usd": 0}
            self.assertEqual(engine.plan_preview("work on issue #272", self.cap)["plan"], "",
                             f"{sentinel!r} leaked to the gate as a plan")

    def test_preview_runs_in_plan_mode_with_scoped_gh_reads(self):
        plan = engine.plan_preview("work on issue #272", self.cap)["plan"]
        self.assertTrue(plan)
        self.assertEqual(self.kw.get("permission_mode"), "plan")
        # Still the tight read-only allowlist — scoped gh reads, no unscoped Bash/Edit/Write.
        self.assertEqual(self.kw.get("allowed_tools"), config.PLAN_TOOLS)
        self.assertIn("Bash(gh issue view:*)", config.PLAN_TOOLS)
        self.assertNotIn("Bash", config.PLAN_TOOLS)
        self.assertNotIn("Write", config.PLAN_TOOLS)


class PlanPreviewLocalSessionTests(unittest.TestCase):
    """A resume is bound to the backend that MINTED the session, and the plan preview was the one
    resume path that never checked. `claude -p --resume local-…` is rejected outright ("is not a
    UUID and does not match any session title"), so a follow-up on a local-backend chat that
    escalated to a write reached its approval gate with NO plan on it — 1.5s, 0 tokens, $0
    (web-ce430e45). The gate is the one artefact with no verify ladder behind it, and an empty
    one reads as "the preview decided there was nothing to do", not as "the preview never ran"."""

    _POOL = [{"name": "local-flash", "provider": "openai", "base_url": "http://x/v1",
              "model": "flash"},
             {"name": "claude-tier", "provider": "claude", "model": "claude-sonnet-5"}]

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="otto-planlocal-")
        self._saved = {"claude": engine._claude, "run_json": local_runtime.run_json,
                       "sessions": local_runtime.SESSIONS, "load": gateway.load,
                       "resolve": gateway.resolve_model, "exec_id": gateway.exec_model_id,
                       "db": engine._DB, "trace": plans.trace}
        plans.trace = engine.trace = engine.say = lambda *a, **k: None
        engine._DB = os.path.join(self._tmp, "otto.db")
        local_runtime.SESSIONS = os.path.join(self._tmp, "local-sessions")
        gateway.load = lambda: {"pool": [dict(m) for m in self._POOL]}
        gateway.resolve_model = lambda n, cfg=None: next(
            (dict(m) for m in self._POOL if m["name"] == n), None)
        gateway.exec_model_id = lambda cap_name=None: "claude-sonnet-5"
        self.claude_calls, self.local_calls = [], []

        def fake_claude(prompt, **kw):
            self.claude_calls.append(kw)
            return {"result": "1. do the thing", "total_cost_usd": 0}

        def fake_local(prompt, **kw):
            # Snapshot what the fork held AT CALL TIME — the real run_json would append this
            # turn and save it back, which is exactly what must not touch the real session.
            sid = kw.get("resume_session")
            self.local_calls.append({**kw, "fork_history": local_runtime._load_session(sid),
                                     "fork_existed": os.path.exists(
                                         local_runtime.session_path(sid)) if sid else False})
            if sid:
                local_runtime._save_session(sid, [{"role": "user", "content": "plan pass"}])
            return {"result": "1. do the thing locally", "is_error": False,
                    "total_cost_usd": 0, "session_id": sid,
                    "usage": {"input_tokens": 7, "output_tokens": 3}}

        engine._claude, local_runtime.run_json = fake_claude, fake_local
        self.cap = registry.Capability("agent", "sre-minion", "implements a ticket")
        self.cap.risk = "write"
        self.sid = "local-deadbeef1234"
        local_runtime._save_session(self.sid, [{"role": "user", "content": "the original ask"}],
                                    "local-flash")

    def tearDown(self):
        engine._claude, local_runtime.run_json = self._saved["claude"], self._saved["run_json"]
        local_runtime.SESSIONS, gateway.load = self._saved["sessions"], self._saved["load"]
        gateway.resolve_model, gateway.exec_model_id = self._saved["resolve"], self._saved["exec_id"]
        engine._DB, plans.trace = self._saved["db"], self._saved["trace"]
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_local_session_is_previewed_on_the_local_backend(self):
        out = engine.plan_preview("apply the review comments", self.cap, resume_session=self.sid)
        self.assertEqual(self.claude_calls, [],
                         "a `local-` session id was handed to `claude -p --resume`, which "
                         "rejects it — the gate renders with no plan")
        self.assertEqual(len(self.local_calls), 1)
        self.assertEqual(out["plan"], "1. do the thing locally")

    def test_the_preview_runs_the_model_that_minted_the_session(self):
        engine.plan_preview("apply the review comments", self.cap, resume_session=self.sid)
        self.assertEqual(self.local_calls[0]["model_entry"]["name"], "local-flash")

    def test_a_claude_session_still_previews_on_claude(self):
        # The control: the fix must not divert the path that was always correct.
        out = engine.plan_preview("apply the review comments", self.cap,
                                  resume_session="8f14e45f-ceea-467a-9d2f-2b0a1c9d0e11")
        self.assertEqual(self.local_calls, [])
        self.assertEqual(len(self.claude_calls), 1)
        self.assertEqual(self.claude_calls[0].get("permission_mode"), "plan")
        self.assertEqual(out["plan"], "1. do the thing")

    def test_the_preview_reads_the_conversation_but_never_writes_into_it(self):
        # print-mode `--resume` COPIES a session forward, so plan mode costs the Claude backend
        # nothing. This runtime resumes IN PLACE, so without a fork the plan instruction and the
        # plan itself end up in the history the approved run resumes next.
        before = local_runtime._load_session(self.sid)
        engine.plan_preview("apply the review comments", self.cap, resume_session=self.sid)
        call = self.local_calls[0]
        self.assertNotEqual(call["resume_session"], self.sid,
                            "the preview resumed the REAL session — its turn is saved back into "
                            "the history the approved run then resumes")
        self.assertTrue(call["fork_existed"])
        self.assertEqual(call["fork_history"], before,
                         "the fork did not carry the conversation, so the preview plans blind")
        self.assertEqual(local_runtime._load_session(self.sid), before,
                         "the real session was mutated by the preview")

    def test_the_spent_fork_is_deleted(self):
        engine.plan_preview("apply the review comments", self.cap, resume_session=self.sid)
        fork = self.local_calls[0]["resume_session"]
        self.assertFalse(os.path.exists(local_runtime.session_path(fork)),
                         "every gated follow-up leaks a session file")

    def test_the_local_preview_can_mutate_nothing(self):
        # `--permission-mode plan` has no local equivalent, so read-only rests entirely on the
        # tool set. `_offered_tools` matches literal names, so PLAN_TOOLS' three scoped
        # `Bash(gh … view:*)` rules are never offered — and unscoped Bash must not appear either,
        # since this runtime does not permission-scope Bash at all.
        engine.plan_preview("apply the review comments", self.cap, resume_session=self.sid)
        self.assertEqual(self.local_calls[0]["allowed_tools"], config.PLAN_TOOLS)
        offered = {t["function"]["name"]
                   for t in local_runtime._offered_tools(config.PLAN_TOOLS)}
        self.assertTrue(offered, "the local preview was handed no tools at all")
        self.assertEqual(offered - {"Read", "Grep", "Glob"}, set(),
                         "the local plan preview can act before the human approves anything")

    def test_no_local_model_left_yields_no_plan_rather_than_a_doomed_claude_resume(self):
        gateway.load = lambda: {"pool": [{"name": "claude-tier", "provider": "claude",
                                          "model": "claude-sonnet-5"}]}
        gateway.resolve_model = lambda n, cfg=None: None
        out = engine.plan_preview("apply the review comments", self.cap, resume_session=self.sid)
        self.assertEqual(out["plan"], "")
        self.assertEqual(self.claude_calls, [],
                         "falling back to Claude re-runs the exact failure this branch exists "
                         "to stop")

    def test_the_audit_row_names_the_backend_that_served_the_preview(self):
        # A bad local preview and a bad Claude one otherwise leave identical rows.
        engine.plan_preview("apply the review comments", self.cap, resume_session=self.sid,
                            wid="w-local-1")
        row = next(e for e in engine.iter_audit_entries() if e["workflow"] == "w-local-1")
        self.assertEqual(row["outcome"], "plan_preview")
        self.assertEqual(row["backend"], "local")
        # The canonical id the server serves, with the operator's pool LABEL beside it — the
        # local paths hand `_audit` the label, and left raw it put a second namespace in the
        # column `scorecard` groups by (`ModelIdNamespaceTests`).
        self.assertEqual(row["model"], "flash")
        self.assertEqual(row["model_entry"], "local-flash")
        self.assertEqual(row["tokens"]["output"], 3)


class PlanInstructionTests(unittest.TestCase):
    """The plan preview is asked for a list of OPERATIONS, and it faithfully delivers one — which
    is exactly how a plan lands enforcement before the callers it enforces on are ready. The
    instruction has to ask for execution ORDER, blast radius, and the unknown resolved first, or
    those never appear; and the old '~8 steps / output ONLY the plan' framing actively suppressed
    them (a phased rollout costs steps, and stating an assumption is a closing remark)."""

    def test_instruction_demands_ordering_blast_radius_and_assumptions(self):
        t = engine._PLAN_INSTRUCTION.lower()
        for want in ("blast radius", "acceptance criteria", "rollback",
                     "risks & assumptions", "must be TRUE".lower()):
            self.assertIn(want, t, f"plan instruction no longer asks for: {want}")
        # Enforcement is ordered after what makes it safe, and the unknown comes first.
        self.assertIn("observe first", t)
        self.assertIn("load-bearing", t)
        # The two constraints that used to crowd all of the above out.
        self.assertNotIn("at most ~8 steps", t)

    def test_instruction_demands_the_plan_inline_in_the_reply(self):
        # The gate renders the reply verbatim and nothing else. A cap whose own prompt writes a
        # plan document (sre-minion) will otherwise return "the plan is written to <path>" plus a
        # summary — measured on the real platform#342 dry run — leaving the human approving prose
        # about a plan they cannot see, and the critic judging the summary instead of the plan.
        t = engine._PLAN_INSTRUCTION.lower()
        self.assertIn("in your reply", t)
        self.assertIn("do not save the plan to a file", t)
        self.assertIn("the reply is the plan", t)


class GateStateForwardingTests(unittest.TestCase):
    """`server._wf_state`'s awaiting_approval branch is a WHITELIST over OttoWorkflow.status, so a
    new workflow field reaches the gate only if it is named there too. Measured failure: the plan
    critic ran and the workflow held its findings, but the browser got a gate with no warning
    block, because the field was added to the workflow and the UI and not to the whitelist —
    invisible, since a missing key renders exactly like "no concerns". Same shape as the
    delivery.AUDIENCE grep test: catch the forgotten registration, not the logic."""

    def _gate_branch(self):
        with open(os.path.join(os.path.dirname(__file__), "server.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index('"state": "awaiting_approval"')
        return src[i:src.index("}", i)]

    def test_every_field_the_gate_ui_reads_is_forwarded(self):
        with open(os.path.join(os.path.dirname(__file__), "web", "index.html"),
                  encoding="utf-8", errors="surrogateescape") as f:
            html = f.read()
        call = re.search(r"await gate\(content, cap, (st\..*?)\);", html)
        self.assertIsNotNone(call, "the chat's gate() call moved — re-point this test")
        fields = [f.strip().removeprefix("st.") for f in call.group(1).split(",")]
        # `wid` (the workflow id the client already has, used to signal revise_plan) and `onReplan`
        # (the callback gate() repaints the pipeline through) are not OttoWorkflow.status fields, so
        # they have nothing to be forwarded FROM.
        fields = [f for f in fields if f not in ("wid", "onReplan")]
        branch = self._gate_branch()
        for f in fields:
            self.assertIn(f'"{f}"', branch,
                          f"the gate UI reads st.{f} but _wf_state never forwards it")

    def test_plan_concerns_defaults_to_a_list_not_null(self):
        # The UI does (concerns||[]).filter(...), but a null here would also mean "no concerns"
        # for any future consumer that trusts the shape. Keep it a list.
        self.assertIn('"plan_concerns": st.get("plan_concerns") or []', self._gate_branch())


class PlanRevisionFeedbackTests(unittest.TestCase):
    """"Request changes" re-previews the plan, which takes MINUTES — so the only thing on screen
    meanwhile is the gate's note, and clearing it early is indistinguishable from the feedback
    having been dropped. `plan_revisions` bumps the moment the signal lands, so the poll must gate
    on `replanning` (in-flight) and never on the counter alone."""

    def _revision_poll(self):
        with open(os.path.join(os.path.dirname(__file__), "web", "index.html"),
                  encoding="utf-8", errors="surrogateescape") as f:
            html = f.read()
        m = re.search(r"async function awaitRevision\(\).*?\n      }", html, re.S)
        self.assertIsNotNone(m, "the gate's revision poll moved — re-point this test")
        return m.group(0)

    def test_the_poll_waits_out_the_re_preview_instead_of_the_counter(self):
        poll = self._revision_poll()
        self.assertIn("st.replanning", poll,
                      "the poll ignores replanning — it repaints the PREVIOUS plan ~1.5s in")
        # It must also survive a signal not yet processed (nothing bumped, nothing replanning) —
        # otherwise the very first tick repaints the old plan before the workflow has moved.
        self.assertIn("working", poll)

    def test_the_note_is_only_cleared_once_the_new_plan_is_painted(self):
        poll = self._revision_poll()
        paint, clear = poll.index("paint(st.plan"), poll.index("note.hidden=true")
        self.assertLess(paint, clear, "the note must outlive the repaint, not precede it")

    def test_the_pipeline_diagram_moves_off_the_gate_while_re_planning(self):
        # The run-loop is parked inside gate() for the whole round, so the diagram is frozen on
        # "waiting for your approval" — blaming the human for a wait that is Otto re-planning. The
        # poll has to drive the two stages it is standing in for.
        self.assertIn("onReplan", self._revision_poll(),
                      "the revision poll never repaints the pipeline — it reads as gate-held")
        with open(os.path.join(os.path.dirname(__file__), "web", "index.html"),
                  encoding="utf-8", errors="surrogateescape") as f:
            html = f.read()
        painter = re.search(r"const onReplan=\(phase,t\)=>\{.*?\n        \};", html, re.S)
        self.assertIsNotNone(painter, "the pipeline painter moved — re-point this test")
        painter = painter.group(0)
        self.assertIn("if(!alive()) return;", painter,
                      "an un-guarded painter repaints whatever pipe is on screen after a chat switch")
        # PLAN and GATE each run TWICE across a revision, so both re-anchor or their timers report
        # the sum of both passes.
        self.assertEqual(painter.count("restart:true"), 2)


class PlanCritiqueTests(unittest.TestCase):
    """The plan was the one artefact nothing judged — attempts get verify/retry, PRs get review
    and QA. critique_plan is its judge, and it is ADVISORY: it must never block or break the gate,
    so every failure path returns no concerns rather than raising."""

    def setUp(self):
        self._complete = gateway.complete
        engine.trace = engine.say = lambda *a, **k: None
        self.cap = registry.Capability("agent", "sre-minion", "implements a ticket")
        self.cap.risk = "write"

    def tearDown(self):
        gateway.complete = self._complete

    def _reply(self, text):
        self.prompt = ""

        def fake(tier, prompt, **kw):
            self.tier, self.prompt = tier, prompt
            return text
        gateway.complete = fake

    def test_concerns_are_returned_most_serious_first(self):
        self._reply("- Step 2 lands the DENY before any caller sends the header — 403s live "
                    "traffic. Log first, enforce once New Relic shows every caller emitting it.\n"
                    "- The router policy is unscoped, so it also denies the :8000 metrics scrape.")
        out = engine.critique_plan("add header attribution", self.cap, "1. edit tf\n2. deploy")
        self.assertEqual(len(out["concerns"]), 2)
        self.assertIn("403s live traffic", out["concerns"][0])
        self.assertEqual(self.tier, "verify")

    def test_the_critic_is_told_the_planner_could_not_reach_live_systems(self):
        # Otherwise it reports "you didn't check production" — true, unfixable, and it drowns the
        # findings that ARE actionable. config.PLAN_TOOLS has no cluster/cloud access at all.
        self._reply("NONE")
        engine.critique_plan("add header attribution", self.cap, "1. edit tf")
        self.assertIn("no access to live systems", self.prompt)

    def test_a_real_length_plan_is_not_truncated(self):
        # The first live plan under the new instruction was 9358 chars against a 6000 cap, and the
        # critic reported the cut ("the plan text cuts off mid-step 9") as a defect of the PLAN —
        # true of its input, false of the plan. Findings must never be manufactured by clipping.
        self._reply("NONE")
        plan = "\n".join(f"{i}. a genuine planning step with real detail" for i in range(200))
        self.assertGreater(len(plan), 9000)
        engine.critique_plan("do the thing", self.cap, plan)
        self.assertIn(plan, self.prompt, "a realistic plan was clipped out of the critic prompt")
        self.assertNotIn("TRUNCATED FOR REVIEW", self.prompt)

    def test_a_truncated_plan_says_so_and_forbids_reporting_the_cut(self):
        self._reply("NONE")
        engine.critique_plan("r", self.cap, "x" * (engine._PLAN_CRITIQUE_CHARS + 500))
        self.assertIn("TRUNCATED FOR REVIEW", self.prompt)
        self.assertIn("Do NOT report the cut-off", self.prompt)

    def test_none_and_empty_plan_yield_no_concerns(self):
        self._reply("NONE")
        self.assertEqual(engine.critique_plan("r", self.cap, "1. edit tf")["concerns"], [])
        self._reply("- a real concern that is long enough to keep")
        self.assertEqual(engine.critique_plan("r", self.cap, "   ")["concerns"], [])

    def test_a_dead_gateway_costs_the_warning_and_nothing_else(self):
        def boom(*a, **k):
            raise RuntimeError("gateway down")
        gateway.complete = boom
        self.assertEqual(engine.critique_plan("r", self.cap, "1. edit tf")["concerns"], [])

    def test_parse_ignores_prose_and_caps_the_list(self):
        p = engine._parse_plan_concerns
        # Prose around a clean verdict must not become a fake concern — the block is a warning on
        # the approval gate, and a fabricated one teaches the human to skip the real ones.
        self.assertEqual(p("I reviewed the plan.\nNONE\nLooks reasonable overall."), [])
        self.assertEqual(p("Here are my findings:\n- the first real concern goes here"),
                         ["the first real concern goes here"])
        self.assertEqual(p(""), [])
        self.assertEqual(p("<think>hmm</think>\nNONE"), [])
        self.assertEqual(len(p("\n".join(f"- concern number {i} spelled out" for i in range(12)))),
                         engine._PLAN_CONCERN_CAP)
        # Bullet dialects the prompt didn't ask for, plus a too-short line dropped as noise.
        self.assertEqual(p("1. numbered concern spelled out\n* starred concern spelled out\n- ok"),
                         ["numbered concern spelled out", "starred concern spelled out"])


class VerifyTests(unittest.TestCase):
    def test_pass(self):
        v = engine._parse_verdict("PASS")
        self.assertTrue(v["passed"])
        self.assertEqual(v["critique"], "")

    def test_pass_case_insensitive_with_trailing(self):
        v = engine._parse_verdict("pass — looks complete")
        self.assertTrue(v["passed"])

    def test_fail_with_critique(self):
        v = engine._parse_verdict("FAIL\nThe PR number is missing from the summary.")
        self.assertFalse(v["passed"])
        self.assertIn("PR number", v["critique"])

    def _judge_prompt(self, result):
        """Run verify against a stub judge and return the prompt it was actually shown."""
        import gateway
        cap = registry.Capability("skill", "daily-summary", "writes the daily summary")
        orig, seen = gateway.complete, []
        gateway.complete = lambda task, prompt: seen.append(prompt) or "PASS"
        try:
            engine.verify("daily-summary", cap, result)
        finally:
            gateway.complete = orig
        return seen[0]

    def test_a_clipped_result_is_marked_so_the_cut_is_not_read_as_a_defect(self):
        # The bug this guards: `result[:4000]` with no marker MANUFACTURED the defect it then
        # failed the run for. Live evidence — a complete 4668-char daily-summary was cut at
        # character 4000, mid-"Signed AWS C", and the judge's critique quoted that exact string
        # back as "the output is truncated mid-sentence". Two attempts, $5.07, nothing wrong
        # with the work. Same failure the plan critic already had (`_PLAN_CRITIQUE_CHARS`).
        tail = "THE-REAL-ENDING"
        long_result = ("x" * (judging._VERIFY_RESULT_CHARS + 500)) + tail
        prompt = self._judge_prompt(long_result)
        self.assertIn("TRUNCATED FOR REVIEW", prompt)
        self.assertIn("Do NOT report the cut-off", prompt)
        self.assertNotIn(tail, prompt)            # it really was cut — the marker isn't cosmetic

    def test_a_result_that_fits_is_passed_whole_with_no_marker(self):
        # The marker must not appear on output that wasn't cut, or every judge starts excusing
        # incompleteness it should be failing.
        prompt = self._judge_prompt("Done. Full summary below.\n- one\n- two")
        self.assertNotIn("TRUNCATED FOR REVIEW", prompt)
        self.assertIn("Full summary below", prompt)

    def test_the_judge_reads_far_more_than_the_old_4k_cap(self):
        # 4k cut 4.6% of results measured over the live trail, and the long ones are exactly the
        # substantial deliverables (PR reviews, summaries) — the cases where judging half the
        # work is worst.
        body = "y" * 9000 + "CONCLUSION-LINE"
        self.assertIn("CONCLUSION-LINE", self._judge_prompt(body))


class VerifyParseTailTests(unittest.TestCase):
    """Continuation of VerifyTests (split by FollowupHandoffTests above)."""

    def test_fail_without_second_line_keeps_reason(self):
        v = engine._parse_verdict("FAIL")
        self.assertFalse(v["passed"])
        self.assertTrue(v["critique"])   # never empty so the retry has something to act on

    def test_fail_reasoning_on_first_line(self):
        v = engine._parse_verdict("This did not address the request at all.")
        self.assertFalse(v["passed"])
        self.assertIn("did not address", v["critique"])

    def test_leaked_reasoning_with_stray_close_tag_reads_trailing_pass(self):
        # Observed: a local reasoning model (qwen3.6) leaked its chain-of-thought into content
        # with a stray </think>, the real PASS only at the tail. First-line-only parse scored
        # this FAIL, laundering a genuine PASS into needs-review.
        leaked = ("The request is to review a PR. The capability is github-pr-review.\n"
                  "I need to check if the output fulfils the request. It looks plausible.\n"
                  "Therefore the result should be PASS. I will output PASS on the first line.\n"
                  "</think>\n\nPASS")
        v = engine._parse_verdict(leaked)
        self.assertTrue(v["passed"])

    def test_unfenced_leak_recovers_standalone_tail_verdict(self):
        # No tag at all — recover a bare PASS/FAIL on its own line at the tail.
        v = engine._parse_verdict("Let me think about whether this is complete.\nLooks good.\nPASS")
        self.assertTrue(v["passed"])
        v = engine._parse_verdict("Weighing the output against the request.\nMissing the URL.\nFAIL")
        self.assertFalse(v["passed"])

    def test_reasoning_with_the_word_pass_in_prose_stays_fail(self):
        # "pass" buried in a sentence is NOT a standalone verdict — must not launder to PASS.
        v = engine._parse_verdict("The reviewer noted this would pass if the import were removed.")
        self.assertFalse(v["passed"])

    def test_prompt_states_the_capabilitys_real_tool_grant(self):
        # A judge that sees only the final text (never the tool-call transcript) can otherwise
        # infer "no tool access" from a capability's description alone and wrongly dismiss a
        # genuinely tool-verified investigation as fabricated — tell it the real grant up front.
        orig_complete = gateway.complete
        seen = {}
        try:
            def fake(task, prompt):
                seen["prompt"] = prompt
                return "PASS"
            gateway.complete = fake
            cap = registry.Capability("custom", "assistant", "General assistant, read-only.")
            cap.risk = "read"
            engine.verify("check something", cap, "some tool-verified output")
        finally:
            gateway.complete = orig_complete
        self.assertIn("Bash", seen["prompt"])
        self.assertIn("Do NOT assume", seen["prompt"])

    def test_write_capability_prompt_states_write_tools(self):
        orig_complete = gateway.complete
        seen = {}
        try:
            def fake(task, prompt):
                seen["prompt"] = prompt
                return "PASS"
            gateway.complete = fake
            cap = registry.Capability("agent", "sre-minion", "implements issues")
            cap.risk = "write"
            engine.verify("do the ticket", cap, "done")
        finally:
            gateway.complete = orig_complete
        self.assertIn("Edit", seen["prompt"])
        self.assertIn("Write", seen["prompt"])


class WorkerRegistrationTests(unittest.TestCase):
    """Guard against the silent-hang bug: if worker.py forgets to register an
    activity the workflow calls, Temporal retries it forever and the run sits at
    "executing…" with no way to tell it's stuck. Every @activity.defn in
    activities.py must appear in worker.ACTIVITIES."""

    def test_worker_registers_every_activity(self):
        try:
            import worker  # imports temporalio; skip when not installed
        except ImportError:
            self.skipTest("temporalio not installed")
        import activities
        defined = {
            name for name, fn in vars(activities).items()
            if hasattr(fn, "__temporal_activity_definition")
        }
        registered = {fn.__name__ for fn in worker.ACTIVITIES}
        missing = defined - registered
        self.assertEqual(missing, set(),
                         f"activities defined but not registered in worker.py: {missing}")


class PlanArtifactForwardingTests(unittest.TestCase):
    """The approved plan reaches the PR only if it survives two hops: the workflow's
    finalize_workspace payload, and the activity's call into workspace.finalize. Either hop
    dropping it is silent — the PR just quietly lacks the spec file."""

    def test_activity_forwards_the_plan_to_workspace_finalize(self):
        try:
            import activities  # imports temporalio; skip when not installed
        except ImportError:
            self.skipTest("temporalio not installed")
        import workspace
        seen = {}
        orig_fin, orig_copy = workspace.finalize, engine.pr_copy
        workspace.finalize = lambda run_id, **kw: seen.update(kw) or {"pushed": True}
        engine.pr_copy = lambda *a, **k: {"title": "t", "body": "b"}
        try:
            activities.finalize_workspace(
                {"run_id": "web-1", "title": "do a thing", "head": "abc",
                 "plan": "1. step one", "request": "do a thing", "cap": "general worker",
                 "concerns": ["no rollback"]})
        finally:
            workspace.finalize, engine.pr_copy = orig_fin, orig_copy
        self.assertEqual(seen.get("plan"), "1. step one")
        self.assertEqual(seen.get("cap"), "general worker")
        self.assertEqual(seen.get("request"), "do a thing")
        self.assertEqual(seen.get("concerns"), ["no rollback"])

    def test_repo_mode_finalize_call_site_passes_the_plan(self):
        """The fresh repo-mode finalize is the one that opens the PR — if its payload omits
        `plan`, every other piece of this feature is dead code."""
        src = open("workflows.py").read()
        # Anchored on the call, not its indentation — the payload is what matters, and
        # pinning leading spaces re-breaks this on any extraction that moves the block.
        i = re.search(r"finalize_workspace,\s*\{\"run_id\": workflow\.info\(\)\.workflow_id", src)
        self.assertIsNotNone(i, "the finalize_workspace call site moved or changed shape")
        payload = src[i.start():i.start() + 1400]
        self.assertIn('"plan": self._plan', payload)
        self.assertIn('"concerns": self._plan_concerns', payload)


class CapResolutionTests(unittest.TestCase):
    """activities._cap must resolve a config default like "agent:sre-qa" even though catalogue
    cap names are BARE (the frontmatter name). Regression: QA/review silently reported the cap
    "unavailable" because "agent:sre-qa" != "sre-qa"."""

    def setUp(self):
        try:
            import activities  # imports temporalio; skip when not installed
        except ImportError:
            self.skipTest("temporalio not installed")
        self.activities = activities
        self._saved = activities._caps
        # Inject the config defaults BY NAME so this test tracks them and can't drift when a
        # default changes (e.g. QA_CAP moved from sre-qa -> the bundled stock cap qa-tester).
        activities._caps = [registry.Capability("agent", "sre-qa", "validates a change"),
                            registry.Capability("skill", "github-pr-review", "reviews a PR"),
                            registry.Capability("custom", config.QA_CAP, "validates a change"),
                            registry.Capability("custom", config.REVIEW_CAP, "reviews a PR")]

    def tearDown(self):
        self.activities._caps = self._saved

    def test_bare_name_resolves(self):
        self.assertIsNotNone(self.activities._cap("sre-qa"))

    def test_kind_prefixed_name_resolves_to_bare(self):
        self.assertEqual(self.activities._cap("agent:sre-qa").name, "sre-qa")
        self.assertEqual(self.activities._cap("skill:github-pr-review").name, "github-pr-review")

    def test_unknown_name_is_none(self):
        self.assertIsNone(self.activities._cap("agent:does-not-exist"))
        self.assertIsNone(self.activities._cap("nope"))

    def test_config_defaults_resolve(self):
        # The shipped defaults must map onto the (bare) catalogue names.
        self.assertIsNotNone(self.activities._cap(config.QA_CAP))
        self.assertIsNotNone(self.activities._cap(config.REVIEW_CAP))


class SupervisorVerdictTests(unittest.TestCase):
    """Shadow supervisor (issue #143): the verdict parser is a STRICT enum that defaults to
    CONTINUE on anything unparseable — the supervisor reads untrusted transcript data, so an
    unrecognized reply must never count as a kill vote (mirror of _parse_qa_verdict's
    default-INCONCLUSIVE)."""

    def test_continue(self):
        self.assertEqual(supervisor.parse_verdict("CONTINUE")["verdict"], "continue")
        self.assertEqual(supervisor.parse_verdict("continue")["verdict"], "continue")
        self.assertEqual(supervisor.parse_verdict("Continue — looks fine")["verdict"], "continue")

    def test_retry_with_critique(self):
        v = supervisor.parse_verdict("RETRY: querying the US account; the alert is in EU")
        self.assertEqual(v["verdict"], "retry")
        self.assertIn("EU", v["critique"])

    def test_retry_multiline_critique_folds_in(self):
        v = supervisor.parse_verdict("RETRY: wrong repo\nIt cloned infra but the ticket names deployer.")
        self.assertEqual(v["verdict"], "retry")
        self.assertIn("deployer", v["critique"])

    def test_bare_retry_gets_default_critique(self):
        v = supervisor.parse_verdict("retry")
        self.assertEqual(v["verdict"], "retry")
        self.assertTrue(v["critique"])

    def test_everything_else_is_continue(self):
        for text in ("", None, "FAIL", "PASS", "KILL IT", "The agent seems lost, maybe stop?",
                     "I think you should RETRY"):   # RETRY not at line start -> not a verdict
            self.assertEqual(supervisor.parse_verdict(text)["verdict"], "continue",
                             f"{text!r} must default to continue")


class SupervisorRetryCritiqueTests(unittest.TestCase):
    """A retry is steered by the VERIFIER's critique while the supervisor judges against the
    ORIGINAL request — so the critique must reach the supervisor prompt, or it kills the agent
    for obeying the other judge (web-7ff7e792: verify said "split #259 into a follow-up issue",
    the agent did, the supervisor killed it for "decomposing instead of implementing")."""

    def _sup(self, critique=None):
        return supervisor.Supervisor("w1", 3, "Work on issue #259", "sre-minion", "does issues",
                                     critique=critique)

    def test_prompt_carries_the_critique_and_says_it_amends_the_task(self):
        p = self._sup("split #259 into a tracked follow-up issue")._prompt("assistant: x")
        self.assertIn("split #259 into a tracked follow-up issue", p)
        self.assertIn("This is a RETRY", p)
        self.assertIn("AS AMENDED", p)

    def test_first_attempt_prompt_is_unchanged(self):
        p = self._sup()._prompt("assistant: x")
        self.assertNotIn("This is a RETRY", p)
        self.assertIn("Work on issue #259", p)

    def test_start_passes_the_critique_to_the_supervisor(self):
        knob, config.SUPERVISE = config.SUPERVISE, True
        self.addCleanup(setattr, config, "SUPERVISE", knob)
        cap = registry.Capability("skill", "demo", "a demo capability")
        sup = supervisor.start("w1", 2, "do it", cap, critique="use the EU account")
        self.assertEqual(sup.critique, "use the EU account")


class SupervisorCompactTests(unittest.TestCase):
    """Stream events -> short transcript lines; anything without activity signal (init,
    result, unknown future types, malformed) yields None."""

    def test_assistant_text_and_tool_use(self):
        line = supervisor.compact_event({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Checking the   dashboard now"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "gh pr list"}}]}})
        self.assertIn("assistant: Checking the dashboard now", line)
        self.assertIn("tool_use Bash", line)
        self.assertIn("gh pr list", line)

    def test_tool_result_string_and_parts(self):
        line = supervisor.compact_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "exit 0"}]}})
        self.assertEqual(line, "tool_result: exit 0")
        line = supervisor.compact_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "42 pods"}]}]}})
        self.assertEqual(line, "tool_result: 42 pods")

    def test_no_signal_types_yield_none(self):
        for evt in ({"type": "system", "subtype": "init"}, {"type": "result", "result": "done"},
                    {"type": "rate_limit_event"}, "not-a-dict", None,
                    {"type": "assistant", "message": None}):
            self.assertIsNone(supervisor.compact_event(evt))


class SupervisorCadenceTests(unittest.TestCase):
    """Checkpoints fire on time AND activity (so short runs cost nothing), one in flight at
    a time, bounded by MAX_CHECKS. Clock and spawn are injected for determinism."""

    def setUp(self):
        self._knobs = {k: getattr(config, k) for k in
                       ("SUPERVISOR_EVERY_S", "SUPERVISOR_MIN_EVENTS", "SUPERVISOR_MAX_CHECKS")}
        config.SUPERVISOR_EVERY_S, config.SUPERVISOR_MIN_EVENTS, config.SUPERVISOR_MAX_CHECKS = 60, 2, 2
        self.now = 0.0
        self.checks = []
        self._complete, self._trace = gateway.complete, supervisor.trace
        gateway.complete = lambda task, prompt: self.checks.append(task) or "CONTINUE"
        supervisor.trace = lambda *a, **k: None
        self.sup = supervisor.Supervisor("w1", 1, "req", "democap",
                                         clock=lambda: self.now,
                                         spawn=lambda fn: (fn(), None)[1])   # synchronous

    def tearDown(self):
        gateway.complete, supervisor.trace = self._complete, self._trace
        for k, v in self._knobs.items():
            setattr(config, k, v)

    def _evt(self, n):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": f"step {n}"}}]}}

    def test_short_run_never_checkpoints(self):
        for i in range(10):                       # plenty of events, no time elapsed
            self.sup.note(self._evt(i))
        self.assertEqual(self.checks, [])
        self.assertIsNone(self.sup.finish())      # no checkpoints -> nothing to report

    def test_needs_both_time_and_activity(self):
        self.now = 61                             # interval elapsed, but only 1 new event
        self.sup.note(self._evt(0))
        self.assertEqual(self.checks, [])
        self.sup.note(self._evt(1))               # 2nd event -> due
        self.assertEqual(self.checks, ["supervise"])

    def test_window_resets_and_max_checks_bounds(self):
        for tick in (61, 122, 183, 244):          # 4 windows' worth of activity...
            self.now = tick
            self.sup.note(self._evt(tick))
            self.sup.note(self._evt(tick + 0.5))
        self.assertEqual(len(self.checks), 2)     # ...but MAX_CHECKS=2 caps it
        summary = self.sup.finish()
        self.assertEqual(summary["checkpoints"], 2)
        self.assertEqual(summary["would_retry"], 0)

    def test_retry_verdicts_counted(self):
        gateway.complete = lambda task, prompt: "RETRY: chasing the wrong service"
        self.now = 61
        self.sup.note(self._evt(0))
        self.sup.note(self._evt(1))
        summary = self.sup.finish()
        self.assertEqual(summary["would_retry"], 1)
        self.assertTrue(summary["shadow"])
        self.assertEqual(summary["verdicts"][0]["verdict"], "retry")

    def test_prompt_fences_transcript_and_states_task(self):
        prompts = []
        gateway.complete = lambda task, prompt: prompts.append(prompt) or "CONTINUE"
        self.now = 61
        self.sup.note(self._evt(0))
        self.sup.note(self._evt(1))
        self.assertIn('"""', prompts[0])              # transcript fenced as data
        self.assertIn("req", prompts[0])              # the task
        self.assertIn("step 0", prompts[0])           # the activity
        self.assertIn("CONTINUE", prompts[0])         # the enum contract

    def test_gateway_failure_is_swallowed(self):
        def boom(task, prompt):
            raise OSError("supervise tier down")
        gateway.complete = boom
        self.now = 61
        self.sup.note(self._evt(0))
        self.sup.note(self._evt(1))                   # must not raise
        self.assertIsNone(self.sup.finish())          # no verdict recorded, run unharmed


class SupervisorWiringTests(unittest.TestCase):
    """engine.run_attempt wires the supervisor through the _claude seam (shadow only): a
    supervised attempt returns the summary and writes ONE supervisor_shadow audit row; a
    resumed session and a disabled supervisor pay zero overhead."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="otto-suptest-")
        self._audit_db, engine._DB = engine._DB, os.path.join(self._tmp, "otto.db")
        self._knobs = {k: getattr(config, k) for k in
                       ("SUPERVISE", "SUPERVISE_MODE", "SUPERVISOR_EVERY_S",
                        "SUPERVISOR_MIN_EVENTS", "SUPERVISOR_MAX_CHECKS")}
        config.SUPERVISE, config.SUPERVISOR_EVERY_S = True, 0
        config.SUPERVISOR_MIN_EVENTS, config.SUPERVISOR_MAX_CHECKS = 1, 1
        config.SUPERVISE_MODE = "shadow"   # the shadow tests below; enforce has its own
        self._claude, self._exec_id = engine._claude, gateway.exec_model_id
        self._complete, self._traces = gateway.complete, (engine.trace, engine.say, supervisor.trace)
        engine.trace = engine.say = supervisor.trace = lambda *a, **k: None
        gateway.exec_model_id = lambda cap_name=None: "claude-test"
        # Pin the BACKEND choice too: exec_model_entry reads the developer's real
        # data/models.json, and a live local execution pick there would reroute this mocked
        # run at the real local runtime instead of the fake engine._claude below.
        self._exec_entry = gateway.exec_model_entry
        gateway.exec_model_entry = lambda cap_name=None, cfg=None: {
            "name": "claude-test", "provider": "claude", "model": "claude-test"}
        gateway.complete = lambda task, prompt: "RETRY: fetched the US dashboard; the task is EU"
        self.on_events = []

        def fake_claude(prompt, on_event=None, **kw):
            self.on_events.append(on_event)
            if on_event:                               # simulate the live stream
                on_event({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]}})
            return {"result": "done", "total_cost_usd": 0.01, "session_id": "s1",
                    "usage": {"output_tokens": 5}}
        engine._claude = fake_claude
        self.cap = registry.Capability("skill", "demo", "a demo capability")
        self.cap.risk = "read"

    def tearDown(self):
        engine._claude, gateway.exec_model_id, gateway.complete = \
            self._claude, self._exec_id, self._complete
        gateway.exec_model_entry = self._exec_entry
        engine.trace, engine.say, supervisor.trace = self._traces
        for k, v in self._knobs.items():
            setattr(config, k, v)
        engine._DB = self._audit_db
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _audit_rows(self):
        return list(engine.iter_audit_entries())

    def _content_rows(self):
        return list(engine.iter_content_entries())

    def test_supervised_attempt_returns_summary_and_audits_shadow_row(self):
        att = engine.run_attempt("check the EU dashboard", self.cap, wid="w-sup")
        self.assertEqual(att["result"], "done")            # the run itself is untouched
        self.assertEqual(att["supervision"]["would_retry"], 1)
        rows = [r for r in self._audit_rows() if r.get("outcome") == "supervisor_shadow"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "supervisor_would_retry")
        content = [r for r in self._content_rows() if r.get("workflow") == "w-sup"]
        self.assertTrue(any("[supervisor shadow]" in r.get("result", "") for r in content))

    def test_retry_critique_reaches_the_supervisor_prompt(self):
        prompts = []
        gateway.complete = lambda task, prompt: prompts.append(prompt) or "CONTINUE"
        engine.run_attempt("check the EU dashboard", self.cap, attempt=2,
                           critique="split it into a tracked follow-up issue", wid="w-crit")
        self.assertTrue(any("split it into a tracked follow-up issue" in p for p in prompts),
                        "the verifier's critique must reach the supervisor's prompt")

    def test_resume_skips_supervision(self):
        att = engine.run_attempt("follow-up", self.cap, resume_session="sess-1", wid="w-res")
        self.assertIsNone(att["supervision"])
        self.assertEqual(self.on_events, [None])           # no watcher handed to the stream

    def test_disabled_supervisor_is_free(self):
        config.SUPERVISE = False
        att = engine.run_attempt("check things", self.cap, wid="w-off")
        self.assertIsNone(att["supervision"])
        self.assertEqual(self.on_events, [None])
        self.assertEqual([r for r in self._audit_rows()
                          if r.get("outcome") == "supervisor_shadow"], [])

    def test_enforce_mode_kills_the_attempt_and_steers_the_retry(self):
        # The supervisor's RETRY verdict arms the abort switch; the backend stops the
        # attempt; the killed attempt is a FAILED attempt whose critique steers the next
        # verify-ladder rung. (User ask: the supervisor should DO something, not just watch.)
        config.SUPERVISE_MODE = "enforce"
        killed = []

        def fake_claude(prompt, on_event=None, abort=None, **kw):
            self.on_events.append(on_event)
            if on_event:
                on_event({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]}})
            # emulate claude_cli's abort semantics: the kill switch fires mid-attempt
            if abort is not None and abort.wait(5):
                killed.append(abort.reason)
                return {"result": f"(aborted by supervisor: {abort.reason})",
                        "is_error": True, "total_cost_usd": 0, "aborted": True}
            return {"result": "done", "total_cost_usd": 0.01, "session_id": "s1",
                    "usage": {"output_tokens": 5}}
        engine._claude = fake_claude
        att = engine.run_attempt("check the EU dashboard", self.cap, wid="w-kill")
        self.assertTrue(att["is_error"])
        self.assertIn("aborted by supervisor", att["result"])
        self.assertTrue(killed and "US dashboard" in killed[0])
        self.assertTrue(att["supervision"]["killed"])
        rows = [r for r in self._audit_rows() if r.get("outcome") == "supervisor_kill"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "supervisor_retry")
        # ...and the ladder's synthetic verdict carries the supervisor's steering.
        v = engine.error_verdict(att["result"])
        self.assertFalse(v["passed"])
        self.assertIn("US dashboard", v["critique"])
        self.assertIn("different approach", v["critique"])


class SupervisorKillBudgetTests(unittest.TestCase):
    """The kill switch is armed by the LADDER, not by run_attempt alone — the only layer that
    knows whether a rung remains for the critique to steer and how many kills this run has spent.

    Measured over every enforce kill in the trail: 1 kill -> 9/17 runs eventually passed, 2 kills
    -> 2/7, 3 kills -> 0/6, and a 2nd-or-later kill was followed by a pass in 2 of 13 runs. A
    plain judge-failed attempt allowed to FINISH is rescued 49% of the time, so the 2nd kill
    onward trades a good rung for a worse one. Killing the FINAL rung is worse still: the critique
    has nowhere to go, and the run ends holding an aborted partial instead of whatever that
    attempt would have produced (`web-7ff7e792` delivered `(aborted by supervisor: …)` over an
    attempt 2 that had already opened a clean draft PR).

    Both ladders must agree — `engine._ladder_core` and `OttoWorkflow._verify_ladder` are
    deliberate mirrors (`LadderJudgeContextTests`)."""

    def setUp(self):
        self.armed = []          # supervise_enforce as each rung saw it
        self._orig = {n: getattr(engine, n) for n in ("run_attempt", "verify", "record_attempt")}
        self._knob = config.MAX_SUPERVISOR_KILLS
        self._settings = config.setting

        def fake_run_attempt(request, cap, *, attempt=1, supervise_enforce=True, **kw):
            self.armed.append(supervise_enforce)
            # Every rung is "killed" when armed, so an unbounded implementation kills all three.
            killed = bool(supervise_enforce)
            return {"workflow": kw.get("wid") or "w-kb", "attempt": attempt,
                    "result": "(aborted by supervisor: off-course)" if killed else "a real answer",
                    "cost": 0.0, "tokens": {"output": 1}, "model": "m", "session_id": "s",
                    "is_error": killed,
                    "supervision": {"killed": killed, "would_retry": 1, "checkpoints": 1,
                                    "shadow": False, "verdicts": []}}
        engine.run_attempt = fake_run_attempt
        engine.verify = lambda *a, **k: {"passed": False, "critique": "no"}
        engine.record_attempt = lambda *a, **k: None

    def tearDown(self):
        for n, fn in self._orig.items():
            setattr(engine, n, fn)
        config.MAX_SUPERVISOR_KILLS = self._knob

    def test_the_final_rung_is_never_armed_and_kills_are_bounded(self):
        engine._ladder_core("do it", registry.Capability("skill", "demo", "d"), "w-kb",
                            recall=False, project=None, remember=False)
        # Three rungs: the first may kill, the second must not (budget spent), the third is
        # final and must not be armed whatever the budget says.
        self.assertEqual(self.armed, [True, False, False])

    def test_zero_makes_enforce_observe_only(self):
        config.MAX_SUPERVISOR_KILLS = 0
        engine._ladder_core("do it", registry.Capability("skill", "demo", "d"), "w-kb0",
                            recall=False, project=None, remember=False)
        self.assertEqual(self.armed, [False, False, False])

    def test_run_attempt_honours_the_disarm(self):
        # Attribution: the flag must reach the Abort() construction, not just be accepted.
        seen = {}
        real = self._orig["run_attempt"]
        engine.run_attempt = real
        knobs = {k: getattr(config, k) for k in ("SUPERVISE", "SUPERVISE_MODE")}
        config.SUPERVISE, config.SUPERVISE_MODE = True, "enforce"
        orig_start = supervisor.start
        supervisor.start = lambda wid, attempt, request, cap, **kw: seen.setdefault(
            "abort", kw.get("abort")) and None
        orig_claude, orig_exec = engine._claude, gateway.exec_model_entry
        gateway.exec_model_entry = lambda cap_name=None, cfg=None: {
            "name": "c", "provider": "claude", "model": "c"}
        engine._claude = lambda *a, **k: {"result": "ok", "total_cost_usd": 0}
        try:
            cap = registry.Capability("skill", "demo", "d")
            cap.risk = "read"
            engine.run_attempt("x", cap, wid="w-d", supervise_enforce=False)
            self.assertIsNone(seen.get("abort"), "disarmed, yet an Abort() was still handed over")
            seen.clear()
            engine.run_attempt("x", cap, wid="w-a", supervise_enforce=True)
            self.assertIsNotNone(seen.get("abort"))
        finally:
            supervisor.start = orig_start
            engine._claude, gateway.exec_model_entry = orig_claude, orig_exec
            for k, v in knobs.items():
                setattr(config, k, v)


class SteerLadderArmingTests(unittest.TestCase):
    """WHO arms steering, and why it is not the flag that arms the kill switch.

    `supervise_enforce` is the LADDER's permission to KILL, and it is off on the final rung
    (a kill leaves no rung for the critique to steer) and off past the kill budget (a second kill
    rescues worse than letting the attempt finish). Neither reason transfers to a steer: it
    spends no rung and discards no work, so the final rung is where it is worth the most — it is
    the last chance to fix the run at all. Steering therefore travels its own setting and its own
    budget, and an operator collecting steer data must not have to arm kills to do it."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="otto-steerarm-")
        self._audit_db, engine._DB = engine._DB, os.path.join(self._tmp, "otto.db")
        self._knobs = {k: getattr(config, k) for k in
                       ("SUPERVISE", "SUPERVISE_MODE", "SUPERVISE_STEER",
                        "MAX_SUPERVISOR_STEERS", "SUPERVISOR_EVERY_S",
                        "SUPERVISOR_MIN_EVENTS", "SUPERVISOR_MAX_CHECKS")}
        config.SUPERVISE, config.SUPERVISE_MODE = True, "shadow"
        config.SUPERVISE_STEER, config.MAX_SUPERVISOR_STEERS = "enforce", 2
        config.SUPERVISOR_EVERY_S, config.SUPERVISOR_MIN_EVENTS = 0, 1
        config.SUPERVISOR_MAX_CHECKS = 1
        self._claude, self._exec_id = engine._claude, gateway.exec_model_id
        self._complete = gateway.complete
        self._traces = (engine.trace, engine.say, supervisor.trace)
        engine.trace = engine.say = supervisor.trace = lambda *a, **k: None
        gateway.exec_model_id = lambda cap_name=None: "claude-test"
        self._exec_entry = gateway.exec_model_entry
        gateway.exec_model_entry = lambda cap_name=None, cfg=None: {
            "name": "claude-test", "provider": "claude", "model": "claude-test"}
        gateway.complete = lambda task, prompt: "STEER: read the EU dashboard, not the US one"
        self.steers = []

        def fake_claude(prompt, on_event=None, steer=None, **kw):
            self.steers.append(steer)
            if on_event:
                on_event({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]}})
            # Stand in for the backend's own drain (claude_cli's steer-watch thread /
            # local_runtime's turn boundary). Nothing counts as steered until a backend TAKES
            # it — a queued instruction the attempt ended before consuming must read as
            # undelivered, not as one the agent ignored. The checkpoint runs on its own thread,
            # so wait for it the way a real backend does by simply still being alive.
            if steer is not None:
                deadline = time.time() + 5
                while not steer.offered and time.time() < deadline:
                    time.sleep(0.01)
            self.taken = steer.take() if steer is not None else []
            return {"result": "done", "total_cost_usd": 0.01, "session_id": "s1", "usage": {}}
        engine._claude = fake_claude
        self.cap = registry.Capability("skill", "demo", "a demo capability")
        self.cap.risk = "read"

    def tearDown(self):
        engine._claude, gateway.exec_model_id = self._claude, self._exec_id
        gateway.complete, gateway.exec_model_entry = self._complete, self._exec_entry
        engine.trace, engine.say, supervisor.trace = self._traces
        for k, v in self._knobs.items():
            setattr(config, k, v)
        engine._DB = self._audit_db
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_the_final_rung_can_still_be_steered(self):
        att = engine.run_attempt("check the EU dashboard", self.cap, wid="w-st",
                                 supervise_enforce=False)   # the ladder has forbidden a KILL
        self.assertIsNotNone(self.steers[0], "a steer needs no rung to spend")
        self.assertEqual(att["steers"], ["read the EU dashboard, not the US one"])

    def test_shadow_records_without_arming_the_backend(self):
        config.SUPERVISE_STEER = "shadow"
        att = engine.run_attempt("check the EU dashboard", self.cap, wid="w-st2")
        self.assertIsNone(self.steers[0], "shadow mode must not reach the backend")
        self.assertEqual(att["steers"], [])
        self.assertEqual(att["supervision"]["would_steer"], 1)

    def test_off_is_the_pre_steering_behaviour(self):
        config.SUPERVISE_STEER = "off"
        att = engine.run_attempt("check the EU dashboard", self.cap, wid="w-st3")
        self.assertIsNone(self.steers[0])
        self.assertEqual(att["supervision"]["would_steer"], 0,
                         "the verdict is never offered, so it is never reached")

    def test_a_resumed_session_is_never_steered(self):
        # Same rule the supervisor already follows: a resume is a mid-conversation reply, not a
        # task attempt in the verify ladder.
        engine.run_attempt("and the US one?", self.cap, wid="w-st4", resume_session="sess-1")
        self.assertIsNone(self.steers[0])

    def test_a_delivered_steer_writes_its_own_audit_row(self):
        engine.run_attempt("check the EU dashboard", self.cap, wid="w-st5")
        rows = [r for r in engine.iter_audit_entries()
                if r.get("outcome") == "supervisor_steer"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "supervisor_steer")


class SteeredRunVerifyTests(unittest.TestCase):
    """A steer amends the task mid-run, so the verifier has to be told — otherwise Otto's two
    judges fight: the supervisor narrows the job at minute 4, the agent obeys, and verify fails
    the attempt for delivering less than the request asked for. Exactly the collision
    `supervisor._prompt`'s retry_note prevents between the verifier and the supervisor, one
    layer further on."""

    def setUp(self):
        self._complete = gateway.complete
        self.prompts = []
        gateway.complete = lambda task, prompt: (self.prompts.append(prompt), "PASS")[1]
        self.cap = registry.Capability("custom", "sre", "investigates alerts")
        self.cap.risk = "read"

    def tearDown(self):
        gateway.complete = self._complete

    def test_the_judge_is_told_what_the_supervisor_changed(self):
        engine.verify("investigate every alert", self.cap, "Investigated the EU alert.",
                      steers=["look at the EU alert only"])
        p = self.prompts[0]
        self.assertIn("look at the EU alert only", p)
        self.assertIn("STEERED", p)
        self.assertIn("AS AMENDED", p)

    def test_an_unsteered_run_says_nothing_about_steering(self):
        engine.verify("investigate every alert", self.cap, "Investigated them.")
        self.assertNotIn("STEERED", self.prompts[0],
                         "a rule about an amendment that never happened is a defect the judge "
                         "can invent against")

    def test_a_concealed_redirection_is_still_judgeable(self):
        # The steer text tells the agent to report that it was redirected (config.STEER_MESSAGE);
        # the judge is what makes that enforceable, on the same principle as an unannounced
        # departure from an approved plan.
        engine.verify("investigate every alert", self.cap, "Investigated the EU alert.",
                      steers=["look at the EU alert only"])
        self.assertIn("conceals a redirection", self.prompts[0])


class SupervisorSteerTests(unittest.TestCase):
    """Mid-run STEERING: the supervisor's non-destructive intervention. A steer amends the task
    inside the LIVE session instead of killing the attempt, so the properties that matter are
    different from the kill switch's:

      - the STEER verdict exists only where the PROMPT offered it (a model volunteering the word
        must never silently convert a kill into a no-op),
      - the instruction is bounded to one line, because unlike a RETRY critique it is delivered
        verbatim into an agent that will obey it and cannot be told to stop,
      - shadow mode records and delivers nothing, which is the dataset the arming decision needs,
      - a spent budget WITHDRAWS the option rather than leaving the judge voting for an
        intervention that cannot happen.
    """

    def setUp(self):
        self._complete, self._trace = gateway.complete, supervisor.trace
        supervisor.trace = lambda *a, **k: None
        self.now, self.replies = 0.0, ["STEER: read the EU dashboard, not the US one"]
        gateway.complete = lambda task, prompt: (
            self.prompts.append(prompt) or self.replies[min(len(self.prompts) - 1,
                                                            len(self.replies) - 1)])
        self.prompts = []
        self._knobs = {k: getattr(config, k) for k in
                       ("SUPERVISOR_EVERY_S", "SUPERVISOR_MIN_EVENTS", "SUPERVISOR_MAX_CHECKS")}
        config.SUPERVISOR_EVERY_S, config.SUPERVISOR_MIN_EVENTS = 0, 1
        config.SUPERVISOR_MAX_CHECKS = 1

    def tearDown(self):
        gateway.complete, supervisor.trace = self._complete, self._trace
        for k, v in self._knobs.items():
            setattr(config, k, v)

    def _sup(self, **kw):
        return supervisor.Supervisor("w1", 1, "check the EU dashboard", "democap",
                                     clock=lambda: self.now,
                                     spawn=lambda fn: (fn(), None)[1], **kw)

    def _evt(self):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "x"}}]}}

    # --- the parser -----------------------------------------------------------------
    def test_steer_is_only_recognized_when_the_caller_offered_it(self):
        self.assertEqual(supervisor.parse_verdict("STEER: do X")["verdict"], "continue")
        self.assertEqual(supervisor.parse_verdict("STEER: do X", allow_steer=True)["verdict"],
                         "steer")

    def test_steer_takes_the_first_line_only_and_is_capped(self):
        # A RETRY critique folds the tail in; a steer must not. The `supervise` tier is
        # local-model eligible and a weak model leaks chatter after its verdict line — folded in,
        # that chatter becomes an instruction inside a running agent.
        v = supervisor.parse_verdict("STEER: stop and re-read the ticket\nAlso I think maybe...",
                                     allow_steer=True)
        self.assertEqual(v["critique"], "stop and re-read the ticket")
        long = supervisor.parse_verdict("STEER: " + "x" * 900, allow_steer=True)
        self.assertLessEqual(len(long["critique"]), supervisor._STEER_CHARS)

    def test_empty_steer_is_not_an_intervention(self):
        # Interrupting a working agent to tell it nothing is strictly worse than continuing.
        self.assertEqual(supervisor.parse_verdict("STEER:", allow_steer=True)["verdict"],
                         "continue")

    # --- the channel ----------------------------------------------------------------
    def test_budget_is_spent_at_offer_and_take_drains_once(self):
        ch = supervisor.Steer(budget=2)
        self.assertTrue(ch.offer("one"))
        self.assertTrue(ch.offer("two"))
        self.assertFalse(ch.offer("three"), "budget is consumed at offer, not at delivery")
        self.assertEqual(ch.take(), ["one", "two"])
        self.assertEqual(ch.take(), [], "an instruction is delivered exactly once")
        self.assertEqual(ch.delivered, ["one", "two"])
        self.assertFalse(ch.armed())

    # --- the prompt -----------------------------------------------------------------
    def test_prompt_offers_steer_only_when_it_could_be_delivered(self):
        plain = self._sup()
        self.assertNotIn("STEER", plain._prompt("transcript"))
        armed = self._sup(steer=supervisor.Steer(budget=1))
        self.assertIn("STEER:", armed._prompt("transcript"))
        shadow = self._sup(steer_shadow=True)
        self.assertIn("STEER:", shadow._prompt("transcript"),
                      "shadow mode must offer the verdict — recording it is the whole point")

    def test_a_spent_budget_withdraws_the_option(self):
        ch = supervisor.Steer(budget=1)
        ch.offer("already used")
        sup = self._sup(steer=ch)
        self.assertNotIn("STEER", sup._prompt("transcript"),
                         "with the budget gone the judge must fall back to CONTINUE/RETRY, "
                         "not vote for something that silently cannot happen")

    # --- delivery -------------------------------------------------------------------
    def test_enforce_queues_the_instruction_for_the_backend(self):
        ch = supervisor.Steer(budget=2)
        sup = self._sup(steer=ch)
        sup.note(self._evt())
        self.assertEqual(ch.take(), ["read the EU dashboard, not the US one"])
        summary = sup.finish()
        self.assertEqual(summary["would_steer"], 1)
        self.assertEqual(summary["steers"], ["read the EU dashboard, not the US one"])
        self.assertFalse(summary["shadow"])

    def test_shadow_records_the_verdict_and_delivers_nothing(self):
        sup = self._sup(steer_shadow=True)
        sup.note(self._evt())
        summary = sup.finish()
        self.assertEqual(summary["would_steer"], 1)
        self.assertEqual(summary["steers"], [],
                         "shadow mode is the false-steer dataset, not a delivery path")
        self.assertFalse(summary["verdicts"][0]["delivered"])

    def test_a_steer_never_kills_the_attempt(self):
        abort, ch = supervisor.Abort(), supervisor.Steer(budget=1)
        sup = self._sup(abort=abort, steer=ch)
        sup.note(self._evt())
        self.assertFalse(abort.is_set(),
                         "steering is the alternative to killing, never both")

    def test_a_zero_budget_is_indistinguishable_from_steering_off(self):
        # Not "offer it and refuse to deliver": with nothing left to deliver the option is
        # withdrawn up front, so the judge spends its verdict on CONTINUE/RETRY — the choices
        # that can still be acted on — instead of voting for a no-op it is never told about.
        ch = supervisor.Steer(budget=0)
        sup = self._sup(steer=ch)
        self.assertNotIn("STEER", sup._prompt("transcript"))
        sup.note(self._evt())
        summary = sup.finish()
        self.assertEqual(summary["would_steer"], 0)
        self.assertEqual(summary["verdicts"][0]["verdict"], "continue",
                         "an unoffered STEER reply is not an intervention")

    def test_a_delivered_steer_is_visible_in_the_compacted_transcript(self):
        # server._transcript_events compacts through this same function, so without a branch here
        # the debug drawer is the one place a reader cannot see that the task was amended.
        line = supervisor.compact_event({"type": "otto-steer", "text": "look at the EU alert"})
        self.assertIn("look at the EU alert", line)
        self.assertIn("correction", line)

    def test_earlier_steers_are_replayed_to_a_later_checkpoint(self):
        # Same reason the RETRY history is: each checkpoint sees only a bounded TAIL of the
        # transcript, so without this a later one re-diagnoses a problem it already corrected.
        config.SUPERVISOR_MAX_CHECKS = 2
        ch = supervisor.Steer(budget=2)
        sup = self._sup(steer=ch)
        sup.note(self._evt())
        self.now = 1
        sup.note(self._evt())
        self.assertIn("STEER — read the EU dashboard", self.prompts[-1])


class VerdictModelOnTheTrailTests(unittest.TestCase):
    """WHICH judge reached a verdict, recorded on the audit row.

    The trail already carried `verdict_source` — judge / supervisor / harness — but not the model
    that WAS the judge. Measured over 2026-07-06..2026-08-25, 197 real judge critiques were
    unattributable for exactly this reason: a run that failed verification and a run whose JUDGE
    was wrong leave identical rows, so `scorecard`'s `false_fails` (the signal that points at a
    bad verify prompt rather than a bad capability) cannot be split by judge model. It costs one
    string per row and is the only way to tell the two apart after the fact."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="otto-vmodel-")
        self._db, engine._DB = engine._DB, os.path.join(self._tmp, "otto.db")
        self._last = gateway.last
        self.cap = registry.Capability("agent", "sre-qa", "tests things")
        self.cap.risk = "read"

    def tearDown(self):
        engine._DB = self._db
        gateway.last = self._last
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _rows(self):
        return list(engine.iter_audit_entries())

    def test_a_judged_attempt_records_the_model_that_judged_it(self):
        engine.record_attempt("w-vm1", "do it", self.cap, "done", 0.1, 1,
                              {"passed": False, "source": "judge", "critique": "no",
                               "model": "claude-sonnet-5"})
        row = self._rows()[-1]
        self.assertEqual(row["verdict_model"], "claude-sonnet-5")
        self.assertEqual(row["verdict_source"], "judge")
        self.assertIs(row["verified"], False)

    def test_the_judge_model_is_separate_from_the_model_that_RAN_the_attempt(self):
        # The whole point: `model` is the executor, `verdict_model` the judge. A local execution
        # judged by a Claude tier is the common shape, and collapsing them loses the comparison.
        engine.record_attempt("w-vm2", "do it", self.cap, "done", 0.1, 1,
                              {"passed": True, "source": "judge", "model": "claude-sonnet-5"},
                              model="qwen38-27b", backend="local")
        row = self._rows()[-1]
        self.assertEqual(row["model"], "qwen38-27b")
        self.assertEqual(row["verdict_model"], "claude-sonnet-5")

    def test_an_unjudged_attempt_carries_no_judge_model(self):
        # A supervisor kill and a harness death are not judgements — neither has a judge, so
        # neither may claim one, or the field stops meaning "the model that judged this".
        engine.record_attempt("w-vm3", "do it", self.cap, "(timed out)", 0, 1,
                              {"passed": False, "source": "harness", "critique": "died"})
        self.assertNotIn("verdict_model", self._rows()[-1])
        engine.record_attempt("w-vm4", "do it", self.cap, "x", 0, 1, None)
        self.assertNotIn("verdict_model", self._rows()[-1])

    def test_verify_stamps_the_tier_that_actually_answered(self):
        # gateway.last("verify") reflects the call the verdict came from, INCLUDING a fallback
        # ("qwen → claude"), which is precisely the case worth being able to see afterwards.
        gateway.last = lambda task: ({"model": "qwen3.6 \u2192 claude (fallback)"}
                                     if task == "verify" else None)
        confirm = judging.confirm_adverse
        judging.confirm_adverse = lambda *a, **k: {"passed": False, "critique": "nope"}
        try:
            v = judging.verify("do it", self.cap, "some output")
        finally:
            judging.confirm_adverse = confirm
        self.assertEqual(v["source"], "judge")
        self.assertEqual(v["model"], "qwen3.6 \u2192 claude (fallback)")


class ModelIdNamespaceTests(unittest.TestCase):
    """`model` and `verdict_model` must name the model in ONE namespace.

    A pool entry carries two names: `name`, the operator's freely-editable LABEL, and `model`,
    the id the server serves. Every Claude path recorded the id (`exec_model_id` returns
    `m["model"]`) and every local path plus `gateway.last()` recorded the LABEL (`m["name"]`), so
    the trail held both. Measured on the live trail before the fix: 43 of 58 attributed verdicts
    said `claude-sonnet` while all 297 execution rows for that same model said `claude-sonnet-5`
    — so `verdict_model`, whose entire purpose is letting `scorecard` tell a bad judge from a bad
    capability, could not be joined to the executor column at all."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="otto-mid-")
        self._db, engine._DB = engine._DB, os.path.join(self._tmp, "otto.db")
        self._path, gateway._PATH = gateway._PATH, os.path.join(self._tmp, "models.json")
        gateway.save({"pool": [{"name": "claude-sonnet", "provider": "claude",
                                "model": "claude-sonnet-5"},
                               {"name": "Deepseek Flash", "provider": "openai",
                                "model": "deepseek-v4-flash", "endpoint": "ep"}],
                      "assign": {}, "endpoints": [{"name": "ep", "base_url": "http://x/v1"}]})
        self.cap = registry.Capability("agent", "sre-qa", "tests things")
        self.cap.risk = "read"

    def tearDown(self):
        engine._DB, gateway._PATH = self._db, self._path
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_label_resolves_to_the_id_the_server_serves(self):
        self.assertEqual(gateway.model_id("claude-sonnet"), "claude-sonnet-5")
        self.assertEqual(gateway.model_id("Deepseek Flash"), "deepseek-v4-flash")

    def test_an_id_and_an_unknown_label_pass_through_unchanged(self):
        # Fails OPEN. A retired entry's label can no longer be resolved from the pool, and
        # returning None (or a guess) there would erase or fabricate what actually ran — in an
        # audit trail that is immutable by design.
        self.assertEqual(gateway.model_id("claude-sonnet-5"), "claude-sonnet-5")
        self.assertEqual(gateway.model_id("DeepSeek (Tyler)"), "DeepSeek (Tyler)")
        self.assertIsNone(gateway.model_id(None))

    def test_the_judge_and_the_executor_on_one_model_record_ONE_string(self):
        # The invariant the whole change exists for. The executor arrives as an id (Claude
        # dispatch) and the judge as a label (gateway.last) — the same model, two spellings, so
        # any group-by splits it in two.
        engine.record_attempt("w-mid1", "do it", self.cap, "done", 0.1, 1,
                              {"passed": False, "source": "judge", "critique": "no",
                               "model": "claude-sonnet"},
                              model="claude-sonnet-5")
        row = list(engine.iter_audit_entries())[-1]
        self.assertEqual(row["verdict_model"], row["model"])
        self.assertEqual(row["model"], "claude-sonnet-5")

    def test_the_label_is_kept_beside_the_id_when_it_differed(self):
        # Collapsing two labels onto one id is right for model QUALITY but loses what the
        # operator configured — which is the actionable half when an endpoint is the problem.
        engine.record_attempt("w-mid2", "do it", self.cap, "done", 0, 1,
                              {"passed": True, "source": "judge"},
                              model="Deepseek Flash", backend="local")
        row = list(engine.iter_audit_entries())[-1]
        self.assertEqual(row["model"], "deepseek-v4-flash")
        self.assertEqual(row["model_entry"], "Deepseek Flash")

    def test_a_row_whose_model_was_already_canonical_gains_no_label(self):
        engine.record_attempt("w-mid3", "do it", self.cap, "done", 0, 1, None,
                              model="claude-sonnet-5")
        self.assertNotIn("model_entry", list(engine.iter_audit_entries())[-1])


class WorkflowComplexityTests(unittest.TestCase):
    """`_run_impl` is the most expensive function in the repo to let sprawl: it is the single
    convergence point of all five ingresses AND deterministic replay code, so a mid-run edit
    can fail a workflow that is already in flight. It reached 845 lines / 137 branches against
    a median of 3 branches for its own file — 45x — before the phase split.

    `ClaudeMdBudgetTests` ratchets doc bytes and `RuleEnforcementTests` ratchets doc
    enforcement; this ratchets the shape of the code those docs describe. Both numbers are
    ceilings with no headroom: they may only ever be driven DOWN, and lowering one is an
    explicit constant edit that shows up in the diff.

    Extraction here must be PURE — the sequence of activity commands a run issues is its replay
    history, so reordering, adding or dropping one is not a refactor. `_route_or_resume`,
    `_plan_and_gate` and `_finalize_pr` were verified command-for-command against the pre-split
    source before landing."""

    MAX_RUN_IMPL_BRANCHES = 87    # was 137 before the phase split. DOWN only.
    MAX_RUN_IMPL_LINES = 514      # was 845. DOWN only.
    MAX_METHOD_BRANCHES = 87      # no OttoWorkflow method may exceed _run_impl itself

    _BRANCH = (ast.If, ast.For, ast.While, ast.Try, ast.ExceptHandler, ast.With,
               ast.BoolOp, ast.IfExp, ast.comprehension, ast.Assert, ast.Match)

    def _methods(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows.py")
        with open(path) as fh:
            tree = ast.parse(fh.read())
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "OttoWorkflow")
        out = {}
        for m in cls.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                branches = sum(1 for n in ast.walk(m) if isinstance(n, self._BRANCH))
                out[m.name] = (branches, m.end_lineno - m.lineno + 1)
        return out

    def test_run_impl_stays_within_its_ceiling(self):
        branches, lines = self._methods()["_run_impl"]
        self.assertLessEqual(branches, self.MAX_RUN_IMPL_BRANCHES,
                             "_run_impl grew branches - extract a phase, don't raise this")
        self.assertLessEqual(lines, self.MAX_RUN_IMPL_LINES,
                             "_run_impl grew lines - extract a phase, don't raise this")

    def test_no_workflow_method_out_grows_run_impl(self):
        over = {n: b for n, (b, _l) in self._methods().items()
                if b > self.MAX_METHOD_BRANCHES}
        self.assertEqual({}, over, "a new hotspot formed in OttoWorkflow")

    def test_every_extracted_phase_returns_only_bound_names(self):
        """The split's one real hazard, and the one that bit: inline, `subtask` was left unbound
        on the resume path and simply never read (that path returns first). Lifting the block
        into a method made the return tuple read it on EVERY path - UnboundLocalError, which
        surfaced as the Temporal test worker retrying forever rather than as a failure."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows.py")
        with open(path) as fh:
            tree = ast.parse(fh.read())
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "OttoWorkflow")

        def targets(st):
            """Every Name bound by one statement, including tuple/list unpacking."""
            tgts = st.targets if isinstance(st, ast.Assign) else (
                [st.target] if getattr(st, "target", None) is not None else [])
            return {n.id for t in tgts for n in ast.walk(t)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}

        def binds(body, name):
            for st in body:
                if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and \
                        name in targets(st):
                    return True
                if isinstance(st, ast.If) and st.orelse:
                    if binds(st.body, name) and binds(st.orelse, name):
                        return True
                if isinstance(st, ast.Try) and binds(st.body, name) and all(
                        binds(h.body, name) for h in st.handlers):
                    return True
            return False

        unbound = []
        for m in cls.body:
            if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            rets = [s for s in m.body if isinstance(s, ast.Return) and s.value is not None]
            if not rets:
                continue
            val = rets[-1].value
            elts = val.elts if isinstance(val, ast.Tuple) else [val]
            args = {a.arg for a in m.args.args}
            for e in elts:
                if isinstance(e, ast.Name) and e.id not in args and not binds(m.body, e.id):
                    unbound.append(f"{m.name} returns `{e.id}`")
        self.assertEqual([], unbound)


@unittest.skipUnless(_HAS_TEMPORAL, "the heartbeat seam lives on the Temporal layer")
def _until(pred, timeout=10, tick=0.01):
    """Block until `pred()` holds, or the deadline. Returns the final verdict.

    A heartbeat test must wait for BEATS, never for a wall-clock window. A fixed sleep turns
    "does this beat?" into "does this machine schedule a starved helper thread promptly?", and
    a loaded CI runner answers no: macOS delivered 4 beats where a 0.15s sleep at a 0.01s
    interval expects 15, failing a working implementation. The deadline exists only so a
    genuinely dead heartbeat fails in seconds instead of hanging the suite."""
    end = time.monotonic() + timeout
    while not pred() and time.monotonic() < end:
        time.sleep(tick)
    return pred()


class ExecutionHeartbeatTests(unittest.TestCase):
    """Long execution activities beat on a timer so Temporal learns a WORKER died within
    `_HEARTBEAT` instead of at `start_to_close_timeout`. That is the whole reason the ceiling
    is raisable: it used to double as the stall a worker restart cost, pinning every execution
    to 20 minutes while seven attempts across haiku, sonnet and opus died mid-task on the
    1100s watchdog under it."""

    def _src(self, name):
        with open(os.path.join(os.path.dirname(__file__), name), encoding="utf-8") as f:
            return f.read()

    def test_heartbeating_beats_from_its_own_thread_while_the_work_blocks(self):
        # The beat runs in a bare thread, which does NOT inherit contextvars — and temporalio
        # keeps the activity context in one, so a naive thread makes activity.heartbeat() raise
        # "not in an activity" and nothing ever beats. copy_context() is the fix; this proves it
        # by driving the real helper with the real contextvar seam.
        import contextvars
        from temporalio import activity as tact
        beats = []
        real_in, real_hb = tact.in_activity, tact.heartbeat
        marker = contextvars.ContextVar("otto-test-activity")

        def fake_hb(*details):
            # Resolving the contextvar is the actual assertion: it only succeeds if the beat
            # thread ran inside the copied activity context.
            beats.append((marker.get(), details))
        tact.in_activity, tact.heartbeat = (lambda: True), fake_hb
        self.addCleanup(lambda: setattr(tact, "heartbeat", real_hb))
        self.addCleanup(lambda: setattr(tact, "in_activity", real_in))
        try:
            marker.set("in-activity")
            with activities._heartbeating("run", every_s=0.01):
                _until(lambda: len(beats) >= 3)
        finally:
            pass
        self.assertGreaterEqual(len(beats), 3, f"a 0.01s beat produced none in 10s: {beats}")
        self.assertTrue(all(b[0] == "in-activity" for b in beats),
                        "a beat ran outside the activity context — contextvars were not copied")
        self.assertEqual(beats[0][1][0], "run")               # labelled, so the UI names it
        # And it STOPS: a beat outliving its activity heartbeats a slot that is doing nothing.
        n = len(beats)
        time.sleep(0.08)
        self.assertEqual(len(beats), n, "the beat thread outlived the context manager")

    def test_a_failing_beat_never_stops_the_beating(self):
        # `return` here would be the worst kind of bug this change can introduce: one transient
        # miss stops the heartbeats, and heartbeat_timeout then kills a perfectly healthy
        # attempt three minutes later. A miss must be survivable.
        from temporalio import activity as tact
        calls = []
        real_in, real_hb = tact.in_activity, tact.heartbeat

        def flaky(*details):
            calls.append(1)
            if len(calls) <= 2:
                raise RuntimeError("transient heartbeat failure")
        tact.in_activity, tact.heartbeat = (lambda: True), flaky
        self.addCleanup(lambda: setattr(tact, "heartbeat", real_hb))
        self.addCleanup(lambda: setattr(tact, "in_activity", real_in))
        with activities._heartbeating("run", every_s=0.01):
            _until(lambda: len(calls) > 4)
        self.assertGreater(len(calls), 4, f"beating stopped after the failures: {len(calls)}")

    def test_heartbeating_outside_an_activity_is_a_no_op(self):
        # Every activity is also called directly by tests and the web path; the helper must not
        # need a Temporal context to exist.
        with activities._heartbeating("run", every_s=0.01):
            time.sleep(0.03)

    def test_every_long_execution_activity_declares_a_heartbeat_timeout(self):
        # The bug class is a new long activity (or a new call site of an existing one) added
        # without one: it works, and only a worker death months later shows that the run stalled
        # for the whole ceiling. Parsed, not grepped, so a timeout in a comment can't satisfy it.
        import ast
        tree = ast.parse(self._src("workflows.py"))
        missing = []
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "execute_activity"):
                continue
            kw = {k.arg: k.value for k in n.keywords}
            sto = kw.get("start_to_close_timeout")
            long_ = (isinstance(sto, ast.Name) and sto.id == "_EXEC_CEILING") or (
                isinstance(sto, ast.Call) and any(
                    k.arg == "minutes" and isinstance(k.value, ast.Constant)
                    and k.value.value >= 10 for k in sto.keywords))
            if long_ and "heartbeat_timeout" not in kw:
                missing.append(n.lineno)
        self.assertEqual(missing, [],
                         f"execute_activity at workflows.py:{missing} runs for >=10min with no "
                         "heartbeat_timeout — a dead worker stalls that run for the whole ceiling")

    def test_every_activity_promised_a_heartbeat_actually_beats(self):
        # The DANGEROUS inverse of the test above, and the reason both exist. Declaring
        # heartbeat_timeout on an activity that never beats does not stall a run — it kills a
        # healthy one after 3 minutes, which is far worse than the 20min stall this change set
        # out to fix. The decorator is what makes the promise true.
        import ast
        wf = ast.parse(self._src("workflows.py"))
        promised = set()
        for n in ast.walk(wf):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "execute_activity" and n.args
                    and isinstance(n.args[0], ast.Name)
                    and any(k.arg == "heartbeat_timeout" for k in n.keywords)):
                promised.add(n.args[0].id)
        self.assertTrue(promised, "no heartbeat_timeout call sites found — re-point this test")
        beating = set()
        for n in ast.walk(ast.parse(self._src("activities.py"))):
            if not isinstance(n, ast.FunctionDef):
                continue
            for d in n.decorator_list:
                f = d.func if isinstance(d, ast.Call) else d
                if isinstance(f, ast.Name) and f.id == "_heartbeats":
                    beating.add(n.name)
        self.assertEqual(promised - beating, set(),
                         "these activities are given a heartbeat_timeout but never beat — "
                         "Temporal will kill them mid-work")

    def test_the_exec_watchdog_stays_under_the_activity_ceiling(self):
        # Two numbers in two files that must move together: the in-process watchdog has to fire
        # FIRST, or `claude -p` is killed by Temporal with no result, no audit row and no
        # critique for the ladder to retry on.
        ceiling = workflows._EXEC_CEILING.total_seconds()
        self.assertLess(config.EXEC_TIMEOUT_S, ceiling,
                        "EXEC_TIMEOUT_S must fire before Temporal kills the activity")
        self.assertGreaterEqual(ceiling - config.EXEC_TIMEOUT_S, 60,
                                "under a minute of headroom for activity overhead")
        # LOCAL_RUN_TIMEOUT_S defaults to EXEC_TIMEOUT_S, so it inherits the same relationship.
        self.assertLess(config.LOCAL_RUN_TIMEOUT_S, ceiling)
        # And the beat has to be comfortably inside the window it feeds.
        self.assertLess(config.HEARTBEAT_EVERY_S * 3, workflows._HEARTBEAT.total_seconds())

    def test_no_execution_activity_can_be_replayed_for_duplicate_spend(self):
        # _RETRY_EXEC (max_attempts=1) exists precisely so a lost activity is never silently
        # re-run: duplicate subscription spend, duplicate side effects, no audit row. The resume
        # branch was on _RETRY (max 3) — and unlike the fresh path it has no verify ladder that
        # would notice the duplicate.
        import ast
        tree = ast.parse(self._src("workflows.py"))
        EXEC = {"run_capability", "qa_capability", "review_capability", "execute_plan"}
        bad = []
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "execute_activity" and n.args
                    and isinstance(n.args[0], ast.Name) and n.args[0].id in EXEC):
                continue
            rp = {k.arg: k.value for k in n.keywords}.get("retry_policy")
            if not (isinstance(rp, ast.Name) and rp.id == "_RETRY_EXEC"):
                bad.append((n.args[0].id, n.lineno))
        self.assertEqual(bad, [], f"execution activities not on _RETRY_EXEC: {bad}")


class DiscussionTurnNarrowingTests(unittest.TestCase):
    """`activities.run_capability` re-resolves the cap by NAME, so the workflow's per-turn risk has
    to travel in the payload. Two things about that channel are load-bearing on their own:

    it must only ever NARROW (a payload cannot widen a read cap into a write one, or the approval
    gate is bypassable by anything that reaches this dict), and it must COPY the capability —
    `_capabilities()` memoizes ONE list for the worker's whole lifetime, so mutating the shared
    object in place would silently re-risk that capability for every later run on that worker,
    long after the discussion turn ended."""

    def setUp(self):
        import activities
        self.activities = activities
        self.write_cap = registry.Capability("agent", "sre-minion", "implements a ticket")
        self.write_cap.risk = "write"
        self.read_cap = registry.Capability("skill", "demo-read", "answers questions")
        self.read_cap.risk = "read"
        self._orig_caps = activities._caps
        activities._caps = [self.write_cap, self.read_cap]
        self.seen = []
        self._orig_attempt = engine.run_attempt
        engine.run_attempt = lambda request, cap, **k: (
            self.seen.append((cap, cap.risk, k.get("discussion"))) or
            {"workflow": "wf-x", "result": "r", "cost": 0.0, "attempt": 1})

    def tearDown(self):
        self.activities._caps = self._orig_caps
        engine.run_attempt = self._orig_attempt

    def _run(self, name, **extra):
        self.activities.run_capability({"request": "q", "name": name, **extra})
        return self.seen[-1]

    def test_a_read_payload_narrows_a_write_cap_and_flags_the_turn(self):
        cap, risk, discussion = self._run("sre-minion", risk="read")
        self.assertEqual(risk, "read")
        self.assertTrue(discussion, "the executor was never told this turn is a conversation")

    def test_it_never_widens_a_read_cap(self):
        # The gate hangs off cap.risk. If a payload could raise read->write it would also be able
        # to lower it back somewhere else; the only safe direction is down.
        _, risk, discussion = self._run("demo-read", risk="write")
        self.assertEqual(risk, "read")
        self.assertFalse(discussion)

    def test_the_narrowed_cap_is_a_copy_not_the_cached_one(self):
        cap, _, _ = self._run("sre-minion", risk="read")
        self.assertIsNot(cap, self.write_cap)
        self.assertEqual(self.write_cap.risk, "write",
                         "a discussion turn permanently de-risked the cached capability")
        # And the very next run on this worker is unaffected.
        _, risk, _ = self._run("sre-minion")
        self.assertEqual(risk, "write")

    def test_no_risk_key_changes_nothing(self):
        _, risk, discussion = self._run("sre-minion")
        self.assertEqual(risk, "write")
        self.assertFalse(discussion)


class PlanVisibilityTests(unittest.TestCase):
    """The plan preview is a full agentic `claude -p` pass that recorded nothing: no transcript,
    so the board's model chip (which resolves the model BY reading one) stayed blank for up to
    15 minutes, reading as "nothing is running", and its tool calls were unreviewable."""

    def test_the_preview_has_its_own_transcript_name(self):
        p = claude_cli.plan_transcript_path("web-1")
        self.assertTrue(p.endswith("web-1-plan.jsonl"))
        # Never `-aN`: nothing that enumerates execution attempts may mistake it for one.
        self.assertNotIn("-a", os.path.basename(p).replace("-plan.jsonl", ""))

    def test_plan_preview_passes_that_transcript(self):
        src = open("plans.py").read()
        self.assertIn("claude_cli.plan_transcript_path(wid) if wid else None", src)

    def test_the_board_chip_falls_back_to_the_plan_transcript(self):
        d = tempfile.mkdtemp(prefix="otto-planchip-")
        orig = claude_cli.TRANSCRIPTS
        claude_cli.TRANSCRIPTS = d
        try:
            wid = "web-chip"
            with open(claude_cli.plan_transcript_path(wid), "w") as f:
                f.write(json.dumps({"type": "otto-meta", "model": "claude-opus-4-8"}) + "\n")
            self.assertEqual(server._run_model(wid)[0], "claude-opus-4-8")
            # An EXECUTION transcript must win once one exists — the chip tracks the newest attempt.
            with open(claude_cli.transcript_path(wid, 1), "w") as f:
                f.write(json.dumps({"type": "otto-meta", "model": "deepseek-v4-flash",
                                    "runtime": "local"}) + "\n")
            self.assertEqual(server._run_model(wid)[0], "deepseek-v4-flash")
        finally:
            claude_cli.TRANSCRIPTS = orig
            shutil.rmtree(d, ignore_errors=True)


class PlanBranchNoteTests(unittest.TestCase):
    """The preview runs from the repo's LIVE checkout, before anything is provisioned — so for a
    request about an open PR it reads the default branch. `web-a6122d6c` burned the full 909s
    ceiling planning a change to code that is not in that tree."""

    def test_the_note_names_the_branch_and_how_to_read_it(self):
        n = plans._pr_branch_note({"number": 106, "branch": "otto/x"})
        self.assertIn("#106", n)
        self.assertIn("otto/x", n)
        # PLAN_TOOLS already grants this — the note's whole job is to point at it.
        self.assertIn("gh pr diff 106", n)
        self.assertTrue(any("Bash(gh pr diff" in t for t in config.PLAN_TOOLS),
                        "the note tells the planner to run a command it isn't granted")

    def test_it_forbids_the_wrong_conclusion(self):
        """Left to itself the planner concludes the code is missing or already fixed — and plans
        against that, which is worse than not planning."""
        n = plans._pr_branch_note({"number": 1, "branch": "b"}).lower()
        self.assertIn("not in your working directory", n)
        self.assertIn("wrong branch", n)

    def test_no_pr_means_no_note(self):
        self.assertEqual(plans._pr_branch_note(None), "")
        self.assertEqual(plans._pr_branch_note({}), "")

    def test_the_target_is_resolved_before_the_gate_and_reused(self):
        """Resolved once, above the gate (the preview needs it) and read from cache at provision
        — two `gh` round-trips per run would be the obvious wrong way to wire this."""
        src = open("workflows.py").read()
        self.assertEqual(src.count("resolve_pr_target, {\"repo\": repo, \"request\": request}"), 2,
                         "expected exactly the pre-gate resolve and the resume repair tier")
        gate = src.index("# Approval for writes.")
        pre = src.index("self._pr_target = await workflow.execute_activity(")
        self.assertLess(pre, gate, "the PR target must resolve BEFORE the plan preview runs")
        self.assertIn('"pr": self._pr_target', src)


class HarnessDeathKeepsLocalTests(unittest.TestCase):
    """A harness death is not a judgement, so it must not banish a run from the local backend.

    `web-a056884d`: deepseek emitted 75k output tokens of reasoning and hit
    LOCAL_EXEC_MAX_TOKENS with no final answer. No judge read that attempt — but issue #172's
    write-local escalation fired anyway, latching the rest of the ladder onto Claude for three
    more attempts ending on Opus ($2.60), over a token ceiling an env var raises."""

    def test_both_ladders_spare_a_harness_death(self):
        """The loop is written twice on purpose (CLAUDE.md: change one, mirror the other), so a
        guard that checks one copy proves nothing about the run path the other serves."""
        for path in ("engine.py", "workflows.py"):
            src = open(path).read()
            i = src.index("WRITE_LOCAL_ESCALATE_REASON")
            block = src[max(0, i - 900):i]
            self.assertIn('verdict.get("source") != "harness"', block,
                          f"{path}'s write-local escalation still fires on a harness death")

    def test_a_judged_local_failure_still_escalates(self):
        """Issue #172's actual purpose — a write cap that a JUDGE failed on local moves to
        Claude. Narrowing must not disable it."""
        for path in ("engine.py", "workflows.py"):
            src = open(path).read()
            i = src.index("WRITE_LOCAL_ESCALATE_REASON")
            block = src[max(0, i - 900):i]
            self.assertIn('write_local', block)
            self.assertIn('local_fallback', block)

    def test_it_matches_the_rung_rule_directly_below_it(self):
        """The same loop already spares a harness death from spending a ladder rung, for the same
        stated reason. The two decisions disagreeing is the bug."""
        src = open("workflows.py").read()
        i = src.index("WRITE_LOCAL_ESCALATE_REASON")
        after = src[i:i + 1200]
        self.assertIn('verdict.get("source") == "harness"', after,
                      "expected the rung-sparing check just below the escalation check")


def _read(name):
    """Read a source file for a shape guard, without leaking the handle into the suite."""
    with io.open(name, encoding="utf-8", newline="") as fh:
        return fh.read()


class BrainstormModeTests(unittest.TestCase):
    """Brainstorm is a MODE, not a route: the read-only conversational capability the user opts
    into when they want to think something through rather than hand over a task.

    It exists because a web chat has no `reply_to`, so `delivery.audience_for` never runs and the
    output contract falls all the way back to `_REPORT_FORMAT` — which REQUIRES a "**TLDR** — "
    opening line and forbids ending on a question. Every first turn of every web chat was
    therefore shaped as an operator report about a finished task, including the ones that were
    someone thinking out loud. Four things make the mode work, and each is one edit away from
    silently reverting to that:

      • it must be unreachable by Router #1 (it skips the verify ladder — a route into it by
        mistake drops a real task's only quality check),
      • its output contract must win over whatever the delivery target implies,
      • the RESUME contract must not re-impose the TLDR the mode's own contract forbids, and
      • an unjudged run must not read as a FAILED verify.
    """

    def _caps(self):
        import contracts, routing, workflows            # noqa: F401 — imported for the asserts
        return registry.load()

    # --- unreachable by routing ------------------------------------------------------------
    def test_a_route_hidden_cap_is_never_a_router_candidate(self):
        """The shortlist bounds Router #1 absolutely: what it drops, the model cannot pick."""
        import routing
        caps = self._caps()
        self.assertTrue(any(c.name == "brainstorm" for c in caps),
                        "the brainstorm cap is not being discovered at all")
        # Both paths through _shortlist: the top-N cut (large catalogue, real signal) ...
        picked = routing._shortlist("should we weigh the options and think this through", caps)
        self.assertNotIn("brainstorm", [c.name for c in picked])
        # ... and the no-signal fallback, which returns the WHOLE catalogue and would otherwise
        # hand the router every hidden cap on any request it can't rank.
        self.assertNotIn("brainstorm", [c.name for c in routing._shortlist("", caps)])
        # The general fallbacks are still there — this filter must not have eaten them.
        self.assertIn("assistant", [c.name for c in picked])

    def test_it_is_still_discoverable_so_a_slash_pin_resolves(self):
        """Hidden from ROUTING, not from the registry — `/brainstorm` and the composer toggle
        both resolve the name against the trusted catalogue, never against the client."""
        caps = registry.apply_policy(self._caps(), {})
        c = next(c for c in caps if c.name == "brainstorm")
        self.assertTrue(c.enabled)
        self.assertTrue(c.route_hidden)

    def test_its_risk_is_pinned_not_inferred_from_its_prose(self):
        """`apply_policy` OVERWRITES cap.risk with `classify(name, description)`, so the risk set
        in `_brainstorm()` never survives. Read risk is what keeps the mode out of the approval
        gate; leaving it to a keyword heuristic means an edit to the description ("weighs
        options", "pushes back") can park a conversation behind an approval card."""
        self.assertEqual(registry.classify("brainstorm", "anything at all, any words"), "read")
        self.assertEqual(
            next(c for c in registry.apply_policy(self._caps(), {}) if c.name == "brainstorm").risk,
            "read")

    # --- the contracts ---------------------------------------------------------------------
    def test_the_mode_picks_the_thinking_partner_contract(self):
        import contracts
        self.assertIs(contracts._output_contract(contracts.BRAINSTORM_AUDIENCE),
                      contracts._THINKING_PARTNER_FORMAT)
        # ...and nothing else moved.
        self.assertIs(contracts._output_contract(None), contracts._REPORT_FORMAT)
        self.assertIs(contracts._output_contract(contracts.CONVERSATION_AUDIENCE),
                      contracts._DIRECT_REPLY_FORMAT)

    def test_the_contract_forbids_the_report_shape_it_exists_to_replace(self):
        import contracts
        t = contracts._THINKING_PARTNER_FORMAT
        self.assertIn("**TLDR**", t)                     # named, in order to be forbidden
        self.assertIn("NO report shape", t)
        self.assertIn("ENDING ON A QUESTION IS A COMPLETE ANSWER", t)
        # The one thing it must NOT relax: a short answer is still not a licence to guess.
        self.assertIn("primary source", t)

    def test_the_resume_contract_does_not_re_impose_the_tldr(self):
        """The load-bearing one. `_RESUME_CONTRACT` folds in `_TLDR_SHAPE`, and it is prepended to
        EVERY resumed turn — so turn 2 of a brainstorm would carry "open with **TLDR** —" and
        "no **TLDR** line" in the same system prompt. Turn 1 reads fine and turn 2 reverts, which
        is the hardest version of this bug to notice."""
        import contracts
        self.assertNotIn("**TLDR**", contracts._resume_contract(contracts.BRAINSTORM_AUDIENCE))
        self.assertIn("**TLDR**", contracts._resume_contract(None))
        # Both variants keep the clause that is true of every resume: it is one-shot.
        for a in (None, contracts.BRAINSTORM_AUDIENCE):
            self.assertIn("report back when done", contracts._resume_contract(a))

    def test_engine_picks_the_resume_contract_by_audience(self):
        """`_resume_contract` computed and then not used is the classic shape here."""
        src = _read("engine.py")
        self.assertIn("_resume_contract(audience)", src)
        self.assertNotIn("\n            _RESUME_CONTRACT,", src,
                         "the resume path still hard-codes the report-shaped contract")

    # --- the audience decision -------------------------------------------------------------
    def test_the_capability_outranks_the_delivery_target(self):
        """The mode is a property of what was ASKED FOR, not of where the answer lands — a
        pinned /brainstorm in a Slack thread is still a brainstorm."""
        import contracts, workflows
        slack = {"kind": "slack_thread", "channel": "C1", "thread_ts": "1.1"}
        bs = {"name": "brainstorm", "kind": "custom", "risk": "read"}
        self.assertEqual(workflows._audience_of({"cap": bs}, slack), contracts.BRAINSTORM_AUDIENCE)
        # A web chat has NO reply_to at all — the case the whole mode exists for.
        self.assertEqual(workflows._audience_of({"cap": bs}, None), contracts.BRAINSTORM_AUDIENCE)
        # Everything else is unchanged: the target still decides.
        self.assertEqual(workflows._audience_of({"cap": {"name": "assistant"}}, slack),
                         contracts.CONVERSATION_AUDIENCE)
        self.assertIsNone(workflows._audience_of({"cap": {"name": "assistant"}}, None))
        self.assertIsNone(workflows._audience_of({}, None))

    # --- an unjudged run is not a failed one -----------------------------------------------
    def test_an_unjudged_run_reports_verified_as_none_not_false(self):
        import workflows
        self.assertIsNone(workflows._verified_of(None))
        self.assertTrue(workflows._verified_of({"passed": True}))
        self.assertFalse(workflows._verified_of({"passed": False}))

    def test_needs_human_is_gated_on_a_verdict_existing(self):
        """`passed` is False for a brainstorm (there is no verdict), so without the `verdict is
        not None` guard the deferred verify_exhausted branch fires on the mode's very first reply
        and the chat renders Blocked."""
        src = _read("workflows.py")
        self.assertIn("if verdict is not None and not passed and not self._needs_human:", src)

    # --- the pipeline skips ----------------------------------------------------------------
    def test_the_ladder_short_circuits_before_any_judge(self):
        """One attempt, no verify_capability, no retry — asserted on the AST rather than the
        prose so a judge sneaking back into the brainstorm path is caught."""
        tree = ast.parse(_read("workflows.py"))
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "OttoWorkflow")
        turn = next(m for m in cls.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and m.name == "_brainstorm_turn")
        called = {n.args[0].id for n in ast.walk(turn)
                  if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "execute_activity"
                  and n.args and isinstance(n.args[0], ast.Name)}
        self.assertEqual(called, {"run_capability", "record_attempt"},
                         "the brainstorm turn gained an activity — a judge here defeats the mode")
        # And the ladder itself hands off before spending a rung.
        ladder = next(m for m in cls.body
                      if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and m.name == "_verify_ladder")
        src = ast.get_source_segment(_read("workflows.py"), ladder)
        self.assertLess(src.index("_brainstorm_turn"), src.index("verify_capability"),
                        "the brainstorm hand-off must come before the ladder does any work")

    def test_the_supervisor_is_disarmed_on_the_only_attempt(self):
        """`supervise_enforce` is documented as armed only while a rung remains for its critique
        to steer. There is none here — a kill would discard the only attempt and leave the user's
        message unanswered."""
        tree = ast.parse(_read("workflows.py"))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call)
                    and getattr(n.func, "attr", None) == "execute_activity"
                    and n.args and getattr(n.args[0], "id", None) == "run_capability"):
                continue
            d = next((a for a in n.args[1:] if isinstance(a, ast.Dict)), None)
            keys = {k.value: v for k, v in zip(d.keys, d.values) if isinstance(k, ast.Constant)}
            if "supervise_enforce" in keys and isinstance(keys["supervise_enforce"], ast.Constant):
                self.assertIs(keys["supervise_enforce"].value, False)
                return
        self.fail("no run_capability call sets supervise_enforce to a literal False")

    def test_clarify_and_the_write_intent_guard_are_both_skipped(self):
        """Clarify is a second LLM round-trip that blocks the first turn on a modal prompt — the
        mode answers its own questions in-band. The write-intent guard is worse than redundant:
        `intent["redirect"]` is skipped for a PINNED cap, so a write-shaped musing would fall
        through to the risk bump and park the conversation behind an approval card."""
        src = _read("workflows.py")
        self.assertIn('if not subtask and not _is_brainstorm(cap) and '
                      '(not unattended or params.get("clarify")):', src)
        self.assertIn('if cap["risk"] == "read" and not unattended and not _is_brainstorm(cap):',
                      src)

    def test_a_follow_up_never_re_classifies_out_of_the_mode(self):
        """A bound session has no redirect available, so an up-classify on turn 4 ("so we'd just
        delete the QA loop then?") would gate the CONVERSATION for work nobody asked to start."""
        src = _read("workflows.py")
        i = src.index("downgradeable = cap[\"risk\"] == \"write\" and approval != \"auto\"")
        j = src.index("classify_followup", i)
        self.assertIn("_is_brainstorm(cap)", src[i:j],
                      "the resume path re-classifies a brainstorm follow-up")

    # --- mutually exclusive Run-mode controls ----------------------------------------------
    def test_brainstorm_never_engages_repo_mode(self):
        """Measured before the fix: a pinned brainstorm with a repo picked reached PLAN_PREVIEW
        and never ran the turn — an explicit repo forces `cap.risk` to write in `_route_or_resume`,
        so a read-only conversation bought a plan preview, an approval card, a clone and a PR."""
        import workflows
        bs = {"name": "brainstorm", "kind": "custom", "risk": "read"}
        self.assertIsNone(workflows._repo_of({"cap": bs, "repo": "otto"}))
        # Every other run is untouched — this must not have become a general repo kill switch.
        self.assertEqual(workflows._repo_of({"cap": {"name": "worker"}, "repo": "otto"}), "otto")
        self.assertIsNone(workflows._repo_of({"cap": {"name": "worker"}}))

    def test_brainstorm_never_decomposes_into_steps(self):
        """Plan-then-execute WINS over the ladder outright (`if plan_list:`), so with both
        toggles on the brainstorm turn never ran at all — measured: the musing was replaced by a
        decomposition into atomic steps, each with its own verify ladder."""
        import workflows
        bs = {"name": "brainstorm", "kind": "custom", "risk": "read"}
        other = {"name": "worker", "kind": "custom", "risk": "write"}
        self.assertFalse(workflows._may_plan_steps(False, "on", None, False, bs))
        # The control: identical arguments, ordinary cap — otherwise this passes for any reason.
        self.assertTrue(workflows._may_plan_steps(False, "on", None, False, other))
        # And the three pre-existing exclusions still hold.
        self.assertFalse(workflows._may_plan_steps(True, "on", None, False, other))   # runbook
        self.assertFalse(workflows._may_plan_steps(False, "off", None, False, other))
        self.assertFalse(workflows._may_plan_steps(False, "on", "otto", False, other))  # repo
        self.assertFalse(workflows._may_plan_steps(False, "on", None, True, other))   # sub-task

    def test_the_composer_disables_the_losing_control_rather_than_ignoring_it(self):
        """The workflow resolves every conflict on its own, so the UI is not a safety control —
        it is there because a toggle that silently does nothing is the bug being fixed."""
        html = _read("web/index.html")
        self.assertIn("function applyModeExclusions(){", html)
        # Wired to all three controls, not just the one that changed last.
        self.assertIn('if(["repopick","bscheck","plancheck"].includes(e.target.id))', html)
        # ...and re-applied when the session binding changes the brainstorm lock.
        i = html.index("function applyBrainstorm(){")
        self.assertIn("applyModeExclusions();", html[i:i + 900])

    # --- the composer toggle ---------------------------------------------------------------
    def test_the_toggle_is_expressed_as_a_pin_and_an_explicit_slash_wins(self):
        html = _read("web/index.html")
        self.assertIn('id="bscheck"', html)
        # The fallback must be guarded on `!pin`, i.e. a typed "/assistant …" outranks a toggle
        # the user set three messages ago.
        self.assertIn("if(!pin && selectedBrainstorm()){", html)
        i = html.index("const slash=parseSlash(text);")
        self.assertLess(i, html.index("if(!pin && selectedBrainstorm()){"),
                        "the toggle must be applied AFTER the slash pin is resolved")
        # `const pin` would make the fallback a TypeError at runtime and the mode dead on arrival.
        self.assertIn("let pin=slash?slash.cap:null", html)
        # A bound session locks it, rather than leaving a live checkbox that does nothing.
        self.assertIn("applyBrainstorm();", html)
        self.assertIn("c.disabled=true;", html)
