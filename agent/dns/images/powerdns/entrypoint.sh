#!/bin/sh
set -eu
: "${CONTROL_PLANE_URL:?CONTROL_PLANE_URL is required}"
: "${DNS_AGENT_KEY:?DNS_AGENT_KEY is required}"
: "${PDNS_FLAVOR:=auth}"
export PDNS_FLAVOR

mkdir -p /var/lib/spatium-dns-agent
chown -R spatium:spatium /var/lib/spatium-dns-agent || true
exec su-exec spatium:spatium /usr/local/bin/spatium-dns-agent
