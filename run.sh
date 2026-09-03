#!/usr/bin/env bash
# Start Otto with Temporal as the default execution layer.
# Brings up (and cleans up on exit): the Temporal dev server (if not already running),
# the worker, and the web server. Open the URL the web server prints.
set -euo pipefail
cd "$(dirname "$0")"

# Local config: load a gitignored .env if present (e.g. OTTO_EVENT_SECRET for the event
# ingress). `set -a` exports every assignment so the worker + server inherit them.
if [ -f .env ]; then
  echo ".env: loading local environment"
  set -a; . ./.env; set +a
fi

PY="./.venv/bin/python"
TCLI="${HOME}/.temporalio/bin/temporal"

if [ ! -x "$PY" ]; then
  echo "No .venv — run: python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

pids=()
cleanup(){ for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
# A signal stop (systemctl stop / launchctl bootout) is a CLEAN exit: launchd's
# KeepAlive SuccessfulExit=false must see 0 or it treats an operator stop as a crash.
trap 'cleanup; exit 0' INT TERM

# 1) Temporal dev server — skip if 7233 is already serving.
if "$PY" - <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", 7233)) == 0 else 1)
PY
then
  echo "temporal dev server: already running"
else
  echo "temporal dev server: starting (UI -> http://localhost:8233)…"
  # --db-filename persists schedules + workflow history to disk; without it the dev
  # server is in-memory and everything is wiped on restart.
  # journal_mode=WAL is NOT optional at this db's size: in the default rollback-journal
  # mode a single writer blocks every reader, so under normal load (a reaper sweep, two
  # pollers, a schedule describe) the history shard cannot renew its range lease. It
  # drops, every client retries harder, and the retries keep the db busy — a livelock
  # that does not recover on its own. It surfaces as `shard status unknown`: visibility
  # (workflow list, cluster health) keeps answering while EVERY history op — submit,
  # describe, schedule describe — times out, so the stack looks up while nothing runs.
  mkdir -p data
  "$TCLI" server start-dev --db-filename "$(pwd)/data/temporal.db" \
    --sqlite-pragma journal_mode=WAL >/tmp/otto-temporal.log 2>&1 &
  pids+=($!)
  sleep 3
fi

# 2) worker
echo "worker: starting (log -> /tmp/otto-worker.log)…"
"$PY" worker.py >/tmp/otto-worker.log 2>&1 &
pids+=($!)
sleep 1

# 3) web server — backgrounded (not exec'd/foregrounded) so an incoming SIGTERM is
# serviced immediately instead of waiting for this to exit first; a plain foreground
# child defers the cleanup trap until it exits on its own, so a signal to run.sh
# (e.g. from launchd) never reaches it and the whole tree hangs.
echo "web server: starting…"
"$PY" server.py &
pids+=($!)

# Supervise: ANY child dying is fatal to the whole stack. A bare `wait` here blocked on
# ALL children, so a worker-only (or server-only) death left the survivors keeping the
# service "alive" — systemd/launchd never restarted, the UI kept accepting work, and no
# workflow progressed: a silent failure the in-worker terminal-state guarantee cannot
# cover, because it IS the worker. Exit non-zero so Restart=on-failure / KeepAlive
# SuccessfulExit=false relaunch the stack (Temporal workflows resume from history).
# No `wait -n` (macOS bash is 3.2); the sleep is backgrounded + waited so a SIGTERM is
# serviced immediately (same reason server.py is backgrounded above).
while :; do
  for p in "${pids[@]}"; do
    if ! kill -0 "$p" 2>/dev/null; then
      echo "run.sh: child $p exited — stopping the stack so the service manager restarts it" >&2
      exit 1
    fi
  done
  sleep 5 & wait $! || true
done
