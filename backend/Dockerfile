# ── Stage 1: build dependencies ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System dev deps for compiled Python packages: asyncpg (libpq), python3-saml
# (libxml2 + xmlsec).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libxml2-dev \
    libxmlsec1-dev \
    libxmlsec1-openssl \
    && rm -rf /var/lib/apt/lists/*

# Copy only the dependency manifest first so this layer is cached independently
# of source code changes.
COPY pyproject.toml .

# Create a minimal stub package so hatchling can resolve metadata without
# needing the full source tree, then install runtime deps only.
RUN mkdir -p app && touch app/__init__.py && \
    pip install --no-cache-dir --prefix=/install .


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    NMAP_PRIVILEGED=1
    # NMAP_PRIVILEGED=1 — Debian's nmap does an early getuid()==0 check
    # for SYN scans + ``-O`` OS detection and bails before consulting the
    # process's actual capabilities. This env var (or ``--privileged``)
    # tells nmap to trust that it has the necessary raw-socket privilege
    # via Linux capabilities (granted by the setcap-on-nmap line below).
    # Without it operators get "TCP/IP fingerprinting requires root
    # privileges" even though CAP_NET_RAW is in the process's effective
    # set. The cap itself is still required — env var alone doesn't help.

WORKDIR /app

# Runtime system deps: libpq5 for asyncpg, libxmlsec1 for python3-saml,
# iputils-ping for the DNS pool ICMP health-check (the binary ships
# with cap_net_raw+ep on Debian, so it works as the non-root app user).
# libcap2-bin gives us setcap so we can grant nmap CAP_NET_RAW —
# without it ``nmap -O`` (and any SYN scan) bails with
# "TCP/IP fingerprinting requires root privileges" when invoked by
# the non-root app user. The cap is in Docker's default bounding set
# so this works without needing cap_add at the compose / k8s layer.
# Add the official PostgreSQL apt repo so we can pin
# ``postgresql-client-16`` (Debian trixie's default repos only carry
# client-17). We need the client major to match the postgres SERVER
# version we ship in compose/k8s — pg_dump 17 emits
# ``SET transaction_timeout = 0;`` and ``\restrict`` directives that
# a postgres-16 server rejects on replay. When we eventually bump
# the postgres image to 17, bump this pin in lockstep.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && wget -qO /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libxml2 \
    libxmlsec1-openssl \
    nmap \
    iputils-ping \
    libcap2-bin \
    postgresql-client-16 \
    && setcap cap_net_raw,cap_net_bind_service+eip /usr/bin/nmap \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Pre-create + chown the on-volume paths so the
# matching named volumes inherit app-user ownership on first attach.
# Docker only propagates the underlying directory's mode/owner to a
# fresh volume when the path exists on the image; without these
# mkdirs the api / worker hit EACCES on the first write attempt.
#
#   /var/lib/spatiumddi/backups      — issue #117 (backup archive volume)
#   /var/lib/spatiumddi/certs        — issue #134 Phase 4b.2 (appliance TLS
#                                      cert volume, shared with the frontend
#                                      nginx container at /etc/nginx/certs)
#   /var/lib/spatiumddi/slot-images  — #170 follow-up (air-gap appliance
#                                      slot-image upload storage)
#
# The user is created BEFORE the COPYs so we can use BuildKit's
# ``--chown=app:app`` flag and avoid a trailing ``chown -R /app``
# pass — that rewrite would materialise a duplicate copy of every
# file in a new layer (~550 MB doubled). Setting ownership at COPY
# time keeps the source layer authoritative.
RUN useradd --no-create-home --shell /bin/false app \
    && mkdir -p /var/lib/spatiumddi/backups /var/lib/spatiumddi/certs /var/lib/spatiumddi/slot-images \
    && chown -R app:app /var/lib/spatiumddi

COPY --from=builder /install /usr/local
COPY --chown=app:app . .

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')"

# --proxy-headers + --forwarded-allow-ips=* makes uvicorn honor
# X-Forwarded-For / X-Forwarded-Proto when populating request.client
# and request.url.scheme. Required so the audit log captures the real
# end-user IP rather than the frontend nginx container's IP. The
# wildcard is safe here because the api container is only reachable
# from inside the compose / k8s network — the frontend nginx is the
# only entity talking to it, and nginx is under our control.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]


# ── Stage 3: dev (used by docker-compose.dev.yml) ─────────────────────────────
# Adds the ``[dev]`` extras (pytest + pytest-asyncio + pytest-xdist +
# pytest-cov) on top of the runtime stage so ``make test`` can run pytest
# inside the api container without bloating the production image. The dev
# compose's ``build.target: dev`` selects this stage; the release pipeline
# stops at ``runtime``.
#
# We install as root (the runtime stage's USER app would block site-packages
# writes), then switch back to ``app`` so the runtime CMD stays unchanged.
FROM runtime AS dev
USER root
RUN pip install --no-cache-dir --root-user-action=ignore \
        "pytest>=8.3.0" \
        "pytest-asyncio>=0.24.0" \
        "pytest-cov>=6.0.0" \
        "pytest-xdist>=3.6.0"
USER app
