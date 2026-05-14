#!/bin/sh
# Container entrypoint — delegates to the Python supervisor.
# tini is PID 1 (provided by ENTRYPOINT in the Dockerfile).
set -eu

# Phase A1 pre-checks intentionally minimal — the supervisor doesn't
# talk to a control plane yet. Wave A2 will tighten this to require
# at least one of (CONTROL_PLANE_URL+BOOTSTRAP_PAIRING_CODE) once the
# register path lands.

mkdir -p /var/lib/spatium-supervisor
chown -R spatium:spatium /var/lib/spatium-supervisor || true

exec su-exec spatium:spatium /usr/local/bin/spatium-supervisor
