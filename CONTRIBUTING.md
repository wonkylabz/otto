# Contributing to Otto

Issues and PRs are welcome. Otto is a personal project, so review may be slow — for anything
substantial, open an issue first so we don't both build it.

## Setup

```bash
./install.sh --no-service     # venv + deps + Temporal CLI + smoke tests, no background service
./run.sh                      # Temporal dev server + worker + web UI on http://localhost:8765
```

You need the [`claude` CLI](https://claude.com/claude-code) installed and logged in. Otto runs
on a Claude **subscription** via `claude -p` — no API key required. Linux and macOS only.

## Tests

```bash
./.venv/bin/python -m unittest
```

**Use the venv interpreter, never bare `python3`.** The Temporal tests self-skip when
`temporalio` isn't importable, and Temporal is the only production run path — so bare
`python3` prints a green `OK (skipped=93)` having tested nothing. This trips everybody once.

Nothing in the suite touches the network or spends tokens.

Two more things that will surprise you:

- **`test_core.ClaudeMdBudgetTests` is a byte ratchet** on `CLAUDE.md` and `.claude/rules/`.
  It carries no headroom, so adding a line of documentation fails the suite until you delete
  or merge another one. That is the point — see `docs/maintaining-docs.md`. Raising a ceiling
  is an explicit constant edit that shows up in the diff.
- **`test_core.OpenSourceHygieneTests`** fails if a tracked file names a real person, company
  or internal hostname. The comments here cite the real incident behind each guard, which is
  exactly why the examples must stay fictional (`acme-corp`, `example.com`). Keep the
  reasoning, rewrite the name.

## Editing prompts

Otto is mostly prompts. Unit tests can only assert that a prompt *contains* a clause — never
that a model still *obeys* it. The other half is the regression corpus:

```bash
python3 regress.py            # cheap tier, ~2 min
python3 regress.py --tier all # real `claude -p`, ~10 min, spends tokens
```

**Run it before and after any prompt change**, and read `docs/testing.md` first — a case that
flips is often served by a different model than you assume.

## Writing a guard test

A green suite is not evidence a guard works. Before committing one, **prove it fails without
the fix**: revert the source change, re-run, confirm it goes red. A test that passes either
way documents an intention, not an invariant.

## Where things live

`CLAUDE.md` is the working guide for editing this codebase — architecture, the layer table,
and the conventions that bind any change. Each layer's invariants live in
`.claude/rules/<layer>.md`; read the one for the layer you're touching before you edit it.
Operator documentation is in `docs/`.

Those files are written for Claude Code as much as for you: Otto is developed with it, and the
rules files are digested into its own convention judges. Follow the same tiering when you add
a rule — `docs/maintaining-docs.md` explains which tier it belongs in.

## Style

Match the surrounding code. In particular: comments here explain **why** a guard exists,
usually citing the failure that caused it, because that is the thing a future reader cannot
recover from the code. Short comments, load-bearing ones, no narration of what the line does.

## Security

Please don't open a public issue for anything exploitable — see [SECURITY.md](SECURITY.md).
