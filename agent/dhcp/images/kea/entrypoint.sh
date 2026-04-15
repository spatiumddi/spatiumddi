#!/bin/sh
# Container entrypoint — starts kea-dhcp4, kea-ctrl-agent, and the spatium-dhcp-agent.
# tini (PID 1) reaps zombies and forwards signals.
set -eu

: "${SPATIUM_API_URL:?SPATIUM_API_URL is required}"
: "${SPATIUM_AGENT_KEY:?SPATIUM_AGENT_KEY is required}"

# Ensure state + runtime dirs are writable by the agent user.
mkdir -p /var/lib/spatium-dhcp-agent /var/lib/kea /run/kea /var/log/kea
chown -R spatium:spatium /var/lib/spatium-dhcp-agent /var/lib/kea /run/kea /var/log/kea || true

# Start kea-dhcp4 in the background. It will pick up config at KEA_CONFIG_PATH;
# the first sync from the control plane overwrites that file and the agent
# issues a control-socket `config-reload`.
su-exec spatium:spatium kea-dhcp4 -c "${KEA_CONFIG_PATH:-/etc/kea/kea-dhcp4.conf}" &
KEA_PID=$!

# Start the Kea control agent so operators can poke at the REST API on :8000.
su-exec spatium:spatium kea-ctrl-agent -c /etc/kea/kea-ctrl-agent.conf &
CTRL_PID=$!

# Forward SIGTERM/SIGINT to the children and the agent.
_term() {
    kill -TERM "$KEA_PID" "$CTRL_PID" "$AGENT_PID" 2>/dev/null || true
}
trap _term TERM INT

# Run the Python agent in the foreground — if it exits, the whole container exits.
su-exec spatium:spatium spatium-dhcp-agent &
AGENT_PID=$!

wait -n "$KEA_PID" "$CTRL_PID" "$AGENT_PID"
EXIT_CODE=$?
_term
wait
exit "$EXIT_CODE"
