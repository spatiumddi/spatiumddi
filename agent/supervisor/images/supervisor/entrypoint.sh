#!/bin/sh
# Container entrypoint — delegates to the Python supervisor.
# tini is PID 1 (provided by ENTRYPOINT in the Dockerfile).
#
# Issue #183 Phase 7 simplification — the appliance is k3s-only; the
# pre-Phase-7 docker.sock gid-juggling is gone (no docker, no socket,
# no supplementary group dance). The supervisor runs as an in-cluster
# pod with a ServiceAccount + auto-mounted token; cap_net_admin for
# nftables comes from the Pod's securityContext.
#
# Two paths still need write access:
#
# * /var/lib/spatium-supervisor → identity key + cert + session token
#   + role-compose.env + role-switch-state. Chowned below.
# * /etc/nftables.d → spatium-role.nft drop-in (the only file the
#   supervisor manages there). Chown the *directory* only; existing
#   host-managed SNMP / NTP drop-ins keep their canonical ownership.
#
# Phase 7 also sources /etc/spatiumddi-host/.env so configuration the
# operator's spatium-install wizard wrote (CONTROL_PLANE_URL,
# BOOTSTRAP_PAIRING_CODE, AGENT_GROUP, …) lands in the supervisor's
# environment without a separate Secret / ConfigMap.

set -eu

mkdir -p /var/lib/spatium-supervisor
chown -R spatium:spatium /var/lib/spatium-supervisor || true

if [ -d /etc/nftables.d ]; then
    chown spatium:spatium /etc/nftables.d || true
    chmod 0775 /etc/nftables.d || true
fi

# Source the host's /etc/spatiumddi/.env (mounted at
# /etc/spatiumddi-host/.env in the pod via hostPath) so the
# supervisor's SupervisorConfig.from_env() picks up the operator's
# install-time choices. Best-effort — a fresh appliance before
# spatium-install completion has no file here yet.
HOST_ENV=/etc/spatiumddi-host/.env
if [ -r "$HOST_ENV" ]; then
    # ``set -a; . file; set +a`` auto-exports each variable so they
    # land in the supervisor process's environment. Comments + blanks
    # are tolerated by the standard ``.`` shell builtin.
    set -a
    # shellcheck disable=SC1090
    . "$HOST_ENV"
    set +a
fi

# Drop privileges to the unprivileged spatium user. ``su-exec spatium``
# (no ``:group`` suffix) calls initgroups() so the user's supplementary
# group set comes through. Phase 7: no docker group; the only extra
# we need is whatever the kubelet's ServiceAccount-mount projection
# uses, which is preserved by the kernel regardless of su-exec.
exec su-exec spatium /usr/local/bin/spatium-supervisor
