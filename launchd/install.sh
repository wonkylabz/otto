#!/usr/bin/env bash
# Install (or re-install) Otto as a macOS launchd LaunchAgent — the Darwin
# equivalent of systemd/install.sh.
# Renders launchd/com.otto.plist with this checkout's absolute path + this shell's
# PATH (where `claude` — and any version-manager-shimmed `npx`/`uvx`/etc — actually
# resolve) into ~/Library/LaunchAgents, then (re)bootstraps it into the user's GUI
# launchd domain so it starts now and on every login.
#
# It's a per-user AGENT, not a root LaunchDaemon: execution is `claude -p` against
# the user's subscription + ~/.claude, so it must run as the user (same reason the
# Linux unit is `systemd --user`). Limitation: a LaunchAgent runs in the user's login
# session, so it starts at login and stops at logout — there is no clean per-user
# "run headless with nobody logged in" on macOS without a root LaunchDaemon (which
# would run as the wrong user). This is the honest ceiling of macOS parity; on a
# workstation that stays logged in it behaves like the linger'd systemd unit.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
TEMPLATE="$DIR/launchd/com.otto.plist"
LABEL="com.otto"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"
# Use the invoking shell's PATH rather than a hand-picked list — see the note in the
# plist template. Run this from a normal interactive shell (not a bare `sh -c`) so
# `claude` and any version-manager shims are on $PATH.
PATH_LINE="$PATH"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This installer is macOS-only. On Linux use systemd/install.sh." >&2
  exit 1
fi
# root has no GUI domain (`gui/0` doesn't exist), so every launchctl call below dies
# with "Domain does not support specified action" — and the agent must run as the
# user anyway, for ~/.claude. Refuse rather than half-install.
if [ "$(id -u)" = "0" ]; then
  echo "Do not run this with sudo — Otto is a per-user LaunchAgent (it runs \`claude -p\`" >&2
  echo "against your subscription and ~/.claude). Re-run as yourself:" >&2
  echo "  ./install.sh" >&2
  exit 1
fi
if [ ! -x "$DIR/run.sh" ]; then
  echo "run.sh missing or not executable at $DIR/run.sh" >&2
  exit 1
fi
if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "No .venv — run: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p "$AGENT_DIR" "$DIR/data"
sed -e "s#__OTTO_DIR__#${DIR}#g" -e "s#__PATH__#${PATH_LINE}#g" \
  "$TEMPLATE" > "$PLIST"

# Modern launchctl (macOS 10.11+): bootout any stale copy, then enable + bootstrap +
# kickstart so it's running now and set to start at login. bootout is best-effort
# (errors if not currently loaded).
#
# `enable` comes BEFORE `bootstrap`, not after: a label disabled once (an explicit
# `launchctl disable`, or a `bootout` on some macOS versions) is remembered in the
# domain's disabled list forever, and bootstrapping a disabled label fails with the
# unhelpful "Bootstrap failed: 5: Input/output error". `enable` is a domain-level
# operation that works on a label that isn't loaded, so it clears that first.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl enable "$DOMAIN/$LABEL"
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

cat <<EOF
Installed: $PLIST  (from $DIR)

Manage it:
  launchctl kickstart -k $DOMAIN/$LABEL   # (re)start now
  launchctl print $DOMAIN/$LABEL          # status + last exit code
  tail -f $DIR/data/service.log           # follow logs (Temporal + worker + web server)

Stop / remove:
  launchctl bootout $DOMAIN/$LABEL        # stop now + stop starting at login
EOF
