#!/bin/sh
# Container entrypoint — starts kea-dhcp4, kea-ctrl-agent, and the
# spatium-dhcp-agent. tini (PID 1) reaps zombies and forwards signals.
#
# Each Kea daemon runs under a supervise-loop so a transient crash
# (bind race against Docker/k8s networking during restart, partner
# flap, etc.) doesn't leave the container in an agent-alive /
# Kea-dead zombie state. The supervise loop:
#
#   - scrubs any stale ``*.pid`` file before each launch, because Kea
#     only removes its own PID on GRACEFUL exit — SIGKILL or hard
#     crashes leave the PID file on the tmpfs, and ``createPIDFile``
#     refuses to start with ``DHCP4_ALREADY_RUNNING``;
#   - installs SIGTERM/SIGINT traps that forward to the in-flight
#     daemon AND flip a "stopping" flag so the outer loop doesn't
#     retry during container shutdown;
#   - crash-counts only fast exits (<30s uptime) so we don't
#     eventually give up on a long-running daemon that happens to
#     die after weeks of uptime.
set -eu

: "${SPATIUM_API_URL:?SPATIUM_API_URL is required}"
: "${SPATIUM_AGENT_KEY:?SPATIUM_AGENT_KEY is required}"

# Ensure state + runtime dirs are writable by the agent user.
mkdir -p /var/lib/spatium-dhcp-agent /var/lib/kea /run/kea /var/log/kea
chown -R spatium:spatium /var/lib/spatium-dhcp-agent /var/lib/kea /run/kea /var/log/kea || true
# Kea 2.6+ requires socket parent dir to be exactly mode 0750.
chmod 0750 /run/kea

KEA_CFG="${KEA_CONFIG_PATH:-/etc/kea/kea-dhcp4.conf}"
KEA_PID_FILE="/run/kea/kea-dhcp4.kea-dhcp4.pid"
CTRL_PID_FILE="/run/kea/kea-ctrl-agent.kea-ctrl-agent.pid"

# Top-level cleanup — scrub any leftover PID files from a prior
# container incarnation BEFORE either supervisor starts. Belt and
# suspenders: the per-iteration rm below catches in-container
# crashes, this handles the docker-compose-restart case where the
# entire container came back up with the tmpfs state intact.
rm -f "$KEA_PID_FILE" "$CTRL_PID_FILE" 2>/dev/null || true

supervise_kea() {
    STOPPING=0
    KEA_CHILD=
    # Forward SIGTERM to the live daemon AND flip the stop flag so
    # the outer loop doesn't try to restart during container
    # shutdown. ``exit 0`` here ensures the subshell goes away
    # cleanly so wait -n in the parent returns.
    # shellcheck disable=SC2064
    trap 'STOPPING=1; [ -n "$KEA_CHILD" ] && kill -TERM "$KEA_CHILD" 2>/dev/null; exit 0' TERM INT
    fails=0
    while [ "$STOPPING" -eq 0 ]; do
        rm -f "$KEA_PID_FILE" 2>/dev/null || true
        start_ts=$(date +%s)
        su-exec spatium:spatium kea-dhcp4 -c "$KEA_CFG" &
        KEA_CHILD=$!
        wait "$KEA_CHILD" || true
        code=$?
        KEA_CHILD=
        [ "$STOPPING" -eq 1 ] && break
        end_ts=$(date +%s)
        runtime=$((end_ts - start_ts))
        if [ "$runtime" -ge 30 ]; then
            fails=0
        else
            fails=$((fails + 1))
        fi
        if [ "$fails" -ge 5 ]; then
            echo "kea-dhcp4 crash-looping (5x in <30s), giving up code=$code" >&2
            return "$code"
        fi
        echo "kea-dhcp4 exited code=$code after ${runtime}s, restarting (attempt $fails/5)" >&2
        sleep 2
    done
    return 0
}

supervise_ctrl_agent() {
    STOPPING=0
    CTRL_CHILD=
    # shellcheck disable=SC2064
    trap 'STOPPING=1; [ -n "$CTRL_CHILD" ] && kill -TERM "$CTRL_CHILD" 2>/dev/null; exit 0' TERM INT
    fails=0
    while [ "$STOPPING" -eq 0 ]; do
        rm -f "$CTRL_PID_FILE" 2>/dev/null || true
        start_ts=$(date +%s)
        su-exec spatium:spatium kea-ctrl-agent -c /etc/kea/kea-ctrl-agent.conf &
        CTRL_CHILD=$!
        wait "$CTRL_CHILD" || true
        code=$?
        CTRL_CHILD=
        [ "$STOPPING" -eq 1 ] && break
        end_ts=$(date +%s)
        runtime=$((end_ts - start_ts))
        if [ "$runtime" -ge 30 ]; then
            fails=0
        else
            fails=$((fails + 1))
        fi
        if [ "$fails" -ge 5 ]; then
            echo "kea-ctrl-agent crash-looping (5x in <30s), giving up code=$code" >&2
            return "$code"
        fi
        echo "kea-ctrl-agent exited code=$code after ${runtime}s, restarting (attempt $fails/5)" >&2
        sleep 2
    done
    return 0
}

supervise_kea &
KEA_PID=$!

supervise_ctrl_agent &
CTRL_PID=$!

# Forward container SIGTERM to both supervisor subshells and the
# agent. The supervisors' own traps handle the in-flight daemon.
_term() {
    kill -TERM "$KEA_PID" "$CTRL_PID" "${AGENT_PID:-0}" 2>/dev/null || true
}
trap _term TERM INT

su-exec spatium:spatium spatium-dhcp-agent &
AGENT_PID=$!

# wait -n is a bash-ism; busybox ash accepts it too as of 1.30+
# (Alpine 3.11+). Fall back to plain wait on older variants.
wait -n "$KEA_PID" "$CTRL_PID" "$AGENT_PID" 2>/dev/null || wait "$KEA_PID" "$CTRL_PID" "$AGENT_PID"
EXIT_CODE=$?
_term
wait
exit "$EXIT_CODE"
