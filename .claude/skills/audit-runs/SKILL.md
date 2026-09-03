---
name: audit-runs
description: Investigate Otto's own task runs for errors — supervisor kills, verify failures, budget stops, local-model context overflows, delivery failures, needs-human states. Use when the user reports "something went wrong", asks what happened on a run, wants a health check on recent Otto activity, or after any run that landed in Blocked/Needs-you.
---

# Audit Otto runs

Otto (this repo) is an agent orchestrator: it routes requests to capabilities, runs them
through `claude -p` or a local model, verifies the result, and records everything. This skill
investigates what actually happened on past runs by reading Otto's own logs — it never needs
the web UI or a running server.

## Where the evidence lives

| Source | Contents | Format |
|---|---|---|
| `data/otto.db`'s `audit` table (issue #103, SQLite/WAL) | Every attempt, verdict, supervisor action, terminal outcome — one row per event, `data` column is the full JSON entry | columns: `id`, `at`, `workflow`, `capability`, `verified`, `data`; also promoted inside `data`: `outcome`, `risk`, `attempt`, `cost_usd`, `model`, `backend`, `reason`, `duration_s` |
| `data/otto.db`'s `audit_content` table | Same `workflow`+`at` keys, but with the FULL result text/critique (the `audit` table only has metadata) | columns: `id`, `at`, `workflow`, `attempt`, `data` |
| `data/audit.log` / `data/audit-content.log` (pre-migration history, frozen) | Same shape as above, one JSON object per line — only present if this install predates the SQLite migration; not written to anymore | JSON lines |
| `data/transcripts/<wid>-a<attempt>.jsonl` | The raw `claude -p` / local-runtime stream for one attempt — every tool call, tool result, and the final `result` event | JSON lines, `otto-meta` first line has `cwd` + the full prompt |
| `/tmp/otto-worker.log` | Live trace lines from the worker process (`[RUN]`, `[VERIFY]`, `[SUPERVISE]`, `[ESCALATE]`, `[GATE]`, `[ROUTER]`, `[WORKSPACE]`...) | Plain text, no timestamps — order-only |
| `data/service.log` | Process-level: service start/stop, crashes, port binding | Plain text |
| `data/dismissed.json`, `/api/needs-you` (if server is up) | Runs currently flagged for human attention | JSON |

The `audit` table is the index — query it first to find workflow ids worth digging into, then
pull the matching transcript(s) for the actual root cause. `audit` rows alone never explain WHY
something failed (e.g. `"verified": false` doesn't say if it was a wrong answer, a context-window
crash, or a supervisor kill) — always cross-reference the transcript or the critique in
`audit_content` before concluding.

## Workflow

1. **Scope the window.** Default to the last 24h unless the user names a run or a wider range.
   ```bash
   sqlite3 data/otto.db "SELECT data FROM audit ORDER BY id DESC LIMIT 300"
   ```
   If a workflow id was mentioned, or you need earlier history, filter instead:
   ```bash
   sqlite3 data/otto.db "SELECT data FROM audit WHERE workflow = '<wid>'"
   sqlite3 data/otto.db "SELECT COUNT(*) FROM audit WHERE json_extract(data, '$.outcome') = 'supervisor_kill'"
   ```

2. **Flag the interesting outcomes.** Not every `verified: false` is a bug — repo-mode runs with
   an opened PR are *advisory-only by design* (CLAUDE.md: unverified PR → Review column, not
   Blocked). Focus on:
   - `"outcome": "supervisor_kill"` — a mid-run abort; read its `reason`/critique from
     `audit_content` (same `workflow`+`at`) to judge if it was a legitimate catch or a
     false positive (e.g. confusing an isolated workspace clone path for the wrong repo —
     a known failure mode, see `supervisor.py`'s `_prompt`).
   - Consecutive `"verified": false` across all attempts up to `MAX_VERIFY_ATTEMPTS` (3 by
     default) with no PR opened → real `needs_human` (`reason: verify_exhausted`).
   - `"reason": "budget_exceeded"` — cost ceiling hit mid-ladder.
   - `"outcome": "denied"` — a human declined the write gate (not a bug, just note it).
   - Any `is_error`/errored attempt — pull the transcript's last `result` event; local-runtime
     errors are self-describing (`context window full`, `tools rejected`, `local model server
     unavailable`) and point at a model/config issue rather than an Otto logic bug.
   - `review_fail` / `qa_fail` / `delivery_failed` / `workflow_error` — read
     `_NEEDS_HUMAN_BANNER` reasons in `workflows.py` if the label is unfamiliar.

3. **Pull the transcript for anything non-obvious.** For a given `<wid>` and attempt `<n>`:
   ```bash
   python3 -c "
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

4. **Cross-check the worker log for supervisor/escalation narrative.** `/tmp/otto-worker.log`
   has no timestamps, so match by workflow id and sequence, not by time:
   ```bash
   grep -A2 -B2 '<wid>' /tmp/otto-worker.log
   ```

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
