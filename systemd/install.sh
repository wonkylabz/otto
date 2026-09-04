#!/usr/bin/env bash
# Install (or re-install) Otto as a systemd --user service.
# Renders systemd/otto.service with this checkout's absolute path + this shell's
# PATH (where `claude` — and any version-manager-shimmed `npx`/`uvx`/etc — actually
# resolve), then reloads the user daemon.
#
# It does NOT enable/start anything — the last lines print the commands to do that,
# so you stay in control of when the stack first comes up.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
TEMPLATE="$DIR/systemd/otto.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
# Use the invoking shell's PATH rather than a hand-picked list: `claude` lives in
# ~/.local/bin (not on the systemd --user default PATH), and MCP servers shell out
# to `npx`/`uvx`/etc, which may be managed by a version manager (asdf, nvm, ...)
# whose shims live somewhere PATH-dependent, not a fixed path. Run this script from
# a normal interactive shell (not a bare `sh -c`) so those tools are on $PATH.
PATH_LINE="$PATH"

# Root has its own systemd --user session, not yours: the unit would land in root's manager,
# where it cannot see your ~/.claude — and `claude -p` runs against YOUR subscription and YOUR
# agents, so the service must run as you. Same reasoning as launchd/install.sh, which already
# refuses. This one is reachable directly (the README points at it for reinstalling just the
# service), so the guard in the top-level install.sh does not cover it.
if [ "$(id -u)" = "0" ]; then
  echo "Do not run this with sudo — Otto is a per-user systemd service (it runs \`claude -p\`" >&2
  echo "against your subscription and ~/.claude). Re-run as yourself:" >&2
  echo "  systemd/install.sh" >&2
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

mkdir -p "$UNIT_DIR"
sed -e "s#__OTTO_DIR__#${DIR}#g" -e "s#__PATH__#${PATH_LINE}#g" \
  "$TEMPLATE" > "$UNIT_DIR/otto.service"
systemctl --user daemon-reload

cat <<EOF
Installed: $UNIT_DIR/otto.service  (from $DIR)

Next steps:
  loginctl enable-linger "$USER"        # keep it running after logout / headless boot
  systemctl --user enable --now otto  # start now + on every login
  systemctl --user status otto        # check it
  journalctl --user -u otto -f        # follow logs (web server stdout + worker/temporal echoes)

Stop / disable:
  systemctl --user stop otto
  systemctl --user disable otto
EOF
