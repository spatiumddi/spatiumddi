# syntax=docker/dockerfile:1.7
# SpatiumDDI BGP Looking Glass collector container — receive-only GoBGP +
# spatium-lg-agent. Multi-arch: linux/amd64, linux/arm64 (non-negotiable #11)
#
# RECEIVE-ONLY SAFETY INVARIANT — see spatium_lg_agent/gobgp.py's module
# docstring for the full writeup + empirical verification notes. This
# image runs gobgpd purely as a passive collector: the rendered config's
# global default-export-policy is always "reject-route" and no
# export-policy-list is ever populated, so the daemon can receive routes
# from every configured peer but can never advertise one back.
ARG GOBGP_VERSION=4.5.0

FROM alpine:3.22 AS agent-build
RUN apk add --no-cache python3 py3-pip py3-wheel
WORKDIR /src
COPY agent/looking-glass/pyproject.toml /src/pyproject.toml
COPY agent/looking-glass/spatium_lg_agent /src/spatium_lg_agent
COPY agent/looking-glass/README.md /src/README.md
RUN python3 -m pip install --prefix=/install --no-cache-dir --break-system-packages .

# ── GoBGP binaries ──────────────────────────────────────────────────────
# No apk/apt package exists upstream (GoBGP is not packaged by Alpine or
# Debian), so fetch the official prebuilt release tarball straight from
# GitHub Releases and verify it against a pinned SHA-256 for both
# amd64 + arm64 (docker buildx sets TARGETARCH automatically per platform
# in the multi-arch build below). gobgp/gobgpd are statically-linked
# pure-Go binaries (verified via `file`/`ldd` during development — no
# dynamic deps), so they drop straight into an Alpine (musl) runtime with
# no compatibility shims needed.
#
# Bump GOBGP_VERSION + both SHA256 pins together from
# https://github.com/osrg/gobgp/releases/download/v<version>/gobgp_<version>_checksums.txt
FROM alpine:3.22 AS gobgp-fetch
ARG GOBGP_VERSION
ARG TARGETARCH
RUN apk add --no-cache curl
WORKDIR /fetch
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) SHA256="e098e1d47db93463506e3dc5bb49bb93c9347837e8bf5e2d202c1fe7016385c7" ;; \
      arm64) SHA256="0f83a928848c8c6a01304f6336205b1e4cd29f0f2d24568f6305bf8314dc8717" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    URL="https://github.com/osrg/gobgp/releases/download/v${GOBGP_VERSION}/gobgp_${GOBGP_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    curl -fsSL -o gobgp.tar.gz "${URL}"; \
    echo "${SHA256}  gobgp.tar.gz" | sha256sum -c -; \
    tar xzf gobgp.tar.gz gobgp gobgpd; \
    chmod +x gobgp gobgpd

FROM alpine:3.22 AS runtime
# ``libcap`` brings in ``setcap`` so gobgpd can bind BGP's privileged
# TCP/179 as the unprivileged ``spatium`` user (same pattern as
# kea-dhcp4/6 + named/pdns_server in the DHCP/DNS images — a
# container-level ``cap_add``/``securityContext.capabilities.add`` only
# sets the bounding set, which su-exec's setuid drops on the privilege
# drop; the capability has to live on the binary as a file cap to
# survive it).
#
# ``apk upgrade --no-cache`` runs BEFORE ``apk add`` so the package
# database is fresh and any previously-baked layer carrying an older
# libssl/libcrypto (or other base) version gets re-resolved against the
# current alpine 3.22 index rather than serving a stale GitHub Actions
# layer cache that Trivy would then flag.
RUN apk upgrade --no-cache \
 && apk add --no-cache \
      tini su-exec \
      python3 py3-pip \
      libcap \
      ca-certificates tzdata \
 && addgroup -S spatium \
 && adduser -S -G spatium spatium \
 && mkdir -p /var/lib/spatium-lg-agent /etc/gobgp \
 && chown -R spatium:spatium /var/lib/spatium-lg-agent /etc/gobgp

COPY --from=agent-build /install /usr/local
COPY --from=gobgp-fetch /fetch/gobgp /usr/local/bin/gobgp
COPY --from=gobgp-fetch /fetch/gobgpd /usr/local/bin/gobgpd
RUN setcap cap_net_bind_service=+ep /usr/local/bin/gobgpd
COPY agent/looking-glass/images/gobgp/gobgpd-idle.json /etc/gobgp/gobgpd.json
COPY agent/looking-glass/images/gobgp/entrypoint.sh /usr/local/bin/spatium-lg-entrypoint
RUN chmod +x /usr/local/bin/spatium-lg-entrypoint \
 && chown spatium:spatium /etc/gobgp/gobgpd.json \
 && { \
      echo "alpine_release=$(cat /etc/alpine-release)"; \
      echo "gobgp=${GOBGP_VERSION}"; \
    } > /etc/spatiumddi-versions

ENV AGENT_STATE_DIR=/var/lib/spatium-lg-agent \
    GOBGPD_CONFIG_PATH=/etc/gobgp/gobgpd.json \
    GOBGPD_BIN=/usr/local/bin/gobgpd \
    GOBGP_BIN=/usr/local/bin/gobgp \
    GOBGP_GRPC_HOST=127.0.0.1 \
    GOBGP_GRPC_PORT=50051 \
    PYTHONPATH=/usr/local/lib/python3.12/site-packages \
    LOG_LEVEL=INFO

VOLUME ["/var/lib/spatium-lg-agent"]
# BGP (receive-only — see spatium_lg_agent/gobgp.py's module docstring)
EXPOSE 179/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD /usr/local/bin/gobgp -u 127.0.0.1 -p 50051 -j neighbor >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/spatium-lg-entrypoint"]
