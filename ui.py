"""Terminal trace helpers. The [TAGS] map to the Otto layers, so each request
prints its journey down the stack."""
import os
import sys

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_C = {
    "INGRESS": "\033[96m", "ORCH": "\033[95m", "ROUTER": "\033[94m",
    "GATEWAY": "\033[93m", "RUN": "\033[92m", "GATE": "\033[1;91m",
    "COST": "\033[90m", "MEMORY": "\033[90m", "AUDIT": "\033[90m",
    "ESTOP": "\033[1;91m",
}
_R = "\033[0m"


def trace(tag, msg):
    if _COLOR:
        print(f"   {_C.get(tag,'')}[{tag:<7}]{_R} {msg}")
    else:
        print(f"   [{tag:<7}] {msg}")


def say(msg=""):
    print(msg)


def banner(n_agents, n_skills, n_read):
    say("=" * 70)
    say(" OTTO  -  local orchestrator for your REAL Claude Code agents & skills")
    say("=" * 70)
    say(f" Discovered: {n_agents} subagents + {n_skills} skills  "
        f"({n_read} read-only, {n_agents + n_skills - n_read} writers)")
    say("")
    say(" A request flows:")
    say("   [INGRESS] you  ->  [ROUTER] Claude picks the agent/skill")
    say("   ->  [GATE] reads auto-run / writes need approval")
    say("   ->  [RUN] claude -p executes it  ->  [AUDIT]")
    say("")
    say(" Try:")
    say("   what's on the board                     (read -> auto-runs)")
    say("   refine and implement GitHub issue 1529  (write -> asks approval)")
    say("")
    say(" Commands:  /list   /memory   /help   /quit")
    say(" Env: OTTO_DRY_RUN=1 routes + classifies but does NOT execute.")
    say("=" * 70)
