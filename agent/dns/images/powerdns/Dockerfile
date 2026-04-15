# syntax=docker/dockerfile:1.7
# SpatiumDDI PowerDNS container — pdns_server / pdns_recursor + spatium-dns-agent
FROM alpine:3.20 AS agent-build
RUN apk add --no-cache python3 py3-pip py3-wheel
WORKDIR /src
COPY agent/dns/pyproject.toml /src/pyproject.toml
COPY agent/dns/spatium_dns_agent /src/spatium_dns_agent
RUN python3 -m pip install --prefix=/install --no-cache-dir --break-system-packages .

FROM alpine:3.20 AS runtime
RUN apk add --no-cache \
      pdns pdns-backend-sqlite3 pdns-backend-lmdb pdns-recursor \
      tini python3 py3-pip \
      ca-certificates tzdata \
 && addgroup -S spatium \
 && adduser -S -G spatium spatium \
 && mkdir -p /var/lib/spatium-dns-agent /var/lib/powerdns \
 && chown -R spatium:spatium /var/lib/spatium-dns-agent

COPY --from=agent-build /install /usr/local
COPY agent/dns/images/powerdns/entrypoint.sh /usr/local/bin/spatium-dns-entrypoint
RUN chmod +x /usr/local/bin/spatium-dns-entrypoint

ENV AGENT_DRIVER=powerdns \
    PDNS_FLAVOR=auth \
    AGENT_STATE_DIR=/var/lib/spatium-dns-agent \
    LOG_LEVEL=INFO

VOLUME ["/var/lib/spatium-dns-agent", "/var/lib/powerdns"]
EXPOSE 53/udp 53/tcp

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/spatium-dns-entrypoint"]
