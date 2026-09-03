# Operating Otto

Running the service, not editing it. `CLAUDE.md` is the editing guide; this file is what an
operator or a first-time setup needs, and is deliberately outside `.claude/rules/` so no
session and no convention judge pays for it.

## Setup

- **Fresh setup**: `./install.sh` (idempotent) — venv, Temporal CLI, `.env`, tests,
  systemd/launchd `--user` service. Hard-fails if `claude` isn't on `PATH`. `--guided` adds
  `setup_wizard.py`, which offers a fix per `doctor.py` gap.
- **A new `doctor.py` gap that a human could fix wants a `setup_wizard.STEPS` entry** — the
  doctor only names gaps. A wizard step must no-op without a TTY (install.sh is the unattended
  path) and never overwrite a set value (`test_core.SetupWizardTests`).
- **After any rename or directory move, re-run `systemd/install.sh` (or `launchd/install.sh`)**
  — `config.DATA_DIR` freezes to the import-time cwd, so a stale service silently reads a
  phantom empty `data/` at the old path. Diagnose by comparing `curl localhost:$PORT/api/chats`
  against `sqlite3 data/otto.db "SELECT COUNT(*) FROM chats"`.

## Running

- **Default**: `./run.sh` — Temporal dev server + `worker.py` + `server.py`. Temporal is
  REQUIRED (#278; the direct path and `otto.py` are gone).
- **`run.sh` supervises all three children**: any child death exits 1, a signal stop exits 0 —
  launchd must not read an operator stop as a crash. No `wait -n`; macOS bash is 3.2. Pin the
  port via `PORT=` in `.env`.
- **Background service** (`systemd/`, `launchd/`): per-**user** unit, never root — `claude -p`
  runs as the user. Their PATH excludes `~/.local/bin` where `claude` lives, so the unit must
  set `PATH`; Linux needs `loginctl enable-linger`, macOS a login session.
- **Temporal**: `server.py` is just a client; `worker.py` must run or workflows don't progress.

## Restarting safely

- **Restart the worker after changing any module it imports**, or the edit has no effect and
  the fix looks failed. `OttoWorkflow.run` takes a dict param — restart after changing it too.
- **Restarting mid-activity costs that run its attempt, within `_HEARTBEAT`** (3min) — the
  execution activities beat (`activities._heartbeats`), so a killed worker surfaces there
  rather than at the 40min `_EXEC_CEILING`. It is still a lost attempt, so restart when
  `temporal workflow list --query "ExecutionStatus='Running'"` is empty.

## Stopping work

- **Global pause**: `data/ESTOP` (`estop.py`, `POST /api/estop`) stops every ingress starting
  new work, never kills in-flight — nothing re-checks it mid-activity, and the lever stopping
  the service can't be. Anything at that path pauses, however malformed. The control is in the
  page HEADER, not Admin (`.claude/rules/ui.md`).
