#!/usr/bin/env bash
# Install the IPO Pulse timers on a Linux host.
#
#   ./install.sh                 # system-wide, needs sudo
#   ./install.sh --user          # per-user, no root, but only runs while
#                                # you are logged in unless lingering is on
#
# Idempotent: re-run it after changing a .timer file and it reloads cleanly.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
USER_MODE=0
[[ "${1:-}" == "--user" ]] && USER_MODE=1

# Prefer the repo's venv; fall back to whatever python3 is on PATH.
if [[ -x "$REPO/.venv/bin/python" ]]; then
  PYTHON="$REPO/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

echo "repo   : $REPO"
echo "python : $PYTHON"

# Fail early rather than after installing six units that cannot work.
if ! "$PYTHON" -c 'import ipopulse' 2>/dev/null; then
  ( cd "$REPO/backend" && "$PYTHON" -c 'import ipopulse' ) >/dev/null 2>&1 || {
    echo "error: $PYTHON cannot import ipopulse." >&2
    echo "       pip install -r $REPO/backend/requirements.txt" >&2
    exit 1
  }
fi
if [[ ! -f "$REPO/.env" ]]; then
  echo "warning: no $REPO/.env — jobs needing a key or the sheet will fail." >&2
fi

if (( USER_MODE )); then
  UNIT_DIR="$HOME/.config/systemd/user"
  SYSTEMCTL=(systemctl --user)
  RUN_AS="$USER"
else
  UNIT_DIR="/etc/systemd/system"
  SYSTEMCTL=(sudo systemctl)
  RUN_AS="${SUDO_USER:-$USER}"
fi
mkdir -p "$UNIT_DIR"

install_unit() {
  local src="$1" dst="$UNIT_DIR/$(basename "$1")"
  sed -e "s|__REPO__|$REPO|g" \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__USER__|$RUN_AS|g" "$src" \
    | if (( USER_MODE )); then grep -v '^User=' ; else cat ; fi \
    | ( if (( USER_MODE )); then sudo() { "$@"; }; fi
        if [[ $UNIT_DIR == /etc/* ]]; then sudo tee "$dst" >/dev/null; else cat > "$dst"; fi )
  echo "  installed $(basename "$dst")"
}

cd "$(dirname "${BASH_SOURCE[0]}")"
install_unit ipopulse@.service
for t in ipopulse-*.timer; do install_unit "$t"; done

"${SYSTEMCTL[@]}" daemon-reload
for t in ipopulse-*.timer; do
  "${SYSTEMCTL[@]}" enable --now "$t"
done

echo
"${SYSTEMCTL[@]}" list-timers 'ipopulse-*' --no-pager || true
echo
echo "Run one now :  ${SYSTEMCTL[*]} start ipopulse@sync"
echo "Watch it    :  journalctl ${USER_MODE:+--user }-u 'ipopulse@*' -f"
