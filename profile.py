"""Portable Otto profile CLI (portability) — carry one install's configuration to another.

    python3 profile.py export [out.json]     # secret-free profile (stdout if no file)
    python3 profile.py import <profile.json> # non-clobbering merge; prints what happened

The profile carries custom caps + MCP defs (secret-free), policy overrides, the model config
(pasted API keys stripped), behaviour rules, knowledge docs (re-embedded on import), and
project standing-instructions keyed by git origin. What it deliberately does NOT carry:
schedules (auto-running imported crons is surprising), board config (instance-specific),
learned facts/solutions (earned per install), and ~/.claude itself (version that separately).
Logic lives in policy.export_profile / policy.import_profile.
"""
import json
import sys

import policy


def main(argv):
    if len(argv) >= 1 and argv[0] == "export":
        out = json.dumps(policy.export_profile(), indent=2)
        if len(argv) > 1:
            with open(argv[1], "w") as f:
                f.write(out)
            print(f"profile written to {argv[1]}")
        else:
            print(out)
        return 0
    if len(argv) == 2 and argv[0] == "import":
        with open(argv[1]) as f:
            profile = json.load(f)
        summary = policy.import_profile(profile)
        print(json.dumps(summary, indent=2))
        un = summary.get("projects", {}).get("unmatched") or []
        if un:
            print("\nclone + register these repos (Admin → Project repos), then re-import "
                  "to apply their instructions:")
            for p in un:
                print(f"  - {p['name']}: {p['origin']}")
        return 0
    print(__doc__.strip())
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
