#!/bin/sh
# Container entrypoint — delegates to the Python supervisor. tini is PID 1.
set -eu

: "${CONTROL_PLANE_URL:?CONTROL_PLANE_URL is required}"
: "${DNS_AGENT_KEY:?DNS_AGENT_KEY is required}"

# Ensure state + LMDB dirs are writable by the agent user.
mkdir -p /var/lib/spatium-dns-agent /var/lib/powerdns
chown -R spatium:spatium /var/lib/spatium-dns-agent /var/lib/powerdns || true

# PowerDNS REST API key. Generate once and keep stable across
# container restarts so the agent and pdns_server stay in sync.
# 32 random url-safe bytes is well-padded for the API surface.
API_KEY_FILE=/var/lib/spatium-dns-agent/pdns-api.key
if [ ! -f "$API_KEY_FILE" ]; then
    head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | head -c 32 > "$API_KEY_FILE"
    chmod 600 "$API_KEY_FILE"
fi
chown spatium:spatium "$API_KEY_FILE" || true

# Initialise an empty LMDB database on first boot if it isn't there
# yet. ``pdnsutil create-zone`` would also do this lazily on first
# zone creation, but pre-creating means the daemon starts cleanly
# and reports "no zones" rather than "lmdb file missing".
if [ ! -f /var/lib/powerdns/pdns.lmdb ]; then
    su-exec spatium:spatium pdnsutil --config-dir=/var/lib/powerdns \
        create-bind-db /var/lib/powerdns/pdns.lmdb 2>/dev/null \
        || touch /var/lib/powerdns/pdns.lmdb
    chown spatium:spatium /var/lib/powerdns/pdns.lmdb || true
fi

# Supervise: agent process manages pdns_server lifecycle.
exec su-exec spatium:spatium /usr/local/bin/spatium-dns-agent
