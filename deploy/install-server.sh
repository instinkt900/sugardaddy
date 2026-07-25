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

echo
echo "Done. Check it:"
echo "  docker compose logs -f"
echo "  curl -s http://localhost:8080/healthz"
echo "  phone UI:   http://<host>:8080/"
echo "  desktop UI: http://<host>:8080/desktop"
echo
echo "Credentials changed? Reload env with:  docker compose up -d --force-recreate"
