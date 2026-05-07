# syntax=docker/dockerfile:1.7
# SpatiumDDI PowerDNS-Authoritative container — pdns_server + spatium-dns-agent.
# Issue #127, Phase 1 — LMDB backend, no external DB dependency.
# Multi-arch: linux/amd64, linux/arm64 (non-negotiable #11)
ARG PYTHON_VERSION=3.12

FROM alpine:3.22 AS agent-build
RUN apk add --no-cache python3 py3-pip py3-wheel
WORKDIR /src
COPY agent/dns/pyproject.toml /src/pyproject.toml
COPY agent/dns/spatium_dns_agent /src/spatium_dns_agent
RUN python3 -m pip install --prefix=/install --no-cache-dir --break-system-packages .

FROM alpine:3.22 AS runtime
# pdns + the LMDB backend; bind-tools only for `dig` so the smoke test
# in agent-e2e.yml works against the PowerDNS pod the same way it
# does against the BIND pod.
RUN apk add --no-cache \
      pdns pdns-backend-lmdb \
      bind-tools \
      tini su-exec \
      python3 py3-pip \
      ca-certificates tzdata \
 && addgroup -S spatium \
 && adduser -S -G spatium spatium \
 && mkdir -p /var/lib/spatium-dns-agent /var/lib/powerdns /var/run/pdns \
 && chown -R spatium:spatium /var/lib/spatium-dns-agent /var/lib/powerdns /var/run/pdns \
 && { \
      echo "alpine_release=$(cat /etc/alpine-release)"; \
      apk info -v 2>/dev/null \
        | grep -E '^(pdns|pdns-backend-lmdb)-' \
        | sort -u \
        | sed 's/^\([a-z0-9-]*\)-\([0-9].*\)$/\1=\2/'; \
    } > /etc/spatiumddi-versions

COPY --from=agent-build /install /usr/local
COPY agent/dns/images/powerdns/entrypoint.sh /usr/local/bin/spatium-dns-entrypoint
RUN chmod +x /usr/local/bin/spatium-dns-entrypoint

ENV AGENT_DRIVER=powerdns \
    AGENT_STATE_DIR=/var/lib/spatium-dns-agent \
    PYTHONPATH=/usr/local/lib/python3.12/site-packages \
    LOG_LEVEL=INFO

VOLUME ["/var/lib/spatium-dns-agent", "/var/lib/powerdns"]
EXPOSE 53/udp 53/tcp

# Authoritative servers respond to a CHAOS-class query for "id.server"
# (the analogue of BIND's "version.bind"). Use that as the liveness
# probe so the check is independent of any zone state.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD dig @127.0.0.1 -p 53 id.server CH TXT +short +timeout=2 || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/spatium-dns-entrypoint"]
