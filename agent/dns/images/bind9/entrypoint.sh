#!/bin/sh
# Container entrypoint — delegates to the Python supervisor. tini is PID 1.
set -eu

: "${CONTROL_PLANE_URL:?CONTROL_PLANE_URL is required}"
: "${DNS_AGENT_KEY:?DNS_AGENT_KEY is required}"

# Ensure state dir is writable by the agent user
mkdir -p /var/lib/spatium-dns-agent /var/cache/bind
chown -R spatium:spatium /var/lib/spatium-dns-agent || true

# Agent-controlled rndc credentials. The alpine bind package's
# /etc/bind/rndc.key lives in a directory `spatium` can't traverse,
# and BIND9's auto-generated controls channel uses an in-memory key
# that doesn't match the on-disk file (causing "bad auth" failures
# when rndc tries to authenticate). Solve both by generating our own
# rndc keypair under the agent's state dir on first boot — the agent's
# named.conf renderer adds a matching `controls { }` block keyed off
# the same name, so BIND9 and rndc agree by construction.
if [ ! -f /var/lib/spatium-dns-agent/rndc.key ]; then
    rndc-confgen -a -k spatium-rndc -A hmac-sha256 \
        -c /var/lib/spatium-dns-agent/rndc.key 2>/dev/null || true
fi
if [ -f /var/lib/spatium-dns-agent/rndc.key ]; then
    cat > /var/lib/spatium-dns-agent/rndc.conf <<'EOF'
options {
    default-server 127.0.0.1;
    default-key    "spatium-rndc";
};
include "/var/lib/spatium-dns-agent/rndc.key";
EOF
    chown spatium:spatium \
        /var/lib/spatium-dns-agent/rndc.key \
        /var/lib/spatium-dns-agent/rndc.conf || true
    chmod 600 \
        /var/lib/spatium-dns-agent/rndc.key \
        /var/lib/spatium-dns-agent/rndc.conf || true
fi

# Supervise: agent process manages named lifecycle
exec su-exec spatium:spatium /usr/local/bin/spatium-dns-agent
