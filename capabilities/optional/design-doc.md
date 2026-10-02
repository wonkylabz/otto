---
name: design-doc
description: >
  Writes or revises a design doc for a technical change — decisions, trade-offs, risks —
  in a fixed template with a length budget, and handles reviewer-feedback rounds without
  letting the doc grow into an implementation plan. Use for "write a design doc",
  "document this decision", "apply these review comments to the design doc". Not for
  evaluating a new tool or testing an assumption.
---

# Design Document

A design doc aligns reviewers on **decisions, trade-offs and risks**. It is not an
implementation guide, runbook or build spec — that detail belongs in a separate
implementation plan, linked from the doc.

## Process

1. Title from the request. None → say a title is needed and stop.
2. Output: the path the request names; else, inside a repo, `docs/design/YYYY-MM-DD-<slug>.md`;
   else return the full doc in the reply. An existing file is a reviewer round → edit it,
   never overwrite it.
3. Verify every claim about an existing tool/system against source (code, docs, read-only
   live query) before writing it.
4. Fill the template. Omit **Asks** unless sign-off/spend/cross-team commitment is needed,
   and **What would change my mind** unless the recommendation is non-obvious.
5. Length check (`wc -l`) and the push-to-plan triggers below.
6. Reply summary (≤6 lines): path, line count, TL;DR, open-question count, items moved out.

## Template

```markdown
# YYYY-MM-DD <Title>

> **TL;DR.** ≤4 sentences. Problem; proposed approach; what the POC/test validates; main open risk.

## Asks
- Specific decision, spend, or cross-team commitment the doc requests.

## Context
Current state and why this is on the table now. 2–6 sentences.

## Proposed Solution
### Approach
High-level shape — *what* and *why this shape*.
### Key decisions
- **Decision** — rationale (one sentence).
### Architecture
Small Mermaid or ASCII diagram (≤25 lines).

## Risks
| Severity | Risk | Mitigation |
|---|---|---|
| High / Medium / Low | One sentence | One sentence; detail goes to the implementation plan |

## Verification
Load-bearing exit criteria only (≤6).

## What would change my mind
Assumptions whose change flips the recommendation.

## Open Questions
- Unresolved items needing input. Distinct from risks (which have mitigations).

## References

> Implementation detail lives in the linked implementation plan: *(link or "TBD")*
```

## Length budget

Target 250–400 lines, hard stop at 500. Move to the implementation plan: code blocks > 10
lines, attribute tables > 8 rows, verification lists > 6 items, mitigations that turn into
paragraphs, diagrams > 25 lines.

## Authoring discipline

- TL;DR is mandatory.
- Verify before asserting, especially "tool X can't do Y".
- A cost resting on >2 layers of estimation → order of magnitude ("low-thousands $/yr").
- Layer the audiences: TL;DR/Asks for skimmers, Risks/Verification for reviewers.
- Bullets and tables over paragraphs; short sentences, active voice; no emojis.

## Reviewer rounds

Classify each feedback item before acting:

- **(a) Wrong claim** — fix it in the doc.
- **(b) Scope expansion / new section / more detail** — defer to the implementation plan;
  answer with a one-line summary.
- **(c) Precision or clarification** — one-line edit, no new section.

Most "could you also add…" asks are (b); the default is *not* to grow the doc. Content the
author deliberately cut stays cut. A corrected fact removes the conditional language that
depended on the old assumption.

After every round of cuts, audit for dangling references: `§ Section` links, numbered
references (`Risk #3`), "see above/below" pointers, orphaned footnotes.
