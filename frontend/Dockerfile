# ── Stage 1: build ────────────────────────────────────────────────────────────
# ``--platform=$BUILDPLATFORM`` forces the build stage to run natively on
# the host architecture (typically amd64) instead of emulated under QEMU
# for each ``--platform`` target. The output is a ``dist/`` tree of
# static JS / CSS / HTML — platform-independent — which the final nginx
# stage then wraps for both amd64 and arm64. Prevents the npm bug
# (https://github.com/npm/cli/issues/4828) where a lockfile generated
# on one platform lacks the ``@rollup/rollup-linux-arm64-musl`` optional
# dep needed when ``npm install`` runs on emulated arm64-musl — which
# was the failure mode on the 2026.04.22-1 release build.
FROM --platform=$BUILDPLATFORM node:20-alpine AS builder

# Release-pipeline version stamp. The release workflow passes the git
# tag (e.g. ``2026.04.22-1``); local builds default to ``dev`` so
# unversioned images are obvious in the sidebar.
ARG VITE_APP_VERSION=dev
ENV VITE_APP_VERSION=${VITE_APP_VERSION}

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --prefer-offline

COPY . .
RUN npm run build


# ── Stage 2: serve via nginx ──────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

COPY --from=builder /app/dist /usr/share/nginx/html
# Issue #176: the API upstream + DNS resolver are now templated so the
# same image works on Docker Compose (defaults below) and Kubernetes
# (Helm chart overrides API_UPSTREAM_HOST to ``{{ fullname }}-api`` via
# env). The official nginx image's entrypoint renders
# /etc/nginx/templates/*.template → /etc/nginx/conf.d/* via envsubst.
COPY default.conf.template /etc/nginx/templates/default.conf.template
# Replacement for the upstream 15-local-resolvers.envsh — ours
# respects an operator-supplied NGINX_LOCAL_RESOLVERS so the resolver
# IP can be pinned via env var. See the script's header comment.
COPY 15-local-resolvers.envsh /docker-entrypoint.d/15-local-resolvers.envsh

# Sensible defaults that keep Docker Compose working without operator
# config — Compose's service-discovery puts the api at ``api:8000``.
# Helm chart, Kustomize manifests, or operators on a custom platform
# override these at deploy time.
ENV API_UPSTREAM_HOST=api \
    API_UPSTREAM_PORT=8000

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD wget -qO- http://127.0.0.1/nginx-health || exit 1
