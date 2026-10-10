**TLDR** — PR 16 works as the ticket describes: install, lint and test all pass on a clean checkout, and `make run` starts the current terminal game.

- I tested a fresh clone of the PR branch (commit 7990538) in a throwaway virtualenv on Python 3.13.
- `make install` pulled the pinned versions: Flask 3.1.3, ruff 0.17.0 and pytest 8.4.2.
- `make lint` passed (`ruff check` and `ruff format --check`, 16 files). `make test` passed 95 tests. The old `unittest discover` run also passes all 95.
- The `truco/` and `tests/` changes are only ruff reformatting and import ordering, so gameplay is unchanged.
- `make run` falls back to `python -m truco` because no web module exists yet, and it launched the game. `make cli` also launched it.
- `GOALS.md`: only the Product section changed, and it now holds the full feature list from the ticket with no placeholder text. The Seed and Definition of Done sections are untouched.
- The README uses the make targets and keeps the rules summary. `.gitignore` gained entries for build and cache files.
- `ci.yml` has the right triggers and steps (Python 3.11, then `make install`, `make lint`, `make test`). I couldn't run it on GitHub, so CI passing there is unproven. Locally it ran on Python 3.13, not 3.11 or 3.9.
- `pyproject.toml` lists `packages = ["truco"]`, so a future `truco.web` subpackage would need adding there for non-editable installs. This doesn't affect the PR now.
- Cleanup: I deleted my scratch directory `/tmp/qa-truco.Lhhb` and nothing in the repo was touched. Two other directories, `/tmp/qa-truco.6Dol` and `/tmp/qa-truco.xxio`, appeared to come from earlier runs (they hold scripts I didn't write), so I left them.

PASS