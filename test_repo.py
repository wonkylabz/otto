"""Otto unit tests — workspaces, PRs, review/QA and terminal state.

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

from test_support import setUpModule  # noqa: F401 - unittest calls it per module
from test_support import (_Cap, _FAKE_MCP_SERVER, _cap_stub, _fake_embed, _patched_registry_dirs, _storage_hammer)  # noqa: F401


class PrCopyTests(unittest.TestCase):
    """PR title/body for repo-mode's draft PR (engine.pr_copy): a drafted conventional title
    with the raw request as a fallback that must never block the PR (PR #199's title was the
    verbatim request, composer chatter included)."""

    def _stub(self, reply=None, raises=False):
        import gateway
        orig = gateway.complete

        def fake(task, prompt):
            if raises:
                raise OSError("tier down")
            return reply
        gateway.complete = fake
        self.addCleanup(setattr, gateway, "complete", orig)

    def test_drafted_title_and_result_body(self):
        self._stub("Pin temporalio SDK and Temporal CLI versions")
        out = engine.pr_copy("Pick a good candidate to work on. Use the otto-dev local repo",
                             summary="Pinned temporalio==1.30.0 and the CLI to 1.8.0.")
        self.assertEqual(out["title"], "Pin temporalio SDK and Temporal CLI versions")
        self.assertIn("Pinned temporalio==1.30.0", out["body"])
        self.assertIn("_Automated by Otto for request:_", out["body"])

    def test_a_supervisor_abort_summary_never_becomes_the_pr_description(self):
        # The result of a run whose LAST attempt was killed is just the abort sentinel — using
        # it to draft the title/body presents the failure as the change itself (PR #68's title
        # became "Check platform#358 merge status before implementation", the abort reason
        # verbatim, even though the branch held real, unrelated committed work).
        req = "Convert infra alert deploys to one-click cicd:deploy builds"
        self._stub("this reply must never be reached")
        seen_prompts = []
        import gateway
        orig = gateway.complete
        def fake(task, prompt):
            seen_prompts.append(prompt)
            return req
        gateway.complete = fake
        self.addCleanup(setattr, gateway, "complete", orig)
        out = engine.pr_copy(req, summary="(aborted by supervisor: check platform#358 first)")
        self.assertNotIn("What the change did:", seen_prompts[0])
        self.assertNotIn("aborted by supervisor", out["body"])
        self.assertIn("_Automated by Otto for request:_", out["body"])

    def test_garbage_or_error_falls_back_to_request(self):
        req = "Pick a good candidate to work on from the otto issues"
        self._stub("ok")                                   # too short to be a title
        self.assertEqual(engine.pr_copy(req)["title"], req[:120])
        self._stub(raises=True)                            # gateway down -> never blocks the PR
        self.assertEqual(engine.pr_copy(req)["title"], req[:120])

    def test_parse_strips_reasoning_and_quotes(self):
        t = engine._parse_pr_title("<think>hmm a title</think>\n\"Fix the flaky retry logic\"",
                                   "fallback title")
        self.assertEqual(t, "Fix the flaky retry logic")


class ProfileTests(unittest.TestCase):
    """Portable profile export/import (portability): secret-free by construction,
    non-clobbering on import — a tuned install is never overwritten."""

    def setUp(self):
        import gateway
        import knowledge
        import policy as pol_mod
        import workspace
        self.gateway, self.knowledge, self.policy, self.workspace = \
            gateway, knowledge, pol_mod, workspace
        self._saved = (pol_mod._PATH, pol_mod._CUSTOM, pol_mod._MCPDEF, gateway._PATH,
                       engine._DB, knowledge._DB, registry.PROJECTS_FILE,
                       workspace.git_repos)
        self.repos = []
        workspace.git_repos = lambda: self.repos

    def tearDown(self):
        (self.policy._PATH, self.policy._CUSTOM, self.policy._MCPDEF, self.gateway._PATH,
         engine._DB, self.knowledge._DB, registry.PROJECTS_FILE,
         self.workspace.git_repos) = self._saved

    def _machine(self):
        """Point every store at a fresh tmpdir — one simulated machine."""
        tmp = tempfile.mkdtemp(prefix="otto-profile-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.policy._PATH = os.path.join(tmp, "policy.json")
        self.policy._CUSTOM = os.path.join(tmp, "capabilities.json")
        self.policy._MCPDEF = os.path.join(tmp, "mcp-servers.json")
        self.gateway._PATH = os.path.join(tmp, "models.json")
        engine._DB = os.path.join(tmp, "otto.db")
        self.knowledge._DB = os.path.join(tmp, "otto.db")
        registry.PROJECTS_FILE = os.path.join(tmp, "projects.json")
        return tmp

    def _seed_source(self):
        self._machine()
        self.policy.save({"capabilities": {"sre-minion": {"enabled": False}}})
        cfg = self.gateway.load()
        cfg["pool"].append({"name": "qwen", "provider": "openai",
                            "base_url": "http://gpu:8000/v1",
                            "api_key_env": "sk-PASTED-LITERAL-KEY"})   # the 401 footgun field
        cfg["assign"]["routing"] = "qwen"
        cfg["cap_exec"]["qa-tester"] = cfg["pool"][0]["name"]
        self.gateway.save(cfg)
        engine.add_behavior("always run the tests before opening a PR", "global")
        self.knowledge.add_document("runbook", "restart the frobnicator gently", source="paste")
        self.repos = [{"name": "api", "path": "/src/api", "origin": "git@github.com:o/api.git"}]
        registry.set_project_instructions("/src/api", "use tabs")
        return self.policy.export_profile()

    def test_export_strips_pasted_api_keys_and_carries_origins(self):
        prof = self._seed_source()
        qwen = next(m for m in prof["models"]["pool"] if m["name"] == "qwen")
        self.assertEqual(qwen["api_key_env"], "")               # literal key never exported
        self.assertIn("qwen", prof["models"]["needs_key"])
        self.assertEqual(prof["projects"][0]["origin"], "git@github.com:o/api.git")
        self.assertNotIn("path", prof["projects"][0])           # machine-local paths stay home
        self.assertEqual(prof["knowledge"]["docs"][0]["title"], "runbook")

    def test_import_into_fresh_machine_applies_everything(self):
        prof = self._seed_source()
        self._machine()                                         # fresh target: empty stores
        self.repos = [{"name": "api", "path": "/other/api", "origin": "git@github.com:o/api.git"}]
        s = self.policy.import_profile(prof)
        self.assertIn("sre-minion", s["policy"]["added"])
        self.assertEqual(s["models"]["pool_added"], ["qwen"])
        self.assertTrue(s["models"]["assignments_applied"])     # no models.json existed -> fresh
        self.assertEqual(self.gateway.load()["assign"]["routing"], "qwen")
        self.assertEqual(s["behaviors"]["added"], 1)
        self.assertEqual(s["knowledge"]["added"], ["runbook"])
        self.assertEqual(s["projects"]["matched"], ["api"])     # matched by ORIGIN, not path
        self.assertEqual(registry.project_meta("/other/api")["instructions"], "use tabs")

    def test_import_never_clobbers_a_tuned_install(self):
        prof = self._seed_source()
        self._machine()
        self.repos = []
        self.policy.save({"capabilities": {"sre-minion": {"enabled": True}}})   # local choice
        cfg = self.gateway.load()
        self.gateway.save(cfg)                                  # models.json now EXISTS -> tuned
        self.knowledge.add_document("runbook", "the local version", source="paste")
        s = self.policy.import_profile(prof)
        self.assertIn("sre-minion", s["policy"]["skipped"])
        pol = self.policy.load()
        self.assertTrue(pol["capabilities"]["sre-minion"]["enabled"])   # local override kept
        self.assertFalse(s["models"]["assignments_applied"])            # assignments untouched
        self.assertNotEqual(self.gateway.load()["assign"].get("routing"), "qwen")
        self.assertEqual(s["knowledge"]["skipped"], ["runbook"])
        self.assertEqual(s["projects"]["unmatched"],
                         [{"name": "api", "origin": "git@github.com:o/api.git"}])

    def test_import_rejects_a_non_profile(self):
        self._machine()
        with self.assertRaises(ValueError):
            self.policy.import_profile({"something": "else"})


class RepoScopeNoteTests(unittest.TestCase):
    """A repo-mode run's only anchor to the correct repo is its cwd, and a weak/local model
    can `cd` itself into an unrelated repo it recognizes instead of trusting that cwd
    (observed: a local-model worker run on 'chordelia' wandered into a different registered
    repo mid-task). The note states the repo + cwd explicitly so the model has nothing to
    infer or second-guess."""

    def test_states_repo_and_cwd(self):
        note = engine._repo_scope_note("chordelia", "/tmp/workspaces/w1")
        self.assertIn("chordelia", note)
        self.assertIn("/tmp/workspaces/w1", note)

    def test_none_without_repo(self):
        self.assertIsNone(engine._repo_scope_note(None, "/tmp/workspaces/w1"))

    def test_none_without_cwd(self):
        self.assertIsNone(engine._repo_scope_note("chordelia", None))


class PrBodyContractTests(unittest.TestCase):
    """A PR description is written by the CAPABILITY (its own instructions may run `gh pr
    create`) and re-edited by every post-PR fix round, so `intents.pr_copy`'s bounded body is
    not the only writer and nothing told any of them what a description is for. Measured on
    webapp#565: a 2.1k-char body reached ~9k over three review-fix rounds, each appending a
    section about the ROUND ("Review findings addressed (`c3f46d1b`)", "Drive-by CI fix",
    "Other deviations from the issue") — a reviewer opening the diff got a changelog of Otto's
    own attempts. Three places had to change together: the note, its wiring, and the two texts
    that ASK for the append."""

    def test_the_rule_names_what_a_description_is_not(self):
        note = engine._pr_body_note("webapp", "/tmp/workspaces/w1")
        low = note.lower()
        self.assertIn("describes the change as it stands", low)
        # The three shapes actually observed on #565, each forbidden by name.
        self.assertIn("commit-by-commit", low)
        self.assertIn("review findings addressed", low)
        self.assertIn("methodology", low)
        # Editing in place is the sanctioned move; appending is the forbidden one.
        self.assertIn("in place", low)
        self.assertIn("never append", low)

    def test_only_repo_mode_gets_it(self):
        # A run with no provisioned clone opens no PR — same gate as _repo_scope_note.
        self.assertIsNone(engine._pr_body_note(None, "/tmp/workspaces/w1"))
        self.assertIsNone(engine._pr_body_note("webapp", None))

    def test_a_repo_mode_run_actually_CARRIES_it(self):
        # The load-bearing half: a note nobody injects is dead text. Every fix round runs
        # through this same seam (run_capability -> run_attempt with cwd+repo), so wiring it
        # here is what reaches rounds 1..N as well as the first attempt.
        seen = {}
        cap = registry.Capability("agent", "sre-minion", "implements a ticket")
        cap.risk = "write"
        orig = engine._claude
        try:
            def fake(prompt, **kw):
                seen["sys"] = kw.get("system_context") or ""
                return {"result": "ok", "cost": 0, "session_id": "s"}
            engine._claude = fake
            engine.run_attempt("move the docs", cap, repo="webapp", cwd="/tmp/workspaces/w1",
                               recall=False, wid="wf-prbody")
        finally:
            engine._claude = orig
        self.assertIn("PULL-REQUEST DESCRIPTION", seen.get("sys", ""))

    def test_a_non_repo_run_does_not(self):
        seen = {}
        cap = registry.Capability("agent", "sre-minion", "implements a ticket")
        cap.risk = "write"
        orig = engine._claude
        try:
            def fake(prompt, **kw):
                seen["sys"] = kw.get("system_context") or ""
                return {"result": "ok", "cost": 0, "session_id": "s"}
            engine._claude = fake
            engine.run_attempt("what does this do?", cap, recall=False, wid="wf-noprbody")
        finally:
            engine._claude = orig
        self.assertNotIn("PULL-REQUEST DESCRIPTION", seen.get("sys", ""))

    def test_both_fix_rounds_counter_the_finding_they_are_handed(self):
        # The critique is concatenated with model-written findings that have been observed
        # asking for exactly this ("update the PR description (`gh pr edit 565`) (append a
        # section, don't wipe the existing description)"). A note in the system context is not
        # enough on its own — the immediate instruction has to contradict it.
        import workflows
        for fn in (workflows.OttoWorkflow._run_qa_loop,
                   workflows.OttoWorkflow._run_review_loop):
            src = inspect.getsource(fn)
            self.assertIn("do NOT open a new PR", src)
            self.assertIn("append", src, f"{fn.__name__} does not forbid appending")
            self.assertIn("PR description", src, f"{fn.__name__} does not name the description")

    def test_the_reviewer_may_not_raise_the_finding_at_all(self):
        # Upstream of the fix round: judge_review folds the reviewer's findings VERBATIM into
        # the next round, so a "please extend the PR description" finding becomes an order.
        req = engine.review_request("https://github.com/o/r/pull/1", "r", "do the thing").lower()
        self.assertIn("pr description is not a deliverable", req)
        self.assertIn("never raise a finding", req)
        self.assertIn("extended", req)


class RepoSourceNoteTests(unittest.TestCase):
    """The non-repo-mode counterpart: a READ run has Bash but no cwd anchor, so it finds a repo by
    listing a parent directory. On 2026-07-30 that picked `~/repositories/infra3` — a scratch clone
    six days stale — and answered that prod-a vLLM does not exist. The note names the canonical
    checkouts and forbids concluding absence from a local tree."""

    def setUp(self):
        self._orig = engine.registry.projects
        engine.registry.projects = lambda: ["/home/u/repositories/infra", "/home/u/repositories/webapp"]

    def tearDown(self):
        engine.registry.projects = self._orig

    def test_lists_the_registered_checkouts(self):
        note = engine._repo_source_note()
        self.assertIn("/home/u/repositories/infra", note)
        self.assertIn("/home/u/repositories/webapp", note)
        self.assertIn("origin/HEAD", note)               # and how to check the real default branch

    def test_names_no_repo_it_was_not_given(self):
        """The sibling-clone trap is described by SHAPE, not by example: the ONLY repo names in the
        note come from `registry.projects()`. A hardcoded `infra2`/`infra3` was both wrong for
        anyone else's checkouts and a code change away from every new repo."""
        note = engine._repo_source_note()
        for stray in ("infra2", "infra3", "chordelia", "webapp2"):
            self.assertNotIn(stray, note)
        engine.registry.projects = lambda: ["/srv/code/widget"]
        self.assertIn("/srv/code/widget", engine._repo_source_note())
        self.assertNotIn("infra", engine._repo_source_note())

    def test_silent_in_repo_mode(self):
        """`_repo_scope_note` already pins a repo-mode run to ONE clone — two notes about which
        repo to read would contradict each other."""
        self.assertIsNone(engine._repo_source_note("infra", "/tmp/workspaces/w1"))
        self.assertIsNotNone(engine._repo_source_note("infra", None))

    def test_none_without_registered_projects(self):
        engine.registry.projects = lambda: []
        self.assertIsNone(engine._repo_source_note())


class ProjectCapabilityTests(unittest.TestCase):
    """Importing skills/agents from an external repo's `.claude/` dir (Option B)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "aws-cost-report")
        sk = os.path.join(self.repo, ".claude", "skills", "aws-cost-report")
        ag = os.path.join(self.repo, ".claude", "agents")
        os.makedirs(sk); os.makedirs(ag)
        with open(os.path.join(sk, "SKILL.md"), "w") as f:
            f.write("---\nname: aws-cost-report\ndescription: report on costs\n---\nbody")
        with open(os.path.join(ag, "finops.md"), "w") as f:
            f.write("---\nname: finops\ndescription: investigate cost anomalies\n---\nbody")
        with open(os.path.join(self.repo, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"aws-cost-explorer": {"command": "uvx", "args": ["x"]}}}, f)
        self._orig = registry.PROJECTS_FILE
        registry.PROJECTS_FILE = os.path.join(self.tmp.name, "projects.json")
        registry.save_projects([self.repo])

    def tearDown(self):
        registry.PROJECTS_FILE = self._orig
        self.tmp.cleanup()

    def test_discovery_namespaces_and_tags_cwd(self):
        caps = {c.name: c for c in registry.load() if c.source == "project"}
        self.assertIn("aws-cost-report:aws-cost-report", caps)
        self.assertIn("aws-cost-report:finops", caps)
        skill = caps["aws-cost-report:aws-cost-report"]
        self.assertEqual(skill.invoke_name, "aws-cost-report")
        self.assertEqual(skill.cwd, self.repo)
        self.assertEqual(skill.mcp_config, os.path.join(self.repo, ".mcp.json"))

    def test_invocation_uses_bare_name(self):
        skill = next(c for c in registry.load() if c.name == "aws-cost-report:aws-cost-report")
        self.assertEqual(engine._invocation(skill, "go"), "/aws-cost-report go")
        agent = next(c for c in registry.load() if c.name == "aws-cost-report:finops")
        self.assertIn("finops subagent", engine._invocation(agent, "go"))

    def test_effective_mcp_merges_repo_servers(self):
        skill = next(c for c in registry.load() if c.name == "aws-cost-report:aws-cost-report")
        path, tools = engine._effective_mcp(skill, None)
        self.assertEqual(tools, ["mcp__aws-cost-explorer"])
        with open(path) as f:
            self.assertIn("aws-cost-explorer", json.load(f)["mcpServers"])

    def test_effective_mcp_noop_for_plain_cap(self):
        plain = registry.Capability("skill", "board-status", "desc")
        self.assertEqual(engine._effective_mcp(plain, None), (None, []))

    def test_add_remove_project_roundtrip(self):
        registry.save_projects([])
        p = registry.add_project(self.repo)
        self.assertEqual(p, self.repo)
        self.assertIn(self.repo, registry.projects())
        registry.remove_project(self.repo)
        self.assertNotIn(self.repo, registry.projects())


class WorkspaceTests(unittest.TestCase):
    """Isolated repo workspaces (issue #57) — pure helpers + the allowlist guard."""

    def setUp(self):
        import workspace
        self.ws = workspace
        self._orig = workspace.registry.PROJECTS_FILE

    def tearDown(self):
        self.ws.registry.PROJECTS_FILE = self._orig

    def test_branch_name_is_ref_safe(self):
        self.assertEqual(self.ws.branch_name("web-abc/123"), "otto/web-abc-123")
        self.assertEqual(self.ws.branch_name(""), "otto/run")

    def test_is_github_only_true_for_github_remotes(self):
        self.assertTrue(self.ws._is_github("git@github.com:o/r.git"))
        self.assertTrue(self.ws._is_github("https://github.com/o/r"))
        self.assertFalse(self.ws._is_github("/local/path"))
        self.assertFalse(self.ws._is_github(""))

    def test_resolve_only_returns_allowlisted_git_repos(self):
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        repo = os.path.join(tmp, "myrepo")
        os.makedirs(os.path.join(repo, ".git"))         # a git working tree (no real history needed)
        not_git = os.path.join(tmp, "plain")
        os.makedirs(not_git)
        self.ws.registry.PROJECTS_FILE = os.path.join(tmp, "projects.json")
        self.ws.registry.save_projects([repo, not_git])
        self.assertIsNotNone(self.ws.resolve("myrepo"))           # by name
        self.assertIsNotNone(self.ws.resolve(repo))               # by absolute path
        self.assertIsNone(self.ws.resolve("plain"))               # registered but not a git repo
        self.assertIsNone(self.ws.resolve("not-registered"))      # not allowlisted at all
        self.assertIsNone(self.ws.resolve(""))

    # --- approved-plan PR comment ------------------------------------------------------------
    def test_plan_comment_body_records_the_plan_as_a_point_in_time_approval(self):
        body = self.ws.plan_comment_body("web-abc", "1. bump the chart\n2. apply",
                                 request="bump api-service", cap="general worker", when="2026-08-14")
        self.assertIn("<!-- otto-plan:web-abc -->", body)   # own-artifact marker, delivery convention
        self.assertIn("1. bump the chart", body)
        self.assertIn("bump api-service", body)
        self.assertIn("general worker", body)
        # It describes what was APPROVED, not what the diff became — a reviewer must not read it
        # as a changelog of the PR it sits in.
        self.assertIn("before execution", body)
        self.assertIn("not maintained after the run", body)

    def _fake_gh(self, existing="", comment_rc=0):
        """Stand in for `_run`, recording argv. `existing` is what `gh pr view` reports as the
        PR's current comment bodies."""
        calls = []

        def run(args, cwd=None, timeout=600):
            calls.append(args)
            if args[:3] == ["gh", "pr", "view"]:
                return 0, existing, ""
            if args[:3] == ["gh", "pr", "comment"]:
                return comment_rc, "", "" if comment_rc == 0 else "gh: bad credentials"
            return 0, "", ""
        return run, calls

    def test_post_plan_comments_the_plan_and_writes_nothing_to_the_clone(self):
        tmp = tempfile.mkdtemp(prefix="otto-spec-")
        run, calls = self._fake_gh()
        orig, self.ws._run = self.ws._run, run
        try:
            self.assertTrue(self.ws.post_plan(tmp, "https://github.com/o/r/pull/9", "r1",
                                              "1. a real plan", request="do it"))
        finally:
            self.ws._run = orig
        posted = [c for c in calls if c[:3] == ["gh", "pr", "comment"]]
        self.assertEqual(1, len(posted))
        self.assertIn("1. a real plan", posted[0][-1])
        self.assertIn("<!-- otto-plan:r1 -->", posted[0][-1])
        self.assertEqual([], os.listdir(tmp))    # the target repo's tree is never touched

    def test_post_plan_declines_an_empty_plan_no_pr_and_a_disabled_flag(self):
        tmp = tempfile.mkdtemp(prefix="otto-spec-")
        run, calls = self._fake_gh()
        orig, self.ws._run = self.ws._run, run
        pr = "https://github.com/o/r/pull/9"
        try:
            self.assertFalse(self.ws.post_plan(tmp, pr, "r1", ""))
            self.assertFalse(self.ws.post_plan(tmp, pr, "r1", "   \n "))
            self.assertFalse(self.ws.post_plan(tmp, pr, "r1", None))
            self.assertFalse(self.ws.post_plan(tmp, None, "r1", "1. a real plan"))  # no PR opened
            self.ws.PLAN_COMMENT = False
            self.assertFalse(self.ws.post_plan(tmp, pr, "r1", "1. a real plan"))
        finally:
            self.ws._run, self.ws.PLAN_COMMENT = orig, True
        self.assertEqual([], calls)              # nothing even asks GitHub

    def test_post_plan_is_idempotent_across_qa_and_review_rounds(self):
        """`finalize` runs again for every QA/review fix round against the same PR — without the
        marker check each round posts the same plan again into a human's notifications."""
        tmp = tempfile.mkdtemp(prefix="otto-spec-")
        body = self.ws.plan_comment_body("r1", "1. a real plan")
        run, calls = self._fake_gh(existing=body)
        orig, self.ws._run = self.ws._run, run
        try:
            self.assertFalse(self.ws.post_plan(tmp, "https://github.com/o/r/pull/9", "r1",
                                               "1. a real plan"))
        finally:
            self.ws._run = orig
        self.assertEqual([], [c for c in calls if c[:3] == ["gh", "pr", "comment"]])

    def test_post_plan_survives_a_failing_gh(self):
        """The work is already pushed by the time the plan is posted — a broken `gh` must cost
        the courtesy comment, never the run."""
        tmp = tempfile.mkdtemp(prefix="otto-spec-")
        run, _ = self._fake_gh(comment_rc=1)
        orig, self.ws._run = self.ws._run, run
        try:
            self.assertFalse(self.ws.post_plan(tmp, "https://github.com/o/r/pull/9", "r1",
                                               "1. a real plan"))
        finally:
            self.ws._run = orig

    def test_post_plan_does_not_repost_when_gh_cannot_say(self):
        """An unreadable comment list is ambiguous. Posting would duplicate on every later
        round, so ambiguity fails toward silence."""
        tmp = tempfile.mkdtemp(prefix="otto-spec-")
        calls = []

        def run(args, cwd=None, timeout=600):
            calls.append(args)
            return 1, "", "gh: could not read comments"
        orig, self.ws._run = self.ws._run, run
        try:
            self.assertFalse(self.ws.post_plan(tmp, "https://github.com/o/r/pull/9", "r1",
                                               "1. a real plan"))
        finally:
            self.ws._run = orig
        self.assertEqual([], [c for c in calls if c[:3] == ["gh", "pr", "comment"]])

    # --- clone base + agent-managed git (repo-mode) -----------------------------------------
    def _git_repo(self, tmp):
        """A real git repo on `main` with a `feature` branch that carries extra work."""
        repo = os.path.join(tmp, "src")
        os.makedirs(repo)
        def g(*a):
            subprocess.run(["git", "-C", repo, *a], check=True,
                           capture_output=True, text=True)
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                       capture_output=True, text=True)
        g("config", "user.email", "t@t"); g("config", "user.name", "t")
        with open(os.path.join(repo, "base.txt"), "w") as f:
            f.write("base\n")
        g("add", "-A"); g("commit", "-qm", "base")
        g("checkout", "-qb", "feature")
        with open(os.path.join(repo, "feature.txt"), "w") as f:
            f.write("wip\n")
        g("add", "-A"); g("commit", "-qm", "feature work")
        return repo

    def _register(self, tmp, repo):
        self.ws.registry.PROJECTS_FILE = os.path.join(tmp, "projects.json")
        self.ws.registry.save_projects([repo])

    def test_valid_branch_accepts_ordinary_names(self):
        self.assertTrue(self.ws.valid_branch("otto/web-orig1"))
        self.assertTrue(self.ws.valid_branch("claude-md-secrets-convention"))
        self.assertTrue(self.ws.valid_branch("22"))

    def test_valid_branch_rejects_flag_smuggling_and_unsafe_refs(self):
        # `branch` can arrive from client-supplied chat state (/api/continue's git_branch) —
        # must never be mistakable for a git option or an unsafe ref.
        self.assertFalse(self.ws.valid_branch("--upload-pack=evil"))
        self.assertFalse(self.ws.valid_branch("-x"))
        self.assertFalse(self.ws.valid_branch("foo..bar"))
        self.assertFalse(self.ws.valid_branch("foo.lock"))
        self.assertFalse(self.ws.valid_branch(""))
        self.assertFalse(self.ws.valid_branch(None))

    def test_default_branch_ignores_current_checkout(self):
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = self._git_repo(tmp)              # left checked out on `feature`
        self.assertEqual(self.ws._current_branch(repo), "feature")
        self.assertEqual(self.ws._default_branch(repo), "main")

    def test_provision_clones_default_branch_not_current(self):
        # The live checkout being parked on a feature branch must NOT leak into the isolated
        # clone — otherwise the workspace inherits unrelated work as its base (the bug that made
        # a real run report "no changes to push" while a PR already existed).
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = self._git_repo(tmp)
        self._register(tmp, repo)
        orig_ws = self.ws.WORKSPACES
        self.ws.WORKSPACES = os.path.join(tmp, "workspaces")
        self.addCleanup(setattr, self.ws, "WORKSPACES", orig_ws)
        info = self.ws.provision("src", "wf-test-1")
        self.assertEqual(info["branch"], "otto/wf-test-1")
        self.assertTrue(os.path.isfile(os.path.join(info["path"], "base.txt")))
        # feature branch's work must be ABSENT — the clone starts from the default branch
        self.assertFalse(os.path.isfile(os.path.join(info["path"], "feature.txt")))

    # --- staleness: a registered checkout is only as current as the user's last pull ------------
    def _git_repo_with_remote(self, tmp):
        """A registered checkout whose local `main` is BEHIND its remote — the real situation on
        the live install (`infra`'s local master was 8 commits behind origin/master).

        Returns (checkout, bare). `behind.txt` exists only on the remote's tip."""
        bare = os.path.join(tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True,
                       capture_output=True, text=True)

        def g(repo, *a):
            subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, text=True)

        seed = os.path.join(tmp, "seed")
        subprocess.run(["git", "clone", "-q", bare, seed], check=True, capture_output=True, text=True)
        g(seed, "config", "user.email", "t@t"); g(seed, "config", "user.name", "t")
        with open(os.path.join(seed, "base.txt"), "w") as f:
            f.write("base\n")
        g(seed, "add", "-A"); g(seed, "commit", "-qm", "base"); g(seed, "push", "-q", "origin", "main")

        checkout = os.path.join(tmp, "src")            # what gets registered in projects.json
        subprocess.run(["git", "clone", "-q", bare, checkout], check=True,
                       capture_output=True, text=True)

        # the remote moves on; `checkout` is now behind and doesn't know it
        with open(os.path.join(seed, "behind.txt"), "w") as f:
            f.write("landed after the checkout was cloned\n")
        g(seed, "add", "-A"); g(seed, "commit", "-qm", "later work")
        g(seed, "push", "-q", "origin", "main")
        return checkout, bare

    def test_refresh_repos_updates_remote_refs_without_touching_the_worktree(self):
        """`_repo_source_note` tells a run to trust `origin/HEAD` over the working tree — but in a
        stale clone `origin/HEAD` lies just as confidently, so the refs must be refreshed first.
        Critically this is a FETCH: the user's working tree and current branch are untouched."""
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        checkout, _ = self._git_repo_with_remote(tmp)
        self._register(tmp, checkout)

        def show(*a):
            return subprocess.run(["git", "-C", checkout, *a], capture_output=True,
                                  text=True).stdout.strip()
        stale = show("rev-parse", "origin/main")
        # a fresh clone has no FETCH_HEAD at all — "never fetched" counts as due
        self.assertIsNone(self.ws._last_fetch_age_s(checkout))
        with open(os.path.join(checkout, "scratch.txt"), "w") as f:
            f.write("uncommitted work the user left behind\n")

        self.assertEqual(self.ws.refresh_repos(), ["src"])
        self.assertNotEqual(show("rev-parse", "origin/main"), stale)          # refs moved forward
        self.assertEqual(show("rev-parse", "HEAD"), show("rev-parse", "main"))  # local branch didn't
        self.assertFalse(os.path.isfile(os.path.join(checkout, "behind.txt")))  # worktree untouched
        self.assertTrue(os.path.isfile(os.path.join(checkout, "scratch.txt")))  # ... and preserved

    def test_refresh_repos_skips_a_recently_fetched_checkout(self):
        """The window is what keeps this off the hot path: the common case must cost nothing."""
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        checkout, _ = self._git_repo_with_remote(tmp)
        self._register(tmp, checkout)
        open(os.path.join(checkout, ".git", "FETCH_HEAD"), "a").close()   # fetched just now
        self.assertEqual(self.ws.refresh_repos(), [])
        self.assertEqual(self.ws.refresh_repos(max_age_s=0), [])          # 0 disables entirely

    def test_refresh_repos_is_never_fatal(self):
        """Offline / auth-prompting / black-holed remote must degrade to the old staleness, not
        fail the run."""
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = os.path.join(tmp, "broken")
        os.makedirs(os.path.join(repo, ".git"))          # looks like a checkout, isn't a real one
        self._register(tmp, repo)
        self.assertEqual(self.ws.refresh_repos(), [])    # traced + skipped, no exception

    def test_provision_bases_the_clone_on_the_remote_tip_not_the_stale_local_branch(self):
        """The clone comes from the LOCAL path (fast, offline), so its base used to be the user's
        possibly-stale local branch tip — every repo-mode PR cut from old code, producing phantom
        conflicts and a diff re-doing work already on master."""
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        checkout, _ = self._git_repo_with_remote(tmp)
        self._register(tmp, checkout)
        orig_ws = self.ws.WORKSPACES
        self.ws.WORKSPACES = os.path.join(tmp, "workspaces")
        self.addCleanup(setattr, self.ws, "WORKSPACES", orig_ws)

        # the registered checkout genuinely does NOT have the later commit
        self.assertFalse(os.path.isfile(os.path.join(checkout, "behind.txt")))
        info = self.ws.provision("src", "wf-stale-1")
        self.assertTrue(os.path.isfile(os.path.join(info["path"], "base.txt")))
        self.assertTrue(os.path.isfile(os.path.join(info["path"], "behind.txt")),
                        "clone base must be the remote's default tip, not the local branch")

    def test_provision_still_works_when_the_remote_is_unreachable(self):
        """Cloning from the local path exists so repo-mode works offline — the base refresh must
        not turn a working offline run into a hard failure."""
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        checkout, bare = self._git_repo_with_remote(tmp)
        self._register(tmp, checkout)
        orig_ws = self.ws.WORKSPACES
        self.ws.WORKSPACES = os.path.join(tmp, "workspaces")
        self.addCleanup(setattr, self.ws, "WORKSPACES", orig_ws)
        shutil.rmtree(bare)                                   # remote gone
        info = self.ws.provision("src", "wf-offline-1")
        self.assertEqual(info["branch"], "otto/wf-offline-1")
        self.assertTrue(os.path.isfile(os.path.join(info["path"], "base.txt")))   # local base kept

    def test_provision_rejects_flag_smuggling_branch_and_cleans_up(self):
        # `branch` on a from_branch=True re-provision can arrive from client-supplied chat state
        # — must never reach the git fetch/checkout argv unvalidated, and a rejected provision
        # must not leave a partial clone behind.
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = self._git_repo(tmp)
        self._register(tmp, repo)
        orig_ws = self.ws.WORKSPACES
        self.ws.WORKSPACES = os.path.join(tmp, "workspaces")
        self.addCleanup(setattr, self.ws, "WORKSPACES", orig_ws)
        with self.assertRaises(ValueError):
            self.ws.provision("src", "wf-inject", from_branch=True, branch="--upload-pack=evil")
        self.assertFalse(os.path.isdir(self.ws.workspace_path("wf-inject")))

    def test_finalize_reports_no_changes_in_isolated_clone(self):
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = self._git_repo(tmp)
        self._register(tmp, repo)
        orig_ws = self.ws.WORKSPACES
        self.ws.WORKSPACES = os.path.join(tmp, "workspaces")
        self.addCleanup(setattr, self.ws, "WORKSPACES", orig_ws)
        info = self.ws.provision("src", "wf-test-2")
        out = self.ws.finalize("wf-test-2", title="t", base_head=info["head"])
        self.assertFalse(out["pushed"])
        self.assertIsNone(out["pr_url"])
        self.assertEqual(out["detail"], "no changes in the isolated clone")

    def test_agent_pr_surfaces_capability_opened_pr(self):
        # A capability that drove its own git (branch `22`, `gh pr create`) leaves the clone on
        # a non-Otto branch. finalize must surface THAT PR instead of "no changes".
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = self._git_repo(tmp)
        self._register(tmp, repo)
        orig_ws = self.ws.WORKSPACES
        self.ws.WORKSPACES = os.path.join(tmp, "workspaces")
        self.addCleanup(setattr, self.ws, "WORKSPACES", orig_ws)
        info = self.ws.provision("src", "wf-test-3")
        # Simulate the capability's own branch (Otto's branch stays untouched)
        subprocess.run(["git", "-C", info["path"], "checkout", "-qb", "22"], check=True,
                       capture_output=True, text=True)
        # Stub the GitHub bits: pretend origin is GitHub and the branch has an open PR.
        real_run, real_origin = self.ws._run, self.ws._git_origin
        self.ws._git_origin = lambda p: "https://github.com/o/r.git"
        def fake_run(args, **kw):
            if args[:1] == ["gh"] and "pr" in args and "list" in args:
                return (0, "https://github.com/o/r/pull/24", "")
            return real_run(args, **kw)
        self.ws._run = fake_run
        self.addCleanup(setattr, self.ws, "_run", real_run)
        self.addCleanup(setattr, self.ws, "_git_origin", real_origin)
        out = self.ws.finalize("wf-test-3", title="t", base_head=info["head"])
        self.assertEqual(out["pr_url"], "https://github.com/o/r/pull/24")
        self.assertEqual(out["branch"], "22")
        self.assertEqual(out["detail"], "opened by the capability")

    def test_capability_pr_wins_even_when_ottos_branch_also_has_work(self):
        # web-2f640059: sre-minion opened a properly stacked #355 -> #356 on its own branches AND
        # left work on Otto's branch. finalize checked for a capability PR only when Otto's branch
        # was EMPTY, so it pushed otto/<run> and opened #357 too — a duplicate of #355, against
        # master, outside the stack, with the phasing dropped. One run must yield one PR.
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = self._git_repo(tmp)
        self._register(tmp, repo)
        orig_ws = self.ws.WORKSPACES
        self.ws.WORKSPACES = os.path.join(tmp, "workspaces")
        self.addCleanup(setattr, self.ws, "WORKSPACES", orig_ws)
        info = self.ws.provision("src", "wf-dup-1")
        # The capability's own branch, with its own commit...
        subprocess.run(["git", "-C", info["path"], "checkout", "-qb", "cap-branch"], check=True,
                       capture_output=True, text=True)
        # ...and real work left on OTTO's branch too (the case that used to duplicate).
        subprocess.run(["git", "-C", info["path"], "checkout", "-q", "otto/wf-dup-1"], check=True,
                       capture_output=True, text=True)
        with open(os.path.join(info["path"], "new.txt"), "w") as f:
            f.write("otto's own change\n")
        real_run, real_origin = self.ws._run, self.ws._git_origin
        self.ws._git_origin = lambda p: "https://github.com/o/r.git"
        created = []

        def fake_run(args, **kw):
            if args[:1] == ["gh"] and "pr" in args and "list" in args:
                return (0, "https://github.com/o/r/pull/355", "")
            if args[:1] == ["gh"] and "pr" in args and "create" in args:
                created.append(args)
                return (0, "https://github.com/o/r/pull/357", "")
            if args[:2] == ["git", "-C"] and "push" in args:
                return (0, "", "")
            return real_run(args, **kw)
        self.ws._run = fake_run
        self.addCleanup(setattr, self.ws, "_run", real_run)
        self.addCleanup(setattr, self.ws, "_git_origin", real_origin)
        out = self.ws.finalize("wf-dup-1", title="t", base_head=info["head"])
        self.assertEqual(created, [], "finalize opened a SECOND PR alongside the capability's")
        self.assertEqual(out["pr_url"], "https://github.com/o/r/pull/355")
        self.assertTrue(out["pushed"], "Otto's branch should still be pushed so work isn't lost")

    def _finalize_with_failing_pr_create(self, create_err, pr_view_url=""):
        """finalize on a clone with real work, where `gh pr create` fails. Returns its dict."""
        tmp = tempfile.mkdtemp(prefix="otto-ws-")
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = self._git_repo(tmp)
        self._register(tmp, repo)
        orig_ws = self.ws.WORKSPACES
        self.ws.WORKSPACES = os.path.join(tmp, "workspaces")
        self.addCleanup(setattr, self.ws, "WORKSPACES", orig_ws)
        info = self.ws.provision("src", "wf-test-4")
        with open(os.path.join(info["path"], "new.txt"), "w") as f:
            f.write("work\n")
        real_run, real_origin = self.ws._run, self.ws._git_origin
        self.ws._git_origin = lambda p: "https://github.com/o/r.git"

        def fake_run(args, **kw):
            if args[:1] == ["gh"] and "create" in args:
                return (1, "", create_err)
            if args[:1] == ["gh"] and "view" in args:
                return (0, pr_view_url, "") if pr_view_url else (1, "", "no pr")
            if args[:2] == ["git", "-C"] and "push" in args:
                return (0, "", "")
            return real_run(args, **kw)
        self.ws._run = fake_run
        self.addCleanup(setattr, self.ws, "_run", real_run)
        self.addCleanup(setattr, self.ws, "_git_origin", real_origin)
        return self.ws.finalize("wf-test-4", title="t", base_head=info["head"])

    def test_finalize_recovers_pr_url_when_the_capability_already_opened_it(self):
        # The capability drove its own git on OTTO's branch, so `gh pr create` fails with
        # "already exists". Dropping the URL sends a run WITH an open PR to Blocked and skips
        # the review loop (both key on pr_url) — `gh pr view` is the authoritative recovery.
        out = self._finalize_with_failing_pr_create(
            'a pull request for branch "otto/wf-test-4" into branch "master" already exists:\n'
            "https://github.com/o/r/pull/343",
            pr_view_url="https://github.com/o/r/pull/343")
        self.assertTrue(out["pushed"])
        self.assertEqual(out["pr_url"], "https://github.com/o/r/pull/343")
        self.assertEqual(out["detail"], "opened by the capability")

    def test_finalize_recovers_pr_url_from_stderr_when_gh_view_fails(self):
        out = self._finalize_with_failing_pr_create(
            'a pull request for branch "otto/wf-test-4" into branch "master" already exists: '
            "https://github.com/o/r/pull/343.")
        self.assertEqual(out["pr_url"], "https://github.com/o/r/pull/343")

    def test_finalize_keeps_pr_url_none_on_an_unrelated_create_failure(self):
        # A URL in the stderr of some OTHER failure (docs link, auth help) must not be
        # mistaken for a PR — the run genuinely has no PR and belongs in Blocked.
        out = self._finalize_with_failing_pr_create(
            "GraphQL: Resource not accessible by integration. See "
            "https://docs.github.com/rest/pulls for help")
        self.assertTrue(out["pushed"])
        self.assertIsNone(out["pr_url"])
        self.assertIn("PR not opened", out["detail"])

    def test_diff_flags_head_move_or_dirty_flip(self):
        # In-place edit detection (#59) — pure comparison of two snapshots.
        before = {"r": {"head": "aaa", "dirty": False, "path": "/r"}}
        # HEAD moved (a commit) -> flagged
        self.assertEqual([c["name"] for c in self.ws.diff(before, {"r": {"head": "bbb", "dirty": False, "path": "/r"}})], ["r"])
        # dirty-state flipped (uncommitted edit) -> flagged
        self.assertEqual([c["name"] for c in self.ws.diff(before, {"r": {"head": "aaa", "dirty": True, "path": "/r"}})], ["r"])
        # unchanged -> not flagged
        self.assertEqual(self.ws.diff(before, {"r": {"head": "aaa", "dirty": False, "path": "/r"}}), [])
        # a repo only present after (no baseline) is ignored, not falsely flagged
        self.assertEqual(self.ws.diff(before, {"r": {"head": "aaa", "dirty": False, "path": "/r"},
                                               "new": {"head": "x", "dirty": False, "path": "/n"}}), [])


class ChatGitIdentityForwardingTests(unittest.TestCase):
    """A repo-mode chat is continuable only if its git identity was RECORDED, and that crosses
    three layers (workflow payload -> record_chat activity -> chats.finish_run). A layer that
    drops it is invisible: the run succeeds, the PR opens, and only the FOLLOW-UP fails — with an
    empty reply, which reads like the model having nothing to say. Measured on web-7e400cb0
    (2026-08-05): the workflow payload never carried the fields at all, so the retry's chat stored
    repo=None/git_run_id=None while its audit rows said repo=infra. Same shape as the gate
    whitelist test: catch the forgotten hop, not the logic."""

    def _src(self, name):
        with open(os.path.join(os.path.dirname(__file__), name), encoding="utf-8") as f:
            return f.read()

    def _record_chat_payload(self):
        src = self._src("workflows.py")
        i = src.index("            record_chat,")
        return src[i:src.index("start_to_close_timeout", i)]

    def test_the_workflow_sends_the_git_identity(self):
        payload = self._record_chat_payload()
        for field in ("repo", "git_run_id"):
            self.assertIn(f'"{field}":', payload,
                          f"_record_chat never sends {field}, so a workflow-opened repo-mode "
                          "chat is not continuable (its follow-up returns no output)")
        # Gated on the run actually being repo-mode — a non-repo chat must store neither, and a
        # bare workflow id here would send every plain chat down the re-provision path.
        self.assertIn("if self._repo else None", payload)

    def test_the_activity_forwards_it_to_the_store(self):
        src = self._src("activities.py")
        i = src.index("chats.finish_run(")
        depth, j = 0, src.index("(", i)
        while True:                                  # the call's own closing paren, not an arg's
            depth += {"(": 1, ")": -1}.get(src[j], 0)
            if depth == 0:
                break
            j += 1
        call = src[i:j + 1]
        for field in ("repo", "git_run_id"):
            self.assertIn(f'{field}=payload.get("{field}")', call,
                          f"record_chat drops {field} between the workflow and chats.finish_run")

    def test_api_continue_falls_back_to_the_stored_identity(self):
        # The client is not the authority: an omitted repo must reach chats.git_identity, not just
        # skip provisioning. Grepped because the Temporal branch of /api/continue can't be driven
        # from a unit test, and the failure is silent (a resume that returns `(no output)`).
        src = self._src("server.py")
        i = src.index('params = {"request": body["message"], "resume": body["session_id"]')
        branch = src[i:src.index("wid = ", i)]        # the whole branch, not a fixed-size window
        self.assertIn("chats.git_identity(", branch,
                      "/api/continue trusts the client for repo/git_run_id with no store fallback")
        # The allowlist check must still gate what the fallback produced.
        self.assertLess(branch.index("chats.git_identity("), branch.index("workspace.resolve(repo)"),
                        "the fallback must run BEFORE the allowlist check, not bypass it")

    def test_finish_run_accepts_it(self):
        # The store's own seam — keyword args, so a positional-only signature change is caught too.
        self.assertEqual(
            {"repo", "git_run_id"} & set(inspect.signature(chats.finish_run).parameters),
            {"repo", "git_run_id"})


class ReviewRequestTests(unittest.TestCase):
    """The code-review instruction handed to the review cap is pure, references the PR, forbids
    mutation, and asks for a parseable verdict."""

    def test_includes_pr_repo_and_verdict_contract(self):
        req = engine.review_request("https://github.com/o/r/pull/9", "infra",
                                    "Add a lifecycle block to the S3 bucket")
        self.assertIn("https://github.com/o/r/pull/9", req)
        self.assertIn("infra", req)
        self.assertIn("lifecycle block", req)
        self.assertIn("PASS", req)
        self.assertIn("CHANGES", req)
        self.assertIn("INCONCLUSIVE", req)
        # Review only — must not modify code / push / open a PR itself.
        self.assertIn("Do NOT modify", req)

    def test_omits_repo_clause_when_none(self):
        req = engine.review_request("https://github.com/o/r/pull/9", None, "do a thing")
        self.assertIn("pull/9", req)
        self.assertNotIn("in repo ``", req)


class PrBranchRecoveryTests(unittest.TestCase):
    """Recovering a resumed repo-mode run's branch from the PR its original run opened, when the
    chat didn't record git_branch (agent-managed branch / older chat). Exact, never a guess, and
    strictly scoped to the same repo so a follow-up can't push to another repo's PR."""

    def test_pr_url_from_run_extracts_freshest(self):
        import engine
        entries = [
            {"workflow": "web-1",
             "result": "**Opened draft PR** in `infra`: https://github.com/o/r/pull/12"},
            {"workflow": "web-2",
             "result": "**PR** in `infra` (opened by the capability on branch `443`): "
                       "https://github.com/o/other/pull/99"},
            {"workflow": "web-1",
             "result": "**Updated PR** on `otto/web-1`: https://github.com/o/r/pull/12"},
        ]
        orig = engine.iter_content_entries
        engine.iter_content_entries = lambda: iter(entries)
        try:
            self.assertEqual(engine.pr_url_from_run("web-1"), "https://github.com/o/r/pull/12")
            self.assertEqual(engine.pr_url_from_run("web-2"), "https://github.com/o/other/pull/99")
            self.assertIsNone(engine.pr_url_from_run("web-3"))
            self.assertIsNone(engine.pr_url_from_run(None))
        finally:
            engine.iter_content_entries = orig

    def test_pr_url_from_run_ignores_unopened_mention(self):
        """A run that only CITES a blocking/related PR as context — never opening or pushing to
        it itself — must not have that mention mistaken for its own PR (otherwise a follow-up's
        resume is sent onto a totally unrelated chat's branch, ci#issue observed 2026-08-24)."""
        import engine
        entries = [
            {"workflow": "web-1",
             "result": "blocked on #451, whose PR https://github.com/o/r/pull/452 is still open. "
                       "I stopped short of implementing and need input before proceeding."},
        ]
        orig = engine.iter_content_entries
        engine.iter_content_entries = lambda: iter(entries)
        try:
            self.assertIsNone(engine.pr_url_from_run("web-1"))
        finally:
            engine.iter_content_entries = orig

    def test_origin_slug_parses_https_and_ssh(self):
        import workspace
        self.assertEqual(workspace._origin_slug("https://github.com/acme-corp/ci.git"),
                         "acme-corp/ci")
        self.assertEqual(workspace._origin_slug("git@github.com:acme-corp/ci.git"),
                         "acme-corp/ci")
        self.assertIsNone(workspace._origin_slug(""))

    def test_pr_branch_refuses_other_repo(self):
        # A PR URL whose owner/name doesn't match the resolved repo's origin must be REFUSED —
        # never resolve (and later push a fix to) a PR in a different repo.
        import workspace
        orig_resolve, orig_run = workspace.resolve, workspace._run
        workspace.resolve = lambda repo: {"name": "ci", "path": "/x",
                                          "origin": "https://github.com/acme-corp/ci.git"}
        workspace._run = lambda *a, **k: (0, "some-branch", "")   # gh would answer, but must not be reached
        try:
            self.assertIsNone(workspace.pr_branch(
                "ci", "https://github.com/acme-corp/infra/pull/5"))
            self.assertIsNone(workspace.pr_branch("ci", "not a url"))
            self.assertEqual(workspace.pr_branch(
                "ci", "https://github.com/acme-corp/ci/pull/39"), "some-branch")
        finally:
            workspace.resolve, workspace._run = orig_resolve, orig_run


class AuditTests(unittest.TestCase):
    """The audit log must retain the full output so scheduled/unattended runs (which have
    no chat view) stay inspectable from the Audit tab (issue #22)."""

    def _cap(self):
        c = registry.Capability("skill", "deploy-status", "desc")
        c.risk = "read"
        return c

    def _isolate(self):
        import os, tempfile
        d = tempfile.mkdtemp(prefix="otto-audit-")
        orig = engine._DB
        engine._DB = os.path.join(d, "otto.db")
        return orig

    def _restore(self, orig):
        engine._DB = orig

    def test_content_split_keeps_audit_operational_and_content_full(self):
        # audit.log carries no chat-shaped content (request/result); that lives in the
        # separate content log, correlated back by workflow id.
        cap = self._cap()
        orig = self._isolate()
        try:
            engine._audit("wf-1", "req-short", cap, "ok", 0.0)                       # short
            engine._audit("wf-2", "req-long", cap, "x" * 1000, 0.0, attempt=2, verified=True)  # long
            audit_rows = list(engine.iter_audit_entries())
            content_rows = list(engine.iter_content_entries())
            for row in audit_rows:
                self.assertNotIn("request", row)
                self.assertNotIn("result", row)
                self.assertNotIn("result_preview", row)
            self.assertEqual(audit_rows[1]["attempt"], 2)
            self.assertIs(audit_rows[1]["verified"], True)
            self.assertEqual(content_rows[0]["workflow"], "wf-1")
            self.assertEqual(content_rows[0]["request"], "req-short")
            self.assertEqual(content_rows[0]["result"], "ok")
            self.assertEqual(content_rows[1]["request"], "req-long")
            self.assertEqual(content_rows[1]["result"], "x" * 1000)     # long: full output kept
        finally:
            self._restore(orig)

    def test_usage_normalizes_claude_json(self):
        # Maps the `claude -p` usage block; missing fields default to 0 (so a mock or an
        # error result with no usage never raises).
        u = engine._usage({"usage": {"input_tokens": 1200, "output_tokens": 800,
                                     "cache_read_input_tokens": 5000,
                                     "cache_creation_input_tokens": 300}})
        self.assertEqual(u, {"input": 1200, "output": 800, "cache_read": 5000, "cache_write": 300})
        self.assertEqual(engine._usage({}), {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})

    def test_tokens_and_model_recorded_when_present(self):
        cap = self._cap()
        orig = self._isolate()
        try:
            engine._audit("wf-1", "req", cap, "ok", 0.42, attempt=1,
                          tokens={"input": 10, "output": 20, "cache_read": 0, "cache_write": 0},
                          model="claude-opus-4-8")
            engine._audit("wf-2", "req", cap, "ok", 0.0)        # legacy call — no tokens/model
            rows = list(engine.iter_audit_entries())
            self.assertEqual(rows[0]["tokens"]["output"], 20)
            self.assertEqual(rows[0]["model"], "claude-opus-4-8")
            self.assertNotIn("tokens", rows[1])                 # absent, not null — keeps old logs clean
            self.assertNotIn("model", rows[1])
        finally:
            self._restore(orig)

    def test_duration_recorded_when_present(self):
        cap = self._cap()
        orig = self._isolate()
        try:
            engine._audit("wf-1", "req", cap, "ok", 0.0, duration_s=42.34)
            engine._audit("wf-2", "req", cap, "ok", 0.0)        # no duration
            rows = list(engine.iter_audit_entries())
            self.assertEqual(rows[0]["duration_s"], 42.3)       # rounded to 1 decimal
            self.assertNotIn("duration_s", rows[1])
        finally:
            self._restore(orig)


class ProjectIsolationTests(unittest.TestCase):
    """Per-project context isolation (issue #69): structured projects.json (back-compat),
    namespaced facts (read global+project, write project ns, never leak), per-project instructions."""

    def setUp(self):
        self._o_proj = registry.PROJECTS_FILE
        self._o_data = config.DATA_DIR
        self._o_db = engine._DB
        self._d = tempfile.mkdtemp(prefix="otto-proj-")
        config.DATA_DIR = self._d
        registry.PROJECTS_FILE = os.path.join(self._d, "projects.json")
        engine._DB = os.path.join(self._d, "otto.db")

    def tearDown(self):
        registry.PROJECTS_FILE = self._o_proj
        config.DATA_DIR = self._o_data
        engine._DB = self._o_db

    def _register(self, path, instructions=""):
        registry.save_projects([{"path": path, "instructions": instructions}])

    def test_old_string_list_migrates(self):
        with open(registry.PROJECTS_FILE, "w") as f:
            json.dump(["/repos/aws-cost-report", "/repos/infra"], f)   # OLD bare-string format
        self.assertEqual(registry.projects(), ["/repos/aws-cost-report", "/repos/infra"])
        self.assertEqual(registry.project_meta("/repos/infra")["instructions"], "")

    def test_namespace_slug(self):
        self.assertEqual(registry.project_namespace("/a/b/AWS Cost-Report/"), "aws-cost-report")

    def test_instructions_roundtrip_and_preserved_on_add(self):
        self._register("/repos/p1")
        registry.set_project_instructions("/repos/p1", "Tag team=sre.")
        self.assertEqual(registry.project_meta("/repos/p1")["instructions"], "Tag team=sre.")
        registry.add_project("/repos/p2")    # adding another project must not wipe p1's instructions
        self.assertEqual(registry.project_meta("/repos/p1")["instructions"], "Tag team=sre.")

    def test_facts_namespacing_and_isolation(self):
        proj = os.path.join(self._d, "aws-cost-report")
        engine._remember(registry.Capability("agent", "g", "d"), "r", ["global fact about vpc"])
        engine._remember(registry.Capability("agent", "p", "d"), "r", ["project fact about cost"], project=proj)
        self.assertEqual(engine.recent_facts(), ["global fact about vpc"])              # global run: only global
        self.assertEqual(engine.recent_facts(project=proj),
                         ["global fact about vpc", "project fact about cost"])           # project run: both
        self.assertEqual(len(engine.memory_events(engine._memory_ns(proj))), 1)         # written to its own namespace
        self.assertNotIn("project fact about cost", engine.recent_facts())              # never leaks to global

    def test_remember_dedupes_against_global_in_project(self):
        cap = registry.Capability("agent", "g", "d")
        proj = os.path.join(self._d, "p")
        engine._remember(cap, "r", ["shared fact"])                  # global
        engine._remember(cap, "r", ["shared fact"], project=proj)    # already visible globally -> dropped
        self.assertEqual(engine.memory_events(engine._memory_ns(proj)), [])   # nothing in the project ns

    def test_instructions_injected_only_in_project(self):
        proj = os.path.join(self._d, "p1")
        self._register(proj, "Always tag resources team=sre.")
        cap = registry.Capability("agent", "minion", "d")
        self.assertIn("team=sre", engine._memory_context("do x", cap, project=proj))
        self.assertNotIn("team=sre", engine._memory_context("do x", cap) or "")   # not in a non-project run

    def test_resolve_project(self):
        proj = os.path.join(self._d, "myrepo")
        self._register(proj)
        cap = registry.Capability("agent", "c", "d"); cap.cwd = proj
        self.assertEqual(engine._resolve_project(cap), proj)         # project cap -> via cwd
        cap2 = registry.Capability("agent", "c2", "d")               # global cap, no cwd
        self.assertIsNone(engine._resolve_project(cap2))
        orig = engine.workspace.resolve                              # repo-mode -> via workspace.resolve
        engine.workspace.resolve = lambda r: {"path": proj} if r == "myrepo" else None
        try:
            self.assertEqual(engine._resolve_project(cap2, repo="myrepo"), proj)
        finally:
            engine.workspace.resolve = orig


class ChatHistoryTests(unittest.TestCase):
    """Persisted chat history (issue #18)."""

    def setUp(self):
        self._orig = chats._DB
        self._tmp = tempfile.mkdtemp(prefix="otto-chats-")
        chats._DB = os.path.join(self._tmp, "otto.db")

    def tearDown(self):
        chats._DB = self._orig
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_save_get_list_delete(self):
        cid = chats.save({"id": "a", "title": "first", "session_id": "s1",
                          "cap": {"name": "demo-read"}, "messages": [{"role": "user", "text": "hi"}]})
        self.assertEqual(cid, "a")
        got = chats.get("a")
        self.assertEqual(got["title"], "first")
        self.assertEqual(got["session_id"], "s1")
        self.assertIn("created", got)
        self.assertIn("updated", got)
        summ = chats.list_summaries()[0]
        self.assertEqual(summ["messages"], 1)             # count, not bodies
        self.assertEqual(summ["cap"], "demo-read")
        self.assertNotIn("session_id", summ)              # summary stays lightweight
        chats.delete("a")
        self.assertIsNone(chats.get("a"))

    def test_a_workflow_opened_repo_mode_chat_records_its_git_identity(self):
        # The turn a WORKFLOW records (chat_key — a needs-you retry, any unattended run) has no
        # browser to persist repo/git_run_id client-side. Without them the chat is not continuable
        # at all: /api/continue sends nothing, OttoWorkflow skips _resume_workspace, and the
        # follow-up `claude -p --resume`s from Otto's own directory instead of the clone's path —
        # no session there, so the reply is `(no output)`. Measured on web-7e400cb0 (2026-08-05):
        # two follow-ups, 0 tokens each, stderr "No conversation found with session ID".
        chats.start_run("web-7e400cb0", "work on issue #359", labels=["retry"],
                        cap={"name": "sre-minion"}, run_id="web-7e400cb0")
        chats.finish_run("web-7e400cb0", "opened PR #364", session_id="sess-1",
                         cap={"name": "sre-minion"}, repo="infra", git_run_id="web-7e400cb0")
        got = chats.get("web-7e400cb0")
        self.assertEqual(got["repo"], "infra")
        self.assertEqual(got["git_run_id"], "web-7e400cb0")

    def test_git_identity_is_recoverable_from_the_session_alone(self):
        # The server-side backstop: a client that sends no repo (a stale tab, an old client) must
        # not silently lose resumability — the store can answer from the session id alone.
        chats.finish_run("c", "done", session_id="sess-1", cap={"name": "sre-minion"},
                         repo="infra", git_run_id="web-7e400cb0")
        self.assertEqual(chats.git_identity("sess-1"),
                         {"repo": "infra", "git_run_id": "web-7e400cb0"})

    def test_git_identity_is_empty_when_it_cannot_be_pinned_unambiguously(self):
        self.assertEqual(chats.git_identity(None), {})
        self.assertEqual(chats.git_identity("nope"), {})
        # A non-repo chat has no identity to hand back (and must not yield repo=None as if it did).
        chats.finish_run("plain", "hi", session_id="sess-2", cap={"name": "assistant"})
        self.assertEqual(chats.git_identity("sess-2"), {})
        # Two chats claiming one session: we cannot tell which clone to rebuild, so refuse rather
        # than point a follow-up at another task's workspace.
        chats.finish_run("a", "x", session_id="dup", cap={"name": "m"}, repo="infra",
                         git_run_id="web-1")
        chats.finish_run("b", "y", session_id="dup", cap={"name": "m"}, repo="ci",
                         git_run_id="web-2")
        self.assertEqual(chats.git_identity("dup"), {})

    def test_a_later_non_repo_turn_does_not_clobber_the_git_identity(self):
        # save() merges these via kept(), and finish_run must not defeat that: a follow-up turn in
        # the same thread that isn't repo-mode (or a direct-path resume, which sends neither) would
        # otherwise erase the identity the first turn established and break every turn after it.
        chats.finish_run("c", "first", session_id="s1", cap={"name": "sre-minion"},
                         repo="infra", git_run_id="web-7e400cb0")
        chats.finish_run("c", "second", session_id="s2", cap={"name": "sre-minion"})
        got = chats.get("c")
        self.assertEqual(got["repo"], "infra")
        self.assertEqual(got["git_run_id"], "web-7e400cb0")
        self.assertEqual(got["session_id"], "s2")     # the turn's own fields still advance

    def test_upsert_preserves_created(self):
        chats.save({"id": "x", "title": "t", "messages": []})
        created = chats.get("x")["created"]
        chats.save({"id": "x", "title": "t2", "messages": [{"role": "user", "text": "y"}]})
        self.assertEqual(chats.get("x")["created"], created)   # created preserved on update
        self.assertEqual(chats.get("x")["title"], "t2")        # new fields applied
        self.assertEqual(len(chats.list_summaries()), 1)       # upsert, not append

    def test_save_without_id_is_noop(self):
        self.assertIsNone(chats.save({"title": "no id"}))
        self.assertEqual(chats.list_summaries(), [])

    def test_capped_to_max(self):
        for i in range(chats.MAX_CHATS + 12):
            chats.save({"id": f"c{i}", "title": str(i), "messages": []})
        self.assertLessEqual(len(chats.list_summaries()), chats.MAX_CHATS)

    def test_labels_persist_across_upsert(self):
        chats.save({"id": "L", "title": "t", "labels": ["scheduled-job"], "messages": []})
        self.assertEqual(chats.get("L")["labels"], ["scheduled-job"])
        self.assertEqual(chats.list_summaries()[0]["labels"], ["scheduled-job"])
        # An upsert that omits labels keeps the existing ones (None = leave as-is).
        chats.save({"id": "L", "title": "t2", "messages": []})
        self.assertEqual(chats.get("L")["labels"], ["scheduled-job"])

    def test_stats_persist_across_upsert(self):
        chats.save({"id": "S", "title": "t", "stats": {"runs": 3, "appr": 1, "cost": 0.42}, "messages": []})
        self.assertEqual(chats.get("S")["stats"], {"runs": 3, "appr": 1, "cost": 0.42})
        # An upsert that omits stats keeps the existing tallies (None = leave as-is).
        chats.save({"id": "S", "title": "t2", "messages": []})
        self.assertEqual(chats.get("S")["stats"], {"runs": 3, "appr": 1, "cost": 0.42})

    def test_pin_persists_sorts_and_preserves_content(self):
        chats.save({"id": "p1", "title": "old", "messages": []})
        chats.save({"id": "p2", "title": "new", "messages": [{"role": "user", "text": "hi"}]})
        # set_pinned flips the flag without clobbering title/messages (unlike save).
        self.assertTrue(chats.set_pinned("p1", True))
        self.assertTrue(chats.get("p1")["pinned"])
        self.assertEqual(chats.get("p1")["title"], "old")
        # pinned chat floats to the top even though it's older.
        summ = chats.list_summaries()
        self.assertEqual(summ[0]["id"], "p1")
        self.assertTrue(summ[0]["pinned"])
        # an upsert that omits pinned keeps it; unpin clears it.
        chats.save({"id": "p1", "title": "old2", "messages": []})
        self.assertTrue(chats.get("p1")["pinned"])
        self.assertFalse(chats.set_pinned("p1", False))
        self.assertFalse(chats.get("p1")["pinned"])
        self.assertIsNone(chats.set_pinned("missing", True))   # unknown id

    def test_run_id_persists_and_clears(self):
        # A live web run stamps run_id so a reload / chat switch can reattach to the workflow.
        chats.save({"id": "R", "title": "t", "run_id": "web-abc123", "messages": []})
        self.assertEqual(chats.get("R")["run_id"], "web-abc123")
        self.assertEqual(chats.list_summaries()[0]["run_id"], "web-abc123")
        # When the turn finishes the UI clears it (run_id=None) so it's no longer reattached.
        chats.save({"id": "R", "title": "t", "run_id": None, "messages": []})
        self.assertIsNone(chats.get("R")["run_id"])
        self.assertIsNone(chats.list_summaries()[0]["run_id"])

    def test_append_run_creates_then_appends(self):
        # First firing of a scheduled job creates the chat (request + result as two turns).
        chats.append_run("chat-sched1", "check builds", "all green",
                         title="check builds", session_id="s1",
                         cap={"name": "ci-cli"}, labels=["scheduled-job"])
        c = chats.get("chat-sched1")
        self.assertEqual([m["role"] for m in c["messages"]], ["user", "otto"])
        self.assertEqual(c["messages"][1]["text"], "all green")
        self.assertEqual(c["labels"], ["scheduled-job"])
        self.assertEqual(c["title"], "check builds")
        # Second firing appends to the SAME chat and keeps the original title.
        chats.append_run("chat-sched1", "check builds", "one red build",
                         title="check builds", labels=["scheduled-job"])
        c = chats.get("chat-sched1")
        self.assertEqual(len(c["messages"]), 4)            # appended, not replaced
        self.assertEqual(c["messages"][-1]["text"], "one red build")
        self.assertEqual(len(chats.list_summaries()), 1)   # one chat, not two

    def test_start_run_then_finish_run(self):
        # start_run opens the thread mid-flight: request + a PENDING placeholder, visible in
        # the sidebar before any result exists.
        chats.start_run("gh-issue-9", "do the thing", title="do the thing",
                        labels=["github-ticket"])
        c = chats.get("gh-issue-9")
        self.assertEqual([m["role"] for m in c["messages"]], ["user", "otto"])
        self.assertTrue(c["messages"][1].get("pending"))   # placeholder, not a real reply yet
        self.assertEqual(c["labels"], ["github-ticket"])
        self.assertIsNone(c["session_id"])                 # no session until it finishes
        # finish_run REPLACES the placeholder in place (not appended) and records session/cap.
        chats.finish_run("gh-issue-9", "all done", session_id="s7", cap={"name": "x"})
        c = chats.get("gh-issue-9")
        self.assertEqual(len(c["messages"]), 2)            # placeholder filled, not appended
        self.assertEqual(c["messages"][1]["text"], "all done")
        self.assertFalse(c["messages"][1].get("pending"))
        self.assertEqual(c["session_id"], "s7")
        self.assertEqual(c["title"], "do the thing")       # title preserved

    def test_finish_run_without_start_falls_back_to_append(self):
        # If start_run was skipped, finish_run still records the result rather than dropping it.
        chats.finish_run("orphan-1", "result only", session_id="s8")
        c = chats.get("orphan-1")
        self.assertEqual(c["messages"][-1]["text"], "result only")
        self.assertEqual(c["session_id"], "s8")

    def test_find_reattach_matches_unresolved_chat(self):
        # A board-retry reattaches to the interactive chat still awaiting a good result.
        chats.save({"id": "c1", "title": "add tyler", "messages": [
            {"role": "user", "text": "Add tyler to CODEOWNERS"},
            {"role": "otto", "text": "⚠️ **Needs human review** — this did not pass verify."}]})
        self.assertEqual(chats.find_reattach("Add tyler to CODEOWNERS"), "c1")
        # A pending (still in-flight) last reply also counts as unresolved.
        chats.save({"id": "c2", "title": "deploy", "messages": [
            {"role": "user", "text": "Deploy registry"},
            {"role": "otto", "text": "…", "pending": True}]})
        self.assertEqual(chats.find_reattach("Deploy registry"), "c2")

    def test_find_reattach_ignores_resolved_and_ambiguous(self):
        # A chat that already got a clean answer must NOT be reattached to.
        chats.save({"id": "ok", "messages": [
            {"role": "user", "text": "Do X"}, {"role": "otto", "text": "Done."}]})
        self.assertIsNone(chats.find_reattach("Do X"))
        # Two unresolved chats with the same opening request → ambiguous → fresh thread.
        for i in (1, 2):
            chats.save({"id": f"dup{i}", "messages": [
                {"role": "user", "text": "Same ask"},
                {"role": "otto", "text": "⚠️ **Needs human review** — nope."}]})
        self.assertIsNone(chats.find_reattach("Same ask"))
        self.assertIsNone(chats.find_reattach("never asked"))
        self.assertIsNone(chats.find_reattach(""))

    def test_origin_run_id_survives_run_id_being_cleared(self):
        # run_id is the LIVE spinner indicator and gets nulled out the moment a turn finishes
        # (clearRun() client-side, finish_run/append_run server-side) — origin_run_id must stay
        # sticky so a finished run can still be traced back to its chat (user-reported: a
        # completed board card had no Chat button because chat_key was never set for an
        # interactive web-* run, and run_id had already been cleared by the time the board asked).
        chats.save({"id": "web-abc", "run_id": "web-abc", "messages": [
            {"role": "user", "text": "hello"}]})
        self.assertEqual(chats.get("web-abc")["run_id"], "web-abc")
        self.assertEqual(chats.find_by_run_origin("web-abc"), "web-abc")
        # The turn finishes: client sends run_id=null, same as clearRun()/persistChat().
        chats.save({"id": "web-abc", "run_id": None, "messages": [
            {"role": "user", "text": "hello"}, {"role": "otto", "text": "hi"}]})
        self.assertIsNone(chats.get("web-abc")["run_id"])
        self.assertEqual(chats.find_by_run_origin("web-abc"), "web-abc",
                         "origin_run_id must not be cleared along with run_id")

    def test_find_by_run_origin_ignores_ambiguous_and_unknown(self):
        chats.save({"id": "only-one", "run_id": "wid-1", "messages": []})
        self.assertEqual(chats.find_by_run_origin("wid-1"), "only-one")
        self.assertIsNone(chats.find_by_run_origin("never-seen"))
        self.assertIsNone(chats.find_by_run_origin(""))
        self.assertIsNone(chats.find_by_run_origin(None))

    def test_message_round_trip_omits_unset_ts_and_pending(self):
        # Messages live in their own table (issue #103), so read-back REBUILDS each dict — it
        # must not invent `ts: null` / `pending: false` keys the writers never wrote, or a
        # round-tripped chat stops matching what the client and find_reattach() expect.
        chats.save({"id": "m", "title": "t", "messages": [
            {"role": "user", "text": "no ts here"},
            {"role": "otto", "text": "with ts", "ts": "2026-07-29T10:00:00"},
            {"role": "otto", "text": "…", "pending": True, "ts": "2026-07-29T10:00:01"}]})
        msgs = chats.get("m")["messages"]
        self.assertEqual(msgs[0], {"role": "user", "text": "no ts here"})
        self.assertEqual(msgs[1], {"role": "otto", "text": "with ts", "ts": "2026-07-29T10:00:00"})
        self.assertEqual(msgs[2], {"role": "otto", "text": "…", "pending": True,
                                   "ts": "2026-07-29T10:00:01"})
        self.assertEqual(chats.list_summaries()[0]["messages"], 3)   # count, no bodies loaded

    def test_empty_message_text_survives(self):
        # append_run(cid, "", result) writes an empty-string user turn — it must round-trip as
        # "" and not collapse to None (the falsy-omit rule applies to ts/pending only).
        chats.append_run("e", "", "the result")
        msgs = chats.get("e")["messages"]
        self.assertEqual(msgs[0]["text"], "")
        self.assertEqual(msgs[0]["role"], "user")

    def test_concurrent_saves_from_separate_processes(self):
        """server.py and worker.py are separate PROCESSES writing the same db, so save()'s
        read-merge-write runs under storage.tx's BEGIN IMMEDIATE. Two things must hold under real
        contention: no writer errors out with "database is locked", and no write is lost —
        including the `pinned` flag every writer is supposed to carry forward from the prior row."""
        chats.save({"id": "shared", "title": "seed", "messages": []})
        chats.set_pinned("shared", True)
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import chats\n"
            "chats._DB = %r\n"
            "n = sys.argv[1]\n"
            # Each process writes its OWN chat and also re-saves the shared one, so the shared
            # row takes concurrent read-merge-write traffic from every process at once.
            "chats.save({'id': 'p' + n, 'title': 'from ' + n,\n"
            "            'messages': [{'role': 'user', 'text': n}]})\n"
            "chats.save({'id': 'shared', 'title': 'touched by ' + n, 'messages': []})\n"
        ) % (os.path.dirname(os.path.abspath(chats.__file__)), chats._DB)
        procs = [subprocess.Popen([sys.executable, "-c", script, str(i)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(8)]
        for p in procs:
            _out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err.decode()[-2000:])

        # Every process's own chat landed (no lost inserts, no lock errors).
        ids = {c["id"] for c in chats.list_summaries()}
        for i in range(8):
            self.assertIn(f"p{i}", ids)
        # The contended row survived as ONE row, still pinned — the merge wasn't lost.
        shared = chats.get("shared")
        self.assertTrue(shared["pinned"])
        self.assertTrue(shared["title"].startswith("touched by"))
        self.assertEqual(sum(c["id"] == "shared" for c in chats.list_summaries()), 1)


class AuditOrderingTests(unittest.TestCase):
    """The audit trail must stay chronological under volume — iter_audit_entries() orders by
    insertion (SQLite `id ASC`), not by the `at` timestamp string, so same-second writes can't
    tie/reorder."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="otto-audit-")
        self._orig_db = engine._DB
        engine._DB = os.path.join(self.dir, "otto.db")
        self.cap = registry.Capability("skill", "order-test", "d")
        self.cap.risk = "read"

    def tearDown(self):
        engine._DB = self._orig_db
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_many_entries_stay_in_insertion_order(self):
        for i in range(50):
            engine._audit(f"wf-{i:04d}", f"request {i}", self.cap, f"result {i}", 0)
        entries = list(engine.iter_audit_entries())
        self.assertEqual(len(entries), 50)
        ids = [e["workflow"] for e in entries]
        self.assertEqual(ids, sorted(ids), "entries must stay in insertion order")
        self.assertEqual(ids[-1], "wf-0049")


class PrTargetTests(unittest.TestCase):
    """A fresh request naming an OPEN pull request must branch off THAT PR's head, not the
    default branch (`web-d2438694`: the request described `hf download` at line ~148 of a file
    whose default-branch copy is 82 lines long, so no attempt could reach the code and the run
    shipped a change to the wrong revision as a second PR against the wrong base)."""

    def test_refs_are_extracted_from_every_shape(self):
        slug = "acme/infra"
        f = workspace.request_pr_refs
        self.assertEqual(f("fix acme/infra#498 please", slug=slug), [(slug, 498)])
        self.assertEqual(f("see https://github.com/acme/infra/pull/12", slug=slug), [(slug, 12)])
        self.assertEqual(f("work on #429", slug=slug), [(None, 429)])

    def test_a_bare_repo_name_ref_is_recognised(self):
        """`web-3b6f2613` wrote "(ci#106)" and NEITHER other pattern matched: the slug form
        wants an owner, and there `#` follows `y`, not a space. The run got a default-branch
        clone for code that lives only on that PR."""
        slug = "acme/ci"
        f = workspace.request_pr_refs
        self.assertEqual(f("fix settings.kts (ci#106) please", slug=slug), [(slug, 106)])
        self.assertEqual(f("ci#106", slug=slug), [(slug, 106)])

    def test_a_bare_name_that_is_not_this_repo_is_ignored(self):
        """Unbounded, `word#123` matches anything — which is how an unrelated number becomes a
        branch switch."""
        self.assertEqual(workspace.request_pr_refs("ticket abc#12", slug="acme/ci"), [])
        self.assertEqual(workspace.request_pr_refs("platform#12", slug="acme/ci"), [])

    def test_a_pr_in_another_repo_is_never_a_target(self):
        """The clone is provisioned against ONE repo; a reference to someone else's PR must not
        steer it. Same containment `pr_branch` applies to a URL."""
        self.assertEqual(workspace.request_pr_refs("see other/repo#12", slug="acme/infra"), [])
        self.assertEqual(
            workspace.request_pr_refs("https://github.com/other/repo/pull/12", slug="acme/infra"),
            [])

    def test_a_qualified_ref_is_not_also_counted_as_a_bare_one(self):
        self.assertEqual(workspace.request_pr_refs("acme/infra#498", slug="acme/infra"),
                         [("acme/infra", 498)])

    def test_only_an_OPEN_same_repo_pr_becomes_a_target(self):
        """OPEN is the whole discriminator. A merged PR ("revert #498", "fix the bug #498
        introduced") must fall through to a fresh branch off the default, and an issue number
        must too — `gh pr view` is what tells them apart, not the wording."""
        seen = []

        def fake_run(args, **kw):
            seen.append(args)
            num = args[3]
            if args[:3] == ["gh", "api", "user"]:
                return (0, "me", "")
            A = '"author":{"login":"me"}'
            body = {"7": '{"state":"OPEN","url":"u/7","headRefName":"feat","isCrossRepository":false,%s}' % A,
                    "8": '{"state":"MERGED","url":"u/8","headRefName":"old","isCrossRepository":false,%s}' % A,
                    "9": '{"state":"OPEN","url":"u/9","headRefName":"fork","isCrossRepository":true,%s}' % A}
            return (0, body[num], "") if num in body else (1, "", "no such pr")

        orig_run, orig_resolve = workspace._run, workspace.resolve
        workspace._run = fake_run
        workspace.resolve = lambda r: {"name": "infra", "path": "/x",
                                       "origin": "https://github.com/acme/infra.git"}
        try:
            self.assertEqual(workspace.pr_target("infra", "do acme/infra#7")["branch"], "feat")
            self.assertIsNone(workspace.pr_target("infra", "revert acme/infra#8"))
            self.assertIsNone(workspace.pr_target("infra", "issue acme/infra#404"))
            # A fork's head branch does not exist on our origin, so from_branch could not fetch it.
            self.assertIsNone(workspace.pr_target("infra", "do acme/infra#9"))
        finally:
            workspace._run, workspace.resolve = orig_run, orig_resolve

    def test_only_the_operators_OWN_pr_is_a_target(self):
        """Merely NAMING a PR is weak evidence of intent ("add a test like the one in #480"),
        and acting on it against a colleague's branch pushes commits into their review — worse
        than the wrong-base PR this feature prevents. Fails closed on an unknown viewer."""
        def gh(author, viewer):
            def fake_run(args, **kw):
                if args[:3] == ["gh", "api", "user"]:
                    return (0, viewer, "") if viewer else (1, "", "no auth")
                return (0, '{"state":"OPEN","url":"u","headRefName":"feat",'
                           '"isCrossRepository":false,"author":{"login":"%s"}}' % author, "")
            return fake_run

        orig_run, orig_resolve = workspace._run, workspace.resolve
        workspace.resolve = lambda r: {"name": "infra", "path": "/x",
                                       "origin": "https://github.com/acme/infra.git"}
        try:
            workspace._run = gh("me", "me")
            self.assertEqual(workspace.pr_target("infra", "do acme/infra#7")["branch"], "feat")
            workspace._run = gh("a-colleague", "me")
            self.assertIsNone(workspace.pr_target("infra", "like acme/infra#7"))
            workspace._run = gh("me", None)
            self.assertIsNone(workspace.pr_target("infra", "do acme/infra#7"))
        finally:
            workspace._run, workspace.resolve = orig_run, orig_resolve

    def test_probes_are_bounded(self):
        """A request quoting an issue thread full of `#N` must not turn one provision into dozens
        of network round-trips."""
        calls = []
        orig_run, orig_resolve = workspace._run, workspace.resolve
        workspace._run = lambda args, **kw: (calls.append(args), (1, "", ""))[1]
        workspace.resolve = lambda r: {"name": "infra", "path": "/x",
                                       "origin": "https://github.com/acme/infra.git"}
        try:
            workspace.pr_target("infra", " ".join(f"#{n}" for n in range(100, 140)))
        finally:
            workspace._run, workspace.resolve = orig_run, orig_resolve
        self.assertLessEqual(len(calls), 3, "pr_target must bound how many refs it probes")

    def test_the_fresh_provision_call_site_uses_the_target(self):
        """Every other piece of this is dead code if the fresh repo-mode provision still hardcodes
        a default-branch clone."""
        src = open("workflows.py").read()
        i = src.index("provision_workspace,\n                {\"repo\": repo, \"run_id\": workflow.info().workflow_id,")
        block = src[i:i + 400]
        self.assertIn('"from_branch": bool(target)', block)
        self.assertIn('"branch": target', block)
        # The target itself is resolved once, above the gate, because the plan preview needs it.
        self.assertIn("target = self._pr_target.get(\"branch\")", src)

    def test_finalize_amends_the_targeted_pr_instead_of_opening_a_second(self):
        """Branching off an open PR and then running `gh pr create` anyway would produce exactly
        the duplicate-PR outcome this feature exists to prevent."""
        src = open("workflows.py").read()
        # Anchored on the call, not its indentation — the payload is what matters, and
        # pinning leading spaces re-breaks this on any extraction that moves the block.
        i = re.search(r"finalize_workspace,\s*\{\"run_id\": workflow\.info\(\)\.workflow_id", src)
        self.assertIsNotNone(i, "the finalize_workspace call site moved or changed shape")
        payload = src[i.start():i.start() + 1400]
        self.assertIn('"existing_pr": bool(self._pr_target.get("branch"))', payload)
        self.assertIn('"branch": self._pr_target.get("branch")', payload)

    def test_the_result_says_it_updated_a_pr_rather_than_opened_one(self):
        """Reporting "opened draft PR" after pushing to someone else's branch names a PR that
        does not exist, and hides the one decision the reader most needs to see."""
        src = open("workflows.py").read()
        self.assertIn("**Updated PR #", src)
        i = src.index("**Updated PR #")
        self.assertIn("self._pr_target['branch']", src[i:i + 400])


class GroundingTests(unittest.TestCase):
    """Does the tree the run was handed actually contain the thing the request is about? Nothing
    in Otto asked this before: all four judges scored "does the output satisfy the request"
    against whatever tree the run held, so a well-executed change to the wrong file passed."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="otto-ground-")
        os.makedirs(os.path.join(self.d, "runbooks", "vllm"))
        self.f = os.path.join(self.d, "runbooks", "vllm", "upload-weights.sh")
        with open(self.f, "w") as fh:
            fh.write("\n".join(f"line {i}" for i in range(82)))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_line_number_the_file_cannot_have_is_reported(self):
        """The real case: "line ~148" against an 82-line copy of the named file."""
        notes = workspace.grounding(
            self.d, "fix `runbooks/vllm/upload-weights.sh` at line ~148 to pin ${SHA}")
        self.assertTrue(notes)
        self.assertIn("148", notes[0])
        self.assertIn("82", notes[0])

    def test_a_line_attached_to_the_path_is_read(self):
        """`path:line` is how this repo's own conventions write it, and the free-floating
        "line ~N" regex did not match it — so `settings.kts:1437-1441` against a 1100-line file
        produced no note at all (`web-3b6f2613`)."""
        notes = workspace.grounding(
            self.d, "fix runbooks/vllm/upload-weights.sh:900-905 where the retry is wrong")
        self.assertTrue(notes)
        self.assertIn("900", notes[0])

    def test_an_attached_line_beats_a_free_floating_one(self):
        """A "line N" elsewhere in the text may be about a different file; the attached number
        is bound to this one."""
        notes = workspace.grounding(
            self.d, "port the fix from line 5 into runbooks/vllm/upload-weights.sh:900")
        self.assertTrue(notes)
        self.assertIn("900", notes[0])

    def test_a_plausible_line_number_is_not_reported(self):
        """Ordinary imprecision ("~75" in an 82-line file) must stay silent — a false mismatch
        tells a run its own request is wrong, which is worse than saying nothing."""
        self.assertEqual(
            workspace.grounding(self.d, "fix `runbooks/vllm/upload-weights.sh` at line 75"), [])

    def test_a_named_file_that_does_not_exist_is_reported(self):
        notes = workspace.grounding(self.d, "update `runbooks/vllm/nope.sh` please")
        self.assertTrue(notes)
        self.assertIn("nope.sh", notes[0])

    def test_a_request_that_matches_the_tree_is_silent(self):
        self.assertEqual(
            workspace.grounding(self.d, "update `runbooks/vllm/upload-weights.sh` please"), [])

    def test_a_url_in_the_request_is_not_read_as_a_repo_path(self):
        """`https://github.com/acme/infra/blob/main/setup.py` parses as a path and would be
        reported missing — telling the run its own request is ungrounded, which is worse than
        silence. Bare hostnames (`example.com/a/b.html`) are the same shape."""
        self.assertEqual(
            workspace.grounding(self.d, "see https://github.com/acme/infra/blob/main/setup.py"), [])
        self.assertEqual(workspace.grounding(self.d, "docs at example.com/a/b.html"), [])

    def test_request_text_can_never_escape_the_workspace(self):
        """A path out of the request reaching `open()` unchecked would let request text probe the
        host filesystem through the mismatch note."""
        self.assertEqual(workspace.grounding(self.d, "read `../../etc/passwd.txt`"), [])

    def test_the_note_tells_the_run_to_report_rather_than_substitute(self):
        note = judging._grounding_note(["the request points at line 148 of `x.sh`"])
        self.assertIn("148", note)
        low = note.lower()
        self.assertIn("say so", low)
        # The two moves that actually happened on web-d2438694, both forbidden by name.
        self.assertIn("do not go looking", low)
        self.assertIn("outside your working directory", low)

    def test_no_mismatch_means_no_note(self):
        self.assertIsNone(judging._grounding_note([]))
        self.assertIsNone(judging._grounding_note(None))

    def test_the_judge_is_told_which_way_each_verdict_goes(self):
        """Reporting the mismatch must PASS and silently substituting must FAIL — a rule that
        only said "there is a mismatch" would push the judge to fail the honest answer."""
        seen = {}

        def fake_confirm(task, prompt, parse, adverse):
            seen["prompt"] = prompt
            return {"passed": True, "critique": ""}

        orig = judging.confirm_adverse
        judging.confirm_adverse = fake_confirm
        try:
            judging.verify("req", _cap_stub(), "out",
                           grounding=["the request names `x.sh`, which does not exist"])
        finally:
            judging.confirm_adverse = orig
        p = seen["prompt"]
        self.assertIn("x.sh", p)
        self.assertIn("PASSES on this point if it reports the mismatch", p)
        self.assertIn("FAILS if the output silently edited something else", p)

    def test_a_clean_run_pays_nothing(self):
        seen = {}

        def fake_confirm(task, prompt, parse, adverse):
            seen["prompt"] = prompt
            return {"passed": True, "critique": ""}

        orig = judging.confirm_adverse
        judging.confirm_adverse = fake_confirm
        try:
            judging.verify("req", _cap_stub(), "out", grounding=[])
        finally:
            judging.confirm_adverse = orig
        self.assertNotIn("contradictions", seen["prompt"])

    def test_the_note_and_the_judge_rule_are_both_threaded_from_the_workflow(self):
        """Computed and then dropped on the floor is the classic shape of this bug."""
        src = open("workflows.py").read()
        self.assertEqual(src.count('"grounding": self._grounding'), 4,
                         "grounding must reach run_capability, verify_capability, the "
                         "resumed-turn run_capability AND the brainstorm turn — a resume is the "
                         "path most likely to be sitting on a stale branch, and a brainstorm "
                         "that reasons about a file the tree doesn't have is worse than a task "
                         "that does: nothing downstream judges it")
        acts = open("activities.py").read()
        self.assertIn('grounding=payload.get("grounding")', acts)
        self.assertEqual(acts.count('grounding=payload.get("grounding")'), 2)


class RepoNameBoundaryTests(unittest.TestCase):
    """`candidate_repo` decides repo-mode for the general worker with no LLM in the loop, so a
    mis-read here silently disables isolation. Underscore had to join the boundary class: this
    codebase is full of `infra_*` / `webapp_*` identifiers."""

    NAMES = ["aws-cost-report", "webapp", "infra", "ci", "productivity-tracker"]

    def test_an_identifier_is_not_a_repo_mention(self):
        """`web-09c964f7`: "the infra_stop_weights_agent build in ... (ci#106)" named TWO
        repos, went ambiguous, returned None — so a write run got no clone, and with every
        registered checkout write-denied it had nowhere to work at all."""
        r = "fix the infra_stop_weights_agent build in acme-corp/ci PR #106"
        self.assertEqual(intents.candidate_repo(r, self.NAMES), "ci")

    def test_a_real_mention_still_matches(self):
        self.assertEqual(intents.candidate_repo("fix a bug in ci", self.NAMES), "ci")

    def test_two_real_repos_are_still_ambiguous(self):
        self.assertIsNone(intents.candidate_repo("sync ci and webapp", self.NAMES))

    def test_the_boundary_is_shared_by_both_passes(self):
        """The leading-segment pass had the same class — it must not regress separately."""
        self.assertIsNone(intents.candidate_repo("see productivity_tracker_v2 notes", ["webapp"]))


class PreviewModelTierTests(unittest.TestCase):
    """"Which model writes the plan I approve" was not a setting — the preview took the EXECUTION
    model, so an operator who set the "plan" tier to Opus got a stronger swarm decomposer and a
    preview that never read it. With execution on a LOCAL model it fell through to
    `_default_claude` (deliberately the CHEAPEST tier) and the gate showed a Haiku-written plan."""

    def _cfg(self, execution, preview=None):
        cfg = {"pool": [{"name": "claude-opus", "provider": "claude", "model": "claude-opus-4-8"},
                        {"name": "claude-sonnet", "provider": "claude", "model": "claude-sonnet-5"},
                        {"name": "claude-haiku", "provider": "claude",
                         "model": "claude-haiku-4-5-20251001"},
                        {"name": "ds", "provider": "openai", "model": "deepseek-v4-flash"}],
               "assign": {"execution": execution, "preview": preview or "claude-sonnet"}}
        return cfg

    def test_the_preview_tier_is_what_decides(self):
        self.assertEqual(
            gateway.preview_model_id(self._cfg("ds", preview="claude-opus")), "claude-opus-4-8")

    def test_it_does_not_follow_the_execution_assignment(self):
        """The bug: setting execution changed which model wrote the plan, as a side effect."""
        self.assertEqual(
            gateway.preview_model_id(self._cfg("claude-haiku", preview="claude-opus")),
            "claude-opus-4-8")

    def test_a_local_preview_model_degrades_to_sonnet(self):
        """Plan mode cannot run a local model at all, so it must fall back."""
        self.assertEqual(gateway.preview_model_id(self._cfg("ds", preview="ds")),
                         "claude-sonnet-5")

    def test_no_fallback_ever_lands_on_haiku(self):
        """Two mistakes bracket this: 'first pool entry' meant OPUS by default (every run on a
        local model silently billed as opus), and correcting it to CHEAPEST made haiku the silent
        answer everywhere a local model couldn't serve — including the preview, which has no
        verify ladder above it to escalate a weak result."""
        self.assertEqual(gateway._default_claude(self._cfg("ds")), "claude-sonnet-5")
        self.assertEqual(gateway.preview_model_id(self._cfg("ds", preview="ds")),
                         "claude-sonnet-5")

    def test_the_soft_budget_downshift_is_NOT_a_fallback(self):
        """`downshift_model_id` is a deliberate spend lever a run opts into by crossing its soft
        budget, not a silent substitution — cheapest is its whole purpose, so it keeps haiku."""
        self.assertEqual(gateway.downshift_model_id(self._cfg("ds")),
                         "claude-haiku-4-5-20251001")

    def test_preview_is_a_real_tier_and_backfills(self):
        self.assertIn("preview", gateway.TASKS)
        self.assertIn("plan", gateway.TASKS)     # the swarm planner keeps its own tier

    def test_plan_preview_reads_the_preview_tier(self):
        src = open("plans.py").read()
        self.assertIn("gateway.preview_model_id()", src)
        self.assertNotIn("gateway.exec_model_id(cap.name), None", src)

    def test_the_store_never_keeps_a_local_preview_assignment(self):
        """The degradation was correct but INVISIBLE: `preview_model_id` substituted sonnet while
        the store, `/api/models` and the Admin radio all went on naming the local model, so an
        operator watched every plan come back sonnet-shaped with qwen ticked (user-observed).
        `_normalize` repoints it, so the recorded setting matches what actually runs."""
        cfg = gateway._normalize({"pool": self._cfg("ds")["pool"],
                                  "assign": {**{t: "claude-sonnet" for t in gateway.TASKS},
                                             "preview": "ds"}})
        self.assertEqual(cfg["assign"]["preview"], "claude-sonnet")
        self.assertEqual(gateway.preview_model_id(cfg), "claude-sonnet-5")

    def test_an_explicit_claude_preview_pick_is_left_alone(self):
        """The repoint must only touch an assignment that could never have run."""
        cfg = gateway._normalize({"pool": self._cfg("ds")["pool"],
                                  "assign": {**{t: "claude-sonnet" for t in gateway.TASKS},
                                             "preview": "claude-opus"}})
        self.assertEqual(cfg["assign"]["preview"], "claude-opus")

    def test_the_admin_radio_refuses_a_local_preview_pick(self):
        """The store repoint is the authority, but a tickable control that silently does nothing
        is its own bug — the operator ticks qwen, the radio moves, and sonnet writes the plan."""
        ui = open("web/index.html", "rb").read().decode("utf-8")
        i = ui.index("const radio=(p,phase)=>")
        block = ui[i:i + 1600]
        self.assertIn('phase==="preview"', block)
        self.assertIn("dis=' disabled'", block)
        self.assertIn("${dis}", block)

    def test_the_gate_is_told_who_wrote_the_plan(self):
        """Approving a plan without being told its author is how the downgrade went unnoticed."""
        self.assertIn('"model": preview.get("model")', open("activities.py").read())
        self.assertIn('"plan_model": self._plan_model', open("workflows.py").read())
        # server._wf_state's gate block is a WHITELIST — absent here, the browser never sees it.
        self.assertIn('"plan_model": st.get("plan_model")', open("server.py").read())
        ui = open("web/index.html", "rb").read().decode("utf-8")
        self.assertIn("written by", ui)



class RepoUrlRegistrationTests(unittest.TestCase):
    """Registering a project repo by its REMOTE URL (the local checkout is optional).

    A repo is identified by its URL; everything downstream of registration still needs a
    directory, so `registry.project_path` resolves one — the operator's checkout when they
    registered it, Otto's managed clone (`data/repos/<slug>`) otherwise. Every consumer
    (`project_skills`, `conventions`, `file_safety`, `workspace`) reads that effective path
    and needed no change, which is exactly what these tests pin.

    No network: the clone is exercised against a LOCAL git repo used as the remote, so the
    `git clone` path is really run rather than stubbed out.
    """

    def setUp(self):
        self._o_proj = registry.PROJECTS_FILE
        self._o_data = config.DATA_DIR
        self._o_managed = repos.MANAGED
        self._d = tempfile.mkdtemp(prefix="otto-repourl-")
        self.addCleanup(shutil.rmtree, self._d, True)
        config.DATA_DIR = self._d
        repos.MANAGED = os.path.join(self._d, "repos")
        registry.PROJECTS_FILE = os.path.join(self._d, "projects.json")

    def tearDown(self):
        registry.PROJECTS_FILE = self._o_proj
        config.DATA_DIR = self._o_data
        repos.MANAGED = self._o_managed

    def _origin(self, name="widget"):
        """A real git repo on disk, used as the remote a managed clone is cut from."""
        src = os.path.join(self._d, "origin", name)
        os.makedirs(os.path.join(src, ".claude", "skills", "poke"))
        with open(os.path.join(src, ".claude", "skills", "poke", "SKILL.md"), "w") as f:
            f.write("---\nname: poke\ndescription: pokes things\n---\nbody\n")
        with open(os.path.join(src, "CLAUDE.md"), "w") as f:
            f.write("# rules\n- never poke prod\n")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
            subprocess.run(["git", "-C", src, *args], check=True, capture_output=True)
        return src

    # --- URL parsing / identity ------------------------------------------------------------

    def test_ssh_and_https_spellings_normalize_to_one_identity(self):
        """Two spellings of one repo must not register as two rows — the whole point of making
        the URL the identity. `git@` and a `.git`-less https URL both mean the same repo."""
        a = repos.parse("git@github.com:owner/repo.git")
        b = repos.parse("https://github.com/owner/repo")
        self.assertEqual(a["url"], b["url"])
        self.assertEqual(a["url"], "https://github.com/owner/repo.git")
        self.assertEqual(a["slug"], "repo")

    def test_non_urls_and_owner_less_urls_are_refused(self):
        for bad in ("", "notaurl", "/home/me/repositories/infra", "https://github.com/owner"):
            self.assertIsNone(repos.parse(bad), bad)

    def test_managed_slug_leads_with_the_repo_name(self):
        """`project_namespace` and the `<repo>:<cap>` catalogue prefix are both the path
        basename, so an `owner--name` directory would silently rename every project capability
        and orphan that project's memory namespace."""
        p = repos.managed_path("https://gitlab.com/group/sub/thing.git")
        self.assertEqual(os.path.basename(p), "thing")
        self.assertEqual(registry.project_namespace(p), "thing")

    def test_managed_path_touches_neither_disk_nor_subprocess(self):
        """`registry.projects()` resolves through this, and `file_safety` calls `projects()` on
        EVERY `claude -p` invocation — a `git remote get-url` in here is a subprocess per repo
        per run. Also why the path must resolve the same before and after the clone lands."""
        calls = []
        orig = repos._run
        repos._run = lambda *a, **k: (calls.append(a) or (0, "", ""))
        self.addCleanup(setattr, repos, "_run", orig)
        before = repos.managed_path("https://github.com/o/widget.git")
        os.makedirs(os.path.join(repos.MANAGED, "widget", ".git"))
        self.assertEqual(repos.managed_path("https://github.com/o/widget.git"), before)
        self.assertEqual(calls, [])

    def test_a_name_collision_from_another_owner_is_refused_not_adopted(self):
        """Handing back the existing `infra` for someone else's `infra` would be undetectable
        downstream: the capability prefix and the memory namespace are both that basename."""
        path = os.path.join(repos.MANAGED, "widget")
        os.makedirs(os.path.join(path, ".git"))
        orig = repos.origin_of
        repos.origin_of = lambda p: "https://github.com/SOMEONE-ELSE/widget.git"
        self.addCleanup(setattr, repos, "origin_of", orig)
        got, err = repos.ensure("https://github.com/o/widget.git")
        self.assertIsNone(got)
        self.assertIn("already registered", err)

    def test_gitlab_and_other_hosts_are_accepted(self):
        self.assertEqual(repos.parse("https://gitlab.com/g/p")["host"], "gitlab.com")
        self.assertEqual(repos.parse("ssh://git@git.example.io/g/p.git")["host"], "git.example.io")

    # --- clone + registration --------------------------------------------------------------

    def test_url_only_registration_clones_and_imports_capabilities(self):
        """The end-to-end shape: a URL registers, `ensure` really runs `git clone`, and every
        consumer downstream reads the managed clone as if it were any other project root."""
        url = "https://github.com/o/widget.git"
        src = self._origin()
        orig = repos._run
        # Only the network hop is substituted — the clone, the working tree and everything that
        # reads it are real. `--depth 1` is asserted on the argv the caller actually built.
        def local_clone(args, cwd=None, timeout=600):
            if args[:2] == ["git", "-C"] or "clone" not in args:
                return orig(args, cwd=cwd, timeout=timeout)
            self.assertIn("--depth", args)
            return orig([a if a != url else src for a in args], cwd=cwd, timeout=timeout)
        repos._run = local_clone
        self.addCleanup(setattr, repos, "_run", orig)

        path, err = repos.ensure(url)
        self.assertIsNone(err)
        self.assertTrue(os.path.isdir(os.path.join(path, ".git")))
        root = registry.add_project("", url)
        self.assertEqual(root, path)
        self.assertTrue(repos.is_managed(root))
        self.assertEqual(os.path.basename(root), "widget")
        self.assertEqual([c[1] for c in registry.project_skills()], ["widget:poke"])
        self.assertEqual([c[2] for c in registry.project_skills()], ["poke"])   # invoked bare
        self.assertEqual(registry.project_meta(root)["url"], url)
        self.assertIn("never poke prod", conventions._read_sources(root)[0])

    def test_registered_checkout_wins_over_the_managed_clone(self):
        """The operator's own tree is a full checkout that already exists; re-cloning it buys
        nothing and would leave two copies drifting apart."""
        src = self._origin()
        registry.add_project(src, "https://github.com/o/widget.git")
        self.assertEqual(registry.projects(), [src])
        self.assertFalse(repos.is_managed(registry.projects()[0]))
        self.assertEqual(registry.project_meta(src)["checkout"], src)

    def test_missing_checkout_falls_back_to_the_managed_path(self):
        """A checkout registered on another machine (or since deleted) must not dead-end the
        entry — the URL is the identity, so the managed clone stands in."""
        registry.add_project("/no/such/checkout", "https://github.com/o/widget.git")
        self.assertEqual(registry.projects(), [os.path.join(repos.MANAGED, "widget")])

    def test_url_identity_updates_the_row_instead_of_forking_one(self):
        registry.add_project("", "git@github.com:o/widget.git")
        registry.set_project_instructions(os.path.join(repos.MANAGED, "widget"), "tag team=sre")
        registry.add_project("/some/checkout", "https://github.com/o/widget")   # same repo
        self.assertEqual(len(registry._project_entries()), 1)
        self.assertEqual(registry._project_entries()[0]["path"], "/some/checkout")
        self.assertEqual(registry._project_entries()[0]["instructions"], "tag team=sre")

    def test_ensure_leaves_nothing_behind_when_the_clone_fails(self):
        """A half-written directory that later reads as `registered` is worse than no clone —
        `project_skills` would import nothing and the failure would look like an empty repo."""
        path, err = repos.ensure("https://github.invalid/o/nope.git")
        self.assertIsNone(path)
        self.assertTrue(err)
        self.assertFalse(os.path.isdir(os.path.join(repos.MANAGED, "nope")))

    # --- back-compat -----------------------------------------------------------------------

    def test_legacy_path_only_entries_still_resolve(self):
        """Both older store formats — a bare string list and `{path, instructions}` — keep
        working untouched, with no URL and no clone."""
        with open(registry.PROJECTS_FILE, "w") as f:
            json.dump(["/repos/infra", {"path": "/repos/webapp", "instructions": "be careful"}], f)
        self.assertEqual(registry.projects(), ["/repos/infra", "/repos/webapp"])
        self.assertEqual(registry.project_meta("/repos/webapp")["instructions"], "be careful")
        self.assertEqual(registry.project_url("/repos/infra"), "")

    def test_backfill_derives_the_url_from_a_checkouts_origin(self):
        src = self._origin()
        subprocess.run(["git", "-C", src, "remote", "add", "origin",
                        "git@github.com:o/widget.git"], check=True, capture_output=True)
        registry.save_projects([{"path": src, "instructions": ""}])
        self.assertTrue(registry.backfill_project_urls())
        self.assertEqual(registry.project_url(src), "https://github.com/o/widget.git")
        self.assertFalse(registry.backfill_project_urls())     # idempotent — one pass, then quiet

    def test_remove_discards_the_managed_clone_but_never_a_checkout(self):
        src = self._origin()
        os.makedirs(os.path.join(repos.MANAGED, "widget", ".git"))
        registry.save_projects([{"url": "https://github.com/o/widget.git", "path": "",
                                 "instructions": ""},
                                {"url": "https://github.com/o/other.git", "path": src,
                                 "instructions": ""}])
        registry.remove_project(os.path.join(repos.MANAGED, "widget"))
        self.assertFalse(os.path.isdir(os.path.join(repos.MANAGED, "widget")))
        registry.remove_project(src)
        self.assertTrue(os.path.isdir(src))                    # the user's tree is never deleted
        self.assertEqual(registry.projects(), [])

    # --- downstream consumers --------------------------------------------------------------

    def test_managed_clone_is_write_denied_and_readable_from_its_own_cwd(self):
        """It is a registered repo, so `file_safety` write-denies it like any other — nothing
        may edit it in place, since `workspace.provision` cuts every run's clone FROM it. The
        `allow_cwd` exemption still lets a project capability work in its own repo."""
        root = os.path.join(repos.MANAGED, "widget")
        os.makedirs(os.path.join(root, ".git"))
        registry.save_projects([{"url": "https://github.com/o/widget.git", "path": "",
                                 "instructions": ""}])
        self.assertTrue(any(root in g for g in file_safety.denied_globs()))
        self.assertFalse(any(root in g for g in file_safety.denied_globs(allow_cwd=root)))
        self.assertFalse(file_safety.is_read_denied(os.path.join(root, "main.tf"), allow_cwd=root))
        self.assertTrue(file_safety.is_read_denied(os.path.join(self._d, "otto.db"),
                                                   allow_cwd=root))

    def test_refresh_updates_a_managed_clones_TREE_not_just_its_refs(self):
        """A fetch alone leaves the working TREE at whatever the clone landed on — and its
        working tree is the entire reason the managed clone exists (`.claude/`, CLAUDE.md).
        Without the reset, a repo registered by URL serves the conventions and capabilities it
        had on the day it was cloned, forever."""
        src = self._origin()
        clone = os.path.join(repos.MANAGED, "widget")
        os.makedirs(repos.MANAGED)
        subprocess.run(["git", "clone", "-q", "--depth", "1", src, clone],
                       check=True, capture_output=True)
        with open(os.path.join(src, "CLAUDE.md"), "w") as f:
            f.write("# rules\n- never poke staging either\n")
        subprocess.run(["git", "-C", src, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qam", "new rule"], check=True, capture_output=True)

        self.assertTrue(repos.refresh(clone, "main"))
        with open(os.path.join(clone, "CLAUDE.md")) as f:
            self.assertIn("never poke staging", f.read())

    def test_refresh_never_touches_the_operators_own_checkout(self):
        """The same hard reset run against a user's live repo destroys their uncommitted work.
        `is_managed` is what keeps `refresh` off it — `workspace.refresh_repos` sends a
        non-managed repo down the fetch-only path instead."""
        src = self._origin()
        with open(os.path.join(src, "CLAUDE.md"), "a") as f:
            f.write("- work in progress, not committed\n")
        subprocess.run(["git", "-C", src, "remote", "add", "origin", src],
                       check=True, capture_output=True)

        self.assertFalse(repos.is_managed(src))
        self.assertFalse(repos.is_managed(repos.MANAGED))       # the container is not a clone
        self.assertTrue(repos.is_managed(os.path.join(repos.MANAGED, "widget")))
        self.assertFalse(repos.refresh(src, "main"))
        with open(os.path.join(src, "CLAUDE.md")) as f:
            self.assertIn("not committed", f.read())            # the reset never ran
        with open("workspace.py") as f:
            self.assertIn('repos.is_managed(r["path"])', f.read())   # refresh_repos routes on it

    def test_the_url_backfill_is_not_an_import_side_effect(self):
        """It WRITES `data/projects.json`, and importing `server` is what the suite and every
        tool does BEFORE `setUpModule` re-points PROJECTS_FILE at a temp dir. At import scope it
        migrated the live store just by being imported."""
        with open("server.py") as f:
            src = f.read()
        head, _, tail = src.partition("def main():")
        self.assertNotIn("backfill_project_urls()", head)
        self.assertIn("backfill_project_urls()", tail)
