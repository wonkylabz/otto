"""Health report over a time window: what went wrong, why, and whether it is getting better.

`audit.scorecard` answers "how reliable is each capability" all-time; nothing answered "what broke
this week, by cause, against last week" — the audit-runs skill hand-wrote SQL for it every time,
and the tier-call ledger (routing/clarify/judge decisions) had no reader outside the tests.

Pure over `(entries, calls)` like `scorecard`, so it is testable against a fixture trail. The
server serves it at `/api/insights`; `python insights.py --since 7d` is the CLI twin the skill
and a weekly cron call instead of SQL.

Window boundaries compare the trail's own `at` strings (local, `isoformat(timespec="seconds")`),
never a parsed datetime — the same spelling both ends, so no timezone guess can shift a row.
"""
import argparse
import datetime
import json
import math
import re
import sys

import audit

# Rows that are a normal day's work, not something a person should look at.
_ROUTINE = {"ran", "plan_preview"}


# An MCP-registry change (`audit.audit_mcp_change`) mints a wid but has no run behind it, so it
# would read as Otto activity on every runs figure.
_CONFIG_ROW = re.compile(r":mcp-[\w-]+$")


def _needs_attention(r):
    """The ONE attention predicate. A shadow row is written for EVERY supervised attempt (SHADOW
    is the default mode), so only one whose supervisor would have retried is a signal; counting
    the rest made Attention track the attempt count."""
    o = r.get("outcome")
    if o in _ROUTINE:
        return False
    return o != "supervisor_shadow" or r.get("reason") == "supervisor_would_retry"
# A comparison that moves by less than this many rows is noise, never a "change".
_MIN_DELTA = 2


def _cutoffs(days, now):
    now = now or datetime.datetime.now()
    since = now - datetime.timedelta(days=days)
    prev = since - datetime.timedelta(days=days)
    fmt = lambda d: d.isoformat(timespec="seconds")
    return fmt(prev), fmt(since), fmt(now)


def _pct(vals, q):
    # Nearest-rank, matching audit.stage_timings, so one report never shows two medians.
    if not vals:
        return None
    vals = sorted(vals)
    return vals[max(0, math.ceil(q * len(vals)) - 1)]


def _window_stats(rows):
    """The headline numbers for ONE window's rows."""
    killed = audit.killed_attempts(rows)
    wids = {r.get("workflow") for r in rows if r.get("workflow")}
    runs = {}
    for r in rows:
        if r.get("attempt") is not None and r.get("verified") is not None and audit.by_judge(r, killed):
            runs.setdefault(r.get("workflow"), []).append(r.get("verified") is True)
    judged = len(runs)
    passed = sum(1 for v in runs.values() if any(v))
    durs = [r["duration_s"] for r in rows
            if r.get("attempt") is not None and isinstance(r.get("duration_s"), (int, float))]
    return {
        "runs": len(wids), "judged": judged,
        "pass_rate": round(passed / judged, 3) if judged else None,
        # RUNS, like the day chart's, so the two figures named attention reconcile on screen.
        "attention": len({r.get("workflow") or f"?{id(r)}" for r in rows if _needs_attention(r)}),
        "cost_usd": round(sum((r.get("cost_usd") or 0) for r in rows), 4),
        "fallbacks": len({r.get("workflow") or f"?{id(r)}" for r in rows if r.get("fallback_from")}),
        "accepted": sum(1 for r in rows if r.get("outcome") == "human_accepted"),
        "attempt_p50_s": _pct(durs, 0.5), "attempt_p90_s": _pct(durs, 0.9),
    }


def _counted(rows, key):
    out = {}
    for r in rows:
        k = key(r)
        if k is not None:
            out[k] = out.get(k, 0) + 1
    return out


def _runs_by(rows, key):
    """Like `_counted`, but DISTINCT RUNS per key — what every attention and fallback figure
    counts, so a run whose terminal row was written twice (record_terminal, then the reaper) is
    one run on every tile, tab and change line, never two."""
    out = {}
    for r in rows:
        k = key(r)
        if k is not None:
            out.setdefault(k, set()).add(r.get("workflow") or f"?{id(r)}")
    return {k: len(v) for k, v in out.items()}


def _attention_key(r):
    if not _needs_attention(r):
        return None
    return (r.get("outcome") or "?", r.get("reason") or "")


def _fail_key(killed):
    # An adverse verdict, attributed to WHO reached it — a bad capability and a bad judge leave
    # identical rows otherwise (memory-privacy.md, Scorecard). A row older than
    # `verdict_source` is attributed through `audit.by_judge`, the one predicate scorecard uses,
    # so a kill or a post-PR round is never blamed on the judge.
    def key(r):
        if r.get("attempt") is None or r.get("verified") is not False:
            return None
        src = r.get("verdict_source") or (
            "judge" if audit.by_judge(r, killed) else
            "supervisor" if (r.get("workflow"), r.get("attempt")) in killed else "post_pr")
        return (src, r.get("verdict_model") or "")
    return key


def _tiers(calls):
    per = {}
    for c in calls:
        t = per.setdefault(c.get("tier") or "?", {"calls": 0, "failed": 0, "fell_back": 0,
                                                  "cost_usd": 0.0, "ms": [], "decisions": {}})
        t["calls"] += 1
        t["failed"] += 0 if c.get("ok") else 1
        t["fell_back"] += 1 if c.get("fell_back") else 0
        t["cost_usd"] += c.get("cost_usd") or 0
        if c.get("duration_ms") is not None:
            t["ms"].append(c["duration_ms"])
        d = (c.get("decision") or "").strip()
        if d:
            t["decisions"][d] = t["decisions"].get(d, 0) + 1
    out = []
    for tier, t in per.items():
        top = sorted(t["decisions"].items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        out.append({"tier": tier, "calls": t["calls"], "failed": t["failed"],
                    "fell_back": t["fell_back"], "cost_usd": round(t["cost_usd"], 4),
                    "p50_ms": _pct(t["ms"], 0.5), "p90_ms": _pct(t["ms"], 0.9),
                    "decisions": [{"decision": d, "count": n} for d, n in top]})
    out.sort(key=lambda r: (-r["calls"], r["tier"]))
    return out


def _by_day(rows, since_at, until_at):
    """One entry per CALENDAR day of the window, quiet days included — a chart that skips them
    draws two busy days as neighbours. `attention` counts RUNS with any non-routine row, so it
    is a share of `runs`; counting rows let one run's three needs-human rows read as 300%."""
    days = {}
    for r in rows:
        day = (r.get("at") or "")[:10]
        if not day:
            continue
        d = days.setdefault(day, {"runs": set(), "attention": set(), "cost_usd": 0.0})
        wid = r.get("workflow") or f"?{id(r)}"
        d["runs"].add(wid)
        if _needs_attention(r):
            d["attention"].add(wid)
        d["cost_usd"] += r.get("cost_usd") or 0
    out, day = [], datetime.date.fromisoformat(since_at[:10])
    last = datetime.date.fromisoformat(until_at[:10])
    while day <= last:
        d = days.get(day.isoformat(), {"runs": (), "attention": (), "cost_usd": 0})
        out.append({"day": day.isoformat(), "runs": len(d["runs"]),
                    "attention": len(d["attention"]), "cost_usd": round(d["cost_usd"], 4)})
        day += datetime.timedelta(days=1)
    return out


def _changes(now, prev, attention, prev_attention):
    """The few movements worth a person's attention — what a weekly cron prints, or stays
    silent on. Empty means nothing moved beyond noise."""
    out = []
    if now["pass_rate"] is not None and prev["pass_rate"] is not None:
        d = now["pass_rate"] - prev["pass_rate"]
        if abs(d) >= 0.1 and now["judged"] >= 5:
            out.append(f"judged pass rate {'fell' if d < 0 else 'rose'} "
                       f"{prev['pass_rate']:.0%} → {now['pass_rate']:.0%}")
    for key in sorted(set(attention) | set(prev_attention)):
        a, b = attention.get(key, 0), prev_attention.get(key, 0)
        # Absolute AND relative: 32 → 35 runs on a cause is a normal week, 0 → 3 is not.
        if a - b >= max(_MIN_DELTA, 0.25 * b):
            out.append(f"{_label(key)}: {b} → {a}")
    if prev["cost_usd"] and now["cost_usd"] > 1.5 * prev["cost_usd"] and now["cost_usd"] - prev["cost_usd"] >= 1:
        out.append(f"spend ${prev['cost_usd']:.2f} → ${now['cost_usd']:.2f} API-equiv")
    return out


def _label(key):
    outcome, reason = key
    return f"{outcome}/{reason}" if reason and reason != outcome else outcome


def ledger_rows(days, now=None):
    """The ledger calls `report` reads — only the current window's, filtered in SQL — and how far
    back the ledger reaches. One door for the route and the CLI."""
    import ledger
    _, since_at, _ = _cutoffs(days, now)
    return ledger.calls(since=since_at, limit=10_000_000), ledger.oldest_at()


def report(entries, calls=(), *, days=7, now=None, ledger_oldest=None):
    """The whole report for the last `days`, compared with the `days` before it."""
    prev_at, since_at, until_at = _cutoffs(days, now)
    cur, prev = [], []
    for e in entries:
        if _CONFIG_ROW.search(e.get("capability") or ""):
            continue
        at = e.get("at") or ""
        if since_at <= at <= until_at:
            cur.append(e)
        elif prev_at <= at < since_at:
            prev.append(e)
    attention, prev_attention = _runs_by(cur, _attention_key), _runs_by(prev, _attention_key)
    fails = _counted(cur, _fail_key(audit.killed_attempts(cur)))
    fell = _runs_by(cur, lambda r: r.get("fallback_reason") if r.get("fallback_from") else None)
    calls_now = [c for c in calls if since_at <= (c.get("at") or "") <= until_at]
    stats_now, stats_prev = _window_stats(cur), _window_stats(prev)
    caps = audit.scorecard(cur)
    for c in caps:
        c.pop("recent", None)            # the per-run sparkline belongs to Admin, not here
    return {
        "window": {"days": days, "since": since_at, "until": until_at, "prev_since": prev_at},
        "now": stats_now, "prev": stats_prev,
        "attention": sorted(
            ({"outcome": k[0], "reason": k[1], "count": n, "prev": prev_attention.get(k, 0)}
             for k, n in attention.items()), key=lambda r: (-r["count"], r["outcome"], r["reason"])),
        "fail_sources": sorted(({"source": k[0], "model": k[1], "count": n} for k, n in fails.items()),
                               key=lambda r: (-r["count"], r["source"])),
        "fallbacks": sorted(({"reason": k, "count": n} for k, n in fell.items()),
                            key=lambda r: (-r["count"], r["reason"])),
        "by_day": _by_day(cur, since_at, until_at),
        "caps": caps,
        "stages": audit.stage_timings(cur),
        "tiers": _tiers(calls_now),
        "ledger_covers": ledger_oldest or min((c.get("at") or "" for c in calls), default=None),
        "changes": _changes(stats_now, stats_prev, attention, prev_attention),
    }


def render_text(r):
    """Plain-text form for the CLI and a Slack/ntfy post: no tables (Slack renders none)."""
    n, p, w = r["now"], r["prev"], r["window"]
    pr = lambda v: "—" if v is None else f"{v:.0%}"
    span = (f"{w['days'] * 24:g}h" if w["days"] < 1 else f"{w['days']:g}d")
    lines = [f"Otto health, last {span} (vs the {span} before)",
             f"- runs {n['runs']} (was {p['runs']}), judged pass rate {pr(n['pass_rate'])} "
             f"(was {pr(p['pass_rate'])})",
             f"- needs attention {n['attention']} (was {p['attention']}), humans overruled the "
             f"judge {n['accepted']}×, fallbacks {n['fallbacks']}",
             f"- spend ≈ ${n['cost_usd']:.2f} API-equiv (was ${p['cost_usd']:.2f})"]
    if r["changes"]:
        lines += ["", "What changed:"] + [f"- {c}" for c in r["changes"]]
    if r["attention"]:
        lines += ["", "Needs attention, by cause:"]
        lines += [f"- {_label((a['outcome'], a['reason']))}: {a['count']} (was {a['prev']})"
                  for a in r["attention"][:12]]
    if r["fail_sources"]:
        lines += ["", "Adverse verdicts, by who reached them:"]
        lines += [f"- {f['source']}{' · ' + f['model'] if f['model'] else ''}: {f['count']}"
                  for f in r["fail_sources"]]
    if r["tiers"]:
        lines += ["", "Tier calls (routing, clarify, judges):"]
        for t in r["tiers"]:
            top = ", ".join(f"{d['decision']}×{d['count']}" for d in t["decisions"][:3])
            lines.append(f"- {t['tier']}: {t['calls']} calls, {t['failed']} failed, "
                         f"{t['fell_back']} fell back{' — ' + top if top else ''}")
    return "\n".join(lines)


def _days(spec):
    m = re.fullmatch(r"\s*(\d+)\s*([dhw]?)\s*", spec or "")
    if not m:
        raise argparse.ArgumentTypeError(f"--since wants e.g. 7d, 48h or 2w, got {spec!r}")
    n, unit = int(m.group(1)), m.group(2) or "d"
    return {"h": n / 24, "d": n, "w": n * 7}[unit]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Otto health report over a time window.")
    ap.add_argument("--since", type=_days, default=7, help="window, e.g. 7d (default), 48h, 2w")
    ap.add_argument("--json", action="store_true", help="the full report as JSON")
    ap.add_argument("--changes-only", action="store_true",
                    help="print nothing (exit 0) unless something moved beyond noise")
    a = ap.parse_args(argv)
    calls, oldest = ledger_rows(a.since)
    r = report(audit.iter_audit_entries(), calls, days=a.since, ledger_oldest=oldest)
    if a.changes_only and not r["changes"]:
        return 0
    sys.stdout.write((json.dumps(r, indent=2) if a.json else render_text(r)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
