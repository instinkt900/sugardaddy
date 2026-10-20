#!/usr/bin/env bash
# Run the checked-out branch as a local staging instance on this machine.
#
#   deploy/staging.sh start|stop|restart|status|logs
#
# Everything it reads lives in the gitignored .staging/ directory:
#   .staging/sugardaddy.db   a snapshot of the live DB (taken with sqlite3's
#                            backup API, so it is consistent mid-write)
#   .staging/config.toml     a config with NO [librelink] credentials, [notify]
#                            or [victoriametrics] — staging must never poll the
#                            LibreLinkUp account (two clients log each other
#                            out) or write anywhere but its own copy
#
# Runs from the repo's editable install, so a restart picks up code changes —
# no image to rebuild. A transient systemd user unit keeps it alive after the
# terminal closes.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT=sugardaddy-staging
CFG="$REPO/.staging/config.toml"

port() { sed -n 's/^port *= *\([0-9]*\).*/\1/p' "$CFG" | head -1; }

start() {
  [[ -f "$CFG" && -f "$REPO/.staging/sugardaddy.db" ]] || {
    echo "missing .staging/config.toml or .staging/sugardaddy.db" >&2; exit 1; }
  if grep -Eq '^\[(notify|victoriametrics)\]' "$CFG"; then
    echo "refusing: $CFG has a [notify] or [victoriametrics] section" >&2; exit 1
  fi
  # -u strips any credentials that happen to be in this shell's environment.
  systemd-run --user --unit="$UNIT" --collect --working-directory="$REPO" \
    env -u SUGARDADDY_LIBRE_EMAIL -u SUGARDADDY_LIBRE_PASSWORD -u SUGARDADDY_VAPID_PRIVATE_KEY \
    "$REPO/.venv/bin/sugardaddy" serve -c "$CFG"
  # Same rule as install-server.sh: a reply without a `readings` count is some
  # other service, not a passing check.
  for _ in $(seq 20); do
    if curl -fsS "localhost:$(port)/healthz" 2>/dev/null | grep -q '"readings"'; then
      echo "staging up: http://$(hostname -I | awk '{print $1}'):$(port)/  (desktop: /desktop)"
      return 0
    fi
    sleep 0.5
  done
  echo "staging did not answer /healthz on port $(port)" >&2
  journalctl --user -u "$UNIT" -n 20 --no-pager >&2 || true
  exit 1
}

stop() { systemctl --user stop "$UNIT" 2>/dev/null || true; }

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) systemctl --user status "$UNIT" --no-pager || true ;;
  logs) journalctl --user -u "$UNIT" -f ;;
  *) echo "usage: $0 start|stop|restart|status|logs" >&2; exit 2 ;;
esac
