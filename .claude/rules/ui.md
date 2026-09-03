# UI conventions (`web/index.html`)

- **`web/index.html` contains NUL bytes** (sentinels around fenced-code-block placeholders) — `grep` needs `-a`; the Edit tool can't string-match a line containing one, so patch those bytes via a script.
- **The mascot's SVG and CSS are JS template literals** — a backtick in a comment there ends the string and the element silently never defines: no error, `<otto-mascot>` just renders nothing (`MascotStateTests`).
- **A subsection is INDENTED** (`.asection .subsection`) — a flush inner heading reads as another sibling, not a child; quieter type alone doesn't say it (`SubsectionIndentTests`).
- Tab copy lives on the control (`title=`), not in prose above it; config forms open in a shared modal (`openFormModal`/`closeFormModal`); every ingress toggles from its card (`.switch`), not inside its config form; long stores render collapsed+filtered+paginated, and overflow must be measured only while visible (`scrollHeight` is 0 under `display:none`); every Admin/Audit section is a real `<table>` with widths in CSS classes, not inline `<col style>`.
- **Nothing that shells out belongs in a request the panel's spinner awaits.** `loadAdmin` fans out 6 fetches in one `Promise.all`, so the slowest is the load time: `/api/policy` serves CACHED MCP health and the client tops it up via `refreshMcpHealth`. `claude mcp list` health-checks every server (~8s) — `policy.all_mcps` needs its result twice and must read `_mcp_status` ONCE (`…never_triggers_the_slow_health_check`/`…one_status_read_serves_both_consumers`).
- **`CSS.escape()` is for identifiers, never inside a quoted attribute selector** — `[data-x="${CSS.escape(p)}"]` injects backslashes the literal value doesn't have. Key a row's live cell by a `data-` attribute on the CELL, not on its buttons, so it stays findable in a busy state that renders no buttons.
- **The global pause is a HEADER control, not an Admin one** (`applyEstop`, `.estopbar`) — a forgotten pause reads as "Otto stopped working", and a tab hides it until you already suspect it. Rides `/api/health`'s pollers, no fourth one (`EstopUiTests`).
- **The mark and mascot wear the ACTIVE palette; the favicon cannot** — a `data:` URI is its own document, so `paintFavicon` repaints it from the resolved `--accent`/`--on-accent`/`--warn` at boot and each theme pick; the static `<link>` matches the default (`OttoMarkTests`).
- **The mascot lives outside `<main>`, `applyMood` is its ONE writer, and he is DRAGGABLE** — a dock inside a view unmounts on tab switch; his position is a fraction of the free area (pixels strand him off-screen) and reserved space is read off his rect (`MascotStateTests`).
- **An in-flight flag for a long action lives OUTSIDE the render** (`GC_RUNNING`, `CONV_BUSY`) — `loadMemory`/`renderAdmin` rebuild their DOM from scratch, so without it a running scan or derivation looks cancelled on any re-render or tab switch.
- **A RUNNING card shows its pipeline STAGE** (`server`'s open `times` span → `.bchip.stage`) — `phase` collapses everything before attempt 1 to "running", so a card sat unchanged through routing, a 15-min preview and the gate, reading as stalled (`BoardStageChipTests`).
- **The Admin phase table's headers and radio columns are two ordered lists** — add a tier to one only and every column to its right is silently mislabelled. Guarded against `gateway.TASKS` (`BoardStageChipTests`).
- **A Run-mode control the pipeline would IGNORE is disabled, never left tickable** (`applyModeExclusions`) — repo forces write and step-mode beats the ladder outright, so Brainstorm + either ran neither. The workflow resolves it too (`BrainstormModeTests`).
- **Two counters for the same noun must reconcile on screen.** The header counts ENABLED caps, Admin lists every discovered one (`enabled / total`); a memory ROW holds up to 3 facts, so the facts badge counts facts. `applyCaps` is the single owner of both cap counts — never recount inline (`HeaderCounterTests`).
- **Verifying UI changes headlessly**: body is `overflow:hidden`, tabs scroll internally, no hash routing — drive it via a same-origin proxy injecting a script, then `activateTab(...)`, click, and read results back through `document.title`.

## Where the UI writes

Admin tab edits `data/policy.json` (cap risk/enable), `data/models.json` (phase models), `data/board.json`, `data/settings.json` (runtime knobs — env still wins).

Repo conventions UI: Admin → Project repos → Conventions column (`GET /api/conventions`, `POST /api/conventions/refresh`). `conventions.status` is cache-only and must stay so; `conventions.refresh` is the only path that derives. The refresh path resolves against `registry.projects()` — never from the client.

Portable profile: `python3 profile.py export/import` (Admin → Share extensions).
