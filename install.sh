#!/usr/bin/env bash
# Fully unattended installer for Otto. Idempotent — safe to re-run any time (e.g.
# after `git pull`, or to recover from the venv-breaks-on-python-upgrade gotcha).
#
# Does, in order:
#   1. Checks hard prerequisites (python3, claude CLI) — dies with instructions if missing.
#   2. Creates/upgrades .venv and installs requirements.txt.
#   3. Installs the Temporal CLI (~/.temporalio/bin) if not already present.
#   4. Seeds .env from .env.example if no .env exists yet (never overwrites one).
#   5. Runs the stdlib test suite as a smoke check (no network / no Claude calls).
#   6. Installs a background service so Otto runs unattended: on Linux (with a
#      systemd --user session) the otto.service user unit + lingering; on macOS a
#      launchd LaunchAgent (~/Library/LaunchAgents). Both run as the user (never root)
#      and wrap run.sh. Other OSes fall back to manual `./run.sh`.
#
# Flags:
#   --no-service   skip step 6 (just leaves the stack ready to start with ./run.sh)
#   --no-tests     skip step 5
#   --guided       after installing, walk the doctor's gaps and offer to fix each one
#                  (setup_wizard.py). No-op without a TTY, so piping the installer is safe.
#   -h, --help     show this help
#
# Nothing here needs root except the two OPTIONAL fallbacks: installing the
# `python3-venv` OS package if it's missing, and `loginctl enable-linger` if the
# current user isn't allowed to set that on themselves. Both are attempted via
# sudo only when needed, and the script prints the manual command instead of
# hanging if sudo isn't available non-interactively.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

NO_SERVICE=0
NO_TESTS=0
GUIDED=0
for arg in "$@"; do
  case "$arg" in
    --no-service) NO_SERVICE=1 ;;
    --no-tests) NO_TESTS=1 ;;
    --guided) GUIDED=1 ;;
    -h|--help)
      sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $arg (see --help)" >&2
      exit 1
      ;;
  esac
done

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# --- 1. hard prerequisites -------------------------------------------------

command -v python3 >/dev/null 2>&1 || die "python3 not found — install Python 3.9+ first."

if ! command -v claude >/dev/null 2>&1; then
  die "claude CLI not found on PATH. Otto runs your existing Claude Code" \
      $'agents/skills via `claude -p` on your Claude subscription — it needs Claude Code' \
      $'installed and logged in first:\n' \
      $'    npm install -g @anthropic-ai/claude-code\n' \
      $'    claude   # first run walks you through login\n' \
      "Then re-run this script."
fi
log "claude CLI: found ($(claude --version 2>/dev/null | head -1))"

for tool in git curl; do
  command -v "$tool" >/dev/null 2>&1 || log "warning: '$tool' not found — some Otto features (repo workspaces, Temporal CLI install) need it."
done
if ! command -v gh >/dev/null 2>&1; then
  log "warning: 'gh' not found — the GitHub board queue and repo-mode PR creation need it. Install later: https://cli.github.com"
fi

# --- 2. venv + deps ----------------------------------------------------------

log "python venv: creating/upgrading .venv"
if ! python3 -m venv --upgrade-deps .venv 2>/tmp/otto-venv.err; then
  if grep -qi "ensurepip is not available\|No module named venv" /tmp/otto-venv.err && command -v apt-get >/dev/null 2>&1; then
    log "system is missing the venv module — installing python3-venv via apt (sudo)…"
    sudo apt-get update -y
    sudo apt-get install -y python3-venv
    python3 -m venv --upgrade-deps .venv
  else
    cat /tmp/otto-venv.err >&2
    die "failed to create .venv (see error above)"
  fi
fi
rm -f /tmp/otto-venv.err

PY="$DIR/.venv/bin/python"
log "installing requirements.txt"
"$PY" -m pip install --quiet -r requirements.txt
"$PY" -c "import temporalio" || die ".venv is missing temporalio after install — check the pip output above"
log "venv ready: $("$PY" --version)"

# --- 3. Temporal CLI ---------------------------------------------------------

TEMPORAL_BIN="$HOME/.temporalio/bin/temporal"
# Pin the CLI so an unpinned always-latest install can't drift onto a breaking release.
# This is the CLI's OWN version scheme — unrelated to the temporalio SDK pin in
# requirements.txt. The two are bumped INDEPENDENTLY; copying one number onto the other
# is what left the SDK pinned at the CLI's 1.8.0 while every run used 1.30.0 (PR #302).
TEMPORAL_CLI_VERSION="1.8.0"
if [ -x "$TEMPORAL_BIN" ]; then
  log "temporal CLI: already installed ($("$TEMPORAL_BIN" --version 2>/dev/null | head -1))"
else
  command -v curl >/dev/null 2>&1 || die "curl is required to install the Temporal CLI (or install it yourself: https://temporal.download)"
  log "temporal CLI: installing v$TEMPORAL_CLI_VERSION to ~/.temporalio/bin"
  curl -sSf https://temporal.download/cli.sh | sh -s -- --version "$TEMPORAL_CLI_VERSION"
  [ -x "$TEMPORAL_BIN" ] || die "Temporal CLI install did not produce $TEMPORAL_BIN"
fi

# --- 4. .env ------------------------------------------------------------------

if [ -f .env ]; then
  log ".env: already exists, leaving untouched"
else
  cp .env.example .env
  log ".env: created from .env.example (all optional — edit later to enable the event ingress, push notifications, etc.)"
fi
# .env holds an xoxp- Slack token that reads and posts as this user, the ingress HMAC key and the
# ntfy topic. cp inherits the umask, which on most distros leaves it world-readable.
chmod 600 .env

mkdir -p data

# --- 5. smoke test ------------------------------------------------------------

if [ "$NO_TESTS" = "1" ]; then
  log "test suite: skipped (--no-tests)"
else
  log "running test suite (stdlib only, no network / no Claude calls)…"
  if ! "$PY" -m unittest >/tmp/otto-tests.log 2>&1; then
    tail -40 /tmp/otto-tests.log >&2
    die "test suite failed — see output above. Installation is otherwise complete; fix the failure and re-run."
  fi
  tail -5 /tmp/otto-tests.log
  rm -f /tmp/otto-tests.log
  log "tests: passing"
fi

# --- 6. background service (systemd --user) -----------------------------------

setup_service_macos() {
  log "background service: installing launchd LaunchAgent"
  bash "$DIR/launchd/install.sh"
  sleep 2
  launchctl print "gui/$(id -u)/com.otto" 2>/dev/null | grep -E "state = |last exit code = " || true
}

setup_service() {
  case "$(uname -s)" in
    Linux) ;;                          # handled below
    Darwin) setup_service_macos; return ;;
    *)
      log "background service: skipped (no systemd/launchd on this OS). Start manually with ./run.sh"
      return ;;
  esac
  if ! command -v systemctl >/dev/null 2>&1; then
    log "background service: skipped (no systemctl on this system). Start manually with ./run.sh"
    return
  fi
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    log "background service: skipped (no systemd --user session available in this shell)."
    log "  Once you're in a normal login session, run: systemd/install.sh"
    return
  fi

  log "background service: installing systemd --user unit"
  bash "$DIR/systemd/install.sh"

  if loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
    log "linger: already enabled for $USER"
  else
    log "enabling linger for $USER (keeps Otto running after logout / at boot)…"
    if ! loginctl enable-linger "$USER" 2>/dev/null; then
      if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        sudo loginctl enable-linger "$USER"
      else
        log "  could not enable linger automatically — run: sudo loginctl enable-linger $USER"
      fi
    fi
  fi

  log "starting otto service…"
  systemctl --user enable --now otto
  sleep 2
  systemctl --user --no-pager --full status otto || true
}

if [ "$NO_SERVICE" = "1" ]; then
  log "background service: skipped (--no-service)"
else
  setup_service
fi

# --- summary --------------------------------------------------------------

# `|| true` is load-bearing: `set -o pipefail` is on, and a .env seeded from .env.example has
# no PORT line at all — so a plain grep miss returned 1 and `set -e` aborted the installer HERE,
# after the tests passed but before the summary, the next steps and the doctor. Every fresh
# install hit it; an install whose .env already set PORT never did.
PORT="$( (grep -E '^PORT=' .env 2>/dev/null || true) | tail -1 | cut -d= -f2)"
PORT="${PORT:-8765}"

cat <<EOF

Otto install complete.

  Web UI:      http://localhost:${PORT}
  Temporal UI: http://localhost:8233

EOF
svc_running=0
if [ "$NO_SERVICE" != "1" ]; then
  case "$(uname -s)" in
    Linux)  systemctl --user is-active --quiet otto 2>/dev/null && svc_running=1 ;;
    Darwin) launchctl print "gui/$(id -u)/com.otto" >/dev/null 2>&1 && svc_running=1 ;;
  esac
fi

if [ "$svc_running" = "0" ]; then
  cat <<EOF
Start it:
  ./run.sh

EOF
elif [ "$(uname -s)" = "Darwin" ]; then
  cat <<EOF
Running as a launchd LaunchAgent:
  launchctl print gui/$(id -u)/com.otto   # check it
  tail -f data/service.log                  # follow logs
  launchctl bootout gui/$(id -u)/com.otto # stop + stop starting at login

EOF
else
  cat <<EOF
Running as a systemd --user service:
  systemctl --user status otto     # check it
  journalctl --user -u otto -f     # follow logs
  systemctl --user stop otto       # stop
  systemctl --user disable otto    # stop starting on login

EOF
fi
if [ "$GUIDED" = "1" ]; then
  # No-ops without a TTY, so `curl … | bash -s -- --guided` still completes unattended.
  "$PY" setup_wizard.py || true
else
  cat <<EOF
Optional next steps:
  - ./.venv/bin/python setup_wizard.py — walk the gaps below and fix each one interactively.
  - Edit .env to enable the event/webhook ingress, push notifications, etc.
  - Register project repos and configure the GitHub board queue from the Admin tab.

EOF
fi

cat <<EOF
Environment doctor (what's silently degraded on this install):
EOF
# Informational only — a fresh install legitimately has warnings (empty catalogue, no repos);
# never fail the install over them.
"$PY" doctor.py || true
