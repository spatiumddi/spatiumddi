# syntax=docker/dockerfile:1.7
# SpatiumDDI BIND9 DNS container — BIND9 + spatium-dns-agent
# Multi-arch: linux/amd64, linux/arm64 (non-negotiable #11)
ARG PYTHON_VERSION=3.12

FROM alpine:3.22 AS agent-build
RUN apk add --no-cache python3 py3-pip py3-wheel
WORKDIR /src
COPY agent/dns/pyproject.toml /src/pyproject.toml
COPY agent/dns/spatium_dns_agent /src/spatium_dns_agent
RUN python3 -m pip install --prefix=/install --no-cache-dir --break-system-packages .

FROM alpine:3.22 AS runtime
RUN apk add --no-cache \
      bind bind-tools \
      tini su-exec \
      python3 py3-pip \
      ca-certificates tzdata \
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
