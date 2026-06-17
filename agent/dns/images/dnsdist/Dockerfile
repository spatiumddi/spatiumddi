# syntax=docker/dockerfile:1.7
# SpatiumDDI dnsdist front for PowerDNS Authoritative (issue #146 Phase 2).
# PowerDNS auth has no Response-Rate-Limiting equivalent, so dnsdist sits on
# :53 and forwards to pdns (loopback :5300 in a shared netns). The config is
# rendered by the PowerDNS spatium-dns-agent into a shared volume; this
# container's entrypoint watches it and (re)starts dnsdist on change.
# Multi-arch: linux/amd64, linux/arm64 (non-negotiable #11).
FROM alpine:3.22 AS runtime

# apk upgrade before add so the layer cache can't serve a stale index that
# Trivy would then flag (mirrors the powerdns image rationale).
RUN apk upgrade --no-cache \
    && apk add --no-cache dnsdist libcap bash tini \
    && addgroup -S spatium \
    && adduser -S -G spatium -h /var/lib/dnsdist spatium \
    # dnsdist binds :53 as the unprivileged spatium user.
    && setcap cap_net_bind_service=+ep /usr/sbin/dnsdist 2>/dev/null \
       || setcap cap_net_bind_service=+ep /usr/bin/dnsdist

COPY agent/dns/images/dnsdist/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Shared with the PowerDNS agent container (network_mode: service:dns-powerdns
# on compose / same pod on k8s). The agent renders dnsdist.conf here.
ENV DNSDIST_CONF=/agent-state/dnsdist.conf
USER spatium
EXPOSE 53/udp 53/tcp
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
