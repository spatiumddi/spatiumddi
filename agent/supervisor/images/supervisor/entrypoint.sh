#!/bin/sh
# Container entrypoint — delegates to the Python supervisor.
# tini is PID 1 (provided by ENTRYPOINT in the Dockerfile).
set -eu

# Phase A1 pre-checks intentionally minimal — the supervisor doesn't
# talk to a control plane yet. Wave A2 will tighten this to require
# at least one of (CONTROL_PLANE_URL+BOOTSTRAP_PAIRING_CODE) once the
# register path lands.

mkdir -p /var/lib/spatium-supervisor

# Wave C: the supervisor runs as root inside the container so it can
# write the firewall drop-in to /etc/nftables.d (root-owned on the
# host via the bind-mount), invoke ``nft -f`` (needs CAP_NET_ADMIN,
# requested in compose), and write the slot-upgrade / reboot trigger
# files under /var/lib/spatiumddi-host/release-state. The Phase-A1
# ``su-exec spatium:spatium`` design was aspirational for an
# unprivileged shape that the Wave-C work never actually delivered —
# the bind-mounted ``/var/run/docker.sock`` already grants effective
# root on the host, so dropping privileges inside the container is
# pure friction without a real security benefit.
exec /usr/local/bin/spatium-supervisor
