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

- Admin → Project repos → **+ Add project repo** now lists every GitHub repo your `gh` login can reach (personal, organisation and collaborator; archived ones are left out). Filter, tick one or several, and they are cloned one after another. Pasting a URL still works, and is the fallback when `gh` cannot list. Needs a restart to pick up the new route.

### Changed

- Memory tab: the **Garbage collector** section is now a bin icon beside the fact/approach/rule counts. Click it to scan; when the scan finishes a count appears next to it, and clicking the count opens the candidates to review and evict. Nothing is deleted until you evict, as before. Ships on a browser refresh.

- The rules that pick which backend serves an attempt (Claude, local or Codex) moved out of the
  attempt runner into one module, `dispatch.py`. Same behaviour; a restart picks it up.
- The mascot is hidden on the Board tab instead of shrinking into its corner. A refresh picks it up.

## [0.16.0] - 2026-11-12

### Added

- Five more themes in Admin → Appearance, drawn from the same Figma colour-combination library as the first five: Ocean Tide, Cherry Blossom, Cozy Campfire, Evening Rose and Emerald Odyssey. Per browser, as before.
- The regression corpus now covers the repo-conventions judge: a pair of cases checks that a convention the request asked to break fails the run, and that the same output passes with no repo rules in view.

### Changed

- The header is restyled: a raised bar, the tabs as one centred pill group with an icon each (the active tab is filled), the capability counters as chips, and Setup, Update and Pause as matching pills. Phone layout is unchanged.
- Chat: Run options now start collapsed (your earlier open/closed choice is reset once), the workflow pipeline rail has a collapse button, and the chat list is wider with two-line titles.
- On the Board, an undragged Otto sits small in the bottom-right corner instead of over the cards. Everywhere else he keeps his Chat position; a spot you dragged him to is kept.
- Events: a warning on a switched-off Slack card (missing scopes, a refused token) is shown at full contrast instead of dimmed with the card.
- Memory: each entry's source line sits under its facts, and the forget/edit/delete controls are larger.
- Audit: the runs-per-day bars are filled and the attention share is red; the smallest labels across the app are now 11px.
- The web server's POST routes moved out of `server.py` into four modules by area (runs, integrations, memory, admin). No route or behaviour changed; the service needs its usual restart after updating.
- The header no longer shows the "N capabilities" and "read · write" chips. Admin → Capabilities still shows `enabled / total`.
- **Factory mode's PM and playtest are stricter about the product's UI.** The PM now readies a design-system ticket (with screenshot criteria at 1280px and 390px, both themes) before feature screens, tickets unmet Definition-of-Done items before polish (at most two polish tickets a sprint), and checks open and closed issues and PRs before filing, so it stops duplicating tickets. The playtest now fails a feature a user can't reach from the interface, and layout problems such as walls of repeated text or mid-word wrapping. The playtest is told to give one line of evidence per page before a `CLEAN`. Takes effect on the next sprint after a worker restart.

### Fixed

- The **FACTORY** header chip was unreadable in dark themes (dark text on a dark pill) and hidden at narrow widths; it now keeps its amber fill everywhere.
- Running the test suite without `temporalio` (a bare `python3`) now includes one failure that names the command to use, and the docs no longer claim that run comes back green.

## [0.15.0] - 2026-11-11

### Added

- Saving the Factory mode form shows a spinner on the button while the save is in flight, instead of a greyed-out button that read as a hang.
- The mascot's dab is redrawn as an actual dab: one arm thrown straight out, the other folded across a face turned into the elbow.
- The mascot fills an idle minute: after 40s of quiet he reads, juggles, grills and fails at the flute in turn, and once asleep he dreams — a thought bubble showing one stored memory at a time (click it to open Memory). Note the dream puts memory text in the corner of every tab; hide him in Admin → Appearance if you share your screen.
- Data health findings have a **Fix** button in the Audit tab's Data tab (remove a stale model pin, detach a chat, re-derive labels, delete an empty workstream), so they no longer need code or a hand-edited store.
- Board cards that a request fanned out into (swarm sub-tasks and Slack delegates) now carry a **family chip** shared with their parent: a short tag, one colour, and "sub-task 2/3" or "3 sub-tasks (1 done)". Hovering it dims every card outside the family; clicking it jumps to the parent.

### Fixed

- The mascot's dab (and any other one-off reaction) is held when a run finishes in a background tab and plays when you come back, instead of playing to nobody.
- A job that had nothing to report no longer posts to its Slack destination when the run picked up a footer note (for example the general worker's "no isolated workspace" warning): the silence stands and the note stays on the run's record.
- A model pin left behind by a renamed capability is reported as **orphaned** instead of "model not in the pool", which pointed at a capability that runs fine. A chat taken out of workstreams no longer reports too many refs, since it can't spread them.

## [0.14.0] - 2026-11-10

### Added

- The Audit tab's Health section has a **Data** tab, and `insights.py` ends with a data health report. It flags stored data that no longer matches what it is built from (workstream and chat labels, missing workstreams, model picks pointing at removed models), the kind of bug that never fails a run.

### Fixed

- The **edited live checkout** warning no longer blames a run for commits you made yourself in that repo while it was running. A repo is flagged only if the run's own commands touched it.
- **Workstreams** no longer pile up every issue and PR their chats mention. Only the links in a chat's opening request and the PRs Otto actually opened or updated count, and a workstream's labels now shrink when a chat leaves it. Existing workstreams are cleaned up on the next restart.
- When the assistant hands a task off, its reply now always includes the link to the PR that task opened.

## [0.13.0] - 2026-11-10

### Added

- Jobs have a **Re-use one chat for every run** option (off by default). When on, every run of that job lands in the same chat instead of opening a new one.

### Fixed

- A link at the end of a sentence in a reply no longer includes the trailing period or comma, so it opens the right page.

## [0.12.1] - 2026-11-09

### Fixed

- **A chat's message count is no longer cut off** when it carries a workstream chip; the chip
  shortens instead. A board card's workstream suggestion keeps its dismiss × when the title is long.

### Changed

- **The header shows only the version** — the "agentic ops · live backend" tagline is gone.

## [0.12.0] - 2026-11-09

### Added

- **Search and clear the chat list.** A search box above the chats matches titles, capabilities,
  workstreams and message text. **Clear** deletes every chat except pinned ones and ones with a
  task still running.
- **Create and delete workstreams from the Workstreams view.** **+ Add workstream** names one and
  picks its chats; the × on a row deletes it. Its chats are kept.
- **Export only part of a snapshot.** Export now asks which stores to include (jobs, events,
  models…), all ticked by default. Importing a partial snapshot leaves the stores it lacks alone.

### Fixed

- **Returning to a chat whose task is still running no longer says "Resuming…"**, which read as
  if the task had paused. It now says the task is still working.
- **Opening a chat no longer moves it to the top of the list.** Only new activity in a chat does.
- **The factory reopens a ticket closed without merged code only once.** If it is closed as
  completed again, for example as superseded by another ticket, it now stays closed. It still
  holds back the factory's done verdict until it is closed as "not planned".
- **A run on a local model has more time left for its final answer.** It is now made to answer
  at 80% of its time limit instead of 90%, so a long review is less likely to time out with
  nothing reported. Set `OTTO_LOCAL_FORCE_REPORT_AT` to change this.
- **A local run's transcript now records the instructions Otto gave it alongside the request**,
  so you can see what a run was told when debugging it.

**Restart the worker** to pick these up.

## [0.11.1] - 2026-11-08

### Changed

- **A retried factory ticket continues its open PR instead of closing it and starting over.** Only
  a PR that conflicts with the base is rebuilt. **Restart the worker** to pick this up.
- **Code review reruns only look at the fix.** After the first round, a review reads what changed
  since the last one and raises only a must-fix on those lines. Anything else is listed as a
  follow-up, and factory mode files those as an issue after the merge. Reviewers also read a clone
  of the PR instead of fetching files one at a time, except one set up in a project's own repo,
  which keeps working there. **Restart the worker** to pick this up.

### Fixed

- **Factory sprints are told how long they have.** A sprint unblocks first, writes tickets from
  what it has read, and one that runs short reports what's left for the next.
- **A PR you merge by hand counts for the factory**, so the line isn't read as idle.
- **A factory ticket can no longer reach Done without merged code.** One closed as completed with
  no merged PR is reopened and sent to Blocked, and it holds back the factory's done verdict. An
  existing ticket like this will reappear once. Dropped tickets now close as "not planned".
- **A board PR with a long description closes its ticket when merged.** The closing line was being
  cut off, so a ticket stayed open after you merged its PR by hand.
- **A code ticket about a calendar no longer moves off your local model.** In a repo run, only
  wording that points at someone's calendar counts, such as "my calendar" or "Google Calendar".
  Other runs behave as before. **Restart the worker** to pick this up.
- **Your own chat messages now render markdown.** A message, or the ticket a board run started
  from, showed as one flat block with its headings, lists and line breaks lost; it now renders
  the same way Otto's replies do. A browser refresh picks this up.
- **Code blocks no longer vanish from rendered text.** A fence in a ticket saved with Windows line
  endings, or one sharing a line with other text, showed as a blank. Literal asterisks such as
  `2*3` or `rm *.log *.tmp` are also no longer turned into italics.

## [0.11.0] - 2026-11-06

### Added

- **Board cards show the ticket they belong to.** A card is labelled with the GitHub issue/PR or
  Jira ticket its run is about (from the request's links, the PR Otto opened, or the board issue
  that started it); a GitHub label links to it. **Restart the server** to pick this up.
- **Knowledge documents can be uploaded as files** (Knowledge → + Add document). Pick or drop
  PDFs, Word `.docx` or text files (several at once, each shown as a removable chip); each becomes
  its own document titled after the file, and a retry resends only the files that failed. PDF
  support needs the new `pypdf` requirement: **re-run `./install.sh`** (until then a PDF is
  refused with a reason, everything else works). Scanned PDFs have no text and are refused.

### Fixed

- **Scrolling up in Chat while Otto is replying no longer snaps back to the bottom.** The chat
  follows new text only while you're already at the bottom; sending a message still jumps down.
- **A local model's stream that fails partway is retried instead of ending the run**: an error sent
  inside the stream goes through the same classification as an HTTP status, and a stream cut off
  with no end marker no longer counts as a finished reply.
- **A code review or QA round that dies on a local backend gets one re-run on Claude**, as a dead
  fix round already did, rather than parking the PR as inconclusive. **Restart the worker** to pick
  this up.
- **A local model server dropping the connection no longer ends a run in seconds.** Otto now backs
  off and retries, including on TLS endpoints, and an outage that outlasts the retries shows as
  the server being unreachable, not a harness failure. **Restart the worker** to pick this up.
- **Review and QA rounds are no longer stopped by a word in an earlier round's report.** A fix
  report mentioning a "calendar day" read as a Google Calendar task, so the round moved off local
  or, with Claude fallback off, stopped the review. Your own request still counts.

## [0.10.0] - 2026-11-04

### Changed

- **Slack triggers can fire on people, not just bots** (Events → Triggers). A new *Who can fire it*
  picks listed bots, listed people, or anyone in the channel, so a channel like #access-requests
  can start runs. Human modes ignore joins, edits, bot posts and the reading account's own posts.
  An *anyone* trigger never auto-approves writes, and a run a person starts is always checked for
  write intent, retries included, so it can't slip past approval on a read-only route.
- Slack trigger matching takes plain phrases (*Only posts containing* / *Skip posts containing*,
  whole words, any case); the regexes moved under *Advanced*. A trigger can carry an optional
  title, shown on its card. Existing triggers keep working unchanged.
- Every version is now published as a GitHub Release with its changelog notes, so the
  repo's Releases panel lists what each tag shipped. Nothing changes on an install.
- The chat uses the full width of its pane instead of a narrow off-centre column; each side's
  bubbles hug their content and stop at 80% (question and approval cards still fill that
  width; on a phone Otto's replies stay full width), replies read larger with more line
  spacing, and the composer's placeholder sits level with its buttons.

### Fixed

- **A `git pull` no longer crashes runs started before the worker restarts.** Workflow code
  now loads once at startup like the rest, so an update takes effect on restart. Restart the
  worker.

## [0.9.1] - 2026-11-03

### Changed

- Delivery, the push notification's source line and the Slack/board bookkeeping hooks now
  dispatch through `integrations/`. Same behaviour, same stores, same schedules; a restart
  picks it up.
- `integrations/` declares which module owns each reply-target kind (Slack, GitHub,
  webhook). Declarations only this release; nothing dispatches through it yet.
- Guards ahead of the integrations refactor: one recorded poll history is replayed against
  all three poll workflows, and a test ratchets the core's per-vendor `reply_to` branches so they can
  only fall. Nothing operational changed.
- README rewritten as a short front page with screenshots; the long-form sections (repo
  mode, factory mode, phone access, backends, configuration, secrets) moved to
  `docs/guide.md`. Nothing operational changed.

### Fixed

- **A request naming one of your open PRs only to close or ignore it no longer works on it.**
  Otto checked that PR's branch out and reviewed the wrong diff; it now starts a fresh branch.
  Restart the worker.
- **A review or QA fix round that changes nothing now stops the loop for you.** Otto used to
  re-review the same unchanged PR until the round budget ran out. Restart the worker.
- **A chat follow-up whose task Otto wrote over several lines now runs.** Otto answered "say it
  in full" whenever its hand-off spanned more than one line, as long tasks with a requirements
  list do. Runs started before the upgrade keep the old behaviour; a restart picks it up.
- **A chat hand-off over the 8000-character limit is now logged** in the worker log, instead
  of only answering "say it in full". Nothing else changes.
- **A long chat no longer stops Otto from taking on a task.** A follow-up asking for work
  answered "I couldn't turn that into a task" once the conversation was long: the hand-off
  accepted at most 2000 characters and silently dropped longer ones. The limit is now 8000.
  A restart picks it up.
- **The mascot no longer waves in his sleep.** The welcome-back greeting fires only from the
  idle pose; a sleeping Otto stays asleep.
- **Saving Otto to a phone's home screen now installs the Otto icon** instead of a generic
  tile: a web manifest (`/manifest.webmanifest`, standalone display) and PNG icons under
  `/icons/` for both Android and iOS.
- **Every loading placeholder is the spinner.** Eleven views and modals painted the word
  "loading…" while they fetched; all of them now share the same spinner (`spinHTML`).

## [0.9.0] - 2026-10-31

### Added

- **Workstreams.** Group every chat about one ticket or PR. Chats that link the same GitHub
  issue/PR or Jira ticket group by themselves; a chat sharing a rare word with a workstream's name
  gets a one-click "Name?" suggestion; anything else is filed from its Swarm card
  (**+ Workstream**). Swarm board's title now switches to a **Workstreams** view listing them all,
  each opening a timeline of its chats, including runs the board no longer shows. A card's
  workstream chip filters the board to it. Existing chats join as they are next written.

### Fixed

- **Fast chat asks what you mean again.** An ambiguous message ("what is this alert?") gets the
  question box, as it does with Fast chat off, instead of a slow tool search that ends in "I can't
  tell". The question is checked at the same time as the existing write check, so it adds no wait.

## [0.8.7] - 2026-10-30

### Added

- **Fast chat** (Admin → Runtime settings → Routing, off by default). Web chat replies straight
  away, like Claude Code: the assistant answers in one turn, with no routing, clarify, plan preview
  or judge. Work that needs a change is handed off to a normal routed run, which still shows its
  plan and waits for your approval. A pinned `/capability`, a repo pick or plan mode takes the full
  path as before. A message that asks for a change skips the quick answer and goes straight to the
  approval path, and a quick answer that dies on a local model is retried once on Claude. In a chat
  running on Codex, a follow-up that asks for a change tells you to start a new chat instead.

### Fixed

- **A local-model Bash call that starts a background process (`cmd &`) returns at once** instead of blocking until the tool timeout, which then killed that process — a server started in one call is now still running in the next. Background jobs live for the rest of that run and are stopped when it ends. Output they write afterwards is read and dropped, so it can't fill memory or crash the process. A long result keeps its first and last lines, so a command's own final line isn't lost behind a server's startup logs. Output that isn't valid text no longer crashes the call.

## [0.8.6] - 2026-10-30

### Changed

- **The web UI works on a phone.** Below 680px wide:
  - The tabs become a bottom bar: Chat, Board, Jobs, Audit, and More for the rest.
  - The chat list becomes a collapsible strip above the conversation. It used to be hidden
    with no way to reach it.
  - Run options start collapsed and close when you send.
  - Admin's wide tables become cards, and a section's buttons show only once it is open.
  - Empty board columns shrink to a row of count tiles.
  - Forms and the plan approval open as bottom sheets.

  Nothing changes on a desktop. To reach Otto from a phone, follow the README's "From your
  phone" section: put Otto behind a private tunnel such as `tailscale serve`, add that URL to
  `OTTO_ALLOWED_ORIGINS` and `OTTO_CLICK_URL`, and restart.

## [0.8.5] - 2026-10-30

### Changed

- **Post-PR fix rounds run on your local model.** They used to go to Claude whatever you
  assigned, so a local-only install could not fix its own review or QA findings. They now run
  on the execution model you picked. If a local round fails and Claude fallback is on, that one
  round is re-run on Claude; with fallback off (`OTTO_LOCAL_FALLBACK=0`) it stays local.

### Fixed

- **Local runs deliver their report instead of timing out.** A local model told to wrap up
  kept calling tools until the deadline, and the run returned only "(timed out)". Three
  turns after that notice, or once 90% of the run's time is used, it is offered no tools and
  must answer. Tune with `OTTO_LOCAL_WRAP_UP_GRACE_TURNS` and `OTTO_LOCAL_FORCE_REPORT_AT`.
- **A worker restart during review or QA no longer fails the run.** A run whose PR was already
  open showed up as `workflow_error`; it now stops as review/QA inconclusive and says why.

Restart the worker to pick these up.

## [0.8.4] - 2026-10-29

### Fixed

- **Local models run on macOS.** A local execution model used to stop every run with "no
  working `bwrap`", since `bwrap` is Linux-only. On a Mac the run's shell is now sandboxed by
  macOS's own `sandbox-exec`: it still cannot read Otto's keys, `.env` or other credential
  stores, nor the Keychain where Claude's login lives. `gh` keeps working, and `ps`/`top` do not
  (use `pgrep`). Codex still needs `bwrap`. Restart the worker to pick it up.

## [0.8.3] - 2026-10-29

### Fixed

- **`regress.py` no longer writes into your audit trail.** Its plan-preview cases recorded rows
  under the run id `preview`, which the scorecard and `insights.py` counted as real runs and
  spend. Rows written before this fix stay, because the trail is immutable; filter out the
  `preview` run id when you read it.

## [0.8.2] - 2026-10-29

### Changed

- **Each card in "Needs a look" says what to do next.** It leads with "Suggested: Retry",
  "Accept" or "Dismiss" and highlights that button. It suggests Retry when the cause has passed
  (a usage limit, a crash, review findings to fix), Dismiss when a later run already took over or
  you stopped the run, and Accept only for a result that was posted but not verified. Where only
  reading the PR can tell, it suggests nothing.
- **A card a later run has already handled leaves "Needs a look" by itself.** Once the ticket's
  next run, or a Retry, finishes cleanly, the old card is hidden.

## [0.8.1] - 2026-10-28

### Changed

- **Plans for a deny or restriction add an observation window only when they need one.** When
  the repo already shows who can reach the thing (a bucket or IAM policy, an allowlist, a
  NetworkPolicy), the plan checks those callers directly. A window is kept only for callers
  the code cannot show, and the plan now says why it needs one. Restart the worker to pick it up.

## [0.8.0] - 2026-10-28

### Changed

- **Saving the Factory config no longer starts the factory.** A new factory is saved on hold;
  press **Start** in Admin → Mode once the GitHub events are wired. **Pause** stops new sprints
  and tickets but keeps the budget, counters and board settings, and runs already going finish.
  A factory already running when you upgrade keeps running.

### Fixed

- **QA can build and render test pages.** Every PR workspace now has a git-ignored
  `.otto-scratch/` folder, and the QA tester is told to use it (not `/tmp`) and to try a
  headless browser before calling a layout claim unobservable, and to rerun a check whose
  output it lost rather than report it as done. Its `/tmp` setups were refused
  mid-run, which marked rendering checks inconclusive.
- **Otto's runs no longer pick up your interactive permission mode.** Each run now passes an
  explicit permission mode, so a `"defaultMode": "auto"` in `~/.claude/settings.json` no longer
  puts an auto-mode check in front of every shell command an unattended run makes. Your own
  `permissions.deny` rules still apply, and the QA tester is told not to use `rm -rf`
  (`npm ci` already gives a clean install).
- **A board run that fails outright now stays in Blocked.** Its "something went wrong" reply
  moved the card on to the review column, so when the factory resumed after a usage limit it
  found nothing to re-queue and a product manager retry was spent on it instead.
- **A factory stop can no longer leak one more ticket.** A board poll running at the moment the
  factory stopped (at its budget, say) started the next Ready card as an ordinary board run,
  outside the budget and with every MCP server. The poll now re-checks both switches just before
  it starts each run.
- **A running factory no longer sends its failed runs to Needs-you.** Its sprints already retry
  a Blocked ticket twice and then close it with a reason, so those cards only waited for someone
  to dismiss them. They show again if the factory is paused or stops, and a stop notifies you.
- **A factory sprint card in Needs-you has no Retry button.** A retried sprint ran as an
  ordinary run beside the factory's own sprints, outside its budget and WIP cap, and worked the
  same tickets. The card now clears itself once the factory's next sprint has run.
- **A Needs-you entry now records why it stopped.** Failed merges and review or QA findings
  now save their cause in the audit trail. Before, the trail showed only the reason code.
- **Board runs for the same issue number in different repos no longer share a run.** A board
  run's id and chat thread now include the issue's repo (`gh-issue-2-owner-repo`). Before this,
  issue #2 in two repos shared one chat thread and one audit history, and one could wait for the
  other to finish. Cards already In Progress keep their old id and the reaper still finds them.
  Old chat threads stay where they are; a re-readied card starts a new thread.
- **One ticket, one open PR.** A run stopped by Claude's usage limit no longer pushes its
  half-finished work as a PR. When a board card runs again, its new PR closes the earlier run's
  PR (the branch is kept, so you can reopen it). Retry on a board card now starts the card's next
  run instead of a separate one, and refuses while the card is already running again.
- **A usage limit hit while judging is reported as the usage limit.** It used to show as
  `workflow_error`, which looked like an Otto bug and invited a Retry that hit the same limit.
- **The `needs-human` label now lands on repos that didn't have it.** Otto creates the label
  when it is missing. Before this, a stuck or failed ticket in such a repo got no label.
  **Restart the worker after upgrading.**

## [0.7.3] - 2026-10-27

### Fixed

- **Code review and QA can now compare a PR against its base branch.** Each workspace Otto
  cloned for a PR was missing the commit where the branch split off, so `git diff main...HEAD`
  failed with "no merge base". QA marked clean PRs inconclusive and they landed in Needs you.
  **Restart the worker after upgrading.**

## [0.7.2] - 2026-10-27

### Fixed

- **An approved plan that edits one of your repos now runs in a clone and opens a draft PR.**
  When a request didn't name the repo but the plan you approved did ("in `infra`, change
  `frankfurt.tf`"), the run had no clone, couldn't write to your checkout, and delivered nothing.
  **Restart the worker after upgrading.**

## [0.7.1] - 2026-10-25

### Fixed

- **A run that posts something is judged on what it posted.** A capability whose instructions
  set the format of a Slack message, comment or ticket was failed when its closing summary
  didn't repeat that format, even though the posts met it. Because a run that has already
  posted is never retried, these runs landed in Needs you. **Restart the worker after upgrading.**

## [0.7.0] - 2026-10-23

### Added

- **A health report on the Audit tab.** Runs, judged pass rate, what needs attention and why,
  how often you overruled the judge, and spend, each against the previous 7 or 30 days, with a
  per-day chart and breakdowns by cause, judge, capability, stage and tier call. The same report
  runs from a terminal: `./.venv/bin/python insights.py --since 7d` (`--changes-only` prints
  nothing in a quiet week, so it suits a weekly cron). **Restart the server after upgrading.**
- **The audit trail is paged**, 50 actions at a time; totals still cover the whole trail.
- **An `otto-setup` skill.** In a Claude Code session in the Otto checkout, ask it to "set up
  Otto" and it configures models, repos, MCP servers, jobs, events and Slack through Otto's own
  API. It never asks you to paste a secret.

### Changed

- **Each job firing and each webhook event opens its own chat thread.** Jobs used to pile every
  firing into one chat, and webhook runs recorded no chat at all. **Restart the worker after
  upgrading.**
- **Routing and judge decisions are kept 30 days** (was 7), so the health report can compare a
  full month. `OTTO_TIER_LEDGER_TTL_H` still overrides it.
- **The board's "Needs review" column is now "Needs a look"** — a failed code review lands
  there, and the old name read as the review stage. Each column has its own colour, new cards
  animate in, and empty columns are all the same size.
- **The `audit-runs` skill starts from the health report** and reads which judge failed a run.
- Otto waves hello, shows hearts when clicked, and plans on a landscape whiteboard.

## [0.6.3] - 2026-10-22

### Fixed

- **Buttons that ask "are you sure?" work again.** Evict selected, forget, remove, terminate,
  clear and post-review could silently do nothing in Chrome because the browser cancelled its own
  confirmation popup without showing it. They now ask in an in-app dialog. Takes effect on a page
  refresh.

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

[Unreleased]: https://github.com/wonkylabz/otto/compare/v0.16.0...HEAD
[0.16.0]: https://github.com/wonkylabz/otto/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/wonkylabz/otto/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/wonkylabz/otto/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/wonkylabz/otto/compare/v0.12.1...v0.13.0
[0.12.1]: https://github.com/wonkylabz/otto/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/wonkylabz/otto/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/wonkylabz/otto/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/wonkylabz/otto/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/wonkylabz/otto/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/wonkylabz/otto/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/wonkylabz/otto/compare/v0.8.7...v0.9.0
[0.8.7]: https://github.com/wonkylabz/otto/compare/v0.8.6...v0.8.7
[0.8.6]: https://github.com/wonkylabz/otto/compare/v0.8.5...v0.8.6
[0.8.5]: https://github.com/wonkylabz/otto/compare/v0.8.4...v0.8.5
[0.8.4]: https://github.com/wonkylabz/otto/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/wonkylabz/otto/compare/v0.8.2...v0.8.3
[0.8.2]: https://github.com/wonkylabz/otto/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/wonkylabz/otto/compare/v0.8.0...v0.8.1
[0.8.0]: https://github.com/wonkylabz/otto/compare/v0.7.3...v0.8.0
[0.7.3]: https://github.com/wonkylabz/otto/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/wonkylabz/otto/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/wonkylabz/otto/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/wonkylabz/otto/compare/v0.6.3...v0.7.0
[0.6.3]: https://github.com/wonkylabz/otto/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/wonkylabz/otto/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/wonkylabz/otto/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/wonkylabz/otto/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/wonkylabz/otto/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/wonkylabz/otto/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/wonkylabz/otto/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/wonkylabz/otto/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/wonkylabz/otto/releases/tag/v0.1.0
