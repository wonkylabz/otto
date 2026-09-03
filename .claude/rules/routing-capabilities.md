# Routing & capabilities

## Router #1 — which capability

`engine.route`, keyword-shortlisted (`ROUTE_SHORTLIST`), routed on primary verb not topic. **General assistant** = built-in read-only Q&A cap (always shortlisted); **general worker** = built-in write cap for task-shaped requests with no specialized agent. Both pinnable (`/assistant`, `/worker`). The direct path never auto-routes to project caps.

- **A wrong route is usually retrieval, not the model** — the shortlist is a top-N cut over every discovered cap, and one the router never sees can't be chosen. Diagnose with `registry.rank()` before touching the prompt, which is already several exceptions deep.
- **Rank against the catalogue, never per-cap** (`registry.rank`): IDF + length normalization, since a flat word count can't tell a discriminating word from a ubiquitous one and rewards long descriptions, producing deep ties at the cutoff. The shortlist is always FILLED and tie-broken by name, so routing is reproducible.
- **A WRITE pick is re-sampled; the majority stands** (`routing._confirm_route`, `route_confirmations`=3) — one sample is a coin flip, and only a write route arms the gate and preview. A lone read sample must not win, or a real task lands on assistant (`RouteConfirmationTests`).
- **The listing is numbered from 1 and the LAST integer in the reply wins** — a reasoning preamble naming other options poisons a first-integer parse. Stock bundled caps are marked `[generic]` so a user's own cap wins ties.

## Capabilities

`registry.py` — discovers `~/.claude` agents/skills, plugin skills (`<plugin>:<skill>`), stock caps (`capabilities/bundled/*.md` on, `capabilities/optional/*.md` opt-in). User caps win over stock.

- **Only user-scoped plugin installs are discovered** — a `scope:"project"` plugin skill has no cwd Otto can invoke it from, so offering it guarantees `Unknown command` after burning the ladder. Project *caps* (`registry.project_skills`) are the analogue — they carry `cap.cwd`.
- **Project capabilities** — agents/skills in another repo's `.claude/`, namespaced `<repo>:<name>` but invoked bare. Registered in `data/projects.json`; each carries `cap.cwd`+`cap.mcp_config`.
- **A `route_hidden` cap is never a Router #1 candidate** (`routing._shortlist`, BOTH paths) — `brainstorm` skips the verify ladder, so a route landing there drops a real task's only quality check. Pin-only (`BrainstormModeTests`).
- **A mode cap's prose is a real question's vocabulary**, so it out-ranks `assistant` on the very requests it must not take — retrieval is the only reliable guard, router wording is not (`BrainstormModeTests`).
- **A MODE cap pins its risk in `_RISK`, never leaves it to `classify`** — `apply_policy` overwrites `cap.risk` on every load, so editing the description alone can flip the mode into needing an approval card (`BrainstormModeTests`).
- Adding a stock cap: drop `<name>.md` in `capabilities/bundled/` (on) or `capabilities/optional/` (opt-in); risk default in `registry._RISK`.
- **A missing tool inside an `agent` cap is a frontmatter problem, not a headless/OAuth one.** A `skill` cap runs as `/<name>` in the top-level session (sees every tool); an `agent` cap is a subagent whose `tools:` line is its *complete* grant. Grep a transcript for a successful `mcp__claude_ai_*` call before believing "connectors don't work headless".

## Risk model

Each capability is `read` or `write` (`registry._RISK` + keyword heuristic); unknown defaults to write. `config.READ_TOOLS` still includes `Bash`, so a read cap *can* mutate external state — the approval gate, not the toolset, is the real guard.

- **Fresh-route write gate** (`engine.request_write_intent`) — Router #1 can misroute write intent onto a read cap; a freshly-routed read cap with a human present re-classifies and bumps to write. Unknown output defaults to WRITE.
- **Classifier prompt-injection fence** — every classifier interpolating raw user text (`request_write_intent`, `followup_write_intent`, `repo_edit_intent`, `suggest_behavior_rule`) wraps it via `engine._fenced()`. Advisory only; the cap's static risk + fail-to-WRITE default are the real gate — fenced and unfenced measured equal. A green `intent-write-survives-injected-instruction` is not fence validation.
- **Assistant redirect** — when the tripped cap is the general assistant, a risk bump alone is wrong (its prompt forbids acting); swap in the general worker.
- **Clarify parse biases toward proceeding** (`engine._parse_clarification`) — a weak local model leaks chatter instead of a literal `OK`, so the parser requires an actual `?` to pause. Opposite bias to the write-intent guards: clarification isn't a security control, a false "proceed" is cheap, a silent dead-end is worse.

## Slash commands & continuity

**Slash commands** — `/<cap> [args]` pins a capability, skipping Router #1, resolved from the trusted registry not the client. A bare pinned cap with no args is a valid run, synthesized as `Run the <name> <kind>.`

**Conversation continuity** — `claude -p --resume <session_id>` for follow-ups (raw message, no re-routing), but the write gate still applies: a resumed session is bound to one capability's risk for life, so an escalating follow-up re-classifies via `classify_followup` and can bump read→write. The handoff/resume decision lives in `/api/continue`.

- **The follow-up classifier is read BOTH ways** — WRITE gates a read session; READ makes a write-bound one a **discussion turn**: no preview, no gate, `READ_TOOLS`. A repo chat binds a write cap for life, so "why a mutex here?" paid a 15min preview + a gate.
- **A skipped gate takes the write tools with it** — one cheap call decides, so the toolset is the guard, not the verdict. `run_capability` re-resolves by name, so the turn's risk rides in the payload and only NARROWS. Never under `approval:"auto"` (`DiscussionTurnTests`).
- **Follow-up handoff** (`engine.followup_handoff`, `OTTO_FOLLOWUP_HANDOFF=0` disables) — resume never engages repo-mode/verify/review, so a follow-up *delegating a new task* must re-enter the fresh-run path. Unparseable output defaults to staying in-session — opposite bias to the write-intent guards, since a false handoff breaks normal mid-task Q&A.
- **Anything re-entering `/api/submit` carries the composer** — a handoff re-submit IS a fresh submit, and a cross-backend model pick ends the session and reruns fresh, never resumes on the old model (`ComposerOverrideForwardingTests`, `ResumeModelRebindTests`).
