# Maintaining Otto's documentation

Three tiers, by who pays for the bytes:

| Tier | Where | Paid by | Holds |
| --- | --- | --- | --- |
| Resident | `CLAUDE.md` | **every session** | a constraint any edit could violate |
| Fetched | `.claude/rules/*.md` | a session editing that layer, **and every convention judge** | that layer's invariants |
| Referenced | `docs/*.md` | only a session that follows a pointer | operator + contributor documentation |

Moving a rule down a tier is a real win. Adding one at the top is a per-run tax forever.

## Which tier

- **Resident** — it binds an edit in *any* file. Keep it to one imperative.
- **Fetched** — it only bites inside one layer, or a judge should enforce it. Anything here is
  digested into judging prompts (`conventions._SOURCE_GLOBS` globs `.claude/rules/*.md`, so a
  new file in that directory is picked up automatically), so a rule that no judge could act on
  is dilution — it competes with real rules for the ranked digest budget.
- **Referenced** — how to run, install, test, or release. No judge needs it; no session that
  isn't doing that thing should carry it.

## Writing a rule

**One imperative + its why, ≤280 characters.** Longer means it's two rules — split it.

**Name a guard test instead of describing it.** `(test_core.SetupWizardTests)` beats a
paragraph reconstructing what the test asserts.

**The evidence lives in the commit message and the guard test's name, never restated here** —
no dates, measurements, incident narrative, quotes, or "user-reported". A bullet that re-tells
its own commit subject is a second copy of the git log.

**Prefer deleting a stale entry over adding one.**

## The ceilings

`test_core.ClaudeMdBudgetTests` enforces three: resident bytes, total `.claude/rules/` bytes,
and over-cap lines across both tiers. All are ratchets carrying no headroom, so adding a rule
fails the suite until something is deleted or merged.

Raising one is an explicit constant edit that shows up in the diff — that is the point. When a
move genuinely shifts bytes from resident to fetched, ratchet the resident ceiling DOWN by what
left and raise the fetched one by what arrived, in the same commit.

A rules file must also be named somewhere in `CLAUDE.md`, or nothing can find it
(`test_every_rules_file_is_reachable_from_the_resident_file`).

## The enforcement ratchet

`ClaudeMdBudgetTests` bounds what the docs COST. `test_core.RuleEnforcementTests` bounds what
they are WORTH: a rule must name a guard test, or sit in that class's `UNGUARDED` set.

The set is seeded with every rule that had no guard when it was added, so the suite is green on
day one and the debt is one readable list. It only shrinks:

- a **new** rule naming no test fails `test_no_new_rule_lands_without_a_guard` — name a guard,
  or add its key to `UNGUARDED` and accept that the list grew in your diff;
- **guarding** an existing rule fails `test_the_known_gap_list_has_no_stale_entries` until you
  delete its entry, so paying the debt down is what removes the line;
- a rule citing a test that no longer exists fails `test_every_cited_guard_test_exists` — a
  renamed test otherwise leaves the rule reading as enforced when nothing checks it.

Keys are `<file>:<bold title, backticks stripped, 70 chars>`. Rewording a rule's title changes
its key, which is deliberate: a rewritten rule is worth re-asking whether it can be guarded now.

Not every rule can be a grep. "A plan is enumerated in edit order but approved in deploy order"
is a judgement, and it stays in `UNGUARDED` forever. The point is not that the list reaches zero
— it is that adding an unenforceable rule is a visible act with a cost, not a free assertion.

**Why it exists**: "JSON state writes go through `storage.mutate_json`" was resident, correct
and load-bearing, and four modules violated it for months. The suite could not read it.
