---
name: audit-runs
description: Investigate Otto's own task runs for errors — supervisor kills, verify failures, budget stops, local-model context overflows, delivery failures, needs-human states. Use when the user reports "something went wrong", asks what happened on a run, wants a health check on recent Otto activity, or after any run that landed in Blocked/Needs-you.
---

# Audit Otto runs

Otto (this repo) is an agent orchestrator: it routes requests to capabilities, runs them
through `claude -p` or a local model, verifies the result, and records everything. This skill
investigates what actually happened on past runs by reading Otto's own logs — it never needs
the web UI or a running server.

## Start with the aggregate

Before opening any single run, get the shape of the window. `insights.py` groups the trail by
cause, by WHO reached each adverse verdict (judge model, review, QA, supervisor, harness), by
tier call (routing/clarify/judge decisions from the ledger), by stage and by capability — each
against the window before it:

```bash
./.venv/bin/python insights.py --since 7d          # or 24h, 30d; --json for the full report
./.venv/bin/python insights.py --since 7d --changes-only   # prints nothing if nothing moved
```

The same report is the Audit tab's Health section (`/api/insights?days=7`). Its "What changed"
list is the place to start: a cause that rose, a pass rate that fell, humans overruling the judge
more often. Only then pick runs to dig into.

## Where the evidence lives

| Source | Contents | Format |
|---|---|---|
| `data/otto.db` `audit` table (SQLite/WAL) | Every attempt, verdict, supervisor action, terminal outcome — one row per event; `data` is the full JSON entry | columns `id`, `at`, `workflow`, `capability`, `verified`, `needs_human`, `data`. Inside `data`: `outcome`, `reason`, `risk`, `attempt`, `cost_usd`, `model`, `backend`, `duration_s`, `tokens`, `times` (per-stage spans), `verdict_source`, `verdict_model`, `fallback_from`/`fallback_reason`/`fallback_detail`, `repo`, `mcp` |
| `data/otto.db` `audit_content` table | Same `workflow`+`at` keys, with the FULL request/result/critique | columns `id`, `at`, `workflow`, `attempt`, `data` |
| `data/otto.db` `tier_calls` table (`ledger.py`) | One row per cheap-tier call — routing, clarify, write-intent, verify/supervise samples, plan, memory GC — with its parsed `decision`. TTL `OTTO_TIER_LEDGER_TTL_H` (30 days) | `at`, `wid`, `tier`, `model`, `duration_ms`, `cost_usd`, `ok`, `fell_back`, `error`, `decision` |
| `data/transcripts/<wid>-a<attempt>.jsonl` | The raw `claude -p` / local-runtime stream for one attempt; `otto-meta` first line has `cwd`, the prompt and `system_context` | JSON lines |
| `data/logs/<stream>-<date>.log` | `ui.trace` lines (`[RUN]`, `[VERIFY]`, `[SUPERVISE]`, `[ESCALATE]`, `[GATE]`, `[ROUTER]`, `[WORKSPACE]`…), scrubbed, durable across restarts | `<iso ts> [<wid>] [TAG] msg` |
| `/tmp/otto-worker.log`, `/tmp/otto-server.log`, `/tmp/otto-temporal.log` | RAW stdio — tracebacks that never went through `ui.trace`. Cleared on reboot | Plain text |
| `data/audit.log` / `data/audit-content.log` | Pre-SQLite history, frozen; only on old installs | JSON lines |
| `/api/needs-you`, `/api/stats` (scorecard + `false_fails`), `/api/costs` | Live views, if the server is up | JSON |

**Open the DB read-only**: `sqlite3 -readonly data/otto.db "…"`. If that says `unable to open
database file`, the WAL index (`otto.db-shm`) does not exist yet — a read-only handle cannot
create it; use `insights.py --json` instead of opening it writable.

The `audit` table is the index; it never explains WHY. `"verified": false` can be a wrong answer,
a context-window crash or a supervisor kill — `verdict_source` says who decided, and the critique
in `audit_content` or the transcript says why.

## Workflow

1. **Scope the window.** Use the aggregate above, then list the runs behind a cause:
   ```bash
   sqlite3 -readonly data/otto.db "SELECT workflow, at, json_extract(data,'$.reason') FROM audit
     WHERE json_extract(data,'$.outcome')='needs_human' AND at >= date('now','-7 days') ORDER BY id DESC"
   sqlite3 -readonly data/otto.db "SELECT data FROM audit WHERE workflow = '<wid>'"
   sqlite3 -readonly data/otto.db "SELECT tier, decision, ok, error FROM tier_calls WHERE wid = '<wid>'"
   ```
   The last one answers "why did it route there / why did the judge fail it" for one run.

2. **Flag the interesting outcomes.** Not every `verified: false` is a bug — a repo-mode run with
   an open PR is advisory-only by design (`.claude/rules/repo-work.md`, "Terminal state").
   `outcome` values: `ran`, `plan_preview`, `denied`, `needs_human`, `human_accepted`,
   `supervisor_kill`, `supervisor_shadow`, `supervisor_steer`. On a `needs_human` row, `reason`
   is the cause; the human-facing wording for each is `_NEEDS_HUMAN_BANNER` in `wf_runtime.py`:
   - `verify_exhausted` — every rung failed the judge, no PR to fall back on.
   - `published_unverified` — failed AFTER posting, so not retried (a retry posts twice).
   - `harness_exhausted` — the harness kept dying; not a judgement of the cap.
   - `review_fail` / `review_inconclusive` / `qa_fail` / `qa_inconclusive` — post-PR loops.
   - `budget_exceeded`, `gate_timeout`, `merge_failed`, `merge_unreviewed`, `factory_no_pr`.
   - `claude_usage_limit` / `claude_auth_expired` — walls: a limit resets in hours, a login
     needs re-authenticating; neither is an Otto bug.
   - `delivery_failed`, `workflow_error`, `terminated` (a human hard-stopped it).
   - `human_accepted` (its own outcome) — a human overruled the judges. A cluster of these on one
     judge model is a JUDGE problem: it feeds `false_fails` on the scorecard.
   - `supervisor_kill` — read its critique in `audit_content` (same `workflow`+`at`) to judge a
     real catch from a false positive (e.g. an isolated workspace clone path mistaken for the
     wrong repo, see `supervisor.py`'s `_prompt`).
   - Errored attempts — the transcript's last `result` names the failure; local-runtime errors
     are self-describing (`context window full`, `tools rejected`, `local model server
     unavailable`). A critique starting `prior attempt errored or timed out` is a HARNESS death;
     exclude those from any model-quality claim.

3. **Pull the transcript for anything non-obvious.** For a given `<wid>` and attempt `<n>`:
   ```bash
   ./.venv/bin/python -c "
   import json
   with open('data/transcripts/<wid>-a<n>.jsonl') as f:
       for line in f:
           d = json.loads(line)
           t = d.get('type')
           if t == 'otto-meta':
               print('cwd:', d.get('cwd')); continue
           if t == 'assistant':
               for b in (d.get('message') or {}).get('content') or []:
                   if b.get('type') == 'text': print('TEXT:', b['text'][:300])
                   elif b.get('type') == 'tool_use': print('TOOL:', b.get('name'), json.dumps(b.get('input'))[:200])
           elif t == 'user':
               for b in (d.get('message') or {}).get('content') or []:
                   if b.get('type') == 'tool_result':
                       c = b.get('content')
                       if isinstance(c, list): c = ' '.join(x.get('text','') for x in c if isinstance(x, dict))
                       print('RESULT:', str(c)[:300])
           elif t == 'result':
               print('FINAL:', json.dumps(d)[:500])
   "
   ```
   Read the FINAL `result` event first — for an errored attempt it usually names the exact
   failure (context overflow, tool rejection, timeout). Then skim tool calls chronologically
   for reasoning errors: wrong `cwd`/repo, repeating a failing command, ignoring the actual
   ticket, etc.

4. **Cross-check the trace log for supervisor/escalation narrative.** Every line carries its
   own timestamp AND the run it belongs to, so filter by wid rather than reading an
   interleaving of every concurrent run:
   ```bash
   grep -h '\[<wid>\]' data/logs/*.log          # one run's whole journey, in order
   ```
   It survives a worker restart. For a crash with no trace line behind it, the traceback is in
   the raw stdio at `/tmp/otto-worker.log`.

5. **Classify each real finding**, don't just list outcomes:
   - **Otto bug** (routing/gate/supervisor/workflow logic did the wrong thing) → propose a
     concrete code fix, file+line.
   - **Model/config limitation** (local model too weak, context window too small, wrong
     execution-tier assignment) → point at `data/models.json` / `config.py` knobs, not code.
   - **Expected behavior working as designed** (advisory unverified PR, budget stop, human
     denial) → say so plainly, don't manufacture a problem.
   - **Genuinely inconclusive** → say what's missing (e.g. transcript already GC'd past
     `TRANSCRIPT_TTL_H`) rather than guessing.

## Reporting

Lead with a one-line verdict per run investigated (what broke, or "nothing wrong — advisory
unverified PR as designed"). Then, only for real findings, give root cause + file:line + a
proposed fix. Skip runs that passed cleanly — don't pad the report with them. If nothing in the
window looks wrong, say that plainly instead of manufacturing findings.
