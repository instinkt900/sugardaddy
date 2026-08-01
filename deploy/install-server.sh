#!/usr/bin/env bash
# Install/refresh sugardaddy on the serve host (Docker).
# Run ON the host from the repo root:  bash deploy/install-server.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The runtime config lives in the repo root (compose mounts ../config.toml).
if [[ ! -f "$REPO_DIR/config.toml" ]]; then
  echo "config.toml missing — copy and edit it:"
  echo "    cp config.example.toml config.toml"
  exit 1
fi
if [[ ! -f "$REPO_DIR/docker/.env" ]]; then
  echo "docker/.env missing — create it with your LibreLinkUp credentials:"
  echo "    cp docker/.env.example docker/.env   # then edit SUGARDADDY_LIBRE_EMAIL / _PASSWORD"
  exit 1
fi

echo "==> building and starting sugardaddy container"
cd "$REPO_DIR/docker"
docker compose up -d --build

# Ask compose where it actually published the port instead of assuming 8080.
# SUGARDADDY_PORT remaps it, and checking the wrong port is worse than not
# checking: another service on the host can answer, and a stray 200 reads as a
# healthy deploy. Fall back to the compose default only if compose can't say.
PORT="$(docker compose port sugardaddy 8080 2>/dev/null | sed 's/.*://' || true)"
PORT="${PORT:-${SUGARDADDY_PORT:-8080}}"

echo
echo "==> published on host port $PORT (the container always listens on 8080 inside)"
HEALTH="$(curl -fsS "http://localhost:$PORT/healthz" 2>/dev/null || true)"
if printf '%s' "$HEALTH" | grep -q '"readings"'; then
  # Matching the payload, not just a 200: that is what tells our app apart from
  # whatever else might be listening.
  echo "    healthz: $HEALTH"
elif [[ -n "$HEALTH" ]]; then
  echo "    WARNING: something answered on port $PORT, but it is not sugardaddy:"
  echo "      $HEALTH"
  echo "    Check SUGARDADDY_PORT in docker/.env — another service may hold that port."
else
  echo "    no answer yet — startup takes a few seconds. Check: docker compose logs -f"
fi

echo
echo "Done. Check it:"
echo "  docker compose logs -f"
echo "  curl -s http://localhost:$PORT/healthz"
echo "  phone UI:   http://<host>:$PORT/"
echo "  desktop UI: http://<host>:$PORT/desktop"
echo
echo "Credentials changed? Reload env with:  docker compose up -d --force-recreate"
