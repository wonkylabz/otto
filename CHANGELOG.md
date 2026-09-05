# Changelog

Notable changes to Otto, newest first. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Otto versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) as `docs/releasing.md` defines it
for a service you clone and run.

Entries are written for an **operator upgrading a running install**: what changes for them,
and what they must do about it. The commit log already holds the engineering narrative — a
changelog that restates it is a second copy of `git log`.

## [Unreleased]

### Added

- The running version is shown beside the wordmark in the header, with the commit sha in its
  tooltip, and served by `/api/health` — so "which build is this?" is answerable from the
  browser during a support conversation.
- This changelog, and `docs/releasing.md`: what Otto's version number promises an operator,
  and `release.py` to cut a release from it.

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
