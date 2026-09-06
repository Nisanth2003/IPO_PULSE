#!/usr/bin/env bash
# Start IPO Pulse on Linux — the VM, WSL, or a dev box.
#
#   ./run.sh              docker compose up -d, then wait for health
#   ./run.sh --logs       ...and follow the logs
#   ./run.sh --native     no Docker: python -m ipopulse.cli serve
#   ./run.sh --stop       stop and remove the container
#   ./run.sh --check      preflight only, start nothing
#
# ONE SERVER, NOT TWO. `serve` hands out the static studio and the /api
# routes from the same port, so there is no separate frontend process to
# start. Anything claiming to start "the frontend" separately would just be
# starting this again on another port.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${IPOPULSE_PORT:-8000}"
MODE=up
FOLLOW=0

for arg in "$@"; do
  case "$arg" in
    --logs)   FOLLOW=1 ;;
    --native) MODE=native ;;
    --stop)   MODE=stop ;;
    --check)  MODE=check ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

ok=1
say() { printf '  %-3s%-22s%s\n' "$1" "$2" "${3:-}"; }

echo
echo "IPO Pulse — preflight"
echo

# ── .env ──────────────────────────────────────────────────────────────────
# The CLI loads this itself, and compose passes it to the container, so it is
# the single place secrets live. Missing, everything downstream fails in a way
# that reads like a network problem.
if [[ -f "$REPO/.env" ]]; then
  say ok ".env" "present"
  has() { grep -Eq "^[[:space:]]*$1[[:space:]]*=[[:space:]]*[^[:space:]]" "$REPO/.env"; }

  has GOOGLE_SHEETS_ID  && say ok "sheet id" "set" \
                        || { say "!!" "sheet id" "GOOGLE_SHEETS_ID empty — the store IS the sheet"; ok=0; }
  has GOOGLE_SHEETS_KEY && say ok "sheet key" "set" \
                        || say "??" "sheet key" "empty — reads/writes will fail"

  # In a container the server always binds 0.0.0.0, so the trigger panel — a
  # job runner — is reachable by anything that can reach the port. The
  # password is the only thing in front of it, and that makes it mandatory
  # here rather than merely advisable as it is on localhost.
  if has IPOPULSE_TRIGGER_PASSWORD; then
    say ok "trigger password" "set"
  elif [[ "$MODE" == "up" ]]; then
    say "!!" "trigger password" "unset — a container binds 0.0.0.0 and this is the only gate"
    ok=0
  else
    say "??" "trigger password" "unset — trigger panel disabled"
  fi
else
  say "!!" ".env" "missing — cp .env.example .env and fill it in"
  ok=0
fi

# ── the runtime ───────────────────────────────────────────────────────────
if [[ "$MODE" == "native" ]]; then
  command -v python3 >/dev/null && say ok "python3" "$(python3 -V 2>&1)" \
                                || { say "!!" "python3" "not found"; ok=0; }
else
  command -v docker >/dev/null && say ok "docker" "$(docker --version)" \
                               || { say "!!" "docker" "not found"; ok=0; }
  docker compose version >/dev/null 2>&1 && say ok "compose" "v2 plugin" \
                                         || { say "!!" "compose" "docker compose v2 not available"; ok=0; }
fi

(( ok )) || { echo; echo "Not starting. Fix the !! lines above."; echo; exit 1; }

case "$MODE" in
  check) echo; echo "Preflight only — nothing started."; echo; exit 0 ;;
  stop)  docker compose -f "$REPO/docker-compose.yml" down; exit 0 ;;
  native)
    cd "$REPO/backend"
    exec python3 -m ipopulse.cli serve --host 0.0.0.0 --port "$PORT"
    ;;
esac

# ── up ────────────────────────────────────────────────────────────────────
echo
docker compose -f "$REPO/docker-compose.yml" up -d --build

# Wait for health rather than declaring success on `up` returning 0 — up only
# means the container started, and a container that exits two seconds later
# still gave you a zero here.
echo
printf 'waiting for /api/health '
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
    echo
    echo "healthy -> http://localhost:$PORT"
    (( FOLLOW )) && docker compose -f "$REPO/docker-compose.yml" logs -f
    exit 0
  fi
  printf '.'
  sleep 2
done

echo
echo "never became healthy in 60s. Last 50 lines:"
docker compose -f "$REPO/docker-compose.yml" logs --tail 50
exit 1
