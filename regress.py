#!/usr/bin/env python3
"""Replay the regression corpus — the documented incidents, re-run against the CURRENT prompts.

    python3 regress.py                     # every cheap case (judge-tier calls; ~1 min)
    python3 regress.py --tier all          # cheap + slow (slow = real `claude -p`; ~10 min)
    python3 regress.py --only critic-      # id prefix filter
    python3 regress.py --list              # what's in the corpus, no calls
    python3 regress.py -n 3                # repeat each case N times (see FLAKINESS below)
    python3 regress.py --stability         # judge flip rate per fixture (10 samples each)

Why it exists: `python3 -m unittest` asserts a prompt CONTAINS a clause. That catches a deleted
line; it cannot catch a clause the model stopped OBEYING, which is the real failure mode for every
behaviour CLAUDE.md records as "measured on the real X". Those measurements were each taken once,
by hand, and then trusted indefinitely. This turns them into something a prompt edit has to pass.

FLAKINESS IS THE POINT, not a defect to engineer away. These assert model BEHAVIOUR, so a case can
fail once and pass twice — that is real signal about how reliably a prompt is obeyed, and averaging
it away would hide exactly the margin you want to see before shipping a prompt change. Use `-n` on
a case you are actively tuning and read the ratio; a case that only holds 2 runs in 3 is a weak
clause, not a passing test.

Run this BEFORE and AFTER editing a prompt in engine.py, and compare. It costs real money and
takes minutes, which is why it is not part of the unit suites and never runs in CI by default.
"""
import argparse
import os
import sys
import time
import traceback

import regress_cases


def _run_one(case, repeats):
    """Run one case `repeats` times; return (passes, total, lines)."""
    passes, lines = 0, []
    for i in range(repeats):
        t0 = time.time()
        try:
            out = case["run"]()
            ok, detail = case["check"](out)
        except Exception as e:  # noqa: BLE001 - a broken case must not abort the corpus
            ok, detail = False, f"raised {type(e).__name__}: {e}"
            if "--debug" in sys.argv:
                traceback.print_exc()
        passes += bool(ok)
        lines.append(f"      {'PASS' if ok else 'FAIL'}  {time.time() - t0:5.1f}s  {detail}")
    return passes, repeats, lines


def stability_stats(verdicts, expect):
    """Pure rollup of N verdicts on ONE input. `flip` is the minority share: 0 = the judge always
    agrees with itself, 0.5 = a coin. `wrong` is the share disagreeing with `expect`."""
    n = len(verdicts)
    passes = sum(bool(v) for v in verdicts)
    if not n:
        return {"n": 0, "passes": 0, "flip": 0.0, "wrong": 0.0}
    return {"n": n, "passes": passes, "flip": min(passes, n - passes) / n,
            "wrong": (n - passes if expect else passes) / n}


def _pin_judge(label):
    """Serve the `verify` tier from pool entry `label` for this process only — never saved. A
    local judge runs at temperature 0, so measuring it says nothing about the `claude -p` one."""
    import gateway
    load = gateway.load

    def pinned(*a, **k):
        cfg = load(*a, **k)
        if label not in [m["name"] for m in cfg.get("pool") or []]:
            raise SystemExit(f"--judge {label!r} is not a pool label")
        return {**cfg, "assign": {**cfg.get("assign", {}), "verify": label}}
    gateway.load = pinned


def _stability(fixtures, repeats, confirmed, judge=None):
    """Sample each fixture `repeats` times. Raw judge unless `confirmed`: `confirm_adverse` hides
    the flip it exists to absorb, so the baseline measures what it is absorbing."""
    import gateway
    if not confirmed:
        os.environ["OTTO_JUDGE_CONFIRMATIONS"] = "1"
    if judge:
        _pin_judge(judge)
    served = gateway._model_for("verify")
    print(f"sampling {len(fixtures)} fixture(s) x{repeats}, "
          f"{'confirmed (production)' if confirmed else 'raw single-sample'} verdicts, "
          f"judge {served['name']} ({served.get('provider')})")
    rows = []
    for f in fixtures:
        verdicts, models = [], set()
        for _ in range(repeats):
            gateway._LAST.pop("verify", None)
            try:
                verdicts.append(bool(f["run"]()["passed"]))
            except Exception as e:  # noqa: BLE001 - one bad sample must not abort the baseline
                print(f"      sample raised {type(e).__name__}: {e}")
            models.add((gateway._LAST.get("verify") or {}).get("model", "(no judge call)"))
        s = stability_stats(verdicts, f["expect"])
        rows.append((f, s))
        # A local judge falls back to Claude silently; name what ACTUALLY judged.
        served_by = "" if models == {served["name"]} else f"  served by {sorted(models)}"
        print(f"  {f['id']:28} expect={'PASS' if f['expect'] else 'FAIL'}  "
              f"pass {s['passes']}/{s['n']}  flip {s['flip']:.0%}  wrong {s['wrong']:.0%}"
              f"{served_by}")
    print("\n" + "=" * 78)
    for label, want in (("known-good (false FAIL)", True), ("known-bad (false PASS)", False)):
        group = [s for f, s in rows if f["expect"] is want and s["n"]]
        if group:
            print(f"  {label:26} mean flip {sum(s['flip'] for s in group) / len(group):.0%}  "
                  f"mean wrong {sum(s['wrong'] for s in group) / len(group):.0%}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", choices=["cheap", "slow", "all"], default="cheap",
                    help="cheap = judge-tier calls (default); slow = full `claude -p` passes")
    ap.add_argument("--only", default="", help="run cases whose id starts with this")
    ap.add_argument("--list", action="store_true", help="list the corpus and exit")
    ap.add_argument("-n", "--repeats", type=int, default=1,
                    help="run each case N times and report the pass ratio")
    ap.add_argument("--debug", action="store_true", help="print tracebacks from failing cases")
    ap.add_argument("--stability", action="store_true",
                    help="sample the judge-stability fixtures -n times (default 10) and report "
                         "each one's flip rate")
    ap.add_argument("--confirmed", action="store_true",
                    help="with --stability: measure the production verdict, confirmations on")
    ap.add_argument("--judge", default="",
                    help="with --stability: serve the verify tier from this pool label")
    args = ap.parse_args()

    if args.stability:
        fixtures = [f for f in regress_cases.STABILITY if f["id"].startswith(args.only)]
        if not fixtures:
            print(f"no stability fixtures match only={args.only!r}")
            return 1
        return _stability(fixtures, args.repeats if args.repeats > 1 else 10, args.confirmed,
                          args.judge)

    cases = [c for c in regress_cases.CASES
             if (args.tier == "all" or c["tier"] == args.tier) and c["id"].startswith(args.only)]

    if args.list:
        for c in regress_cases.CASES:
            print(f"  [{c['tier']:5}] {c['id']}\n           {c['what']}\n"
                  f"           from: {c['incident']}")
        return 0
    if not cases:
        print(f"no cases match tier={args.tier} only={args.only!r}")
        return 1

    print(f"replaying {len(cases)} case(s), tier={args.tier}"
          + (f", {args.repeats}x each" if args.repeats > 1 else ""))
    failed, t0 = [], time.time()
    for c in cases:
        print(f"\n  {c['id']}  —  {c['what']}")
        passes, total, lines = _run_one(c, args.repeats)
        for ln in lines:
            print(ln)
        if passes < total:
            failed.append((c, passes, total))
        if total > 1:
            print(f"      -> {passes}/{total}")

    print("\n" + "=" * 78)
    if failed:
        print(f"FAILED {len(failed)}/{len(cases)} in {time.time() - t0:.0f}s")
        for c, p, t in failed:
            print(f"  {c['id']}  ({p}/{t})  regression from: {c['incident']}")
        # A corpus failure is a behaviour change, not necessarily a bug — a deliberate prompt change
        # can legitimately move a case. Decide, then update the case WITH the reason, or revert.
        print("\nEach failure is a behaviour CLAUDE.md documents as measured. If the change was"
              "\ndeliberate, update the case and say why; otherwise the prompt edit regressed it.")
        return 1
    print(f"all {len(cases)} passed in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
