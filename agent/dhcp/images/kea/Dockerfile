# syntax=docker/dockerfile:1.7
# SpatiumDDI Kea DHCP container — kea-dhcp4 + kea-ctrl-agent + spatium-dhcp-agent
# Multi-arch: linux/amd64, linux/arm64 (non-negotiable #11)
ARG PYTHON_VERSION=3.12

FROM alpine:3.22 AS agent-build
RUN apk add --no-cache python3 py3-pip py3-wheel
WORKDIR /src
COPY agent/dhcp/pyproject.toml /src/pyproject.toml
COPY agent/dhcp/spatium_dhcp_agent /src/spatium_dhcp_agent
COPY agent/dhcp/README.md /src/README.md
RUN python3 -m pip install --prefix=/install --no-cache-dir --break-system-packages .

FROM alpine:3.22 AS runtime
# libpcap is pulled in for scapy's BPF socket support, used by the
# optional Phase 2 device-fingerprinting sniffer (DHCP_FINGERPRINT_ENABLED=1).
# scapy itself is pure-python so the install works on amd64 + arm64
# without compilation. The container also needs CAP_NET_RAW at run-time
# for the sniffer to bind the socket — see docs/deployment/DOCKER.md.
#
# ``libcap`` brings in ``setcap`` so we can grant ``kea-dhcp4`` the
# CAP_NET_BIND_SERVICE file capability — without it, the unprivileged
# ``spatium`` user the entrypoint drops to can't bind UDP/67 (the
# DHCPv4 server port). Compose-side ``cap_add: NET_BIND_SERVICE``
# only sets the container bounding set; for a non-root process to
# actually use the cap, the binary needs file caps. Same fix as
# named + pdns_server in the DNS image family.
RUN apk add --no-cache \
      kea kea-dhcp4 kea-ctrl-agent kea-admin \
      kea-hook-lease-cmds kea-hook-ha \
      tini su-exec \
      python3 py3-pip libpcap \
      libcap \
      ca-certificates tzdata \
      lsblk \
 && setcap cap_net_bind_service=+ep /usr/sbin/kea-dhcp4 \
 && addgroup -S spatium \
 && adduser -S -G spatium spatium \
 && mkdir -p /var/lib/spatium-dhcp-agent /var/lib/kea /run/kea /etc/kea /var/log/kea \
 && chown -R spatium:spatium /var/lib/spatium-dhcp-agent /var/lib/kea /run/kea /etc/kea /var/log/kea \
 && { \
      echo "alpine_release=$(cat /etc/alpine-release)"; \
      apk info -v 2>/dev/null \
        | grep -E '^kea(-[a-z]+)?-' \
        | sort -u \
        | sed 's/^\([a-z0-9-]*\)-\([0-9].*\)$/\1=\2/'; \
    } > /etc/spatiumddi-versions

COPY --from=agent-build /install /usr/local
COPY agent/dhcp/images/kea/kea-dhcp4.conf /etc/kea/kea-dhcp4.conf
COPY agent/dhcp/images/kea/kea-ctrl-agent.conf /etc/kea/kea-ctrl-agent.conf
COPY agent/dhcp/images/kea/entrypoint.sh /usr/local/bin/spatium-dhcp-entrypoint
RUN chmod +x /usr/local/bin/spatium-dhcp-entrypoint \
 && chown spatium:spatium /etc/kea/kea-dhcp4.conf /etc/kea/kea-ctrl-agent.conf

ENV CACHE_DIR=/var/lib/spatium-dhcp-agent \
    KEA_CONFIG_PATH=/etc/kea/kea-dhcp4.conf \
    KEA_CONTROL_SOCKET=/run/kea/kea4-ctrl-socket \
    KEA_LEASE_FILE=/var/lib/kea/kea-leases4.csv \
    PYTHONPATH=/usr/local/lib/python3.12/site-packages \
    LOG_LEVEL=INFO

VOLUME ["/var/lib/spatium-dhcp-agent", "/var/lib/kea"]
# DHCPv4 server
EXPOSE 67/udp
# Kea HA hook peer-to-peer HTTP listener (CmdHttpListener bound to the
# this-server peer URL). When HA is inactive the port is unused.
EXPOSE 8000/tcp
# kea-ctrl-agent operator-facing REST. Deliberately NOT 8000 — the HA
# hook owns 8000. See agent/dhcp/images/kea/kea-ctrl-agent.conf.
EXPOSE 8544/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD test -S /run/kea/kea4-ctrl-socket || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/spatium-dhcp-entrypoint"]
