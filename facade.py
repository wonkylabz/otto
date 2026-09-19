"""Late-bound access to the `engine` facade — the one copy.

`engine.py` re-exports the API of the seven modules split out of it (`audit`, `memory`,
`contracts`, `judging`, `intents`, `routing`, `plans`), and callers still address `engine.X`.
An extracted module therefore has to reach names that live on the facade.

What actually matters is reaching them through the MODULE, never binding the value: `from engine
import _DB` captures whatever `_DB` was at import time, and every test that later replaces
`engine._DB` (or `_claude`, or `_extract_solution`) silently stops taking effect — a test that
passes while testing nothing. `engine.X` through the module object is live either way.

Deferring the `import engine` to call time is the second half, and it buys something narrower:
`engine` imports these modules at ITS module scope, so a module-scope `import engine` here would
run during that cycle. Binding a partially-initialised module is harmless in CPython, which is
why swapping this for a top-level import passes the whole suite — it is a robustness margin
against the cycle, not the patching guarantee. Stated precisely because the six copies this
replaced all claimed the stronger version.

Six modules each carried a copy of this four-line function, and four of the six docstrings said
"same contract as audit._eng" — which is the tell. It is one contract, so it is one function.

WHY THIS EXISTS AT ALL is worth stating plainly, because it is not a production requirement:
production never assigns to an `engine` attribute. The test suite does, 443 times. The
indirection is the shape the testing style presses into the code. Converting those call sites to
explicit injection is a real piece of work with its own risk; until someone does it, this keeps
the cost to one place instead of six.
"""


def eng():
    """The live `engine` module. Read attributes OFF it — that is what keeps a patch visible."""
    import engine
    return engine
