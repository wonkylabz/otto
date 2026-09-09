# Changelog

Notable changes to Otto, newest first. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Otto versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) as `docs/releasing.md` defines it
for a service you clone and run.

Entries are written for an **operator upgrading a running install**: what changes for them,
and what they must do about it. The commit log already holds the engineering narrative — a
changelog that restates it is a second copy of `git log`.

## [Unreleased]

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

- **A pinned chat is no longer deleted when the history is trimmed.** The store keeps the 100
  most recent chats; the trim ranked purely by recency while the sidebar floats pins to the
  top, so a pinned thread left idle long enough dropped off the end and took its messages with
  it. Pinned chats are now exempt from the cap and spend no slot against it — the 100 counts
  unpinned chats. **Anything already trimmed is gone**; this only stops it happening again.

### Security

- **Error text from the server is escaped before it reaches the page.** Thirteen of the UI's
  catch blocks wrote a failed request's error straight into `innerHTML`, and several server
  errors echo a field of the request back (`unknown capability '<name>'`) — so a crafted
  request could put markup on the page of whoever triggered it. Nothing for an operator to do;
  the API is still unauthenticated and local-only, which is what bounded this to begin with.

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

[Unreleased]: https://github.com/wonkylabz/otto/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wonkylabz/otto/releases/tag/v0.1.0
