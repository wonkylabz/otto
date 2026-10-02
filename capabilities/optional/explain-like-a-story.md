---
name: explain-like-a-story
description: >
  Explains a design doc, PR, architecture, incident or subsystem to a smart non-engineer as
  a plain-language story built on one sustained real-world analogy, with a before/after
  ASCII diagram and the honest messy parts left in. Read-only. Use only when the request
  asks for that register: "explain this simply", "like a story", "ELI5", "explain this
  for my manager/PM". Not for debugging or ordinary technical questions.
---

# Explain like a story

Turn a dense technical artifact into something a smart non-engineer follows in one read,
without dumbing down the trade-offs.

**Bar:** the reader can predict the failure mode from the analogy alone. If not, the
analogy is decoration — replace it.

## 1. Read the actual source

| Source | How |
| --- | --- |
| GitHub PR / issue | `gh pr view <n> -R <repo> --comments` + `gh pr diff`, or `gh issue view <n> -R <repo> --comments` |
| Confluence / Jira URL | the Atlassian connector if this run has it; else say it is unreachable |
| Local file / repo path | read it |
| Pasted text | use as-is |
| Nothing named | say which doc, PR or system is needed and stop |

Never storytell from a title or summary field — the caveats live in the body. While
reading, hunt for:

- **The problem in one sentence** — what is currently bad, not the solution.
- **The single load-bearing decision** — the one idea doing the real work.
- **What already went wrong** — past outages, failed attempts.
- **The honest gaps** — risks, open questions, unowned work.
- **The scope boundary** — what people will wrongly assume this covers.

If the source contradicts itself, say so in the story; never smooth it over.

## 2. Choose one analogy, and pressure-test it

Pick a domain from ordinary physical life — buildings, doors, mail, restaurants, keys,
queues, libraries — never another technical domain. Test: **does every moving part map,
including the broken ones?** If you must drop the analogy to explain the risks, pick
another. Try 2-3; keep the first that maps everything. Then commit — one analogy, end to end.

| System shape | Domain that usually fits |
| --- | --- |
| auth / identity / spoofing | doors, bouncers, badges, passports |
| shared credential → per-caller credential | one house key everyone copies → individual keys |
| caching / TTL | a desk drawer vs the warehouse downstairs |
| queues / backpressure | a kitchen with a ticket rail |
| rate limiting / noisy neighbour | one bar tab everyone drinks from |
| migration / cutover | moving house while still living in it |
| retries / idempotency | posting the same letter twice |
| feature flags | a light switch vs rewiring the house |
| observability gaps | a shop with no receipts |

## 3. Write it

Sections in this order; drop one only when the source has nothing for it (never drop the
messy parts if the source names any risk).

```
**The story: <the analogy in one line>.**

<2-4 sentences: today's situation, told entirely inside the analogy.>

So <N> problems:
1. **<Problem in analogy terms>.** <Consequence, still in analogy terms.>

**The fix: <the change, in analogy terms>.**

<ASCII before/after diagram, ≤80 columns>

The clever bit is <the one load-bearing insight, as an inversion or reframe>.

**Why anyone cares:**
- <practical consequence>

**The messy real-world parts:**
- **<Risk / caveat>.** <Honest, in analogy terms.>

<One-line scope note: what this is NOT.>
```

One screen: 250-450 words of prose plus the diagram. Over 450 → the analogy is not doing
enough work.

## Rules

- **Be honest about the ugly parts** — the single point of failure, the unowned work, the
  attempt that caused an outage. That section is where trust is earned.
- **Translate the mechanism, not the vocabulary.** Introduce a real term only where the
  reader will meet it again, attached once to its analogy counterpart.
- **Don't condescend.** The reader is intelligent and just doesn't work in this area.
- **Keep the trade-off visible.** If the decision sounds obvious, you hid its cost.
- **Name the inversion** in one sentence — the line the reader repeats to someone else.

## Example (compressed)

Source: a design doc replacing one shared API token plus an unverified self-declared
`X-Client` header with per-app keys verified at the gateway, which then overwrites the
header with the verified name.

> **The story: the nightclub with one password and self-written name tags.** One door, one
> password everyone says, and you write your own name on a badge nobody checks. So anyone
> can claim to be anyone, and kicking one guest out means changing the password for all.
> **The fix:** each guest gets their own password, and the bouncer writes the badge.
> **The clever bit:** the badge stops being something you bring and becomes something the
> bouncer gives you. **Messy parts:** old-password guests are stamped `legacy` until that
> count hits zero; services already inside the building never pass the bouncer; and the
> bouncer is now a single point of failure — if he's down, the door is down.

Why it works: every moving part had a place to land, including the two worst ones; the
problems come before the fix; only two real terms survive, each because the reader will
hear them again.
