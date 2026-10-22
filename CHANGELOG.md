# Changelog

Notable changes to Otto, newest first. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Otto versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) as `docs/releasing.md` defines it
for a service you clone and run.

Entries are written for an **operator upgrading a running install**: what changes for them,
and what they must do about it. The commit log already holds the engineering narrative — a
changelog that restates it is a second copy of `git log`.

## [Unreleased]

## [0.6.2] - 2026-10-22

### Fixed

- **A project capability no longer edits your own checkout.** A write run of a capability that
  lives in a registered repo's `.claude/` now works in an isolated clone of that repo and opens
  a draft PR — including runbook, schedule and Slack runs, which used to run in (and branch and
  commit on) the live checkout. **Restart the worker after upgrading.**

## [0.6.1] - 2026-10-22

### Changed

- **A runbook's Request is now a multi-line box.** Long requests wrap instead of scrolling
  sideways in Jobs → Add/Edit runbook. Multi-line fields in every form also get the same
  border as the other inputs, and Notes is no longer capped at five lines. Takes effect on a
  page refresh.

### Fixed

- **PR descriptions are no longer cut off mid-sentence.** A long request now shows its first
  line in the body, and the full text sits in a collapsed "Full request" block. A long run
  summary ends on a word with a visible "(truncated)" note. **Restart the worker after
  upgrading.**
- **Post-PR review and QA rounds no longer move the goalposts.** Each re-review is now told what
  earlier rounds found and what each fix did, so it checks those first and can't silently ask
  to undo a fix it asked for. Before, a PR could use up every round and land on Needs-you with
  findings that contradicted the round before. **Restart the worker after upgrading.**

## [0.6.0] - 2026-10-21

### Added

- **Chat replies stream as they are written.** The answer appears in the chat while the model
  is still generating it, instead of only once the whole run finishes (Claude and local models;
  not Codex). Memory learning now happens after the reply, and several pre-run checks run side
  by side, so a turn starts and ends sooner. **Restart the worker after upgrading.**
- **Slack triggers take an *ignore* pattern** (Events → Triggers). A post it matches never
  starts a run, whatever *match* says — e.g. `(?i)\bis closed\b` stops an alert's closing post
  from opening a full investigation.
- **A Models step in the setup wizard** explains what each phase does, which deserve a strong
  model, and flags local/hosted models with no turn limit (they get the default of 60).
- **Local models can use an agent's skills.** An agent's `skills:` are listed to the run and a
  `Skill` tool loads them, instead of the model rebuilding each one from scratch in the shell.

### Changed

- **A local run near its turn or time budget is told to wrap up and report** (at 75%,
  `OTTO_LOCAL_WRAP_UP_AT`), and its last turn has no tools — so it returns what it found rather
  than a bare "hit the turn budget" error.
- **In Brainstorm, a follow-up stays in the brainstorm.** Asking it to "go do it" no longer
  hands off to a write task; start a new run for that.

### Fixed

- **Board buttons (Terminate, Retry, Approve, Dismiss) work on the first click** — a background
  refresh landing mid-click used to swallow it.

## [0.5.0] - 2026-10-18

### Added

- **Skip approval in chosen Slack channels** (Events → Slack bot → *Skip approval in these
  channels*). A request from you or an approver that needs only tools you ticked *safe* already
  ran without a gate in a DM; listing a channel the bot reads allows the same there. Only list
  channels nobody else posts in — anyone posting there can steer a run nobody approves. Empty by
  default, so existing installs are unchanged.

### Fixed

- **A Slack request Otto hands off to a write task shows its approval in the chat.** Reopening
  that conversation used to show "running" with no plan to read, approve or revise.
- **An approval only counts for the plan you saw.** The chat, board, Slack and ntfy buttons now
  refuse a decision if the gate was already decided elsewhere, a revised plan is being written,
  or a newer plan replaced the one you were shown. Before, approving during a "Request changes"
  round ran the revised plan unread, and a late Decline could overturn an approval. A refused
  decision says so instead of reporting it as applied. An ntfy button tapped while the worker is
  restarting stays usable.

**Upgrading:** restart the worker (`./run.sh`) along with the server. Until it restarts, every
approval is refused.

## [0.4.0] - 2026-10-18

### Added

- **Onboarding and a guided tour** (header → Setup). On first load Otto asks what you'll use it
  for, how to trade cost against quality, and what it should reach (Slack, email, calendar,
  GitHub…). It shows the exact runtime-settings changes before applying them, lists which
  integrations are connected and where to connect the rest, then tours the UI. Skippable, and
  safe to re-run: applying the same answers twice changes nothing. Existing installs see it once
  on their next page load — Skip dismisses it.
- **Slack and GitHub setup guides** (Events → *setup guide* on each section). Step-by-step:
  creating the Slack app and its scopes, adding the token, choosing who Otto answers; signing in
  `gh`, connecting a project board, PR reviews and repos. Each step shows whether it's already
  done on this install and opens the existing configure form; the guide resumes at the first
  step still to do.

## [0.3.0] - 2026-10-17

### Added

- **Factory mode** (Admin → Mode → Start factory). Point it at an empty registered repo with a
  seed idea — or at one with code, saying what to finish or add — and a budget, and a PM plans tickets on your GitHub board, the worker builds
  them one at a time, and a PR that passes review **and** QA merges itself. It writes a
  `GOALS.md` and a cited `SPEC.md`, playtests `main` after each merge, and stops on its budget,
  a stall or a clean DONE — notifying you each time. A spent Claude usage limit pauses it until
  Claude answers again. **Before enabling:** your `gh` login needs the `project` scope
  (`gh auth refresh -h github.com -s project`), the board needs Ready, In progress, In review,
  Done and **Blocked** columns, and you choose which MCP servers its runs may use (none by
  default). Back to Manual restores your board settings.

### Changed

- **A board card moved back to Ready runs again.** It used to sit in Ready forever after its
  first run; each re-run is now a new generation (`gh-issue-N-r1`, `-r2`…).
- **A PR built from a board ticket closes that issue when it merges.** Board PRs used to leave
  their issue open.
- **QA runs inside a fresh clone of the PR**, so it can run the repo's own tests and lint, and
  its report lists every acceptance criterion with its evidence. PRs used to come back
  INCONCLUSIVE for checks QA was never able to run, or simply didn't name.
- **A criterion only CI can prove (e.g. "passes in CI") no longer makes QA inconclusive** when
  QA ran the same commands locally and they passed.

### Fixed

- **A column added to the board is seen straight away.** The board's columns were cached for
  an hour, so a column added after Otto first read the board stayed invisible to it.

### Security

- **The API now requires a login.** Every `/api/*` route needs this install's token, so a run's
  own shell can no longer approve its own gate, release the pause or read your stores over
  `localhost` (#217, #3). **After upgrading, restart Otto, then run `./run.sh login` and open the
  link** (or paste `data/.api/token` into the login screen). Each browser gets a session you can
  revoke in Admin → Browser sessions. **Scripts calling the API must send
  `X-Otto-Token: $(cat data/.api/token)`**, and a JSON body must be sent as
  `Content-Type: application/json` or it is refused with 415. Webhooks and ntfy gate buttons
  are unchanged.
- **`claude -p` runs now start under `bwrap`**, which hides the token from them. Install
  bubblewrap if `python3 doctor.py` warns `api token mask`; without it a run can still read the
  token.
- `/api/wf/signal` only accepts `approve`, `clarify` and `revise_plan`; anything else is a 400.

### Changed

- **The models list can be dragged into order.** Each row has a grip on the right; drag it to
  rearrange the pool, the same way the Jobs tab reorders. The order is cosmetic with one
  exception the grip's tooltip names: between two models of the same tier, the higher row wins —
  escalation, downshift and the Claude fallback each take the first entry whose id matches their
  tier, so ordering two opus rows decides which one they use. Dropping a row does not reload the
  panel, so the page stays where you were reading.
- **Claude models can be removed from the pool.** The × was hidden on those rows; it is there
  now, and the picker adds them back. Two guards come with it: the last entry of all is refused
  (an empty pool is refilled with the default tiers on the next read, so the delete would look
  like it did nothing), and removing the last *Claude* row asks first, naming the model that
  Claude fallback, escalation and the plan preview will use instead — a model id that no row in
  the table shows.

### Changed

- **Adding a model checks the id before it saves.** A typed model id used to be stored
  unchecked, so a typo landed in the pool with a green "saved" and an empty health column — and
  Claude, Codex and hosted entries are only probed when you ask, so nothing contradicted it
  until you pressed `test` on the row. Add now runs that probe first and shows the failure in
  the form. It is advice, not a gate: press Add again to store the entry anyway, which you want
  for a server that is temporarily down or a vendor that serves an id it does not list.
- **`test` on a Codex row stops reporting a missing endpoint.** A Codex entry is not supposed to
  have one (`codex exec` signs in itself); the row now reports whether the CLI runs.

### Added

- **Admin lists the Claude models for you.** "+ Add Claude" in the LLM models section offers
  the Claude tiers as picks instead of asking you to type a model id from memory. With
  `ANTHROPIC_API_KEY` set the list is live from the Anthropic API; without one there is nothing
  to enumerate (Claude runs through the CLI's own login), so it shows Otto's built-in list and
  says so on the panel.

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

[Unreleased]: https://github.com/wonkylabz/otto/compare/v0.6.2...HEAD
[0.6.2]: https://github.com/wonkylabz/otto/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/wonkylabz/otto/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/wonkylabz/otto/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/wonkylabz/otto/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/wonkylabz/otto/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/wonkylabz/otto/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/wonkylabz/otto/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/wonkylabz/otto/releases/tag/v0.1.0
