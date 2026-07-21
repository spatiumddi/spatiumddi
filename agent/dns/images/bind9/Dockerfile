# syntax=docker/dockerfile:1.7
# SpatiumDDI BIND9 DNS container — BIND9 + spatium-dns-agent
# Multi-arch: linux/amd64, linux/arm64 (non-negotiable #11)
ARG PYTHON_VERSION=3.12

FROM alpine:3.24 AS agent-build
RUN apk add --no-cache python3 py3-pip py3-wheel
WORKDIR /src
COPY agent/dns/pyproject.toml /src/pyproject.toml
COPY agent/dns/spatium_dns_agent /src/spatium_dns_agent
RUN python3 -m pip install --prefix=/install --no-cache-dir --break-system-packages .

FROM alpine:3.24 AS runtime
# ``libcap`` brings in ``setcap`` so we can grant ``named`` the
# CAP_NET_BIND_SERVICE file capability — without it, the unprivileged
# ``spatium`` user the entrypoint drops to can't bind port 53 (named
# starts but only ends up listening on the stats-channel port >1024,
# silently failing on real DNS traffic). Alpine's ``bind`` package
# does NOT set this cap automatically, unlike ``iputils-ping`` etc.
#
# ``apk upgrade --no-cache`` runs BEFORE ``apk add`` so the package
# database is fresh and any previously-baked layer carrying an older
# bind / bind-libs version (CVE-2026-3593, CVE-2026-3039, CVE-2026-5946
# all fixed in 9.20.23-r0) gets re-resolved against the current
# alpine 3.22 index. GitHub Actions Docker layer cache (the
# ``cache-from: type=gha`` on the build workflow) is otherwise
# perfectly happy to serve a 30-day-old ``apk add`` layer that
# Trivy will then flag.
#
# ``libcrypto3``/``libssl3`` pinned ``>=3.5.7-r0`` for CVE-2026-45447
# (HIGH, OpenSSL — alpine 3.22 base ships 3.5.6-r0, fixed in 3.5.7-r0):
# the explicit lower bound installs the patched openssl libs AND busts
# the ``cache-from`` layer so a stale apk add can't reintroduce 3.5.6-r0.
RUN apk upgrade --no-cache \
 && apk add --no-cache \
      bind bind-tools \
      tini su-exec \
      python3 py3-pip \
      libcap \
      ca-certificates tzdata \
      'libcrypto3>=3.5.7-r0' 'libssl3>=3.5.7-r0' \
 && setcap cap_net_bind_service=+ep /usr/sbin/named \
 && addgroup -S spatium \
 && adduser -S -G spatium spatium \
 && mkdir -p /var/lib/spatium-dns-agent /var/cache/bind /var/run/named /var/log/named \
 && chown -R spatium:spatium /var/lib/spatium-dns-agent /var/cache/bind /var/run/named /var/log/named \
 && { \
      echo "alpine_release=$(cat /etc/alpine-release)"; \
      apk info -v 2>/dev/null \
        | grep -E '^(bind|bind-tools|bind-libs)-' \
        | sort -u \
        | sed 's/^\([a-z0-9-]*\)-\([0-9].*\)$/\1=\2/'; \
    } > /etc/spatiumddi-versions

COPY --from=agent-build /install /usr/local
COPY agent/dns/images/bind9/entrypoint.sh /usr/local/bin/spatium-dns-entrypoint
RUN chmod +x /usr/local/bin/spatium-dns-entrypoint

ENV AGENT_DRIVER=bind9 \
    AGENT_STATE_DIR=/var/lib/spatium-dns-agent \
    PYTHONPATH=/usr/local/lib/python3.12/site-packages \
    LOG_LEVEL=INFO

VOLUME ["/var/lib/spatium-dns-agent", "/var/cache/bind"]
EXPOSE 53/udp 53/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD dig @127.0.0.1 -p 53 . NS +short +timeout=2 || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/spatium-dns-entrypoint"]
