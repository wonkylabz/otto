#!/usr/bin/env python3
"""Replay the regression corpus — the documented incidents, re-run against the CURRENT prompts.

    python3 regress.py                     # every cheap case (judge-tier calls; ~1 min)
    python3 regress.py --tier all          # cheap + slow (slow = real `claude -p`; ~10 min)
    python3 regress.py --only critic-      # id prefix filter
    python3 regress.py --list              # what's in the corpus, no calls
    python3 regress.py -n 3                # repeat each case N times (see FLAKINESS below)

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
    args = ap.parse_args()

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
