# UI conventions (`web/index.html` + `web/css/`, `web/js/`)

`index.html` is the head, the markup shell and the tag list — no style, no logic. Everything
else is an asset: `css/tokens.css` (design tokens + the five palettes), `css/app.css`, and the
scripts, one per tab plus `util`/`markdown`/`modal` (shared) and `boot` (startup). Classic
`<script src>`, no bundler, served by `server.Handler._static`.

- **A new asset needs its OWN `"use strict";`, a tag in `index.html`, and a size under the ratchet** — the directive is per-SCRIPT, so omitting it drops that file into sloppy mode silently; an untagged file is dead code that still reads as live (`UiAssetLayoutTests`).
- **Load order: shared primitives before the views, `boot.js` LAST** — a top-level statement runs when its file does, so anything a view calls across a file boundary must be called from an event handler, never at load (`UiAssetLayoutTests`).
- **A test reads the UI through `test_support.ui_src()`, never `web/index.html`** — it re-inlines every asset in document order, which is what keeps the ~60 grep guards (and their `.index()` ordering assertions) meaning anything (`UiAssetLayoutTests`).
- **Assets are read per request and sent `no-store`; never add a cache-buster** — only the ROUTE ever needed a restart, so a UI edit still ships on a plain refresh. `_static` is one directory + one extension + basename only (`UiAssetRouteTests`).
- **The mascot's SVG and CSS are JS template literals** (`web/js/mascot-element.js`) — a backtick in a comment there ends the string and the element silently never defines: no error, `<otto-mascot>` just renders nothing (`MascotStateTests`).
- **A documentation placeholder in a template literal is CODE** — `<code>${VAR}</code>` in help text threw `ReferenceError` and the form never opened; `node --check` and every grep pass it. Escape (`&#36;{`) or write it in words, comments included (`UiAssetLayoutTests`).
- **A form textarea inherits the composer's `textarea` rule and `.aform`'s flex column** — app-wide `max-height:120px`, and shrink-to-min inside a capped modal. A sized field must override both (`.codearea`), or its computed height is not the one set (`UiAssetLayoutTests`).
- **EVERY form opens in the shared modal, the ADD half included** (`openFormModal`) — five rendered inline, under a header or open above the list they add to, so "add" was a different interaction per tab. One shape: `.addbtn.addnew`, `+ Add <thing>` (`AddControlModalTests`).
- **Two scripts must never define the same `show*Form`** — one global scope, so the LAST tag wins: a second `showRuleForm` (Memory's rules) made Events' webhook button open it (`AddControlModalTests`).
- **A subsection is INDENTED** (`.asection .subsection`) — a flush inner heading reads as another sibling, not a child; quieter type alone doesn't say it (`SubsectionIndentTests`).
- **Reordering the Jobs tab is DISPLAY ONLY** (`runbooks.set_order`, its own file, nothing that RUNS reads it) — a row's section is its cron, so a drag never crosses one, and every definition is byte-identical after (`RunbookOrderTests`, `JobReorderUiTests`).
- Tab copy lives on the control (`title=`), not in prose above it; every ingress toggles from its card (`.switch`), not inside its config form.
- Long stores render collapsed+filtered+paginated, and overflow is measured only while visible (`scrollHeight` is 0 under `display:none`); every Admin/Audit section is a real `<table>` with widths in CSS classes, not inline `<col style>`.
- **Nothing that shells out belongs in a request the panel's spinner awaits.** `loadAdmin` fans out 6 fetches in one `Promise.all`, so the slowest is the load time: `/api/policy` serves CACHED MCP health, topped up by `refreshMcpHealth` (`…never_triggers_the_slow_health_check`).
- **`policy.all_mcps` must read `_mcp_status` ONCE** — it needs the result twice and `claude mcp list` health-checks every server (~8s) (`…one_status_read_serves_both_consumers`).
- **Every mutating fetch goes through `util.postJSON`** (`postOr` to toast it, `postForm` for a modal's own error slot) — 51 sites fired a POST then re-rendered, so a 400, a 409 or a 403 from `_csrf_ok` painted the old value back and said nothing (`UiAssetLayoutTests`).
- **`CSS.escape()` is for identifiers, never inside a quoted attribute selector** — `[data-x="${CSS.escape(p)}"]` injects backslashes the literal value doesn't have. Key a row's live cell by a `data-` attribute on the CELL, not on its buttons, which a busy state may not render.
- **A repeating timer goes through `util.poll`, never a bare `setInterval`** — it skips a tick while `document.hidden` and doubles on consecutive failures (60s cap); a backgrounded tab polled `/api/*` forever. A tick fails by throwing or returning `false` (`PollerBackoffTests`).
- **The global pause is a HEADER control, not an Admin one** (`applyEstop`, `.estopbar`) — a forgotten pause reads as "Otto stopped working", and a tab hides it until you already suspect it. Rides `/api/health`'s pollers, no fourth one (`EstopUiTests`).
- **The mark and mascot wear the ACTIVE palette; the favicon cannot** — a `data:` URI is its own document, so `paintFavicon` repaints it from the resolved `--accent`/`--on-accent`/`--warn` at boot and each theme pick; the static `<link>` matches the default (`OttoMarkTests`).
- **The mascot lives outside `<main>`, `applyMood` is its ONE writer, and he is DRAGGABLE** — a dock inside a view unmounts on tab switch; his position is a fraction of the free area (pixels strand him off-screen) and reserved space is read off his rect (`MascotStateTests`).
- **An in-flight flag for a long action lives OUTSIDE the render** (`GC_RUNNING`, `CONV_BUSY`) — `loadMemory`/`renderAdmin` rebuild their DOM from scratch, so without it a running scan or derivation looks cancelled on any re-render or tab switch.
- **A FINISHED card outlives its Temporal execution** (`server._board`, `board_retention_h`) — closed runs need their OWN window (one `StartTime DESC` slice let newer runs evict them) and visibility DELETES one at its TTL, so cards are archived on close (`BoardRetentionTests`).
- **The progress tail covers the PLAN preview, and BOTH branches test `PLAN_PART`** — the server's rank only wins once an `-aN` exists, so after approval the stale preview painted RUN "stuck" for the gate wait. `planActive` is derived, never latched (`PlanVisibilityTests`).
- **A RUNNING card shows its pipeline STAGE** (`server`'s open `times` span → `.bchip.stage`) — `phase` collapses everything before attempt 1 to "running", so a card sat unchanged through routing, a 15-min preview and the gate, reading as stalled (`BoardStageChipTests`).
- **The Admin phase table's headers and radio columns are two ordered lists** — add a tier to one only and every column to its right is silently mislabelled. Guarded against `gateway.TASKS` (`BoardStageChipTests`).
- **A Run-mode control the pipeline would IGNORE is disabled, never left tickable** (`applyModeExclusions`) — repo forces write and step-mode beats the ladder outright, so Brainstorm + either ran neither. The workflow resolves it too (`BrainstormModeTests`).
- **Codex is its own optgroup in the Execution select, with no tool-free option** (`codexModels`) — `tool_free` is one completion against `/chat/completions`, which this backend does not have, so offering it is a control the pipeline ignores (`CodexWiringTests`).
- **Two counters for the same noun must reconcile on screen.** The header counts ENABLED caps, Admin every discovered one (`enabled / total`); a memory ROW holds up to 3 facts, so its badge counts facts. `applyCaps` owns both counts; never recount inline (`HeaderCounterTests`).
- **Verifying UI changes headlessly**: body is `overflow:hidden`, tabs scroll internally, no hash routing — drive it via a same-origin proxy injecting a script, then `activateTab(...)`, click, and read results back through `document.title`.

## Where the UI writes

Admin tab edits `data/policy.json` (cap risk/enable), `data/models.json` (phase models), `data/board.json`, `data/settings.json` (runtime knobs — env still wins).

Repo conventions UI: Admin → Project repos → Conventions column (`GET /api/conventions`, `POST /api/conventions/refresh`). `conventions.status` is cache-only; `conventions.refresh` is the only path that derives, resolving against `registry.projects()`, never the client.

Portable profile: `python3 profile.py export/import` (Admin → Share extensions).
