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
ARG GOBGP_VERSION=4.7.0

FROM alpine:3.23 AS agent-build
RUN apk add --no-cache python3 py3-pip py3-wheel
WORKDIR /src
COPY agent/looking-glass/pyproject.toml /src/pyproject.toml
COPY agent/looking-glass/spatium_lg_agent /src/spatium_lg_agent
COPY agent/looking-glass/README.md /src/README.md
RUN python3 -m pip install --prefix=/install --no-cache-dir --break-system-packages .

# ── GoBGP binaries — built from source ──────────────────────────────────
# GoBGP ships no apk/apt package, and its prebuilt release binaries are
# compiled with whatever Go toolchain upstream had at release time — so the
# embedded ``stdlib`` carries every Go CVE disclosed AFTER that build (Trivy
# scans the Go version baked into the binary and fails HIGH/CRITICAL). We
# therefore build gobgp/gobgpd from the pinned source tag with the CURRENT Go
# toolchain, which keeps the embedded stdlib on the latest patch (CVE-clean)
# independent of GoBGP's own release cadence. gobgp/gobgpd are pure-Go, so
# CGO_ENABLED=0 yields fully static binaries that drop straight into the
# Alpine (musl) runtime with no shims.
#
# ``--platform=$BUILDPLATFORM`` + GOOS/GOARCH from the buildx TARGET* args
# cross-compiles with the native toolchain (fast, no QEMU) for both
# linux/amd64 + linux/arm64. Bump GOBGP_VERSION (a git tag from
# https://github.com/osrg/gobgp/tags) + the golang base image together; the
# repo's committed go.sum verifies every module checksum during the build.
#
# The Go toolchain version is a SECURITY pin, not just a build detail: Go links
# its standard library statically into the binary, so a stdlib CVE ships inside
# ``gobgpd`` itself and Trivy flags it against the ``gobinary`` target — there is
# no separate package to patch and no ``apk upgrade`` that can fix it. The only
# remedy is to rebuild with a fixed toolchain.
#
# ``1.26.5`` clears CVE-2026-39822 (HIGH — ``os.Root`` symlink following allows
# directory traversal), which 1.26.4 carries. Keep this at the latest 1.26.x.
FROM --platform=$BUILDPLATFORM golang:1.26.5-alpine AS gobgp-build
ARG GOBGP_VERSION
ARG TARGETOS
ARG TARGETARCH
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth 1 --branch "v${GOBGP_VERSION}" https://github.com/osrg/gobgp .
# Bump vulnerable transitive deps above the versions gobgp pins so Trivy
# (HIGH/CRITICAL) stays green. gobgp v4.7.0 pins golang.org/x/net v0.48,
# which carries several HIGH CVEs (html XSS, IDNA punycode privilege
# escalation, HTTP/2 DoS — fixed in 0.55). Re-check + drop/bump this pin
# when GoBGP's own go.mod moves past it.
RUN go get golang.org/x/net@v0.56.0
ENV CGO_ENABLED=0
RUN GOOS="${TARGETOS:-linux}" GOARCH="${TARGETARCH}" \
      go build -trimpath -ldflags="-s -w" -o /out/gobgpd ./cmd/gobgpd \
 && GOOS="${TARGETOS:-linux}" GOARCH="${TARGETARCH}" \
      go build -trimpath -ldflags="-s -w" -o /out/gobgp ./cmd/gobgp

FROM alpine:3.23 AS runtime
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
COPY --from=gobgp-build /out/gobgp /usr/local/bin/gobgp
COPY --from=gobgp-build /out/gobgpd /usr/local/bin/gobgpd
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
