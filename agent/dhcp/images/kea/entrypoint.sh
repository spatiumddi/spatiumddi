#!/bin/sh
# Container entrypoint — starts kea-dhcp4, kea-dhcp6, kea-ctrl-agent,
# and the spatium-dhcp-agent. tini (PID 1) reaps zombies and forwards
# signals.
#
# kea-dhcp6 runs always-on alongside kea-dhcp4 (dual-stack): it boots
# from a minimal idle config (``interfaces: []`` + empty ``subnet6``)
# that binds nothing, so it is safe on hosts with no IPv6. Once the
# control plane ships a v6 scope, the agent's sync loop rewrites
# kea-dhcp6.conf and reloads the v6 control socket — the daemon is
# already running and just picks up the new subnets.
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
: "${SPATIUM_AGENT_KEY:?SPATIUM_AGENT_KEY is required (issue #246: pairing-code exchange was removed in #170 Wave A3 — paste the long hex key directly; Application appliances receive it via the supervisor's role-compose.env automatically)}"

# Ensure state + runtime dirs are writable by the agent user.
mkdir -p /var/lib/spatium-dhcp-agent /var/lib/kea /run/kea /var/log/kea
chown -R spatium:spatium /var/lib/spatium-dhcp-agent /var/lib/kea /run/kea /var/log/kea || true
# Kea 2.6+ requires socket parent dir to be exactly mode 0750.
chmod 0750 /run/kea

KEA_CFG="${KEA_CONFIG_PATH:-/etc/kea/kea-dhcp4.conf}"
KEA_CFG6="${KEA_CONFIG_PATH_V6:-/etc/kea/kea-dhcp6.conf}"
KEA_PID_FILE="/run/kea/kea-dhcp4.kea-dhcp4.pid"
KEA6_PID_FILE="/run/kea/kea-dhcp6.kea-dhcp6.pid"
CTRL_PID_FILE="/run/kea/kea-ctrl-agent.kea-ctrl-agent.pid"

# Top-level cleanup — scrub any leftover PID files from a prior
# container incarnation BEFORE either supervisor starts. Belt and
# suspenders: the per-iteration rm below catches in-container
# crashes, this handles the docker-compose-restart case where the
# entire container came back up with the tmpfs state intact.
rm -f "$KEA_PID_FILE" "$KEA6_PID_FILE" "$CTRL_PID_FILE" 2>/dev/null || true

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

supervise_kea6() {
    STOPPING=0
    KEA6_CHILD=
    # shellcheck disable=SC2064
    trap 'STOPPING=1; [ -n "$KEA6_CHILD" ] && kill -TERM "$KEA6_CHILD" 2>/dev/null; exit 0' TERM INT
    fails=0
    while [ "$STOPPING" -eq 0 ]; do
        rm -f "$KEA6_PID_FILE" 2>/dev/null || true
        start_ts=$(date +%s)
        su-exec spatium:spatium kea-dhcp6 -c "$KEA_CFG6" &
        KEA6_CHILD=$!
        wait "$KEA6_CHILD" || true
        code=$?
        KEA6_CHILD=
        [ "$STOPPING" -eq 1 ] && break
        end_ts=$(date +%s)
        runtime=$((end_ts - start_ts))
        if [ "$runtime" -ge 30 ]; then
            fails=0
        else
            fails=$((fails + 1))
        fi
        if [ "$fails" -ge 5 ]; then
            echo "kea-dhcp6 crash-looping (5x in <30s), giving up code=$code" >&2
            return "$code"
        fi
        echo "kea-dhcp6 exited code=$code after ${runtime}s, restarting (attempt $fails/5)" >&2
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

# radvd (issue #524) — opt-in IPv6 Router Advertisement daemon. Only
# runs when RADVD_MANAGED=1. The python agent renders the control-plane's
# radvd.conf to $RADVD_CONFIG_PATH on each bundle apply; this loop waits
# for that file to appear (radvd refuses to boot without an interface
# stanza), then runs radvd in the foreground with a pidfile the agent
# SIGHUPs to reload on later config changes. radvd carries the
# net_raw+net_admin file caps so it works after the su-exec privilege drop.
supervise_radvd() {
    STOPPING=0
    RADVD_CHILD=
    # shellcheck disable=SC2064
    trap 'STOPPING=1; [ -n "$RADVD_CHILD" ] && kill -TERM "$RADVD_CHILD" 2>/dev/null; exit 0' TERM INT
    RADVD_CFG="${RADVD_CONFIG_PATH:-/etc/radvd/radvd.conf}"
    RADVD_PID="${RADVD_PIDFILE:-/run/radvd/radvd.pid}"
    while [ "$STOPPING" -eq 0 ]; do
        if [ ! -s "$RADVD_CFG" ]; then
            sleep 3
            continue
        fi
        rm -f "$RADVD_PID" 2>/dev/null || true
        su-exec spatium:spatium radvd -C "$RADVD_CFG" -p "$RADVD_PID" -n &
        RADVD_CHILD=$!
        wait "$RADVD_CHILD" || true
        RADVD_CHILD=
        [ "$STOPPING" -eq 1 ] && break
        echo "radvd exited, restarting in 3s" >&2
        sleep 3
    done
    return 0
}

supervise_kea &
KEA_PID=$!

supervise_kea6 &
KEA6_PID=$!

supervise_ctrl_agent &
CTRL_PID=$!

# radvd only when opted in — best-effort, not part of the container
# liveness wait (a radvd flap must not take the DHCP server down).
RADVD_PID=
if [ "${RADVD_MANAGED:-0}" = "1" ]; then
    supervise_radvd &
    RADVD_PID=$!
fi

# Forward container SIGTERM to all supervisor subshells and the
# agent. The supervisors' own traps handle the in-flight daemon.
_term() {
    kill -TERM "$KEA_PID" "$KEA6_PID" "$CTRL_PID" "${RADVD_PID:-0}" "${AGENT_PID:-0}" 2>/dev/null || true
}
trap _term TERM INT

# The python agent runs the opt-in scapy rogue-DHCP probe (#370) +
# passive fingerprint sniffer, which need CAP_NET_RAW to open
# AF_PACKET sockets. ``su-exec``'s setuid clears the container's
# NET_RAW on the 0→non-root privilege drop, and a python interpreter
# can't carry a file capability the way kea-dhcp4/6 do — so use
# ``setpriv`` to raise NET_RAW into the *ambient* set, which survives
# setuid + execve into the unprivileged ``spatium`` user (#383).
setpriv --reuid spatium --regid spatium --init-groups \
        --inh-caps +net_raw --ambient-caps +net_raw \
        spatium-dhcp-agent &
AGENT_PID=$!

# wait -n is a bash-ism; busybox ash accepts it too as of 1.30+
# (Alpine 3.11+). Fall back to plain wait on older variants.
wait -n "$KEA_PID" "$KEA6_PID" "$CTRL_PID" "$AGENT_PID" 2>/dev/null \
    || wait "$KEA_PID" "$KEA6_PID" "$CTRL_PID" "$AGENT_PID"
EXIT_CODE=$?
_term
wait
exit "$EXIT_CODE"
