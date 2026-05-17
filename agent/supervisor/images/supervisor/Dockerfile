# syntax=docker/dockerfile:1.7
# SpatiumDDI Supervisor — host-side controller for Application-role appliances.
# Multi-arch: linux/amd64, linux/arm64 (non-negotiable #11).
#
# Phase A1 ships a near-empty supervisor — the box exists, builds
# clean, and logs an idle heartbeat. Real work lands in Waves A2 → D.
# See agent/supervisor/README.md.
ARG PYTHON_VERSION=3.12

FROM alpine:3.22 AS build
RUN apk add --no-cache python3 py3-pip py3-wheel
WORKDIR /src
COPY agent/supervisor/pyproject.toml /src/pyproject.toml
COPY agent/supervisor/spatium_supervisor /src/spatium_supervisor
COPY agent/supervisor/README.md /src/README.md
RUN python3 -m pip install --prefix=/install --no-cache-dir --break-system-packages .

FROM alpine:3.22 AS runtime
# Issue #183 Phase 7 — k3s-only appliance. ``docker-cli`` +
# ``docker-cli-compose`` are gone; the supervisor's k3s lifecycle
# module talks to the local kubeapi via stdlib http.client (no
# ``kubernetes`` Python client needed — see k8s_api.py). The
# supervisor pod uses in-cluster ServiceAccount auth, so it doesn't
# need a kubeconfig file either.
#
# ``nftables`` stays — the supervisor renders nft drop-ins under
# ``/etc/nftables.d/`` and exec()s ``nft -f`` to apply (firewall
# allowlist for the kubeapi-direct CIDRs, role-driven ports).
RUN apk add --no-cache \
      tini su-exec \
      python3 py3-pip \
      nftables libcap \
      ca-certificates tzdata \
 && addgroup -S spatium \
 && adduser -S -G spatium spatium \
 && mkdir -p /var/lib/spatium-supervisor \
 && chown -R spatium:spatium /var/lib/spatium-supervisor \
 && setcap cap_net_admin+eip /usr/sbin/nft
# ``libcap`` ships ``setcap``; the file cap on /usr/sbin/nft lets the
# unprivileged ``spatium`` user (issue #170 design) actually exercise
# the CAP_NET_ADMIN the compose entry requests. Without the file
# cap, compose's ``cap_add: NET_ADMIN`` only adds the cap to the
# container's bounding set — a non-root process still needs file
# caps to use it. Same shape as the api image's setcap on nmap.

COPY --from=build /install /usr/local
COPY agent/supervisor/images/supervisor/entrypoint.sh /usr/local/bin/spatium-supervisor-entrypoint
RUN chmod +x /usr/local/bin/spatium-supervisor-entrypoint

ENV STATE_DIR=/var/lib/spatium-supervisor \
    PYTHONPATH=/usr/local/lib/python3.12/site-packages \
    LOG_LEVEL=INFO

VOLUME ["/var/lib/spatium-supervisor"]

# Process health = the python entry is still pid-of-supervisor.
# Real health (cert valid, control plane reachable, role applied)
# is a Wave A2+ concern.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD pgrep -f spatium-supervisor >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/spatium-supervisor-entrypoint"]
