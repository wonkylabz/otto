"""The judges: verify (pass/fail + critique), QA and code-review verdicts, error guards.

Extracted from engine.py (which re-exports everything here — callers and tests keep using
engine.X, same facade contract as audit.py/memory.py/contracts.py). Owns the verify judge and
its parsers, the QA / code-review request builders and judges, the approved-plan note the
judge receives, and the resume/error result guards.
"""

import config
import conventions
import gateway
import registry
from contracts import CONVERSATION_AUDIENCE, _write_gate_note
from ui import trace


def _eng():
    """The engine facade — tests monkeypatch attributes there, so patch-sensitive values and
    cross-calls resolve through it at call time, never bind at import. Same contract as
    audit._eng / memory._eng."""
    import engine
    return engine


def _parse_verdict(text):
    """Parse the verifier's reply into {passed, critique}. Pure — no LLM call, so it's
    unit-testable. First line carries PASS/FAIL; the remainder is the critique.

    A leaked think-stream is stripped first: a local reasoning model on a mis-parsed server
    leaves its chain-of-thought in `content`, so the real verdict lands AFTER a stray
    `</think>` or at the tail. The old first-line-only parse read that leading reasoning as the
    verdict line and scored a genuine PASS as FAIL — laundering a real pass into
    verify_exhausted / needs-review (observed: a github-pr-review PASS from qwen3.6). We strip
    the fence, then, if the verdict still didn't lead (a fully-unfenced leak with no tag to
    strip), recover a STANDALONE PASS/FAIL from its own line at the tail. Conservative: only a
    bare token counts, never the word inside prose, so ambiguous output still falls through to
    the safe FAIL default (a false FAIL only costs a retry; a false PASS launders bad output)."""
    text = gateway._strip_reasoning(text or "").strip()
    first, _, rest = text.partition("\n")
    token = first.strip().upper()
    passed = token.startswith("PASS")
    if not passed and not token.startswith("FAIL"):
        for line in reversed(text.splitlines()):
            s = line.strip().upper().rstrip(".!:")
            if s in ("PASS", "FAIL"):
                token, passed, rest = s, s == "PASS", ""
                break
    critique = rest.strip()
    if not passed and not critique:
        # Verifier put its reasoning on the first line, or said only "FAIL".
        critique = text if token != "FAIL" else "The output did not satisfy the request."
    return {"passed": passed, "critique": critique}


def _parse_qa_verdict(text):
    """Map a QA judgement into {verdict, critique} where verdict is 'pass' | 'fail' |
    'inconclusive'. Pure (unit-testable). First line carries the verdict word; the rest is
    the critique. Unknown/empty output defaults to 'inconclusive' — never a false PASS, and
    never an endless fix loop on an unparseable verdict (inconclusive stops the loop).

    A leaked think-stream is stripped first (same local-model failure as _parse_verdict).
    No tail-scan here on purpose: this gates an empirical PR validation, so an unfenced
    ramble is left to default to INCONCLUSIVE (stops for a human) rather than heuristically
    promoted to a PASS that would mark a PR validated on the strength of the model rambling."""
    text = gateway._strip_reasoning(text or "").strip()
    first, _, rest = text.partition("\n")
    token = first.strip().upper()
    critique = rest.strip() or text
    if token.startswith("PASS"):
        return {"verdict": "pass", "critique": ""}
    if token.startswith("FAIL"):
        return {"verdict": "fail", "critique": critique or "QA did not confirm the change works."}
    return {"verdict": "inconclusive",
            "critique": critique or "QA could not reach a clear pass/fail verdict."}


def _qa_adverse(verdict):
    """Anything but a clean PASS is adverse for the post-PR loops, so it must reproduce before
    it is acted on (`confirm_adverse`). Both non-pass outcomes cost real work on a PR the
    reviewer may well have approved: FAIL spends a fix round on the write capability, and
    INCONCLUSIVE dead-ends a finished PR as draft-for-a-human. Measured on run web-2bd1a194:
    the SAME judge, model and conventions digest FAILed PR #105 at 10:32 and PASSed the
    identical commit at 10:53, with a 944s fix round in between that changed nothing."""
    return verdict.get("verdict") != "pass"


# Defence-in-depth against reasoning-as-result (run web-ccbb5378): a local model whose server has
# no reasoning parser can leak its raw chain-of-thought into the answer, and the runtime's own
# guards (local_runtime._is_reasoning_stream) are the first line — this tells the judge to FAIL it
# too, so a deliberation stream never passes verify even if it slips through delivery.
_APPROVED_PLAN_CHARS = 12_000

# How much of the RESULT the verifier reads. This was a bare `result[:4000]` with no cut marker,
# which manufactured the defect it then failed the run for: the judge faithfully reported "output
# ends mid-sentence" quoting the exact character the clip landed on, of a result that was complete
# (`daily-summary`, 4223 and 4668 chars, failed twice in a row for $5.07). Same lesson as
# `_PLAN_CRITIQUE_CHARS` — a cut the reader can't see reads as the source's own defect — so it
# goes through `_clipped` and gets the marker. 12k covers 99.2% of results measured over the trail
# (p90 2.5k, p99 8.7k); the extra tokens are only paid on the 4.6% that run long.
_VERIFY_RESULT_CHARS = 12_000


def _grounding_note(notes):
    """What the provisioned tree says about the request, when the two disagree. None when they
    agree — which is almost always, so this costs nothing on a healthy run.

    Measured (`web-d2438694`): the request named line ~148 of a file whose copy on the branch
    the run was given is 82 lines long. Three models saw that tree. The one that noticed had no
    sanctioned move — the worker contract forbids it from touching git — so it spent twenty
    minutes reverse-engineering Otto itself looking for one, and was killed for wandering. The
    two that did not notice edited the wrong revision of the file and shipped it.

    So the note carries the mismatch AND the move: say so, work on what IS here, do not go
    looking for the missing code outside the working directory. Advisory, and deliberately
    hedged — the request may name a file it is asking to CREATE — but a run that proceeds
    silently past a real mismatch is the failure this exists to stop."""
    notes = [n for n in (notes or []) if str(n).strip()]
    if not notes:
        return None
    body = "\n".join(f"  - {n}" for n in notes)
    return (
        "--- WORKSPACE MISMATCH\n"
        "Before you started, the platform compared this request against the branch actually "
        "checked out in your working directory and found:\n"
        f"{body}\n"
        "This usually means the request describes code on a DIFFERENT branch, tag or pull "
        "request than the one you were given. It can also be benign — a file you are being "
        "asked to create, or a line number in a file you are about to grow.\n"
        "Decide which it is, then: if the request is genuinely about code that is not here, "
        "SAY SO PLAINLY as your result and stop — name what you expected and what you found. "
        "Do NOT go looking for the missing code outside your working directory, do not switch "
        "branches, and do not fall back to editing whatever similarly-named thing IS here as "
        "though it were the target. Reporting the mismatch is a complete, successful answer.\n")


def _approved_plan_note(plan):
    """The plan a human APPROVED at the gate, handed to the run that was approved to do it — or
    None when there wasn't one (unattended auto-approve, a read run, a resume).

    Until this existed the gate was an informed veto and nothing more: `self._plan` reached the
    approval card and stopped there, so execution re-derived its own approach from the raw request
    and `verify` judged against the raw request too. Measured (`web-5f9319cd`): a plan whose whole
    structure was "enforcement is the LAST step, gated on adoption evidence" was approved, and the
    run then shipped enforcement and observability together in one unconditional PR — the exact
    production outage the plan existed to prevent. Nothing objected, because no part of Otto was
    comparing the two.

    Deliberately an instruction with an escape hatch rather than a contract. The plan was written
    before the work started, by a read-only pass that could be wrong (this one was: it picked a
    chart key that breaks the apply), so a run that discovers the plan is unworkable must be free
    to depart from it — it just has to SAY so, which is what makes the departure reviewable instead
    of invisible. Pairs with the matching clause in `verify`."""
    plan = (plan or "").strip()
    if not plan:
        return None
    body, note = _eng()._clipped(plan, _APPROVED_PLAN_CHARS)
    return (
        "--- APPROVED PLAN\n"
        "A human read the plan below and approved THIS run to carry it out. It is the shape of "
        "the work they agreed to, not a suggestion: follow its steps and — above all — its "
        "ORDER, including any phasing, precondition or gate it puts on a step. If it defers "
        "something until a condition holds, you may not bring that thing forward.\n"
        "You MAY depart from it where it turns out to be wrong or unworkable — it was written "
        "before the work started, without running anything. If you do, say so EXPLICITLY in your "
        "final report: what you changed, and why. An unannounced departure is the one failure "
        "mode here; a well-explained one is fine.\n"
        "If a step needs access you do not have (credentials, a live cluster or cloud account, a "
        "dashboard), do NOT quietly skip it and do not present its conclusion as established — "
        "carry it out as far as you can, then state plainly in the report that it is unverified "
        "and what remains to be checked.\n\n"
        f"{body}{note}")


# How much of the capability's OWN contract the verifier reads. The judge otherwise sees a cap
# through `name (description[:160])` alone, which is not enough to tell a rule the cap is REQUIRED
# to follow from one the output invented — so it judges the raw request as the whole contract.
# Measured (sched-mosaic-b643e7fd, 2026-08-18): product-manager refines Ready tickets "assigned to
# the authenticated user" (its Capability 3, in the file); the judge failed a correct zero-write run
# for "narrowing the request's scope … without any evidence this assignee restriction is an actual
# documented rule of the capability", and its critique told the retry to refine "all tickets in
# those two columns regardless of assignee". The retry obeyed and wrote to 13 tickets belonging to
# other people. The judge was reasoning honestly about a document nobody had shown it.
_CAP_CONTRACT_CHARS = 5_000


def _cap_text(cap):
    """A capability's own instructions: `cap.prompt` (custom/stock caps carry it inline), else the
    source .md an agent/skill cap was discovered from. Best-effort by design — a cap we cannot read
    yields no contract block rather than an exception, because verify must never fail over it."""
    text = getattr(cap, "prompt", None)
    path = getattr(cap, "path", None)
    if not text and path:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = None
    text = (text or "").strip()
    if text.startswith("---"):                      # drop YAML frontmatter (path-read caps)
        end = text.find("\n---", 3)
        if end != -1:
            text = text[text.find("\n", end + 1) + 1:].strip()
    return text


def _cap_sections(text):
    """Split cap markdown into sections on markdown headings. Everything BEFORE the first heading
    is section 0 and is returned first — a cap's global "always holds" rules live there, above any
    one capability-specific section, so it is never the part that gets dropped."""
    secs, cur = [], []
    for line in (text or "").splitlines():
        if line.startswith("#") and cur:
            secs.append("\n".join(cur).strip())
            cur = [line]
        else:
            cur.append(line)
    if cur:
        secs.append("\n".join(cur).strip())
    return [s for s in secs if s]


def cap_contract_block(cap, request=None, budget=None):
    """The capability's own contract, trimmed to one judging prompt. Pure (no LLM call, no
    network) so it is unit-testable, and free.

    Ranked by keyword overlap with the request, NOT truncated in document order — the lesson
    `conventions.select_rules` already paid for: taking whichever sections come first makes the
    enforced subset a function of heading order, so moving a section silently changes what the
    judge enforces. The count of what was dropped is stated, because a silent trim reads as "that
    was the whole contract" — which is the exact inference that caused the incident this exists
    to prevent.

    A write-risk cap's own contract can carry an interactive rubber-stamp step ("ask before
    writing") written for a live session — `_write_gate_note` tells both this judge and the
    supervisor that Otto's approval gate already satisfied it, so skipping that pause is
    on-course, not a violation of the contract just shown above (see its docstring)."""
    budget = _CAP_CONTRACT_CHARS if budget is None else budget
    secs = _cap_sections(_cap_text(cap))
    gate_note = _write_gate_note(cap)
    gate_block = f"\n\n{gate_note}" if gate_note else ""
    if not secs:
        return gate_note or ""
    head, rest = secs[0], secs[1:]
    if len(head) > budget:                          # preamble alone overruns: clip it, keep no more
        body, note = _eng()._clipped(head, budget)
        return _contract_wrap(body + note, len(rest)) + gate_block
    want = conventions._keywords(request) if request else set()
    scored = sorted(((len(want & conventions._keywords(s)) if want else 0, i, s)
                     for i, s in enumerate(rest)), key=lambda x: (-x[0], x[1]))
    kept, total = [], len(head)
    for _, i, s in scored:
        if total + len(s) > budget:
            continue                                # a shorter later section may still fit
        kept.append((i, s))
        total += len(s)
    kept.sort()                                     # back into document order, for a readable block
    return (_contract_wrap("\n\n".join([head] + [s for _, s in kept]), len(rest) - len(kept))
            + gate_block)


def _contract_wrap(body, dropped):
    """The contract block plus the precedence rule that makes it mean something to the judge."""
    more = ""
    if dropped > 0:
        more = (f"\n\n({dropped} further section(s) of this contract are not shown here. Their "
                "absence is not permission — never conclude a rule the output cites does not "
                "exist just because you cannot see it.)")
    return (
        "--- CAPABILITY CONTRACT\n"
        "These are the capability's OWN standing instructions, which it is required to follow. "
        "They are documentation you are being shown, not something the output invented. A scope "
        "limit, filter, refusal or safety rule that this contract imposes is CORRECT even where "
        "the request appears to ask for more: do NOT fail it as unjustified, invented or "
        "'narrowing the request', and never tell the next attempt to override the contract or to "
        "widen the set of things it changes. Output that BREAKS one of these rules is a FAIL even "
        "when it satisfies the request as worded. Judge whether the request was accomplished "
        "WITHIN this contract.\n\n"
        f"{body}{more}\n--- END CAPABILITY CONTRACT")


def confirm_adverse(task, prompt, parse, adverse, tries=None):
    """Sample a judge until its ADVERSE verdict is contradicted, or `tries` samples agree on it.
    Returns the parsed verdict. Shared by `verify` (FAIL) and `supervisor` (RETRY).

    `claude -p` exposes no temperature, top-p or seed — 65 flags, not one for sampling — so every
    judge tier assigned to a Claude model is sampled and cannot be pinned the way the
    OpenAI-compatible path already is (`temperature: 0`). Measured on ONE fixed, complete, correct
    output: 12 PASS / 8 FAIL across 20 judgements, while a clearly-bad output failed 0/5. The
    instability is not uniform — it sits on GOOD results, which is exactly where a wrong verdict is
    expensive: a false FAIL burns a retry, and for a write capability the retry is what widens
    blast radius (sched-mosaic-b643e7fd's retry wrote to 13 tickets belonging to other people).

    Deliberately ASYMMETRIC rather than a majority vote. Majority-of-N does nothing for a verdict
    that is genuinely 50/50 — it converges on the same coin flip — whereas requiring an adverse
    verdict to REPRODUCE cuts it down. Measured A/B on that same input, 10 verdicts each way: false
    FAIL 5/10 → 2/10, at 2.20 judge calls per verdict instead of 1.00. It does NOT eliminate the
    flip and cannot: at p≈0.5 three-sample unanimity still lets ~12% through, and only removing the
    ambiguity (or a temperature knob `claude -p` does not have) would. A PASS returns on the first
    sample, so a run the judge likes still costs one call; the 2.2 here is what an input that fails
    half the time costs. The trade is stated plainly: verify becomes more forgiving, so a marginal
    run is likelier to pass — the right direction when a false PASS on repo-mode is already
    delivered as advisory "⚠ unverified", and a false FAIL spends a retry."""
    tries = config.setting("judge_confirmations") if tries is None else tries
    try:
        tries = max(1, int(tries))
    except (TypeError, ValueError):
        tries = 1
    first = None
    for i in range(tries):
        verdict = parse(gateway.complete(task, prompt))
        if not adverse(verdict):
            if i:
                trace("VERIFY", f"adverse verdict did not reproduce on sample {i + 1} — not acted on")
            return verdict
        first = verdict if first is None else first
    return first


_JUDGE_REASONING_RULE = (
    "A finished RESULT is required, not the model's thinking. If the output is largely internal "
    "chain-of-thought — first-person deliberation ('let me…', 'but wait…', 'I need to reconsider…'), "
    "options weighed but never resolved, or notes-to-self instead of the actual answer/deliverable — "
    "that is a FAIL: the real answer never surfaced cleanly, even if it's buried at the very end.")


_GRANT_FLOOR_RULE = (
    "That list is a FLOOR, never the complete set, and you cannot see this run's tool-call "
    "transcript. A capability brings its own declared tools on top of it — MCP servers and "
    "third-party connectors (Google Calendar, Gmail, Slack, Notion, Atlassian, New Relic, "
    "Grafana, Kubernetes and the like) are inherited from the operator's own configuration and "
    "do NOT appear above. So you have NO evidence about which tools were unavailable. NEVER "
    "reason that a source could not have been reached, that a step was impossible, or that a "
    "result was fabricated, on the grounds that its tool is missing from the list — that "
    "inference is unsound and it has failed correct, tool-verified work. Judge fabrication only "
    "on the content itself (internally inconsistent, contradicted by the request, or claiming "
    "something the output elsewhere admits it did not do)."
)


def _refused_note(tools_failed, tools_used=None):
    """Tools this attempt CALLED and got nothing back from — every call errored or was refused.

    The mirror image of the grant, and just as load-bearing: an ungranted claude.ai connector
    answers with "you haven't granted it yet", so the capability genuinely could not reach that
    source. Without this, naming the tool in the grant turns a truthful "that source was blocked"
    into what reads as an invented excuse, and the judge fails the run for honesty
    (measured on probe-e2e-0001)."""
    names = sorted({str(t) for t in (tools_failed or []) if t} - {str(t) for t in (tools_used or [])})
    if not names:
        return ""
    return ("\nThese tools WERE called and returned nothing usable — every call errored or was "
            f"refused at runtime: {', '.join(names)}. So the capability genuinely could not reach "
            "whatever they serve. An output that reports those sources as unavailable, blocked or "
            "not permitted is telling the TRUTH: do not fail it for that, and do not read it as an "
            "excuse. Equally, do not credit any data it claims to have gotten FROM them.")


def _grant_list(cap, tools_used=None):
    """What to tell the judge this attempt could reach.

    `config.READ_TOOLS`/`WRITE_TOOLS` is the `--allowedTools` floor, not the grant: an `agent`
    cap contributes its own `tools:` frontmatter and every MCP server is inherited from the
    user's config, so the real set is routinely a superset. `tools_used` — harvested from the
    attempt's own event stream (`claude_cli._note_tools`) — is the only ground truth available,
    so anything actually observed is folded in. The floor is still named because a tool granted
    and never called is still a tool it had."""
    floor = config.WRITE_TOOLS if cap.risk == "write" else config.READ_TOOLS
    return sorted(set(floor) | {str(t) for t in (tools_used or []) if t})


def verify(request, cap, result, project=None, local=False, unattended=False, audience=None,
           approved_plan=None, tools_used=None, tools_failed=None, grounding=None,
           steers=None):
    """Claude (or the configured 'verify' tier) judges whether the run satisfied the
    request. Returns {passed, critique}. The critique is fed back into the next attempt.
    `project` (a registered repo path) injects that repo's own CLAUDE.md conventions with
    precedence over the request — so a request that itself prescribes a convention
    violation (the PR #251 failure) is judged against the repo's rules, not just its own
    wording. Automatic and zero-config: the executor already reads CLAUDE.md via cwd;
    this closes the same gap for the judge.

    The judge sees only the final text output — never the attempt's tool-call transcript — so
    without being told what tools the capability actually had, it can wrongly infer "no tool
    access" from a capability's description alone and dismiss a genuinely tool-verified
    investigation as fabricated (observed failure: a read-risk capability that looked like a
    knowledge-only assistant was falsely accused of inventing `git`/CLI output it had actually
    run via Bash). Stating the real tool grant up front forecloses that specific hallucination.

    `unattended=True` (schedule/board/event runs — no human present) adds a dead-end rule:
    an output whose bottom line is a question to the user ("want me to retry?") is a FAIL, so the
    retry ladder gets a shot at a self-standing report instead of delivering a question nobody
    can answer (the daily-summary blocked-Write failure, 2026-07-28).

    `audience="conversation"` REPLACES that rule, because its premise is false there: the output goes
    to a person who is present and can reply (their reply resumes the session), so asking them a
    short clarifying question is a legitimate final answer. Judged for the wrong thing instead —
    report scaffolding leaking to a stranger, or a question aimed at the operator, who is not
    reading it.
    Without this carve-out the two fixes fight: the 2026-07-31 DM had "Dammit" and "nope" each burn
    2-3 attempts and an Opus escalation because every attempt ended by asking, the dead-end rule
    failed it, and the ladder retried — for a message with nothing to do in it."""
    if not result or result == "(no output)":
        return {"passed": False, "critique": "The capability produced no output."}
    # "Nothing to say back" is a legitimate answer in a conversation, and the judge must not fail it
    # into a retry ladder — the message it's declining to answer has nothing in it to do better at.
    # Deterministic, so it costs no LLM call. Never honoured for a report audience: a report saying
    # NO_REPLY is just a broken report.
    if audience == CONVERSATION_AUDIENCE and config.is_no_reply(result):
        trace("VERIFY", "nothing to reply — accepted, nothing will be posted")
        return {"passed": True, "critique": ""}
    trace("VERIFY", f"judging output of [{cap.kind}] {cap.name}")
    conv = conventions.judge_block(project, request) if project else None
    # The cap's own rules, so a contract-mandated limit isn't judged as an invented one.
    contract = cap_contract_block(cap, request)
    tools = _grant_list(cap, tools_used)
    if local:
        # Local tool-free attempt (issue #42): the inverse framing of the Claude grant below —
        # this attempt had NO tools, so tool-shaped "evidence" cannot be real and must not be
        # credited (a local model fabricating command output would otherwise pass laundered).
        grant = ("This attempt ran on a LOCAL model with NO tool access at all — it could only "
                 "reason over the request and the context it was given. Any tool-shaped output "
                 "(command results, live file contents, specific fresh IDs) it claims to have "
                 "gathered is fabricated; FAIL if the request needed real investigation.")
    else:
        grant = (f"This capability actually ran with real tool access — as a "
                 f"'{cap.risk}' capability it had at least: {', '.join(tools)}. Do NOT assume it "
                 "\"has no tool access\" or that tool-shaped output (command results, file "
                 "contents, specific IDs) is fabricated just because the description above reads "
                 "like a knowledge-only assistant — judge only whether the output plausibly "
                 "satisfies the request.\n"
                 + _GRANT_FLOOR_RULE + _refused_note(tools_failed, tools_used))
    dead_end = ""
    if audience == CONVERSATION_AUDIENCE:
        dead_end = (
            "\nThis output is posted VERBATIM to the person who messaged on Slack — judge it as a "
            "message to them, not as a report. A short clarifying question back to them PASSES: "
            "they are present and their reply continues the conversation, so asking for the one "
            "detail needed to act is a complete answer, and so is a brief honest \"I can't help "
            "with that\". FAIL it when: it leaks internal scaffolding a stranger should not see "
            "(\"here's the reply to send\", \"---\" wrappers, notes about what the run did or did "
            f"not do, mentions of tools/permissions/access); it addresses {config.OWNER_NAME} rather "
            "than the sender, asks HIM for permission or a decision, or tells the reader to check something "
            "themselves; or it reads as a status report rather than a reply. Judge substance too: "
            "if the conversation context makes the request answerable, an \"I need more context\" "
            "reply is a FAIL — say in the critique what the context already establishes.\n"
            "THE READER IS NOT THE OWNER, so also FAIL it when it discloses more than the question "
            "needed: any credential or credential-equivalent (a password, API key, token, private "
            "key, connection string, .env or secret-manager VALUE, a signed/pre-signed URL) — "
            "which is a FAIL even if the sender asked for it and even if the reason sounded good; "
            "internal detail volunteered beyond the answer (remembered context from unrelated "
            "work, adjacent findings, a tour of what else it noticed, file or config contents that "
            "were not asked about); or anything quoted out of a different conversation or another "
            "person's message. Naming WHERE a secret lives is fine; reproducing its value is not. "
            "In the critique, say which part to drop rather than asking for a rewrite.\n"
            "A warm, natural, colleague-like phrasing is not itself a defect — don't fail a reply "
            "for its tone alone; judge it only against the concrete failures listed above.\n")
    elif unattended:
        dead_end = (
            "\nThis run is UNATTENDED: no human is watching and nothing can answer a question. "
            "FAIL any output whose bottom line is a question to the user, a request for "
            "approval/permission, or an offer of options (\"want me to retry, or skip it?\") "
            "instead of a delivered result — it is a dead end by construction. In the critique, "
            "tell the next attempt to complete everything it can, skip what it cannot, and "
            "report the substance without asking anything.\n")
    # The plan a human approved for this run, if any. Judging output against the REQUEST alone is
    # what let an approved "enforcement is the LAST step" plan pass verification as one
    # unconditional PR — the request was satisfied; the agreement was not.
    plan_rule = ""
    if approved_plan and str(approved_plan).strip():
        pbody, pnote = _eng()._clipped(str(approved_plan).strip(), _APPROVED_PLAN_CHARS)
        plan_rule = (
            "A human APPROVED the plan below for this run, and the output is judged against it "
            "as well as against the request.\n"
            f"\nAPPROVED PLAN:\n{pbody}{pnote}\n"
            "\nBefore answering, check the output against that plan and ask:\n"
            "  (a) Did it do something the plan says NOT to do in this run, or defer to later?\n"
            "  (b) Did it bring forward, drop or reorder a phase, precondition or gate?\n"
            "  (c) Did it claim a step it could not actually have performed?\n"
            "If the answer to any of these is yes AND the output does not say so, that is a FAIL "
            "however good the work otherwise looks — name the step in the critique. Departing from "
            "the plan is legitimate (it was written before anything ran and may be wrong) and a "
            "departure the output ANNOUNCES and justifies is a PASS on this point. Silence about a "
            "departure is the failure.\n")
    # Corrections the mid-run supervisor delivered INTO this attempt (supervisor.Steer). Without
    # them the verifier judges the output against the unamended request and fails the attempt for
    # doing exactly what Otto's other judge told it to do mid-flight — the same two-judges-fighting
    # collision `supervisor._prompt`'s retry_note exists to prevent, one layer further on. Placed
    # deliberately AFTER the approved-plan block: a steer amends the request, never the plan a
    # human approved, so a steered run that silently departs from that plan is still a FAIL.
    steer_rule = ""
    steered = [str(t).strip() for t in (steers or []) if str(t).strip()]
    if steered:
        steer_rule = (
            "\nThis attempt was STEERED mid-run: a supervisor watching it live judged it "
            "off-course and delivered the following correction(s) into the session, which the "
            "agent was instructed to follow:\n"
            + "\n".join(f"  - {t}" for t in steered[:5]) + "\n"
            "Judge the output against the request AS AMENDED by those corrections. Work that "
            "follows them is on-course even where it departs from the literal wording of the "
            "request, and narrower output is expected where a correction narrowed the job — "
            "never FAIL for that. The corrections came from an automated judge, not the user, so "
            "they do NOT license dropping anything the request asked for that they did not "
            "mention. The agent was told to report that it had been redirected: an output that "
            "conceals a redirection it clearly acted on is a FAIL, on the same principle as an "
            "unannounced departure from an approved plan.\n")
    # The tree the attempt was given vs. what the request describes. No judge asked this before:
    # every one of them scored "does the output satisfy the request" against whatever tree the
    # run happened to hold, so a run aimed at the wrong branch passed on the strength of a
    # well-executed change to the wrong file (`web-d2438694` -> PR #503, waved through by the
    # verifier, four review rounds and QA).
    ground_rule = ""
    gnotes = [str(n).strip() for n in (grounding or []) if str(n).strip()]
    if gnotes:
        glist = "\n".join(f"  - {n}" for n in gnotes)
        ground_rule = (
            "The platform compared the request against the branch checked out for this run and "
            f"found these contradictions BEFORE the attempt ran:\n{glist}\n"
            "The output is judged knowing that. It PASSES on this point if it reports the "
            "mismatch — saying the code described is not on this branch is a correct and "
            "complete answer, not a failure to deliver. It also passes if the mismatch was "
            "benign and the output explains why (a file it was asked to create, a line number "
            "in a file it grew).\n"
            "It FAILS if the output silently edited something else instead — a similarly-named "
            "file, a different function, the nearest plausible target — and presents that as the "
            "requested change without mentioning the discrepancy. Say in the critique that the "
            "work looks aimed at the wrong branch or revision, and that the next attempt should "
            "report this rather than pick a substitute target.\n")
    rbody, rnote = _eng()._clipped(result or "", _VERIFY_RESULT_CHARS)
    prompt = (
        "You are a strict quality reviewer for an automation platform. A capability was run "
        "to satisfy a user's request. Judge whether the OUTPUT genuinely fulfils the REQUEST — "
        "not whether it is well written, but whether the task was actually accomplished.\n\n"
        + (conv + "\n\n" if conv else "")
        + f"Capability: {cap.name} ({cap.description[:160]})\n"
        + (contract + "\n\n" if contract else "")
        + grant + "\n"
        + dead_end
        + _JUDGE_REASONING_RULE + "\n"
        f"Request: {request}\n\n"
        f"Output:\n{rbody}{rnote}\n\n"
        # The plan comparison sits LAST, immediately before the verdict is asked for. Placed up
        # with the preamble it was obeyed about half the time (measured 1/2 on
        # regress `verify-fails-silent-departure`); adjacent to the decision, with the checks
        # spelled out as questions, it holds. Recency matters more than order-of-topic here.
        + plan_rule
        + steer_rule
        + ground_rule
        + "Reply with PASS or FAIL on the first line. If FAIL, add a second line with a short, "
        "specific critique of what is missing or wrong so the next attempt can fix it.")
    # A FAIL costs a retry — and on a write cap, the retry is what widens blast radius — so it has
    # to reproduce before it is acted on. A PASS returns on the first sample.
    verdict = confirm_adverse("verify", prompt, _parse_verdict, lambda v: not v["passed"])
    # Who reached this verdict. Only a "judge" verdict is evidence about the CAPABILITY — see
    # `error_verdict` for the two impostors that used to be counted as one.
    verdict["source"] = "judge"
    # WHICH judge. Without it, a wrong verdict is unattributable after the fact: the trail records
    # that a run failed verification and what the critique said, but not whether a Claude tier or
    # a local model said it — so a bad judge model and a bad capability look identical, and
    # `scorecard`'s false_fails cannot be split by judge. Best-effort: gateway.last() reflects the
    # call this verdict came from, and a missing entry must not break a verdict.
    verdict["model"] = ((gateway.last("verify") or {}).get("model")
                        if hasattr(gateway, "last") else None)
    trace("VERIFY", "PASS" if verdict["passed"] else f"FAIL — {verdict['critique'][:120]}")
    return verdict


def qa_review_request(pr_url, repo, request):
    """The instruction handed to the QA capability (default agent:sre-qa) for the post-PR
    loop. Pure (unit-testable). Tells QA to validate the PR empirically in dev/staging,
    tear down, confirm zero residue, and end with a one-word verdict the judge can parse."""
    repo = f" in repo `{repo}`" if repo else ""
    return (
        f"QA this pull request{repo} before it merges: {pr_url}\n\n"
        f"It was opened to satisfy this request:\n{request}\n\n"
        "Design and run a reversible empirical test in a safe environment (dev/staging only), "
        "prove whether the change actually behaves as intended, then tear everything down and "
        "confirm it left zero residue. Do NOT mark the PR ready or merge it — only validate.\n\n"
        "End your reply with a final line that is exactly one of: PASS (the change is proven to "
        "work and is safe to merge), FAIL (a concrete defect or unmet requirement — state it), "
        "or INCONCLUSIVE (could not be empirically proven either way — say why).")


def judge_qa(request, qa_result, project=None):
    """Read the QA capability's transcript and distil it to {verdict, critique} where
    verdict is 'pass'|'fail'|'inconclusive'. Runs on the 'verify' tier (a cheap judge over
    QA's own findings, not a fresh test). FAIL's critique is folded into the next fix round;
    INCONCLUSIVE and FAIL-after-budget both stop the loop for a human. `project` injects the
    target repo's own CLAUDE.md conventions (see verify())."""
    if not qa_result or qa_result == "(no output)":
        return {"verdict": "inconclusive", "critique": "The QA capability produced no output."}
    trace("QA", "judging QA transcript")
    conv = conventions.judge_block(project, request) if project else None
    prompt = (
        "You are reading the transcript of a QA/test agent that was asked to empirically "
        "validate a pull request. Classify the OUTCOME it reached — not the writing quality.\n\n"
        + (conv + "\n\n" if conv else "")
        + f"Original request the PR addresses: {request}\n\n"
        f"QA transcript:\n{qa_result[:6000]}\n\n"
        "Reply with PASS, FAIL, or INCONCLUSIVE on the first line:\n"
        "  PASS — QA empirically proved the change works and is safe to merge.\n"
        "  FAIL — QA found a concrete defect, regression, or unmet requirement.\n"
        "  INCONCLUSIVE — QA could not prove it either way (e.g. blocked, partial, "
        "or its own verdict was inconclusive).\n"
        "On the next line(s), give a short, specific summary of what's wrong or unproven so "
        "the next fix attempt can act on it (omit if PASS).")
    verdict = confirm_adverse("verify", prompt, _parse_qa_verdict, _qa_adverse)
    trace("QA", f"verdict={verdict['verdict']} — {verdict['critique'][:120]}")
    return verdict


def review_request(pr_url, repo, request):
    """The instruction handed to the review capability (default: the stock code-reviewer) for the
    post-PR review loop. Pure (unit-testable). Tells the reviewer to read the PR diff and
    review it strictly WITHOUT changing anything — the platform folds any findings into a fix
    on the same branch. This is the code-review sibling of qa_review_request (which validates
    empirically); together they mirror sre-minion's self-review + iterate phases."""
    repo = f" in repo `{repo}`" if repo else ""
    return (
        f"Review this pull request{repo} as a strict code reviewer before it merges: {pr_url}\n\n"
        f"It was opened to satisfy this request:\n{request}\n\n"
        "Fetch the diff with `gh pr diff` / `gh pr view` (and `gh api` for anything else about "
        "the PR) and review THAT — do not clone, `cd` into, or read files from any local "
        "checkout, even one you can find on this machine. A local checkout can be on the wrong "
        "branch, hold uncommitted changes, or simply be a different repo than the one this PR "
        "is against (observed live: a reviewer browsed local files instead of the PR diff and "
        "wandered into an entirely unrelated repo mentioned only in passing in the ticket text). "
        "The PR's own diff via `gh` is the only source of truth for what this review judges.\n\n"
        "Review it for correctness bugs, security/IAM issues, resource-lifecycle and "
        "blast-radius concerns, hardcoded values, missing error handling, and deviations from "
        "the repo's own conventions. Do NOT modify code, push, comment on the PR, or mark it "
        "ready — review only; the platform addresses findings.\n\n"
        "Review the CODE. The PR description is not a deliverable you are reviewing: never "
        "raise a finding asking for it to be extended, restructured, or to record what this "
        "review found — a fix round acts on your findings verbatim, and a description that "
        "grows a section per round becomes a log of Otto's own attempts instead of a "
        "description of the change (measured: webapp#565).\n\n"
        "End your reply with a final line that is exactly one of: PASS (no must-fix or "
        "should-fix findings — safe to merge), CHANGES (there ARE must-fix/should-fix findings — "
        "list each concisely so they can be addressed), or INCONCLUSIVE (couldn't review, e.g. "
        "an empty diff — say why).")


def judge_review(request, review_result, project=None):
    """Read the review capability's transcript and distil it to {verdict, critique} where
    verdict is 'pass' (clean) | 'fail' (must/should-fix findings) | 'inconclusive'. Runs on the
    'verify' tier. FAIL's critique (the findings) is folded into the next fix round; INCONCLUSIVE
    and FAIL-after-budget both stop the loop for a human. Reuses the pass/fail/inconclusive
    vocabulary of judge_qa so _parse_qa_verdict maps it directly. `project` injects the target
    repo's own CLAUDE.md conventions (see verify())."""
    if not review_result or review_result == "(no output)":
        return {"verdict": "inconclusive", "critique": "The review capability produced no output."}
    trace("REVIEW", "judging review transcript")
    conv = conventions.judge_block(project, request) if project else None
    prompt = (
        "You are reading the transcript of a code reviewer that reviewed a pull request. "
        "Classify the OUTCOME — does the PR need changes before it can merge? Judge the "
        "findings, not the writing quality.\n\n"
        + (conv + "\n\n" if conv else "")
        + f"Original request the PR addresses: {request}\n\n"
        f"Review transcript:\n{review_result[:6000]}\n\n"
        "Reply with PASS, FAIL, or INCONCLUSIVE on the first line:\n"
        "  PASS — no must-fix or should-fix findings; the PR is clean to merge.\n"
        "  FAIL — the review raised must-fix or should-fix findings (correctness, security, "
        "blast-radius, robustness, unmet requirement).\n"
        "  INCONCLUSIVE — the review couldn't assess it (empty diff, blocked, or its own "
        "verdict was unclear).\n"
        "On the next line(s), list the concrete findings to address so the next fix attempt can "
        "act on them (omit if PASS).")
    verdict = confirm_adverse("verify", prompt, _parse_qa_verdict, _qa_adverse)
    trace("REVIEW", f"verdict={verdict['verdict']} — {verdict['critique'][:120]}")
    return verdict


def error_verdict(result):
    """A synthetic FAILED verdict for an attempt that errored/timed out (is_error). Used so the
    timed-out `(timed out)` string is never handed to the verifier as if it were real output —
    the attempt just counts as a failure and the retry/escalation ladder gets its next shot.
    A supervisor-killed attempt gets the supervisor's own critique as steering — that IS the
    enforce-mode mechanism: kill, then restart with the course-correction folded in.

    Both carry `source` so they are never mistaken for a judgement downstream. They are shaped
    like a verify FAIL because the LADDER needs them to be (a failed attempt, steered), but no
    judge read any output: measured over the trail, 98 of 291 recorded verify failures were one
    of these two, and `scorecard` was pricing all of them as the capability's fault."""
    s = str(result)
    if "(aborted by supervisor:" in s:
        reason = s.split("(aborted by supervisor:", 1)[1].strip().rstrip(")").strip()
        return {"passed": False, "source": "supervisor",
                "critique": ("the mid-run supervisor stopped the previous attempt because it was "
                             f"off-course: {reason} — take a different approach this time.")}
    return {"passed": False, "source": "harness",
            "critique": "prior attempt errored or timed out: " + str(result)[:200]}


def _is_duplicated(text, block=400, threshold=3):
    """True when a result re-emits its own opening block `threshold`+ times — the shape of an
    accretion blob a local model produces by restarting its answer across output-limit cutoffs
    (run web-96799819). Model-agnostic: measures the OUTPUT shape, not any model's wording, and a
    verbatim ~400-char opening repeated three times is never legitimate content (so structured but
    distinct output — an enumerated review — is not flagged). Belt-and-braces behind the local
    runtime's own restart guard, which stops the accretion at the source."""
    norm = " ".join((text or "").split())
    if len(norm) < block * threshold:
        return False
    return norm.count(norm[:block]) >= threshold


def guard_resume_result(result):
    """A model-agnostic delivery floor for the verify-less resume/follow-up path. Resume runs skip
    BOTH the verify loop and the supervisor, so a leaked or duplicated turn would ship to the chat
    unchecked (run web-96799819: a follow-up delivered a 97KB blob of repeated reasoning). Strip a
    fenced think-stream, and replace a self-duplicated accretion blob with an honest short notice.
    NOT a correctness judge — resume is deliberately not judged — only a shape guard."""
    cleaned = gateway._strip_reasoning(result) if result else result
    if _is_duplicated(cleaned):
        return ("⚠️ The model's reply looped — it restarted its answer repeatedly instead "
                "of finishing (often an output-token-limit cutoff on a local model), so nothing "
                "usable was produced. Send the message again, or raise OTTO_LOCAL_EXEC_MAX_TOKENS.")
    return cleaned or result
