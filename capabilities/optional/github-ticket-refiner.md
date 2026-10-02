---
name: github-ticket-refiner
description: >
  Refines ONE existing GitHub issue into an implementation-ready ticket: grounds every
  claim in the repo(s) it touches, asks only load-bearing questions, scores the original's
  readiness 1-5 and ends on a VERDICT line (READY / NEEDS-INPUT). Rewrites the issue only
  when the request asks to apply it. Use for "refine #N", "is #N ready", "prep this ticket
  for implementation". Never implements the ticket and never creates new issues.
---

# GitHub Ticket Refiner

Worst outcome = a polished ticket that is wrong. Grounding prevents it.

**Scope:** you refine ticket TEXT. You never implement what it describes, never touch
labels, board columns or assignees, and never create issues.

## Input

Issue as URL, `owner/repo#N`, or `#N` with the repo named in the request. No repo
resolvable → say which input is missing and stop.

Apply = the request explicitly asks to apply / update / write the issue. Anything else =
propose only, write nothing.

## Gap rule (apply to every gap)

| Gap | Action |
|---|---|
| Answerable from repo / docs / linked evidence / a read-only query | Resolve it. Cite `path:line` or the query. Never ask. |
| Load-bearing and only a human can decide (intent, target value, which-of-N, priority/scope, business call) | Ask. |
| Not load-bearing | Proceed; record under "Evidence & assumptions". |
| Grounding contradicts the ticket's premise (already exists, wrong mechanism) | Load-bearing → ask or state the correction. Never silently change the goal. |

## Steps

1. Read: `gh issue view <n> --repo <owner/repo> --json number,title,body,labels,comments`.
2. Write down the deliverable + why in one sentence. Refining ≠ rescoping.
3. Ground in **every repo the ticket touches** — a claim about repo B needs repo B read. Use
   the working checkout if it is that repo, else `gh api repos/<o>/<r>/contents/<path>`,
   else a shallow clone into a temp dir. Verify: exact files/paths; the repo's real
   mechanism for the ask; whether it is already done or a no-op; every factual claim in the
   ticket (verified vs assumption).
4. Score the ORIGINAL ticket (rubric below) + a one-line rationale naming its biggest gap.
5. Verdict: any load-bearing gap left → `NEEDS-INPUT`, else `READY`.
6. Present:
   - READY → action-led title + body in the Definition of Ready template, rewritten clean.
   - NEEDS-INPUT → one comment with all questions batched; each offers options with the
     recommended one first, says why it is load-bearing, and lists what was already resolved.
   First line of the body/comment: `> Readiness (before refining): ★★☆☆☆ (2/5) - <rationale>`.
7. Only when applying, write via REST (`gh issue edit` can silently no-op):
   ```bash
   gh api --method PATCH repos/<o>/<r>/issues/<n> -f title="<title>" -f body="$(cat <file>)"   # READY
   gh issue comment <n> --repo <o>/<r> --body-file <file>                                       # NEEDS-INPUT
   ```
   Then re-read the issue and confirm the change landed.
8. Last line of the reply, exactly one of:
   ```
   VERDICT: READY · Readiness (before): ★★☆☆☆ (2/5)
   VERDICT: NEEDS-INPUT · Readiness (before): ★☆☆☆☆ (1/5)
   ```

Bundled deliverables → keep the primary, move the rest to Out of scope.

## Definition of Ready (body template)

Drop a section only when it truly does not apply.

```markdown
## Objective
<1-2 sentences led by the imperative verb. One deliverable.>

## Context
<why, with links to incident/discussion/evidence>

## Where
<verified paths per repo; what NOT to touch>

## Change
<the change in the repo's real mechanism and terms>

## Acceptance criteria
- [ ] <objectively checkable>

## Out of scope
<non-goals, follow-ups>

## Evidence & assumptions
<verified (with refs) vs assumed; accepted tradeoffs>
```

## Readiness rubric (score the original, be strict)

| Score | Meaning |
|---|---|
| ★☆☆☆☆ 1 | Intent only (a title or one sentence) |
| ★★☆☆☆ 2 | Has a why, but no files/mechanism/ACs, or premise unverified/partly wrong |
| ★★★☆☆ 3 | Real context, missing either grounding or verifiable ACs |
| ★★★★☆ 4 | Grounded, clear change, ACs exist; minor gaps |
| ★★★★★ 5 | Hand straight to an implementer |

## Example

"Add a grace window before paging on self-healing alerts." Grounding: the alert conditions
in Terraform already use a ~5 min `threshold_duration`; auto-resolve is the vendor default;
one condition causes ~75% of pages. → `NEEDS-INPUT · ★★☆☆☆` — asks only which condition(s)
and the target window (with a default); does not ask where or how.
