#!/bin/sh
# Container entrypoint — delegates to the Python supervisor. tini is PID 1.
set -eu

: "${CONTROL_PLANE_URL:?CONTROL_PLANE_URL is required}"
: "${DNS_AGENT_KEY:?DNS_AGENT_KEY is required}"

# Ensure state dir is writable by the agent user
mkdir -p /var/lib/spatium-dns-agent /var/cache/bind
chown -R spatium:spatium /var/lib/spatium-dns-agent || true

# Supervise: agent process manages named lifecycle
exec su-exec spatium:spatium /usr/local/bin/spatium-dns-agent
