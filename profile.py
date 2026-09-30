"""Portable Otto config snapshot CLI (#166) — carry one install's configuration to another.

    python3 profile.py export [out.json]                   # secret-free snapshot (stdout if no file)
    python3 profile.py import <file> [--replace] [--yes]   # preview, then apply on confirmation

`merge` (default) never overwrites local config; `--replace` makes this install match the
snapshot. Imported ingresses, rules and crons land disabled. Logic lives in snapshot.py.
"""
import json
import sys

import snapshot


def _print_plan(plan):
    for c in plan["changes"]:
        why = f"  ({c['reason']})" if c.get("reason") else ""
        print(f"  {c['action']:7} {c['section']}: {c['key']}{why}")
    if not plan["changes"]:
        print("  nothing to change")
    for s in plan["secrets"]:
        print(f"  secret needed: {s['for']} — {s['what']}" + (f" {s['name']}" if s["name"] else ""))
    for w in plan["warnings"]:
        print(f"  warning: {w}")


def main(argv):
    if argv[:1] == ["export"] and len(argv) <= 2:
        out = json.dumps(snapshot.export(), indent=2)
        if len(argv) > 1:
            with open(argv[1], "w") as f:
                f.write(out)
            print(f"snapshot written to {argv[1]}")
        else:
            print(out)
        return 0
    if argv[:1] == ["import"] and len(argv) >= 2:
        with open(argv[1]) as f:
            snap = json.load(f)
        mode = "replace" if "--replace" in argv else "merge"
        plan = snapshot.preview(snap, mode)
        print(f"{mode} import would:")
        _print_plan(plan)
        if "--yes" not in argv and input("apply? [y/N] ").strip().lower() != "y":
            print("nothing applied")
            return 1
        summary = snapshot.apply(snap, mode, plan["fingerprint"])
        print(f"applied {len(summary['applied'])} change(s)")
        for s in summary["status"]:
            print(f"  {s}")
        return 0
    print(__doc__.strip())
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
