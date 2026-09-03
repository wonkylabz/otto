#!/usr/bin/env python3
"""Dev harness for the plan-then-execute go/no-go (design doc 2026-07-16).

Calls the STRONG planner (Claude) on a request and prints the atomic-step plan, so you can
eyeball decomposition quality WITHOUT the full orchestration (no routing gate, no PLAN_MODE
activation). Optionally executes the plan on whatever execution model is configured — point
that at a local qwen to check the other half: can the weak model actually eat the steps?

    python3 plan_eval.py "refactor the retry logic in engine.py and add tests"
    python3 plan_eval.py --cap worker "..."          # pin the executor cap (skip routing)
    python3 plan_eval.py --run "..."                 # also EXECUTE the plan (uses exec model)
    python3 plan_eval.py --samples                   # plan a few built-in multi-step requests

Plan quality needs only Claude on PATH. --run needs the execution model set to a local model
(Admin tab / data/models.json) and that server up, to see qwen execute each step.
"""
import argparse
import sys

import engine
import gateway
import registry


_SAMPLES = [
    "Investigate why the orion-platform EKS build has been failing since last week and open a "
    "ticket summarising the root cause with the failing step and the fix.",
    "Add a --dry-run flag to the deploy script: parse it, thread it through, skip the actual "
    "apply when set, print what would happen, and update the README.",
    "Audit data/models.json for any capability pinned to a local execution model that isn't "
    "tool-free, and produce a table of the offenders with a one-line risk note each.",
]


def _resolve_cap(caps, name, request):
    if name:
        cap = next((c for c in caps if c.name == name), None)
        if not cap:
            sys.exit(f"no capability named {name!r}")
        return cap
    return engine.route(request, caps)


def _print_plan(steps):
    for i, s in enumerate(steps, 1):
        dep = f"  needs={s['needs']}" if s["needs"] else ""
        print(f"  {i}. [{s['id']}] {s['goal']}{dep}")
        if s.get("context"):
            print(f"       context: {s['context']}")
        if s.get("done_when"):
            print(f"       done_when: {s['done_when']}")


def _one(request, caps, cap_name, run):
    print("\n" + "=" * 78)
    print(f"REQUEST: {request}")
    cap = _resolve_cap(caps, cap_name, request)
    entry = gateway.exec_model_entry(cap.name)
    backend = "LOCAL runtime" if entry.get("provider") != "claude" else "claude -p"
    print(f"executor cap : [{cap.kind}] {cap.name} ({cap.risk})")
    print(f"exec model   : {entry.get('name')} ({entry.get('provider')}) -> {backend}")
    print("-" * 78)
    steps = engine.plan_steps(request, cap)
    if not steps:
        print("PLANNER returned no multi-step plan (single atomic task / unparseable) -> "
              "would run as a single turn.")
        return
    print(f"PLAN ({len(steps)} steps):")
    _print_plan(steps)
    if run:
        print("-" * 78)
        print(f"EXECUTING on {entry.get('name')} ...\n")
        result = engine.run_plan(request, cap, steps)
        print("\n" + "-" * 78)
        print("FINAL RESULT:\n")
        print(result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request", nargs="?", help="the multi-step request to plan")
    ap.add_argument("--cap", help="pin the executor capability by name (skip routing)")
    ap.add_argument("--run", action="store_true", help="also EXECUTE the plan (uses exec model)")
    ap.add_argument("--samples", action="store_true", help="plan the built-in sample requests")
    args = ap.parse_args()

    caps = registry.load()
    if args.samples:
        for r in _SAMPLES:
            _one(r, caps, args.cap, args.run)
    elif args.request:
        _one(args.request, caps, args.cap, args.run)
    else:
        ap.error("give a request, or --samples")


if __name__ == "__main__":
    main()
