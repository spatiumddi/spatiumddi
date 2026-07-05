#!/bin/sh
# Container entrypoint — delegates to the Python supervisor, which owns
# the gobgpd subprocess directly (mirrors
# agent/dns/images/bind9/entrypoint.sh — a single managed daemon, unlike
# Kea's three-daemon shell supervise loop). tini is PID 1.
set -eu

: "${CONTROL_PLANE_URL:?CONTROL_PLANE_URL is required}"
: "${LG_AGENT_KEY:?LG_AGENT_KEY is required. Pairing-code exchange was removed in #170 Wave A3 -- paste the long hex key directly. Application appliances receive it via the supervisor role-compose.env automatically.}"

# Ensure state + config dirs are writable by the agent user. named/kea's
# equivalent images do the same dance -- the image bakes an initial
# (root-owned, from COPY) idle gobgpd.json, but spatium_lg_agent needs to
# be able to overwrite it in place once it has a real peer-config bundle.
mkdir -p /var/lib/spatium-lg-agent /etc/gobgp
chown -R spatium:spatium /var/lib/spatium-lg-agent /etc/gobgp || true

# Supervise: agent process manages gobgpd lifecycle (spawn, SIGHUP
# reload, dead-process detection -> exit(2) -> container restart).
exec su-exec spatium:spatium /usr/local/bin/spatium-lg-agent
