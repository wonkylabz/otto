# Changelog

Notable changes to Otto, newest first. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Otto versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) as `docs/releasing.md` defines it
for a service you clone and run.

Entries are written for an **operator upgrading a running install**: what changes for them,
and what they must do about it. The commit log already holds the engineering narrative — a
changelog that restates it is a second copy of `git log`.

## [Unreleased]

### Fixed

- **Adding Fable in Admin now works.** A one-off migration for a since-revived model was
  stripping every pool entry whose name or id contained `fable` on every read, so the model
  saved without an error and was gone by the time the page reloaded. Fable is also offered as a
  Claude tier now (`claude-fable` -> `claude-fable-5-1`). An entry you added before this fix was
  stored, only hidden, so it reappears on upgrade with nothing to re-enter.

- **A browser tab left open no longer polls Otto forever when the server is down.** The UI's
  timers now pause while the tab is in the background and back off when a read fails (doubling
  to 60s, back to full rate on the first success), instead of holding a fixed rate for the life
  of the tab. Nothing to do: a hidden tab refreshes the moment you switch back to it.

## [0.2.0] - 2026-09-19

### Changed

- **The approval plan can be written by a non-Claude model.** The PLAN phase (Admin → phase
  matrix) accepts a local or hosted model: Otto now has a read-only plan mode of its own, so
  the pick runs for real instead of silently falling through to Sonnet.
  **If your `data/models.json` already had a non-Claude model assigned to `preview`,** it was
  being ignored and Sonnet wrote every plan — after this upgrade that assignment starts taking
  effect, and your plans will change. Re-tick the PLAN column if you want Sonnet back. This
  phase has no retry ladder above it and its output is what you approve, so pick deliberately.
- The plan pass's shell is confined by `bwrap` (read-only filesystem, scratch `/tmp`, Otto's
  own state masked) where the kernel supports it, and by an argv-only command allowlist where
  it does not — the transcript records which served each run. Neither confines the network:
  the approval gate is still what stands between a plan pass and a remote write.

- **An MCP server you add is now stored inactive until you activate it.** Registering one
  writes a command line that Otto spawns on your machine as you, so adding it and allowing it
  to run are now two steps: Admin → MCP servers shows a new server with the exact command line
  and an Activate button. **Servers already in `data/mcp-servers.json` keep working** — the new
  flag is only applied to what is added from here on, including profile/bundle imports. Adds,
  activations and removals are recorded in the audit trail.

### Added

- The running version is shown beside the wordmark in the header, with the commit sha in its
  tooltip, and served by `/api/health` — so "which build is this?" is answerable from the
  browser during a support conversation.
- This changelog, and `docs/releasing.md`: what Otto's version number promises an operator,
  and `release.py` to cut a release from it.

### Fixed

- **The swarm board no longer calls a hosted frontier model "local".** A card's model chip
  names the class of model that served the attempt — `local ·` for a model on your own
  endpoint, `hosted ·` for a vendor API — instead of prefixing both "local ·" because Otto's
  own runtime drives them the same way. Finished runs keep the label they were recorded with.
- **A killed run no longer leaves its commands running.** `claude -p`, the local runtime's Bash
  tool and each MCP server now get their own process group, and every kill path signals the
  group. Before this, a watchdog or supervisor abort firing mid `Bash(terraform apply …)` killed
  only the CLI — the command itself, and Claude Code's own MCP servers, kept running while the
  ladder started a retry in the same workspace. **If you have been restarting the worker to
  clear stray `npx`/`node` processes, that should stop being necessary.**
- **A finished turn is no longer reported as a timeout.** The watchdog stayed armed while the
  CLI was shutting down, so a turn that had completed and been billed could come back as
  `(timed out)` with a cost of 0 — and burn a harness retry re-running work that had succeeded.
  A `result` now outranks the watchdog; the late kill is still recorded in the transcript.
- **An MCP server that fails to start is shut down instead of left running.** A handshake that
  timed out, or a tool listing that failed, leaked the server process for the life of the worker.
- **A pinned chat is no longer deleted when the history is trimmed.** The store keeps the 100
  most recent chats; the trim ranked purely by recency while the sidebar floats pins to the
  top, so a pinned thread left idle long enough dropped off the end and took its messages with
  it. Pinned chats are now exempt from the cap and spend no slot against it — the 100 counts
  unpinned chats. **Anything already trimmed is gone**; this only stops it happening again.

### Removed

- **The two one-shot migration scripts are gone** (`migrate_to_sqlite.py`, `migrate_model_ids.py`).
  Both predate the first public release: Otto has stored its history in `data/otto.db` and
  recorded canonical model ids since 0.1.0, so there is no install for either to run against.
  If yours still has the frozen `data/*.json`, `data/*.log` and `otto.db.pre-*` files the first
  of them read, `docs/operating.md` now says which ones are safe to archive and which one
  (`schedules.json`) is still read at startup.

### Security

- **Error text from the server is escaped before it reaches the page.** Thirteen of the UI's
  catch blocks wrote a failed request's error straight into `innerHTML`, and several server
  errors echo a field of the request back (`unknown capability '<name>'`) — so a crafted
  request could put markup on the page of whoever triggered it. Nothing for an operator to do;
  the API is still unauthenticated and local-only, which is what bounded this to begin with.

- **A GitHub ticket or PR title can no longer break out of its own data fence.** Text Otto did
  not write — an issue body from the board queue, a pull request title from the review queue —
  is wrapped in a fence and labelled as data rather than instructions. The fence was written
  three times and only one copy escaped a closing marker found inside the text, so a ticket
  containing `"""` ended it early and whatever followed read as instructions to the run.
  All three now share one implementation. Nothing for an operator to do; the capability's
  static risk and the approval gate were, and remain, the real guard.

## [0.1.0] - 2026-09-04

Initial public release.

- Chat UI that routes a request to one of your own Claude Code subagents or skills, clarifies
  when it must, gates writes behind an approval preview, and runs it via `claude -p`.
- Durable run pipeline on Temporal: verify → retry → escalate, LLM supervisor, per-run cost
  budget, and stuck-run recovery that surfaces to a human instead of vanishing.
- Five ingresses — web chat, Temporal schedules, webhooks, GitHub board, Slack — plus GitHub
  PR reviews.
- Isolated repo workspaces: clone, branch, draft PR, then bounded code-review and QA rounds.
- Memory, solutions, behaviours, knowledge and an immutable audit trail in SQLite.
- Model gateway with a local (OpenAI-compatible) backend beside the Claude one, MCP over
  stdio, and file-safety guardrails on both.

[Unreleased]: https://github.com/wonkylabz/otto/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/wonkylabz/otto/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/wonkylabz/otto/releases/tag/v0.1.0
