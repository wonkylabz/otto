"""Otto config."""
import os
import re
import shlex
import subprocess

# Model used for routing (Router #1's decision, on the cloud tier).
ROUTER_MODEL = os.environ.get("OTTO_ROUTER_MODEL", "claude-sonnet-4-6")

# The operator Otto runs on behalf of — used in Slack framing/prompts that need to name them
# to a third party (e.g. "Sam isn't available, I'm their assistant"). Set OTTO_OWNER_NAME to
# your own name; the generic default reads correctly but says nothing useful to a colleague.
OWNER_NAME = os.environ.get("OTTO_OWNER_NAME", "the operator")

# Per-risk tool allowlists handed to `claude -p --allowedTools`.
# This is the concrete guardrail: a read capability literally cannot Edit/Write.
#   NOTE: Bash is broad here for the prototype - it's the leaky boundary. Real
#   Otto would scope it (Bash(gh:*), Bash(kubectl:*) ...) per capability.
READ_TOOLS = ["Bash", "Read", "Grep", "Glob", "WebFetch", "WebSearch"]
WRITE_TOOLS = READ_TOOLS + ["Edit", "Write"]

# Tools for the pre-approval PLAN pass (engine.plan_preview). Strictly read-only
# INTROSPECTION — no unscoped Bash, Edit, or Write — so producing the "here's what I'll
# do" preview can never mutate state before the human has approved. (Broad Bash is excluded
# even though it's in READ_TOOLS: a plan that runs `git push`/`gh pr create` would defeat
# the gate.) The scoped gh reads are the exception: a ticket-driven task can't be planned
# without reading the ticket (the planner otherwise emits "I could not read issue #N" +
# guesswork), and `gh issue/pr view|diff` cannot mutate anything.
#   The preview MUST run under `--permission-mode plan` (engine.plan_preview) for these to
#   work: a SCOPED Bash allow trips Claude Code's command classifier, which flags NETWORK
#   commands (gh hits GitHub) as needing interactive approval a headless `claude -p` can't give
#   ("This command requires approval") — flaky, and the cause of the "can't read tickets" bug.
#   Plan mode consistently permits read-only network commands and forbids all mutations.
PLAN_TOOLS = ["Read", "Grep", "Glob",
              "Bash(gh issue view:*)", "Bash(gh pr view:*)", "Bash(gh pr diff:*)"]

# Every built-in Claude Code tool Otto may ever need. `--allowedTools` grants PERMISSION but
# unloads nothing — the full built-in set plus every skill/agent listing sits in the system
# prompt of every turn, re-read on each one. `--disallowedTools` on the complement of this
# set is what actually removes them (measured: 45.4k -> 37.2k tokens per turn).
#   Task/Skill dispatch agent- and skill-kind caps; ToolSearch is how DEFERRED MCP tool
#   schemas get loaded, so dropping it silently severs every MCP server. The plan-mode pair
#   is kept because `--permission-mode plan` needs it.
KEEP_TOOLS = sorted(set(WRITE_TOOLS) | {"Task", "Skill", "ToolSearch",
                                        "EnterPlanMode", "ExitPlanMode"})

# The built-in tools observed in a `claude -p` session (init event's `tools`, minus `mcp__*`).
# A name this list has never heard of is NOT trimmed — and on the tool-free tiers below, untrimmed
# means still CALLABLE, not merely unbudgeted. `ListAgents` (confirmed present in a live init
# event, 2026-08-13) was missing, so the cheap tiers' "pure text judgement, never calls a tool"
# premise was not actually enforced for it: a judge could enumerate the operator's other Claude
# sessions. Staleness costs tokens on an execution turn but correctness on a judge turn — re-read
# the init event when Claude Code updates rather than copying names from an interactive session,
# which exposes a different set.
_BUILTIN_TOOLS = [
    "Artifact", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync", "Edit",
    "EnterPlanMode", "EnterWorktree", "ExitPlanMode", "ExitWorktree", "Glob", "Grep",
    "ListAgents", "ListMcpResourcesTool", "LSP", "Monitor", "NotebookEdit", "PushNotification",
    "Read", "ReadMcpResourceDirTool", "ReadMcpResourceTool", "RemoteTrigger", "ReportFindings",
    "ScheduleWakeup", "SendMessage", "ShareOnboardingGuide", "Skill", "Task", "TaskCreate",
    "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate", "ToolSearch", "WebFetch",
    "WebSearch", "Workflow", "Write",
]

# Handed to `--disallowedTools` on every execution turn.
DISALLOWED_TOOLS = [t for t in _BUILTIN_TOOLS if t not in KEEP_TOOLS]

# Handed to the cheap tiers (routing/clarify/verify/memory — `gateway._claude_complete`),
# which are pure text completions that never call a tool yet still paid the full tool+skill
# preamble on every call. Measured: 45.4k -> 9.3k tokens per call.
ALL_BUILTIN_TOOLS = list(_BUILTIN_TOOLS)

# How hard the model thinks before answering. `claude -p --effort <low|medium|high|xhigh|max>`
# is a first-class CLI flag; on the LOCAL backend the analogue is the OpenAI-compatible
# `reasoning_effort` body field, which a server that does not implement it ACCEPTS AND IGNORES
# (measured against vLLM: no 400, no observable change) — so this is a real lever on Claude and
# strictly advisory on local. "default" means pass nothing and let each backend pick, which is
# why it is a sentinel rather than a level: there is no neutral level to name.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT = os.environ.get("OTTO_EFFORT", "default").strip().lower() or "default"


def effort_level(value):
    """Normalize an effort value to a level `--effort` accepts, or None for "leave it alone".

    The ONE normalizer, called at every leaf that puts effort on a wire, because the CLI does not
    fail on a bad value — it prints `Warning: Unknown --effort value 'x' — ignoring it` to stderr
    and runs at the default. So an unvalidated string does not break a run, it silently produces a
    run at the wrong effort that reports itself as having honoured the setting."""
    v = str(value or "").strip().lower()
    return v if v in EFFORT_LEVELS else None


def resolve_effort(pick, default):
    """Precedence for one run: the per-chat pick, else the Admin default, else the sentinel.

    A module-level PURE function on purpose. It is called from workflow code, where a `x or y`
    would be one more branch in `_run_impl` — whose branch count is a DOWN-only ratchet
    (`test_pipeline.WorkflowComplexityTests`) — and where precedence spread across two ingresses
    and a snapshot read is exactly the kind of decision that should have one home."""
    return effort_level(pick) or effort_level(default) or "default"


# Verify -> retry loop: how many execution attempts before we give up. The final
# attempt escalates the execution model (gateway.escalation_model_id) one last time.
MAX_VERIFY_ATTEMPTS = int(os.environ.get("OTTO_MAX_ATTEMPTS", "3"))
# Extra attempts granted for HARNESS deaths only (timeout, worker crash, activity failure).
# These never reached a judge, so they must not spend a rung of MAX_VERIFY_ATTEMPTS — but they
# still need a ceiling, or a capability that times out every time loops forever.
MAX_HARNESS_RETRIES = int(os.environ.get("OTTO_MAX_HARNESS_RETRIES", "2"))

# Follow-up handoff: a resumed follow-up that DELEGATES a new task (accepting a task the
# capability OFFERED — "yes, work on that") is re-issued as a FRESH, normally-routed run
# instead of running inside the bound session, where the wrong cap would keep the work and
# repo-mode/verify/review never engage. Classification is biased hard toward staying in the
# session (the mid-task question->answer flow must never break on a misfire). Kill switch:
FOLLOWUP_HANDOFF = os.environ.get("OTTO_FOLLOWUP_HANDOFF", "1") != "0"

# Safe local write execution (issue #172): a WRITE-risk cap MAY run on a local backend, but
# the ladder must escalate it off local to Claude once a local attempt FAILS verify — local
# runs are otherwise local-only (escalation/downshift stay local), so a weak local model on a
# hard write would just dead-end or ship a shallow-but-plausible PR instead of escalating. This
# is safe-escalation, NOT a ban: a capable local model that PASSES verify still executes the
# write locally (the cost win is preserved). The reason string tags the resulting local->Claude
# fallback in the audit trail / UI (distinct from the tool-incapable local_disabled reason).
WRITE_LOCAL_ESCALATE_REASON = ("write capability failed verification on the local backend — "
                               "escalating the rest of the ladder to Claude (issue #172)")

# Claude-fallback switch. ON by default = every behaviour described above and below: when a model
# assigned to a LOCAL pool entry can't do the job, Otto continues on Claude so the work still
# lands. Set OTTO_LOCAL_FALLBACK=0 to make that substitution ILLEGAL — the local failure becomes
# the run's terminal outcome, spelled out in the result/audit/board instead of being masked by a
# silent Claude run. That masking is the whole problem this flag solves: with fallback on, a dead
# local endpoint looks like a series of successful runs, and the only trace is a fallback badge
# nobody reads — so you cannot tell whether local is carrying the work (the cost north-star) or
# quietly bankrolling Claude.
#
# Strict mode covers every local-eligible tier AND both execution backends (tool-free completion +
# local_runtime), and it also keeps a verify-FAILED local run LOCAL: no escalation to Claude, so a
# weak local model retries locally and lands in needs-human rather than being rescued.
#
# EXEMPT: the `verify` tier (LOCAL_FALLBACK_EXEMPT_TIERS). The judge is what catches a bad local
# execution, so a dead local endpoint must not take IT down too — a run would then hard-stop on the
# judge instead of on the thing you actually wanted to see fail, and every verdict would be
# unavailable exactly when strict mode is most interesting.
LOCAL_FALLBACK = os.environ.get("OTTO_LOCAL_FALLBACK", "1").lower() not in ("0", "false", "no", "off")
LOCAL_FALLBACK_EXEMPT_TIERS = ("verify",)

# needs_human reason + audit tag for a strict-mode stop (mirrors "verify_exhausted"/"budget_exceeded").
STRICT_STOP_REASON = "local_fallback_disabled"
# Claude itself rejected our credentials (`claude -p` could not authenticate). Its own terminal
# reason, NOT harness_exhausted: nothing timed out and no worker crashed — the subscription
# session expired, and unlike every other needs-human state this one has a known one-command fix.
AUTH_STOP_REASON = "claude_auth_expired"


def local_fallback_allowed(task=None):
    """May a failed/unavailable LOCAL call continue on Claude? Always in the default mode; in
    strict mode only for the exempt `verify` tier. `task` is a gateway tier name, or None for an
    execution-backend call (never exempt)."""
    return setting("local_fallback") or task in LOCAL_FALLBACK_EXEMPT_TIERS


# --- "say nothing" sentinel (conversational audiences only) ----------------
# Every run must produce text, and for a conversational audience that text is POSTED. So a message
# with nothing in it to answer — an acknowledgment, a reaction, an aside — used to be replied to
# with the model's own meta-commentary about it: on 2026-07-31 a colleague got *'"Dammit" isn't a
# request — there's nothing to act on here'* and *'That message isn't a self-contained request'* as
# Slack replies. The model identifies these correctly; it just had no way to say "send nothing".
# It now answers with this exact sentinel and delivery stays silent. Lives here because engine
# (contract + judge) and delivery (the sink) both need it and neither imports the other.
NO_REPLY = "NO_REPLY"

# The wrappers a model reaches for around a bare token — fence, quotes, bold/italic, end punctuation.
_NO_REPLY_TRIM = " \t\r\n`*_\"'.!"


def is_no_reply(text):
    """Whether a conversational result means "post nothing". The WHOLE output must reduce to the
    sentinel — a mention of it inside a real reply is just prose. PURE.

    Strict on purpose, and note the direction: a false positive silently swallows an answer someone
    is waiting for, while a miss merely posts a reply we would rather have skipped. So this tolerates
    the wrappers above and nothing else — no "NO_REPLY (nothing to add)", no leading prose."""
    s = (text or "").strip()
    if s.startswith("```") and s.endswith("```"):
        s = s[3:-3]
    return s.strip(_NO_REPLY_TRIM).upper() == NO_REPLY


def strict_stop_message(model, what, task=None):
    """The user-facing body for a strict-mode stop. Deliberately loud and self-explaining: this
    string IS the delivered result, so it has to answer "why did nothing happen?" on its own —
    what failed, that no Claude run silently substituted, and the three ways out."""
    where = f"{task} tier" if task else "execution"
    return ("⛔ **STOPPED — local model failed and Claude fallback is disabled** "
            "(`OTTO_LOCAL_FALLBACK=0`)\n\n"
            f"- **stage:** {where}\n"
            f"- **local model:** {model}\n"
            f"- **failure:** {what}\n"
            "- **nothing ran on Claude** — this is the local failure itself, not a degraded "
            "substitute, and no tokens were spent covering for it.\n\n"
            "Fix: bring the local endpoint back (Admin health pills / `python3 doctor.py`), "
            "reassign this tier or capability to a Claude model in Admin, or turn the fallback back "
            "on (Admin → Runtime settings, or `OTTO_LOCAL_FALLBACK=1`).")

# Post-PR QA loop (opt-in, repo-mode only). After a draft PR is opened, run the QA
# capability against it; on a FAIL, fold its findings back into a fix on the SAME branch
# and re-QA. MAX_QA_ROUNDS bounds the FIX rounds (so the QA cap runs at most
# MAX_QA_ROUNDS + 1 times). A still-FAIL after the budget — or any INCONCLUSIVE — stops
# and surfaces for a human (PR left draft). Enabling the loop IS the authorization for the
# (write-capable, dev/staging-only) QA cap to run unattended through the loop.
MAX_QA_ROUNDS = int(os.environ.get("OTTO_MAX_QA_ROUNDS", "2"))
# Defaults to the stock `qa-tester` cap bundled in `capabilities/` so the loop is self-contained
# (no dependency on a user ~/.claude/agents/sre-qa.md). Being a stock cap, it runs on the gateway
# exec tier — set a Claude `cap_exec` for qa-tester if the exec phase is a local model.
QA_CAP = os.environ.get("OTTO_QA_CAP", "qa-tester")

# Post-PR code-review loop. After a draft PR is opened, run the review capability
# (the stock code-reviewer) against it as a strict reviewer; on actionable must-fix/should-fix
# findings, fold them into a fix on the SAME branch and re-review. MAX_REVIEW_ROUNDS bounds
# the FIX rounds (the reviewer runs at most MAX_REVIEW_ROUNDS + 1 times). A still-CHANGES
# after the budget — or an INCONCLUSIVE review — stops for a human (PR left draft). This is
# the platform equivalent of sre-minion's Phase 5-7 review→fix→recheck loop. DEFAULT-ON for
# the general worker (config.WORKER_CAP); opt-in (params["review"]) for other caps. Reads the
# PR via `gh` (read-only reviewer); enabling it authorizes the same-branch fix runs.
MAX_REVIEW_ROUNDS = int(os.environ.get("OTTO_MAX_REVIEW_ROUNDS", "3"))
# Default is the BUNDLED stock cap so a fresh install's review loop is self-contained
# (github-pr-review was a user ~/.claude skill — unresolvable on any other machine).
REVIEW_CAP = os.environ.get("OTTO_REVIEW_CAP", "code-reviewer")

# Memory garbage collection (on-demand, Admin -> Memory tab "Run GC" button only, never scheduled):
# a cheap classifier pass batches stored facts/solutions/rules and flags each KEEP/STALE/VERIFY;
# MEMORY_GC_BATCH_SIZE bounds how many items go in one classifier call. Items flagged VERIFY assert
# a CURRENT-STATE claim the classifier can't judge from text alone, so each gets a real `claude -p`
# tool-verification turn — MEMORY_GC_MAX_VERIFY caps how many of those run per pass (each is an
# actual billed turn, so this is a cost ceiling, not a coverage guarantee: excess VERIFY items are
# skipped this round, reported as `verify_skipped`, and picked up on the next run).
MEMORY_GC_BATCH_SIZE = int(os.environ.get("OTTO_MEMORY_GC_BATCH_SIZE", "25"))
MEMORY_GC_MAX_VERIFY = int(os.environ.get("OTTO_MEMORY_GC_MAX_VERIFY", "15"))

# Plan-preview revision rounds at the approval gate (OttoWorkflow.revise_plan signal): instead of
# only approve/decline, the human can send free-text feedback ("only touch dev, not prod") that
# gets folded into the request and re-previewed — a fresh plan + critique, re-shown at the same
# gate. MAX_PLAN_REVISIONS bounds how many times this can happen before the gate only accepts a
# decision (further feedback signals are dropped). 0 disables the affordance entirely.
MAX_PLAN_REVISIONS = int(os.environ.get("OTTO_MAX_PLAN_REVISIONS", "3"))

# How long the approval gate waits for a human before giving up, in hours. The gate used to wait
# forever, which is only safe when the approval card and the requester are the same screen. They
# are not: an ingress with no gate UI of its own (Slack, chiefly) trips the write gate, pushes one
# notification, and then parks a workflow nobody can see — 7 of 52 Slack runs died exactly that
# way, the person who asked getting silence rather than an answer or a refusal. On expiry the run
# is NOT approved (that would make a timeout a privilege escalation): it declines itself, writes
# its terminal row and says so at the reply target. 0 restores the unbounded wait.
GATE_TIMEOUT_H = float(os.environ.get("OTTO_GATE_TIMEOUT_H", "24"))

# How many times an ADVERSE judge verdict (verify FAIL, supervisor RETRY) must reproduce before it
# is acted on. `claude -p` exposes no temperature/top-p/seed — 65 flags, none for sampling — so a
# judge on the Claude backend is sampled and cannot be pinned the way the OpenAI-compatible path
# is (`temperature: 0`). Measured on one fixed, complete, CORRECT output: 12 PASS / 8 FAIL over 20
# judgements, while a clearly-bad output failed 0/5 — the instability sits on good results, which
# is where a wrong verdict is expensive. Measured A/B at 3: false FAIL 5/10 → 2/10 on that input,
# for 2.20 judge calls per verdict instead of 1.00. A PASS still returns on the first sample, so a
# run the judge likes costs one call. 1 restores the old single-sample behaviour.
JUDGE_CONFIRMATIONS = int(os.environ.get("OTTO_JUDGE_CONFIRMATIONS", "3"))
# Router #1 samples for a WRITE pick (1 = off). A write route arms the approval gate and the
# Opus plan preview, so an unstable sample there costs money and a human decision; a read pick
# is ungated and stays at one call. See routing._confirm_route.
ROUTE_CONFIRMATIONS = int(os.environ.get("OTTO_ROUTE_CONFIRMATIONS", "3"))

# The built-in general worker capability (registry._general_worker). Named here so the
# workflow can default the review loop on for it without importing registry.
WORKER_CAP = "worker"

# The built-in brainstorm capability (registry._brainstorm). Named here for the same reason as
# WORKER_CAP: workflow code must recognize the mode — to pick its output contract and to skip the
# verify ladder — and workflows.py cannot import registry (filesystem I/O on a replayed path).
BRAINSTORM_CAP = "brainstorm"

# --- Plan-then-execute mode (local-executor decomposition) ---------------------------------
# A strong model (Claude) decomposes a big task into an ordered list of ATOMIC steps a weak
# LOCAL model can run one at a time (engine.plan_steps); the executor threads outputs forward
# by declared dependency, not a growing context blob. The verify ladder runs PER STEP (stays
# local); a step that exhausts it triggers a Claude RE-PLAN of the remaining tail (preserving
# completed work), bounded by PLAN_MAX_REPLANS; only then needs-human. Kept ALONGSIDE the swarm
# decompose and the single-turn path — gated by PLAN_MODE so nothing else changes.
#   off        - disabled (single-turn/swarm as today)
#   opt-in     - only when the run explicitly requests it (params["plan_mode"] / a /plan pin)
#   auto-local - also auto-engage when the resolved executor is a LOCAL model AND the planner
#                returns >=2 steps (invisible on a Claude executor or a small task)
PLAN_MODE = os.environ.get("OTTO_PLAN_MODE", "off")
# Granularity ceiling: cap plan length so a runaway planner can't emit an unbounded chain.
PLAN_MAX_STEPS = int(os.environ.get("OTTO_PLAN_MAX_STEPS", "12"))
# Plan-level re-plans (each is one strong Claude call) before we give up to needs-human.
PLAN_MAX_REPLANS = int(os.environ.get("OTTO_PLAN_MAX_REPLANS", "2"))
# Per-injected-artifact truncation: a prior step's output is truncated to this many chars when
# threaded into a step that declares it in `needs` (weak models have small windows — relevance
# beats completeness, so we inject only the needed outputs, each bounded).
# 1500 was under one realistic artifact: the inventory steps that plan-mode exists to fan out
# produce tables of 20+ rows, so the ONE output a later step was told to "use this" arrived cut
# mid-row. Relevance still beats completeness — that is what `needs` is for — but the bound has
# to clear a normal step's actual deliverable, and a cut is now marked (plans._clipped_input).
PLAN_ARTIFACT_CHARS = int(os.environ.get("OTTO_PLAN_ARTIFACT_CHARS", "6000"))
# Max plan steps run CONCURRENTLY. run_plan walks the toposort in dependency WAVES: every step
# whose `needs` are already satisfied runs together, up to this many at once. 1 = fully sequential
# (the old behaviour / escape hatch). Steps run in an activity thread pool, so this is IO-bound
# concurrency over blocking `claude -p`/local-runtime turns — no workflow determinism impact.
PLAN_MAX_PARALLEL = int(os.environ.get("OTTO_PLAN_MAX_PARALLEL", "3"))

# Reaper: a board card stuck in In-Progress whose workflow is RUNNING but older than this many
# hours is treated as stuck and moved to Blocked (needs-human). Backstops a run that hangs without
# ever failing. Dead workflows (FAILED/TERMINATED/…) are reaped immediately regardless.
STUCK_TTL_H = float(os.environ.get("OTTO_STUCK_TTL_H", "6"))
# How often the reaper sweep runs (Temporal Schedule interval, seconds).
REAPER_SECONDS = int(os.environ.get("OTTO_REAPER_SECONDS", "300"))
# How far back the reaper's GENERAL sweep (non-board workflows) looks. Bounds the first sweep
# after a deploy: without it, every TERMINATED/TIMED_OUT run from months past would flood
# needs-you at once (their in-workflow finalizer never wrote a terminal row).
REAP_WINDOW_H = float(os.environ.get("OTTO_REAP_WINDOW_H", "168"))

# Per-run cost/token budget (0 = disabled). Output tokens are the primary meter (the scarce
# subscription resource); USD is a secondary notional meter. At the SOFT threshold the run
# downshifts the execution model tier; at the HARD ceiling it stops and surfaces for a human.
BUDGET_SOFT_TOKENS = int(os.environ.get("OTTO_BUDGET_SOFT_TOKENS", "0"))
BUDGET_HARD_TOKENS = int(os.environ.get("OTTO_BUDGET_HARD_TOKENS", "0"))
BUDGET_SOFT_USD = float(os.environ.get("OTTO_BUDGET_SOFT_USD", "0"))
BUDGET_HARD_USD = float(os.environ.get("OTTO_BUDGET_HARD_USD", "0"))

def budget_exceeded(output_tokens, cost_usd, hard=True, snapshot=None):
    """True if a run's accumulated spend has crossed its budget. `hard`=True checks the stop
    ceiling; False checks the soft (downshift) threshold. A 0 knob means that meter is disabled.

    Pure given its inputs. Deterministic workflow code MUST pass `snapshot` (the per-run settings
    snapshot) rather than letting this read the live store — see the settings section below."""
    # `.get` with the code default, never `snapshot[k]`: an in-flight run's snapshot predates any
    # key added since it started, and a KeyError here poisons its history permanently.
    get = (lambda k: snapshot.get(k, SETTINGS_FALLBACK[k])) if snapshot else setting
    tok = get("budget_hard_tokens" if hard else "budget_soft_tokens")
    usd = get("budget_hard_usd" if hard else "budget_soft_usd")
    if tok and output_tokens >= tok:
        return True
    if usd and cost_usd >= usd:
        return True
    return False


# --- UI-editable runtime settings (Admin tab) -----------------------------------
#
# The knobs below are the "run an experiment today" ones, so they're editable from Admin without
# restarting server.py + worker.py. Everything else stays env-only: timeouts and retry backoffs are
# machine tuning, and secrets (OTTO_SLACK_USER_TOKEN, OTTO_EVENT_SECRET) must never live in a
# UI-writable file under data/ — the web UI has no auth (issue #123).
#
# Precedence is **env > store > code default**. Env winning is deliberate: `.env`/systemd stays the
# escape hatch, a headless deployment can pin behaviour the UI can't override, and an env-pinned
# knob shows as locked in Admin instead of silently ignoring your click.
#
# Read through `setting(name)` — NEVER cache the value in a module constant, because the whole
# point is that the OTHER process picks up an edit. The legacy constants below are retained as the
# code defaults (and as the seam every existing test monkeypatches).
#
# DETERMINISM: workflow code must never call setting() — a value that changes mid-run sends a
# replay down a different branch than history recorded. OttoWorkflow takes ONE snapshot through
# the snapshot_settings activity (recorded in history, so replay-safe) and reads that dict instead.
#   Resolved lazily: DATA_DIR is defined further down this file, and tests point this at a temp
#   path (like gateway._STATS_PATH) so the suite never reads the developer's real store.
_SETTINGS_PATH = None


def _settings_path():
    return _SETTINGS_PATH or os.path.join(DATA_DIR, "settings.json")

# name -> (env var, kind, code-default constant name). `kind` drives coercion + the Admin control.
# How much of a repo's conventions digest fits ONE judging prompt. Measured on this repo at
# the old 2_000: 20 of 224 rules reached the judge, and it failed a compliant result 4 times
# in 5 citing whatever ranked top instead of the violation actually present; at 20_000 the
# whole corpus fits and the judge was right 10 times out of 10 (`ConventionsDigestBudgetTests`).
# Sized for the corpus, NOT for the model: the judge tier here serves a 1M-token window, so
# this is well under a percent of it. Lower it for a genuinely small-context local judge —
# `select_rules` still ranks, so a smaller budget loses the least relevant rules first.
CONVENTIONS_DIGEST_CHARS = 24_000

_SETTING_SPECS = {
    "local_fallback":     ("OTTO_LOCAL_FALLBACK", "bool", "LOCAL_FALLBACK"),
    "max_attempts":       ("OTTO_MAX_ATTEMPTS", "int", "MAX_VERIFY_ATTEMPTS"),
    "plan_mode":          ("OTTO_PLAN_MODE", "choice:off,opt-in,auto-local", "PLAN_MODE"),
    "supervise":          ("OTTO_SUPERVISE", "bool", "SUPERVISE"),
    "supervise_mode":     ("OTTO_SUPERVISE_MODE", "choice:shadow,enforce", "SUPERVISE_MODE"),
    "budget_soft_tokens": ("OTTO_BUDGET_SOFT_TOKENS", "int", "BUDGET_SOFT_TOKENS"),
    "budget_hard_tokens": ("OTTO_BUDGET_HARD_TOKENS", "int", "BUDGET_HARD_TOKENS"),
    "budget_soft_usd":    ("OTTO_BUDGET_SOFT_USD", "float", "BUDGET_SOFT_USD"),
    "budget_hard_usd":    ("OTTO_BUDGET_HARD_USD", "float", "BUDGET_HARD_USD"),
    "max_qa_rounds":      ("OTTO_MAX_QA_ROUNDS", "int", "MAX_QA_ROUNDS"),
    "max_review_rounds":  ("OTTO_MAX_REVIEW_ROUNDS", "int", "MAX_REVIEW_ROUNDS"),
    "max_plan_revisions": ("OTTO_MAX_PLAN_REVISIONS", "int", "MAX_PLAN_REVISIONS"),
    "gate_timeout_h":     ("OTTO_GATE_TIMEOUT_H", "float", "GATE_TIMEOUT_H"),
    "memory_gc_batch_size": ("OTTO_MEMORY_GC_BATCH_SIZE", "int", "MEMORY_GC_BATCH_SIZE"),
    "memory_gc_max_verify": ("OTTO_MEMORY_GC_MAX_VERIFY", "int", "MEMORY_GC_MAX_VERIFY"),
    "judge_confirmations": ("OTTO_JUDGE_CONFIRMATIONS", "int", "JUDGE_CONFIRMATIONS"),
    "route_confirmations": ("OTTO_ROUTE_CONFIRMATIONS", "int", "ROUTE_CONFIRMATIONS"),
    "max_harness_retries": ("OTTO_MAX_HARNESS_RETRIES", "int", "MAX_HARNESS_RETRIES"),
    "max_supervisor_kills": ("OTTO_MAX_SUPERVISOR_KILLS", "int", "MAX_SUPERVISOR_KILLS"),
    "supervise_steer":    ("OTTO_SUPERVISE_STEER", "choice:off,shadow,enforce", "SUPERVISE_STEER"),
    "max_supervisor_steers": ("OTTO_MAX_SUPERVISOR_STEERS", "int", "MAX_SUPERVISOR_STEERS"),
    "cap_local_latch_fails": ("OTTO_CAP_LOCAL_LATCH_FAILS", "int", "CAP_LOCAL_LATCH_FAILS"),
    "cap_local_latch_ttl_s": ("OTTO_CAP_LOCAL_LATCH_TTL_S", "float", "CAP_LOCAL_LATCH_TTL_S"),
    "effort":             ("OTTO_EFFORT", "choice:default,low,medium,high,xhigh,max", "EFFORT"),
    "conventions_digest_chars": ("OTTO_CONVENTIONS_DIGEST_CHARS", "int",
                                 "CONVENTIONS_DIGEST_CHARS"),
}

_TRUTHY_OFF = ("0", "false", "no", "off")
_store_memo = {"stamp": None, "data": {}}


def _coerce(kind, value):
    """Coerce a stored/posted value to the setting's type. Returns None when unusable, so a
    corrupt store entry degrades to the code default instead of crashing every run."""
    try:
        if kind == "bool":
            return str(value).strip().lower() not in _TRUTHY_OFF if not isinstance(value, bool) else value
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind.startswith("choice:"):
            v = str(value).strip().lower()
            return v if v in kind.split(":", 1)[1].split(",") else None
    except (TypeError, ValueError):
        return None
    return None


def _settings_store():
    """The raw store dict, memoized on the file's mtime+size. setting() is called per gateway call
    and per attempt, so this must not stat-and-parse JSON on every read; an mtime memo keeps a UI
    edit visible within one filesystem timestamp without the parse cost."""
    path = _settings_path()
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size, path)
    except OSError:
        _store_memo.update(stamp=None, data={})
        return {}
    if _store_memo["stamp"] != stamp:
        import storage
        data = storage.read_json(path, {})
        _store_memo.update(stamp=stamp, data=data if isinstance(data, dict) else {})
    return _store_memo["data"]


def setting(name):
    """Resolve one runtime setting: env > store > code default (the legacy module constant)."""
    env_var, kind, const = _SETTING_SPECS[name]
    raw = os.environ.get(env_var)
    if raw is not None and raw != "":
        val = _coerce(kind, raw)
        if val is not None:
            return val
    if name in _settings_store():
        val = _coerce(kind, _settings_store()[name])
        if val is not None:
            return val
    return globals()[const]


def settings_all():
    """Every setting resolved, plus where the value came from — Admin renders an env-pinned knob as
    locked rather than offering a control whose clicks would be silently overridden."""
    out = {}
    for name, (env_var, kind, const) in _SETTING_SPECS.items():
        pinned = os.environ.get(env_var) not in (None, "")
        out[name] = {"value": setting(name), "kind": kind, "env": env_var,
                     "env_pinned": pinned, "default": globals()[const],
                     "stored": name in _settings_store()}
    return out


def save_settings(updates):
    """Merge UI edits into the store (validated + coerced; unknown keys and unusable values are
    dropped). A value equal to the code default is REMOVED rather than stored, so the store stays a
    diff against the defaults and a later default change isn't silently pinned. Returns the
    resolved settings_all()."""
    import storage
    clean = {}
    for name, raw in (updates or {}).items():
        if name not in _SETTING_SPECS:
            continue
        val = _coerce(_SETTING_SPECS[name][1], raw)
        if val is not None:
            clean[name] = val

    def _mut(d):
        if not isinstance(d, dict):
            d = {}
        for name, val in clean.items():
            if val == globals()[_SETTING_SPECS[name][2]]:
                d.pop(name, None)
            else:
                d[name] = val
        return d
    storage.mutate_json(_settings_path(), _mut, default={})
    _store_memo["stamp"] = None      # force a re-read in THIS process
    return settings_all()


def settings_snapshot():
    """One flat {name: value} snapshot of every setting — what OttoWorkflow records in history at
    run start so its deterministic code can branch on stable values."""
    return {name: setting(name) for name in _SETTING_SPECS}


# --- secret resolution (issue: vault-backed secrets) ----------------------------------
#
# Otto's secrets live in plaintext `.env` by default. A password-manager user wants them to stay
# in the manager, so `secret()` adds ONE optional layer: env (which already includes `.env`,
# exported by run.sh) -> the OTTO_SECRET_COMMAND helper -> unset. Nothing about the default path
# changes: with no helper configured, this is `os.environ.get` plus a dict lookup.
#
# The helper is env-ONLY and deliberately not in _SETTING_SPECS: the API is unauthenticated by
# design (issue #123), so a shell command settable over HTTP would leave _csrf_ok as the only
# thing between a page the user visits and arbitrary code execution as them.
#
# Resolution is MEMOIZED, including failures, so a poll loop can call this per iteration without
# re-spawning the helper. Rotating a secret therefore needs a restart — the same as today, where
# every consumer reads its env var into a module constant at import.
SECRET_COMMAND = os.environ.get("OTTO_SECRET_COMMAND", "")
# Hard ceiling on the helper. A vault whose agent is locked PROMPTS, which without a timeout hangs
# the import that resolved the secret — i.e. the whole worker, silently, at startup.
SECRET_TIMEOUT_S = float(os.environ.get("OTTO_SECRET_TIMEOUT_S", "3"))
_SECRET_MAX_BYTES = 1 << 20
# What may be handed to the helper as {name}. The `api_key_env` field accepts a literal key pasted
# where a var name belongs (gateway.api_key), and a literal must never reach the helper's argv,
# where the process table would carry it.
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_secret_memo = {}


def _secret_from_helper(name):
    """Run OTTO_SECRET_COMMAND for `name`; "" on any failure. Never raises, and never logs the
    helper's output or stderr — either can be the secret itself."""
    cmd = SECRET_COMMAND.strip()
    if not cmd or not _SECRET_NAME_RE.match(name or ""):
        return ""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return ""
    if not argv:
        return ""
    # `{name}` anywhere in the command is substituted; a command without it gets the name appended,
    # so `pass show otto/{name}` and `my-helper` both work.
    argv = [a.replace("{name}", name) for a in argv] if any("{name}" in a for a in argv) \
        else argv + [name]
    try:
        r = subprocess.run(argv, capture_output=True, timeout=SECRET_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — ENOENT, timeout, permission: all "unset", never fatal
        return ""
    if r.returncode != 0:
        return ""
    out = (r.stdout or b"")[:_SECRET_MAX_BYTES]
    # First line only: `pass show` prints the secret on line 1 and metadata below it.
    return out.decode("utf-8", "replace").split("\n", 1)[0].strip()


def secret(name, default=""):
    """One secret, resolved env -> OTTO_SECRET_COMMAND -> `default`."""
    val = os.environ.get(name)
    if val:
        return val
    if name not in _secret_memo:
        _secret_memo[name] = _secret_from_helper(name)
    return _secret_memo[name] or default


def secret_reset():
    """Drop the memo and re-read OTTO_SECRET_COMMAND from the environment. For tests and for
    doctor/setup, which change the helper in-process and must not see a stale resolution."""
    global SECRET_COMMAND, SECRET_TIMEOUT_S
    SECRET_COMMAND = os.environ.get("OTTO_SECRET_COMMAND", "")
    SECRET_TIMEOUT_S = float(os.environ.get("OTTO_SECRET_TIMEOUT_S", "3"))
    _secret_memo.clear()


# Every secret Otto resolves, for doctor/setup to report on. `label` is what the gap reads as to a
# human; the module constant each one lands in is named so a reader can find the consumer.
SECRET_SPECS = {
    "OTTO_EVENT_SECRET":     "event/webhook ingress HMAC key (events.SECRET)",
    "OTTO_SLACK_USER_TOKEN": "Slack user token, xoxp-… (slack.USER_TOKEN)",
    "OTTO_NTFY_TOPIC":       "ntfy push topic (config.NTFY_TOPIC)",
    "ANTHROPIC_API_KEY":     "cloud model-list discovery only (gateway._discover_claude)",
}


def secret_status():
    """Where each known secret came from — for `GET /api/doctor` and Admin. Reports PRESENCE and
    SOURCE only; a value never leaves this function.
      The command is echoed because a helper that resolves nothing is unfixable without seeing it,
    but it is scrubbed first: a `gpg --passphrase … ` or `--token …` helper puts a credential in
    the config string itself, and this lands in the Admin DOM and in doctor output."""
    import privacy
    out = {"command": privacy.redact(SECRET_COMMAND), "timeout_s": SECRET_TIMEOUT_S, "secrets": {}}
    for name, label in SECRET_SPECS.items():
        if os.environ.get(name):
            src = "env"
        elif secret(name):
            src = "command"
        else:
            src = "unset"
        out["secrets"][name] = {"label": label, "source": src, "set": src != "unset"}
    return out


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# The ONE SQLite database the JSON stores are migrating into (issue #103) — audit + chats today,
# more stores per later phases. Modules keep their own module-level alias (engine._DB,
# chats._DB) defaulting from here, since that's the seam the tests monkeypatch to a temp file.
DB_PATH = os.path.join(DATA_DIR, "otto.db")

# LLM supervisor (issue #143), SHADOW mode: on a bounded cadence, a cheap "supervise"-tier
# call reads the live execution stream and records whether it WOULD have retried the attempt
# — it never touches the run. A checkpoint fires only after SUPERVISOR_EVERY_S seconds AND
# SUPERVISOR_MIN_EVENTS new activity events since the last one, so short runs never
# checkpoint at all (zero cost); MAX_CHECKS bounds a marathon attempt.
SUPERVISE = os.environ.get("OTTO_SUPERVISE", "1").lower() not in ("0", "false", "no", "off")
SUPERVISOR_EVERY_S = float(os.environ.get("OTTO_SUPERVISOR_EVERY_S", "120"))
SUPERVISOR_MIN_EVENTS = int(os.environ.get("OTTO_SUPERVISOR_MIN_EVENTS", "4"))
SUPERVISOR_MAX_CHECKS = int(os.environ.get("OTTO_SUPERVISOR_MAX_CHECKS", "10"))
SUPERVISOR_CONTEXT_CHARS = int(os.environ.get("OTTO_SUPERVISOR_CONTEXT_CHARS", "6000"))

# How many times the supervisor may kill an attempt WITHIN one run. Measured over every enforce
# kill in the trail: runs with 1 kill passed 9/17, with 2 kills 2/7, with 3 kills 0/6 — and a
# 2nd-or-later kill was followed by a pass in 2 of 13 runs. The first kill carries information
# (a critique the next rung has not seen); a second one is the supervisor disagreeing with a
# retry it already steered, and it buys a rescue rate well under the 49% a plain judge-failed
# attempt gets by being allowed to FINISH. 0 makes enforce mode observe-only.
MAX_SUPERVISOR_KILLS = int(os.environ.get("OTTO_MAX_SUPERVISOR_KILLS", "1"))

# Mid-run STEERING (supervisor.Steer): instead of killing an off-course attempt, deliver the
# supervisor's correction INTO the live session and let the agent adjust — no rung spent, no
# context lost. Three states, and the ladder up between them is deliberate:
#   off     — the supervisor is never offered the verdict, so behaviour is byte-identical to
#             before this existed. The default: a steer is the first thing in Otto that puts
#             judge-authored text into a running agent's context, and the judge in question has
#             a measured false-positive habit (see supervisor._prompt's cwd_note).
#   shadow  — STEER is offered and every verdict is recorded, but nothing is delivered. This is
#             the false-steer-rate dataset, exactly as SHADOW earned the kill switch its arming.
#   enforce — steers are delivered.
# Not derived from SUPERVISE_MODE: killing and steering are different powers with different
# blast radii, and an operator collecting steer data should not have to arm kills to do it.
SUPERVISE_STEER = os.environ.get("OTTO_SUPERVISE_STEER", "off").strip().lower()

# How many steers ONE attempt may receive. Unlike MAX_SUPERVISOR_KILLS this is per-attempt, not
# per-run: a steer does not end the attempt, so the run-level ceiling it needs is "how far may
# this judge rewrite the task", and each delivery is another sentence of supervisor-authored
# instruction sitting in the agent's context permanently. 0 withdraws the option entirely rather
# than offering it and refusing to deliver: a judge told about a verdict that cannot be acted on
# spends its one line on a no-op instead of on the CONTINUE/RETRY choice that still means
# something, so enforce-with-0 behaves exactly like `off`.
MAX_SUPERVISOR_STEERS = int(os.environ.get("OTTO_MAX_SUPERVISOR_STEERS", "2"))

# How a delivered steer is framed to the agent. Three jobs, all load-bearing: say WHO is
# interrupting (an unattributed imperative reads as the user changing their mind, and the agent
# then treats it as the task rather than a correction to it); keep the work already done, since
# preserving it is the entire reason to steer instead of kill; and require the final report to
# say it was redirected — the same honesty rule `_approved_plan_note` puts on a plan departure,
# and the only way a reader of the result ever learns a judge amended the task mid-run.
STEER_MESSAGE = (
    "[Otto run supervisor] A supervisor watching this run mid-flight thinks it is going off "
    "course, and has sent this correction: {instruction}\n"
    "Adjust from here. Keep whatever you have already done that is still valid — you are being "
    "redirected, not restarted. If this contradicts the task as you understood it, follow this "
    "correction and say so in your final report; state plainly in that report that you were "
    "redirected mid-run and what changed as a result.")

# The per-capability local latch (gateway.record_cap_local). A (capability, model) pairing that
# fails verification this many times IN A ROW stops being offered the local backend, until the
# TTL expires and hands it one probationary attempt. Both calibrated against the audit trail
# 2026-07-06..2026-08-25 rather than picked: at 3, the latch fires on every pairing that never
# recovered (sre-secretary/DeepSeek went 0-for-9) and spares the ones whose failures interleave
# with real passes (board-status, sre-pm/qwen3.6). 0 disables the latch entirely.
CAP_LOCAL_LATCH_FAILS = int(os.environ.get("OTTO_CAP_LOCAL_LATCH_FAILS", "3"))
# 24h. Long enough that a bad pairing stops costing an attempt every run, short enough that a
# swapped server or an edited capability gets re-tested without anyone remembering to clear it.
CAP_LOCAL_LATCH_TTL_S = float(os.environ.get("OTTO_CAP_LOCAL_LATCH_TTL_S", str(24 * 3600)))

# Execution transcripts (issue #89): every `claude -p` execution attempt streams its full
# exchange (tool calls included) to data/transcripts/<wid>-a<attempt>.jsonl — the source for
# the run-detail view and live chat progress. Swept opportunistically after this TTL.
TRANSCRIPT_TTL_H = float(os.environ.get("OTTO_TRANSCRIPT_TTL_H", "168"))

# Push notifications (issue #92): when OTTO_NTFY_TOPIC is set, the human-blocking
# transitions (awaiting approval, awaiting clarification, terminal needs-human) push to
# ntfy.sh — or a self-hosted server via OTTO_NTFY_URL. Unset = feature off. The topic name
# is effectively a secret (anyone who knows it can read/post): pick something unguessable.
# OTTO_CLICK_URL is where tapping the notification lands (the Otto UI; a cloudflared
# tunnel URL once issue #41 is set up).
NTFY_TOPIC = secret("OTTO_NTFY_TOPIC")
NTFY_URL = os.environ.get("OTTO_NTFY_URL", "https://ntfy.sh")
# Opt-in completion pushes: also notify when a run finishes CLEANLY (lower priority). Off by
# default — the human-blocking pushes above are the load-bearing ones; a busy interactive
# session would otherwise ping on every finished turn.
NTFY_ON_COMPLETE = os.environ.get("OTTO_NTFY_ON_COMPLETE", "").lower() in ("1", "true", "yes", "on")
# A push is the ONE thing Otto sends to a third party it does not control, and the topic name is
# the only thing standing between it and anyone who guesses it (ntfy caches messages server-side,
# so "nobody was subscribed at the time" is no protection either). So a push carries WHAT happened
# and WHERE to look — capability, risk, repo, source, run id — and never the request, ticket or
# message text: the run id plus a tap on OTTO_CLICK_URL gets the owner to the full content in the
# local UI, which is where private content belongs. OTTO_NTFY_DETAIL=1 adds a redacted request
# preview back for a self-hosted, access-controlled ntfy server. Credential-shaped strings are
# scrubbed (privacy.redact) either way — that is a floor, not the control.
NTFY_DETAIL = os.environ.get("OTTO_NTFY_DETAIL", "").lower() in ("1", "true", "yes", "on")
# How much of the request preview survives when OTTO_NTFY_DETAIL is on (ntfy's own body cap is
# larger; this is a privacy bound, not a transport one).
NTFY_DETAIL_CHARS = int(os.environ.get("OTTO_NTFY_DETAIL_CHARS", "300"))
# Where tapping a push lands. `delivery.notify` appends `#run=<wid>` so the tap opens the run
# it is about, not the home tab — the notification's whole job is to get the owner to the run,
# and a landing page with no run on it makes the reader hunt for it. The localhost default is a
# DEAD LINK on the phone the push went to: set OTTO_CLICK_URL to the tunnel (issue #41).
CLICK_URL = os.environ.get("OTTO_CLICK_URL",
                           f"http://localhost:{os.environ.get('PORT', '8765')}")
# Approve/Deny buttons on the approval push (ntfy `actions`). OFF by default and deliberately
# so: the button URL carries a single-use token that rides on the third-party broker, so it is
# exactly as private as the topic name — and it is useless anyway until OTTO_CLICK_URL is
# reachable from the phone. On, it turns a 24h blocking gate into one tap.
NTFY_ACTIONS = os.environ.get("OTTO_NTFY_ACTIONS", "").lower() in ("1", "true", "yes", "on")
# How long an action token stays redeemable. Defaults to the gate's own deadline — a token that
# outlives the gate it belongs to can only ever resolve to "that run is gone".
NTFY_ACTION_TTL_S = int(os.environ.get("OTTO_NTFY_ACTION_TTL_S", "86400"))
# Drop a push identical to one sent within this window. Its ONLY job is swallowing a duplicate
# from an activity retry (seconds apart), so keep it SHORT: a re-previewed plan pushes the same
# title for the same run again, minutes later, and that second push is real.
NTFY_DEDUPE_S = int(os.environ.get("OTTO_NTFY_DEDUPE_S", "120"))

# Local (OpenAI-compatible) model calls (issue #90): completion timeout — cheap-tier
# completions, not executions, so fail reasonably fast — and how long to skip a local model
# after a failure (going straight to the Claude fallback), so a dead endpoint doesn't add
# dead air to EVERY routing/clarify/verify call. The token cap must leave a REASONING model
# room to think AND still emit its answer: at the old 300-token cap, gemma-4 spent the whole
# budget in its `reasoning` field and every verify verdict came back empty (= parsed as FAIL).
LOCAL_TIMEOUT_S = float(os.environ.get("OTTO_LOCAL_TIMEOUT_S", "60"))
LOCAL_SKIP_S = float(os.environ.get("OTTO_LOCAL_SKIP_S", "120"))
LOCAL_COMPLETE_MAX_TOKENS = int(os.environ.get("OTTO_LOCAL_COMPLETE_MAX_TOKENS", "2000"))

# Supervisor mode (issue #143): "enforce" KILLS a clearly off-course attempt mid-run and folds
# the supervisor's critique into the next verify-ladder attempt (sharing MAX_VERIFY_ATTEMPTS —
# a kill can never loop beyond the ladder's own budget); "shadow" only records what it would
# have done. Enforce became the default after 31 shadow-supervised attempts produced zero
# retry votes (no false-kill appetite from the judge, whose parser defaults CONTINUE anyway).
SUPERVISE_MODE = os.environ.get("OTTO_SUPERVISE_MODE", "enforce").strip().lower()

# Cheap-tier `claude -p` calls (routing/clarify/verify/…): bounded well inside the Temporal
# activity's 180s so a stalled CLI (rate-limit wait) dies fast and the in-process fallback
# still gets a shot — instead of the whole activity timing out and re-running (3 lost minutes).
CLAUDE_TIER_TIMEOUT_S = float(os.environ.get("OTTO_CLAUDE_TIER_TIMEOUT_S", "120"))

# EXECUTION watchdog for run_attempt (both backends — LOCAL_RUN_TIMEOUT_S below defaults to
# it). Must stay under run_capability's start_to_close_timeout (workflows.py) with headroom
# for activity overhead. Raised from 1100s once the execution activities started heartbeating:
# at 1100s, seven attempts across haiku, sonnet and opus died mid-task on this watchdog with
# 63-435 turns spent, and the ladder could only retry the whole attempt from scratch.
EXEC_TIMEOUT_S = float(os.environ.get("OTTO_EXEC_TIMEOUT_S", "2300"))

# How often a long blocking activity beats (activities._heartbeating). The point is not
# progress reporting: it is how fast Temporal learns the WORKER died. Without it, a killed
# worker is only noticed at start_to_close, so the ceiling above cannot be raised without
# making every restart stall a run for that whole window. Must stay well under the
# heartbeat_timeout the call sites declare (workflows.py).
HEARTBEAT_EVERY_S = float(os.environ.get("OTTO_HEARTBEAT_EVERY_S", "30"))

# Local EXECUTION (issue #42 + the local runtime): a real completion, not a ≤300-token
# cheap-tier call — allow a longer generation and a slower first token (cold local models).
LOCAL_EXEC_TIMEOUT_S = float(os.environ.get("OTTO_LOCAL_EXEC_TIMEOUT_S", "300"))
# 8192 was sized for a model that answers; a REASONING model spends the budget thinking and can
# hit the ceiling with no final answer at all. `local_runtime` deliberately refuses to stitch a
# truncated reasoning stream (continuing one just accretes chain-of-thought — web-ccbb5378), so
# that attempt dies outright: measured on `web-a056884d`, deepseek-v4-flash burned 75k output
# tokens and returned "reasoning but no final answer". Raising this is self-limiting — an
# over-long prompt+budget comes back as a 400 that `run_json` prunes and then HALVES max_tokens
# for, so a tight context still converges instead of failing.
LOCAL_EXEC_MAX_TOKENS = int(os.environ.get("OTTO_LOCAL_EXEC_MAX_TOKENS", "32768"))
# Whole-RUN wall clock for local_runtime.run_json — the local mirror of EXEC_TIMEOUT_S, and
# sized to it for the same reason: both backends run inside the same 20min run_capability
# activity, so there is no reason the local one should stop 200s earlier. It used to be a bare
# `timeout=900` default that engine.run_attempt never overrode, invisible to every env knob.
LOCAL_RUN_TIMEOUT_S = float(os.environ.get("OTTO_LOCAL_RUN_TIMEOUT_S", str(EXEC_TIMEOUT_S)))
# Local agent runtime (local_runtime.py — full tool-driving execution on a non-Claude model,
# local vLLM or a remote OpenAI-compatible API like DeepSeek alike): per-run turn budget
# (model call + tool round = one turn) and per-tool-call timeout.
#   Sized from the retained local transcripts, not guessed: median 23 turns used, p90 30, max 30,
#   with 4 of 13 runs dying EXACTLY at the old cap of 30 — a distribution piling up against the
#   ceiling is censored, not comfortable. A smaller model does less per turn than Claude does, so
#   the one backend Otto caps at all was the one that needed the most room. `claude -p` has no
#   Otto-side turn cap for comparison; this stays bounded only to stop a model looping forever.
#   This is a global fallback, not a per-model measurement — a pool entry with its own
#   `max_turns` (data/models.json) overrides it, since "non-Claude" spans everything from a
#   small local vLLM model to a frontier remote API and one number doesn't fit both.
LOCAL_RUNTIME_MAX_TURNS = int(os.environ.get("OTTO_LOCAL_RUNTIME_MAX_TURNS", "60"))
LOCAL_TOOL_TIMEOUT_S = float(os.environ.get("OTTO_LOCAL_TOOL_TIMEOUT_S", "120"))
# TRANSIENT-availability backoff for the local runtime: a 502/503/504 or a connection-level
# error means the server is momentarily loading/restarting/overloaded ("try again later"),
# NOT a model-quality failure — retrying immediately on the verify ladder just re-hits the
# same down server and dead-ends at needs-human. So the runtime backs off IN PLACE a few
# times (honouring Retry-After when present) before giving up. Stays LOCAL by design — no
# Claude fallback; a persistently-down server fails clean with an actionable reason.
LOCAL_RETRY_ATTEMPTS = int(os.environ.get("OTTO_LOCAL_RETRY_ATTEMPTS", "3"))
LOCAL_RETRY_BACKOFF_S = float(os.environ.get("OTTO_LOCAL_RETRY_BACKOFF_S", "2"))
LOCAL_RETRY_MAX_BACKOFF_S = float(os.environ.get("OTTO_LOCAL_RETRY_MAX_BACKOFF_S", "15"))

# MCP for the LOCAL backend (mcp_client.py). Startup is generous because a `npx`/`uvx` server
# cold-starts by downloading itself; a call is a normal API round-trip. `OTTO_LOCAL_MCP=0`
# turns the client off entirely — the runtime then behaves exactly as it did before it existed
# (built-in tools only), which is the escape hatch if a server misbehaves mid-incident.
LOCAL_MCP = os.environ.get("OTTO_LOCAL_MCP", "1") != "0"
LOCAL_MCP_STARTUP_S = float(os.environ.get("OTTO_LOCAL_MCP_STARTUP_S", "45"))
LOCAL_MCP_CALL_S = float(os.environ.get("OTTO_LOCAL_MCP_CALL_S", "60"))
# Tool-schema budget. MEASURED (2026-08-04, 6 real servers): 140 tools = ~31k tokens of
# schema, re-sent on EVERY turn of the loop — more per attempt than the failure the MCP client
# exists to fix, and fatal on a small context window. grafana alone is 73 tools / ~20k. So the
# offer is ranked against the request and trimmed; 25 tools lands around ~6k tokens, against
# ~1.5k for the seven built-ins. 0 disables the budget (offer everything the servers expose).
LOCAL_MCP_MAX_TOOLS = int(os.environ.get("OTTO_LOCAL_MCP_MAX_TOOLS", "25"))
# How many servers a cap with NO frontmatter `tools:` line may draw from (the general
# worker/assistant, stock/custom caps). Each one is a subprocess to cold-start, so this bounds
# latency where the declaration would otherwise have bounded it.
LOCAL_MCP_MAX_SERVERS = int(os.environ.get("OTTO_LOCAL_MCP_MAX_SERVERS", "3"))
# How long a server that failed to START is remembered as having no tools, so one broken
# server (aws-mcp on an expired SSO token) can't keep selection permanently in its cold-cache
# fallback — but a transiently-down one isn't written off forever either.
LOCAL_MCP_PROBE_TTL_S = float(os.environ.get("OTTO_LOCAL_MCP_PROBE_TTL_S", "3600"))


# The code defaults as a flat dict. Defined at the END of this module because the constants it
# reads are declared throughout it. Safe for deterministic workflow code to read at construction
# time precisely because it's a module constant that cannot change mid-run — it's only the stand-in
# until snapshot_settings() returns the real per-run snapshot.
SETTINGS_FALLBACK = {name: globals()[const] for name, (_e, _k, const) in _SETTING_SPECS.items()}
