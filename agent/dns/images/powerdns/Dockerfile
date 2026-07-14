# syntax=docker/dockerfile:1.7
# SpatiumDDI PowerDNS-Authoritative container — pdns_server + spatium-dns-agent.
# Issue #127, Phase 1 — LMDB backend, no external DB dependency.
# Multi-arch: linux/amd64, linux/arm64 (non-negotiable #11)
#
# ⚠️ DELIBERATELY HELD ON alpine:3.22 — DO NOT BUMP TO 3.23 (issue #638).
#
# Every other agent image moved to alpine:3.23 in #637. This one did not,
# because 3.23 ships pdns 5.0.5 (3.22 ships 4.9.5) and PowerDNS 5.0 performs
# an AUTOMATIC, SILENT, IRREVERSIBLE LMDB schema upgrade (v5 → v6) on the
# first open of the database — a read is enough to trigger it, and there is
# no opt-out (the documented ``lmdb-schema-version=5`` escape hatch is an
# upstream docs bug: it makes pdns refuse to boot outright).
#
# Once 5.0 has opened the LMDB, pdns 4.9 can no longer read it at all:
#     Error: Somehow, we are not at schema version 5. Giving up
#
# Our LMDB is persisted in every deployment shape (compose named volume /
# appliance hostPath / chart PVC), and the appliance A/B slot rollback swaps
# the ROOTFS while /var — where the database lives — is the shared persistent
# partition. So an upgrade-then-rollback leaves pdns 4.9 crash-looping on a
# schema-6 database: DNS down, and the rollback cannot recover it.
#
# Pinning ``pdns=4.9.*`` on a 3.23 base is NOT an option — the 3.23 index has
# no 4.9 package at all. The image therefore stays on 3.22 (a supported Alpine
# branch; ``apk upgrade`` below still pulls 3.22 security fixes) until #638
# lands the pre-upgrade LMDB snapshot + restore path.
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
# does against the BIND pod. ``libcap`` brings in ``setcap`` so we
# can grant ``pdns_server`` CAP_NET_BIND_SERVICE — without it, the
# unprivileged ``spatium`` user can't bind port 53 (the daemon starts
# but silently fails on the privileged-port bind).
#
# ``apk upgrade --no-cache`` runs BEFORE ``apk add`` so the package
# database is fresh and any previously-baked layer carrying an older
# bind-libs version (CVE-2026-3593, CVE-2026-3039, CVE-2026-5946 all
# fixed in 9.20.23-r0 — pdns drags bind-libs transitively for the
# DNS-name parser) gets re-resolved against the current alpine 3.22
# index. GitHub Actions Docker layer cache (the ``cache-from:
# type=gha`` on the build workflow) is otherwise happy to serve a
# 30-day-old ``apk add`` layer that Trivy will then flag.
#
# ``libcrypto3``/``libssl3`` pinned ``>=3.5.7-r0`` for CVE-2026-45447
# (HIGH, OpenSSL — alpine 3.22 base ships 3.5.6-r0, fixed in 3.5.7-r0):
# the explicit lower bound installs the patched openssl libs AND busts
# the ``cache-from`` layer so a stale apk add can't reintroduce 3.5.6-r0.
RUN apk upgrade --no-cache \
 && apk add --no-cache \
      pdns pdns-backend-lmdb \
      bind-tools \
      tini su-exec \
      python3 py3-pip \
      libcap \
      ca-certificates tzdata \
      'libcrypto3>=3.5.7-r0' 'libssl3>=3.5.7-r0' \
 && setcap cap_net_bind_service=+ep /usr/sbin/pdns_server \
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
