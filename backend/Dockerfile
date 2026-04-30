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
    PYTHONPATH=/app

WORKDIR /app

# Runtime system deps: libpq5 for asyncpg, libxmlsec1 for python3-saml,
# iputils-ping for the DNS pool ICMP health-check (the binary ships
# with cap_net_raw+ep on Debian, so it works as the non-root app user).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libxml2 \
    libxmlsec1-openssl \
    nmap \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY . .

# Non-root user
RUN useradd --no-create-home --shell /bin/false app \
    && chown -R app:app /app
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
