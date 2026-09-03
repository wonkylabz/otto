# Engine core — the facade and the store

`engine.py` after the module split (#: `audit.py`, `memory.py`, `contracts.py`, `judging.py`, `intents.py`, `routing.py`, `plans.py`).

- **`engine.py` is a FACADE, and callers keep addressing `engine.X`** — an extracted module resolves patch-sensitive seams back through the facade at call time (`_eng()`), never binds them at import, or every test that patches `engine._claude` silently stops taking effect.
- **A new `run_json` kwarg lands on `engine._claude` in the SAME commit** — the seam is pass-through and every test double is `_fake_claude(prompt, **k)`, so a dropped one is invisible to the suite and raises only in production: #376's `steer=` (`ClaudeSeamSignatureTests`).
- What still lives in `engine.py`: the `claude -p` bridge (`_claude`/`_usage`), the attempt runner (`run_attempt`/`execute`/`_run_ladder`), and the re-exports.
- **One store alias, `engine._DB` (=`config.DB_PATH`)** — every table is created by `engine._schema` (which lives in `audit.py`). Audit writes via `engine._append_audit`/`_append_content`, reads via `engine.iter_audit_entries()`/`iter_content_entries()`.
