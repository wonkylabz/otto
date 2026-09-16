# Testing Otto

## Unit + integration suite

`./.venv/bin/python -m unittest -v`

**Use the venv interpreter, not bare `python3`.** Temporal tests self-skip when `temporalio`
isn't importable, and Temporal is the only production path — so bare `python3` reports a green
`OK (skipped=93)` having tested nothing.

## Regression corpus

`regress.py`, `regress_cases.py`, `regress/fixtures/`.

Unit tests only assert a prompt *contains* a clause, never that the model still *obeys* it.
The corpus is the other half.

- `python3 regress.py` — cheap tier, ~2min
- `--tier all` — real `claude -p`, ~10min
- `--only <prefix>`, `-n <N>` — narrow a run

**Run it before and after editing any prompt.** Fixtures are committed, never sourced from
`data/`.

Corpus instability looks identical to a regression: before blaming an edit, hash the actual
prompt sent against a clean `git archive main` checkout. A case that flips is often served by
a different model than you think — read the `[GATEWAY] tier -> model` line first.

## Planner harness

`plan_eval.py` — plan-then-execute (`config.PLAN_MODE`) without the orchestration around it.
It calls the strong planner on one request and prints the atomic-step plan, so decomposition
quality is readable without a routing gate, an approval preview or a workflow.

- `./.venv/bin/python plan_eval.py "<request>"` — plan only; needs `claude` on `PATH`
- `--cap <name>` pins the executor capability, skipping Router #1
- `--samples` plans a few built-in multi-step requests
- `--run` also EXECUTES the plan on the configured execution model — point that at a local
  model (Admin → Execution) to test the other half: can the weak model eat the steps?

It reads the LIVE `data/` — `--run` spends real money and really runs the steps.

## Writing a guard test

A green suite is not evidence a guard works. Before committing one, **prove it fails without
the fix** — stash the source change, re-run the test, confirm it goes red. A test that passes
either way documents an intention, not an invariant.

Probe behaviour empirically where the seam is a real system (Temporal, `claude -p`, a local
endpoint): a mocked seam proves the plumbing you wrote, not the plumbing that ships.

## Documentation ceilings

`test_core.ClaudeMdBudgetTests` enforces three ratchets — resident `CLAUDE.md` bytes, total
`.claude/rules/` bytes, and over-cap lines across both. See `docs/maintaining-docs.md`.
