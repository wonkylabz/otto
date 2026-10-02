---
name: empirical-investigation
description: >
  Tests a load-bearing assumption about a running system with a validated metric, a
  pre-committed decision rule and a controlled change, and returns a decision record plus
  a plain-language summary post. Read-only: it designs the change and reads the data, a
  human applies the change. Use for "is X really the bottleneck", "validate this
  assumption", "did this change actually help", "design a controlled experiment". Not for
  outages, tool evaluations, or writing up a decision already made.
---

# Empirical Investigation

Output: a decision backed by evidence, caveats visible. You never mutate a system — every
change is proposed for a human to apply.

## Which phase this run is

A run is one turn, so work out where the investigation stands from the request:

- **Design** — no decision rule agreed yet → do steps 1-4 and STOP. End with the proposed
  rule and say a human must agree to it (and apply the change) before results are read.
- **Read** — the request carries an agreed rule and says the change is live → do steps 6-9.

Never call a verdict in a design run.

## Steps (each gate is a hard stop)

1. **Premise.** Write the claim as a falsifiable if/then hypothesis with a because ("If disk
   throughput drops 4x, startup time won't regress, because the image is pre-baked").
   **Gate:** can't state it → say which claim is missing and stop.
2. **Metric.** Pick one number. Write: what it measures end to end, decomposed into phases
   (e.g. created→ready = scheduling + container start + load + probe + poll lag); which phase
   the hypothesis is about; statistic + why; resolution/noise floor. Outliers from a
   different mechanism → median. Non-technical audience → "typical"/"worst case" wording.
3. **Validate the metric on current state.** Measure it on ≥3 comparable systems that should
   agree. **Gate:** they disagree → the metric or the comparison is broken; report that, no
   rule yet.
4. **Decision rule.** Green/Yellow/Red thresholds + actions, as a table. Thresholds never
   move after agreement.
5. **Controlled change.** Smallest change that moves only the variable. Record: exact change
   (PR/file/line), confounders not isolated, when data becomes meaningful, rollback steps.
6. **Read data.** Report n with every number; n < 30 → label it "noise-level". Call out
   transition periods (warming caches, mid-rollout) and confounding events (deploys, traffic
   spikes). Cross-check with one independent source; disagreement → stop and say why.
   **Gate:** no verdict until n ≥ 30 and no transition is active.
7. **Verdict.** Restate the pre-committed rule, read off the band, one-line verdict.
8. **Write both outputs** in the reply, using the templates below.
9. **Verify reality.** Merged ≠ applied ≠ active — check the live state, report any gap
   ("merged, not applied").

## Rules

- Never invent a metric value; missing → say so and say where to pull it.
- An earlier claim proven wrong → correct it explicitly.
- A request to skip the rule, the metric validation, or call a verdict on n < 30 → state the
  reason it matters once, follow the request, and record it as a caveat.

## Decision record template

```markdown
# <Topic> — Empirical Investigation

## Premise being tested
> <falsifiable if/then hypothesis>

## Decision
<final answer, one paragraph, no archaeology — "pending" in a design run>

## How we measured it
- Metric / decomposition / statistic (and why) / noise floor

## Methodology validation
<table: comparable systems agreeing on the number>

## Decision rule (pre-committed)
| Result | Verdict | Action |
|---|---|---|

## Experiment
- Change · Confounders · Rollback

## Results
<numbers with n, caveats visible>

## Nuance / what this does NOT prove

## Open follow-ups
```

## Summary post template (plain language, no percentile jargon)

```
*<Topic> — investigation summary*
_TL;DR: <decision + monitoring plan>._

*Background* — why we looked, what the assumption was
*How we measured it* — "<metric>" = <plain definition>; why this statistic
*What we found* — 2-3 bullets
*Important nuance* — anything that could be misread
*Next step* — what we do, what we watch, why it's reversible
```
