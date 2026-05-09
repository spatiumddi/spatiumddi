#!/usr/bin/env bash
# Runs every time the Codespace starts (including resume from
# stop). Idempotent: brings the stack back up if it was stopped.
# The first-ever start is handled by post-create.sh, which leaves
# the stack already running — this script is a no-op on that pass.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  # Bootstrap hasn't run yet — let post-create.sh own it.
  exit 0
fi

docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  up -d postgres redis api worker beat frontend
