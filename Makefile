.PHONY: help up down dev build build-supervisor migrate lint test lint-backend lint-frontend test-backend \
        ci ci-backend-lint ci-frontend-lint ci-frontend-build screenshots \
        appliance appliance-builder appliance-iso appliance-clean \
        appliance-bake-images appliance-clean-baked-images appliance-dev-iso \
        appliance-baked-iso appliance-stamp-dev appliance-slot-image \
        appliance-fetch-k3s

# ── Configuration ──────────────────────────────────────────────────────────────
COMPOSE        = docker compose
COMPOSE_DEV    = docker compose -f docker-compose.yml -f docker-compose.dev.yml
BACKEND_DIR    = backend
FRONTEND_DIR   = frontend

# Per-build identifier used as the image tag (compose substitutes via
# ``${SPATIUMDDI_VERSION}``). Computed once per ``make`` invocation —
# git short sha + 4 random hex chars — so each ISO cut produces a
# distinct tag visible in ``docker ps`` (e.g. ``ghcr.io/spatiumddi/
# spatiumddi-api:dev-148c437-a3f2``). Mirrors how ISOs are tracked
# by build-NN; gives operators an in-container way to confirm which
# build a running stack came from.
#
# Override by exporting SPATIUMDDI_VERSION before invoking make. CI
# release builds set it to the CalVer tag (e.g. 2026.05.14-1) and
# BAKE_SOURCE=ghcr to pull pre-published images.
ifeq ($(origin SPATIUMDDI_VERSION), undefined)
SPATIUMDDI_VERSION := dev-$(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)-$(shell openssl rand -hex 2 2>/dev/null || date +%s | tail -c5)
endif
export SPATIUMDDI_VERSION

# ── Help ───────────────────────────────────────────────────────────────────────
help:
	@echo "SpatiumDDI development targets:"
	@echo ""
	@echo "  make up          Start the full stack (production images)"
	@echo "  make dev         Start the dev stack (hot-reload)"
	@echo "  make down        Stop and remove containers"
	@echo "  make build       Build all Docker images"
	@echo "  make migrate     Run Alembic migrations inside the running api container"
	@echo "  make lint        Lint backend (ruff+mypy) and frontend (eslint+prettier)"
	@echo "  make test        Run backend tests against a live DB"
	@echo "  make ci          Run the exact same lint + typecheck + build jobs CI runs"
	@echo "  make screenshots Re-capture docs/assets/screenshots/ via headless chromium"
	@echo "  make appliance      Build the OS-appliance qcow2 (Phase 1 — Debian 13 amd64)"
	@echo "  make appliance-iso  Wrap the Phase 1 raw image as a hybrid USB/CD ISO (Phase 2)"
	@echo ""

# ── Stack ──────────────────────────────────────────────────────────────────────
up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

dev:
	$(COMPOSE_DEV) up

build: build-supervisor
	$(COMPOSE) build

# Build the standalone spatium-supervisor image (#170). The image
# isn't in docker-compose.yml — it ships out of band as part of the
# appliance bake — so ``make build`` (which is ``docker compose
# build``) wouldn't otherwise rebuild it. Without this, edits to
# ``agent/supervisor/`` go in unnoticed because the bake reuses
# whatever ``spatium-supervisor:dev`` already sits in local docker
# from an earlier manual build. Tag both the bare name and the
# ghcr canonical name so ``appliance/scripts/bake-images.sh``'s
# local-source resolver finds it under either form.
build-supervisor:
	docker build -t spatium-supervisor:dev \
	             -t ghcr.io/spatiumddi/spatium-supervisor:dev \
	             -f agent/supervisor/images/supervisor/Dockerfile .

# ── Database ───────────────────────────────────────────────────────────────────
migrate:
	$(COMPOSE) run --rm migrate

migration:
	@test -n "$(MSG)" || (echo "Usage: make migration MSG='describe change'"; exit 1)
	$(COMPOSE) run --rm --user root -v $(PWD)/backend:/app api alembic revision --autogenerate -m "$(MSG)"

# ── Linting ────────────────────────────────────────────────────────────────────
lint: lint-backend lint-frontend

lint-backend:
	cd $(BACKEND_DIR) && \
	  python -m ruff check app tests && \
	  python -m black --check app tests && \
	  python -m mypy app

lint-frontend:
	cd $(FRONTEND_DIR) && \
	  npm run lint && \
	  npm run format:check

# ── Tests ──────────────────────────────────────────────────────────────────────
#
# pytest runs *inside* the api container so the suite is exactly the
# version-pinned interpreter + deps the rest of CI uses, without
# requiring the operator to have python3 + pyproject's [dev] extras
# installed on the host. The dev compose's api ``build.target: dev``
# bakes pytest into the image; ``TEST_DATABASE_URL`` is pre-set on the
# environment so the conftest carves its per-worker test DB against
# the dev compose's postgres service.
#
# ``-T`` on ``docker compose exec`` disables TTY allocation so the
# output streams cleanly on CI runners + pipes to ``tee`` / grep
# without ANSI artefacts.
test: test-backend

test-backend:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml exec -T api python -m pytest -n auto

test-one:
	@test -n "$(T)" || (echo "Usage: make test-one T=tests/test_health.py::test_liveness"; exit 1)
	docker compose -f docker-compose.yml -f docker-compose.dev.yml exec -T api python -m pytest $(T) -v

# ── CI parity ──────────────────────────────────────────────────────────────────
# `make ci` runs the same lint + typecheck + build jobs GitHub Actions runs on
# every push (backend-lint, frontend-lint, frontend-build). The separate
# backend-tests job is not included — it needs a fresh `spatiumddi_test`
# database and is covered by `make test`. Requires the dev stack to be running
# (backend checks execute inside the api container) and Node 20+ locally.
ci: ci-backend-lint ci-frontend-lint ci-frontend-build
	@echo ""
	@echo "✓ All CI checks passed — safe to push."

ci-backend-lint:
	@echo "→ Backend — Lint & Type Check (matches .github/workflows/ci.yml)"
	@# The prod `api` image doesn't ship dev tools. Install them on first run;
	@# they persist until the container is recreated.
	@$(COMPOSE_DEV) exec -T api python -m ruff --version >/dev/null 2>&1 || \
	  $(COMPOSE_DEV) exec -T -u root api pip install --quiet --root-user-action=ignore \
	    ruff black mypy
	$(COMPOSE_DEV) exec -T api python -m ruff check app tests
	$(COMPOSE_DEV) exec -T api python -m black --check app tests
	$(COMPOSE_DEV) exec -T api python -m mypy app

ci-frontend-lint:
	@echo "→ Frontend — Lint & Type Check"
	cd $(FRONTEND_DIR) && npm run lint && npm run format:check && npm run typecheck

ci-frontend-build:
	@echo "→ Frontend — Build"
	cd $(FRONTEND_DIR) && npm run build

# ── Screenshots ────────────────────────────────────────────────────────────────
# Re-captures the README screenshots via headless chromium. The dev stack must
# be running and reachable at the configured URL (default http://localhost:8077).
# See scripts/screenshots/README.md for options + troubleshooting.
#
# Pass extra flags via SCREENSHOT_ARGS, e.g.:
#   make screenshots SCREENSHOT_ARGS="--only dashboard,ipam"
#   make screenshots SCREENSHOT_ARGS="--base-url http://localhost:8077 --width 1920"
screenshots:
	@command -v node >/dev/null || (echo "node not installed — apt-get install -y nodejs"; exit 1)
	@test -x /usr/bin/chromium || (echo "chromium missing — apt-get install -y chromium"; exit 1)
	@test -d scripts/screenshots/node_modules || \
	  (cd scripts/screenshots && npm install --no-audit --no-fund)
	node scripts/screenshots/capture.mjs $(SCREENSHOT_ARGS)

# ── OS Appliance (Phase 1: Debian 13 amd64 qcow2 MVP) ──────────────────────────
# See appliance/README.md for the full design + prereqs.
#
# The build runs inside a published builder container so the only host
# requirement is Docker. Override APPLIANCE_BUILDER to point at a
# locally-built image (e.g. for iterating on appliance/builder/Dockerfile):
#   make appliance APPLIANCE_BUILDER=spatiumddi-appliance-builder:dev
APPLIANCE_DIR     = appliance
APPLIANCE_OUT     = $(APPLIANCE_DIR)/build
# mkosi names the output `<ImageId>_<ImageVersion>.raw` — derive both
# at runtime from whatever appears in build/ so a version bump in
# mkosi.conf doesn't break the Makefile.
APPLIANCE_BUILDER = ghcr.io/spatiumddi/appliance-builder:latest

appliance:
	@command -v docker >/dev/null || \
	  (echo "docker not found — the appliance build runs inside a container"; exit 1)
	mkdir -p $(APPLIANCE_OUT)
	@echo "→ Pulling builder image $(APPLIANCE_BUILDER)…"
	@docker pull $(APPLIANCE_BUILDER) 2>/dev/null || \
	  echo "  (couldn't pull — assuming a local image with that tag exists)"
	@echo "→ Building appliance image (this takes ~5–10 min)…"
	docker run --rm --privileged \
	    -v $(PWD)/$(APPLIANCE_DIR):/work \
	    $(APPLIANCE_BUILDER) \
	    --output-directory=build --force build
	@raw=$$(ls $(APPLIANCE_OUT)/spatiumddi-appliance*.raw 2>/dev/null | head -1); \
	if [ -n "$$raw" ]; then \
	  ls -lh "$$raw"; \
	  echo ""; \
	  echo "✓ Built: $$raw"; \
	  echo "  Wrap as ISO with: make appliance-iso"; \
	else \
	  echo "✗ mkosi did not produce a .raw file in $(APPLIANCE_OUT) — check the log above."; \
	  exit 1; \
	fi
	@# qcow2 sidecar (disabled — not needed when shipping the ISO; ~1 GB
	@# disk + a qemu-img convert pass per build). Uncomment to restore.
	@# raw=$$(ls $(APPLIANCE_OUT)/spatiumddi-appliance*.raw 2>/dev/null | head -1); \
	# qcow2=$${raw%.raw}.qcow2; \
	# echo "→ Converting raw → qcow2…"; \
	# docker run --rm --entrypoint qemu-img \
	#     -v $(PWD)/$(APPLIANCE_OUT):/build \
	#     $(APPLIANCE_BUILDER) \
	#     convert -O qcow2 "/build/$$(basename $$raw)" "/build/$$(basename $$qcow2)"; \
	# ls -lh "$$qcow2"; \
	# echo "✓ Built: $$qcow2"; \
	# echo "  Boot it with: qemu-system-x86_64 -enable-kvm -m 4G -smp 2 \\"; \
	# echo "                -drive file=$$qcow2,if=virtio \\"; \
	# echo "                -nic user,hostfwd=tcp::8080-:80,hostfwd=tcp::2222-:22"

# Build the builder container locally (e.g. when iterating on its
# Dockerfile before pushing to ghcr.io).
appliance-builder:
	docker build -t spatiumddi-appliance-builder:dev $(APPLIANCE_DIR)/builder
	@echo ""
	@echo "✓ Built: spatiumddi-appliance-builder:dev"
	@echo "  Use it via: make appliance APPLIANCE_BUILDER=spatiumddi-appliance-builder:dev"

# Phase 2 — wrap the raw image as a hybrid USB/CD ISO. Requires
# `make appliance` to have run first (or for a raw image to exist
# in $(APPLIANCE_OUT)).
appliance-iso:
	@raw=$$(ls $(APPLIANCE_OUT)/spatiumddi-appliance*.raw 2>/dev/null | head -1); \
	if [ -z "$$raw" ]; then \
	  echo "✗ no raw image found in $(APPLIANCE_OUT) — run 'make appliance' first."; \
	  exit 1; \
	fi; \
	iso=$${raw%.raw}.iso; \
	echo "→ Wrapping $$raw → $$iso (hybrid USB/CD)…"; \
	docker run --rm --privileged \
	    --entrypoint /work/scripts/wrap-iso.sh \
	    -v $(PWD)/$(APPLIANCE_DIR):/work \
	    $(APPLIANCE_BUILDER) \
	    "/work/build/$$(basename $$raw)" \
	    "/work/build/$$(basename $$iso)"; \
	echo ""; \
	echo "✓ Built: $$iso"; \
	echo "  Burn to USB:  sudo dd if=$$iso of=/dev/sdX bs=4M conv=fsync"; \
	echo "  Or attach as CD-ROM in your hypervisor."

# Phase 8b-1 — build a slot image (.raw.xz of just the rootfs) suitable
# for sysupdate / spatium-upgrade-slot to write to an inactive A/B
# partition. Requires `make appliance` to have run first (consumes the
# raw output). Output: spatiumddi-appliance-slot-<version>.raw.xz +
# .sha256 next to the existing artifacts in $(APPLIANCE_OUT).
appliance-slot-image:
	@raw=$$(ls $(APPLIANCE_OUT)/spatiumddi-appliance*.raw 2>/dev/null | head -1); \
	if [ -z "$$raw" ]; then \
	  echo "✗ no raw image found in $(APPLIANCE_OUT) — run 'make appliance' first."; \
	  exit 1; \
	fi; \
	echo "→ Building slot image from $$raw …"; \
	docker run --rm --privileged \
	    --entrypoint /work/scripts/build-slot-image.sh \
	    -v $(PWD)/$(APPLIANCE_DIR):/work \
	    $(APPLIANCE_BUILDER) \
	    "/work/build/$$(basename $$raw)" \
	    "/work/build"

appliance-clean:
	@if [ -d $(APPLIANCE_OUT) ]; then \
	  echo "Removing $(APPLIANCE_OUT) (may need sudo — mkosi outputs are root-owned)"; \
	  rm -rf $(APPLIANCE_OUT) 2>/dev/null || sudo rm -rf $(APPLIANCE_OUT); \
	fi

# Bake every container image into the appliance rootfs overlay so the
# next ``make appliance`` ships them inside the ISO. See
# appliance/scripts/bake-images.sh for what's covered + how source
# selection (local :dev tags vs pulled :<calver> from ghcr) works.
#
# Source defaults to ``local`` (uses spatiumddi-*:dev) when
# SPATIUMDDI_VERSION is empty/dev; the release workflow sets
# SPATIUMDDI_VERSION=<calver> + BAKE_SOURCE=ghcr to pull the cut
# tag from the just-published images.
#
# Depends on ``build-supervisor`` so a stale supervisor image (the
# only service container not in docker-compose.yml + not rebuilt by
# ``make build`` before that target gained build-supervisor as a
# dep) can't slip into the baked overlay. The Docker layer cache
# makes the no-op case ~3 s; full rebuild ~30 s.
# Issue #183 — k3s migration. Pinned release tag the slot image ships.
# Bump in PRs alongside the slot image cut; the fetch script caches
# downloads under mkosi.extra/ keyed on this version so re-runs are
# cheap when nothing's changed.
K3S_VERSION ?= v1.35.4+k3s1

# Issue #183 Phase 1 — air-gap-ready k3s baking. Downloads the pinned
# k3s static binary + airgap images tarball + LICENSE into mkosi.extra/
# at build time. The slot rootfs carries everything; fielded appliance
# never reaches github.com on first boot. Idempotent (cache-stamped
# against K3S_VERSION). Runs ahead of ``appliance-bake-images`` in the
# composed targets so the mkosi build sees both image sets.
appliance-fetch-k3s:
	@K3S_VERSION="$(K3S_VERSION)" bash $(APPLIANCE_DIR)/scripts/fetch-k3s.sh

appliance-bake-images: build-supervisor
	@bash $(APPLIANCE_DIR)/scripts/bake-images.sh

# Convenience for fast laptop iteration on appliance-level changes
# (installer, firstboot, console dashboard, partition layout,
# networking stack) where you don't want to wait on the docker-image
# rebuild + bake cycle. Skips the bake — firstboot falls back to
# ``docker compose pull`` from ghcr.io on first boot. NOT how releases
# are cut (the release pipeline always bakes — #170 Phase A4).
appliance-dev-iso: appliance-clean-baked-images appliance-stamp-dev appliance-fetch-k3s appliance appliance-iso
	@echo ""
	@echo "✓ Dev-flavored appliance ISO ready at $(APPLIANCE_OUT)/spatiumddi-appliance_0.1.0.iso"
	@echo "  All container images (api / frontend / DNS / DHCP agents) pull from"
	@echo "  ghcr.io on first boot. Copy this ISO to your NAS / hypervisor library"
	@echo "  and boot a VM from it. Use 'make appliance-baked-iso' instead to"
	@echo "  produce a self-contained (air-gap-ready) ISO."

# Release-style local build — bakes every image at the local :dev tag
# (run ``make build`` first to produce them) into the rootfs, then
# builds the ISO + slot image. Mirrors what the release workflow
# produces, just driven by your local :dev images instead of ghcr.io's
# cut tag. ~1 GB larger than appliance-dev-iso.
appliance-baked-iso: appliance-stamp-dev appliance-fetch-k3s appliance-bake-images appliance appliance-iso appliance-slot-image
	@echo ""
	@echo "✓ Baked appliance ISO ready at $(APPLIANCE_OUT)/"
	@echo "  All container images embedded. Air-gap-ready. First boot does no docker pull."
	@echo "  Mirrors what the .github/workflows/release.yml ``build-appliance-iso`` job"
	@echo "  does on CI — stamps appliance-release with the local commit, bakes the"
	@echo "  docker overlay image, builds the raw image, wraps as ISO, builds the"
	@echo "  slot-upgrade .raw.xz + .sha256. Difference: BAKE_SOURCE=local (uses your"
	@echo "  ``make build`` :dev tags) vs CI's BAKE_SOURCE=ghcr (pulls the cut tag)."

# Wipe any baked overlay artefacts that a previous
# ``appliance-bake-images`` left under the mkosi.extra overlay. mkosi
# copies the overlay verbatim into the rootfs, so a stale image file
# would still be baked even after we dropped the ``appliance-bake-
# images`` dep from ``appliance-dev-iso``. The artefacts are
# gitignored.
appliance-clean-baked-images:
	@d=$(APPLIANCE_DIR)/mkosi.extra/usr/lib/spatiumddi; \
	if [ -f $$d/docker-overlay.img ] || [ -f $$d/docker-overlay.manifest ]; then \
	  echo "→ Cleaning previously-baked docker overlay from $$d …"; \
	  rm -f $$d/docker-overlay.img $$d/docker-overlay.manifest $$d/docker-overlay.version; \
	fi
	@# Pre-E1 tarball layout — wipe any leftovers from an older bake.
	@d2=$(APPLIANCE_DIR)/mkosi.extra/usr/local/share/spatiumddi/images; \
	if ls $$d2/*.tar.zst >/dev/null 2>&1 || [ -f $$d2/BAKED_AT ]; then \
	  echo "→ Cleaning pre-E1 image tarballs from $$d2 …"; \
	  rm -f $$d2/*.tar.zst $$d2/BAKED_AT $$d2/VERSION $$d2/MANIFEST; \
	  rmdir $$d2 2>/dev/null || true; \
	fi
	@# Issue #183 — k3s binary + airgap tarball + LICENSE artefacts.
	@# Force a re-fetch on the next ``appliance-fetch-k3s`` (which is
	@# itself cache-stamped against K3S_VERSION so this is rarely
	@# needed unless the operator is reproducing a build from
	@# scratch). Doesn't touch /etc/rancher/k3s/config.yaml since
	@# that's source-tracked, not generated.
	@d3=$(APPLIANCE_DIR)/mkosi.extra; \
	if [ -x $$d3/usr/local/bin/k3s ] || [ -f $$d3/usr/share/doc/k3s/.version ]; then \
	  echo "→ Cleaning baked k3s artefacts (forces fetch-k3s re-run) …"; \
	  rm -f $$d3/usr/local/bin/k3s; \
	  rm -f $$d3/usr/local/bin/kubectl $$d3/usr/local/bin/crictl $$d3/usr/local/bin/ctr; \
	  rm -f $$d3/var/lib/rancher/k3s/agent/images/*.tar.zst; \
	  rm -f $$d3/usr/share/doc/k3s/LICENSE \
	        $$d3/usr/share/doc/k3s/NOTICE \
	        $$d3/usr/share/doc/k3s/k3s-images.txt \
	        $$d3/usr/share/doc/k3s/.version; \
	fi

# Stamp a dev version into mkosi.extra/etc/spatiumddi/appliance-release
# so a freshly-installed local-build appliance reports a non-empty
# ``installed_appliance_version`` in the Fleet view. The release
# workflow does the same thing in CI with a CalVer tag; for local
# builds we use ``dev-<short-sha>`` so each WIP ISO has a unique stamp
# tied to the commit it came from. The file is gitignored — see
# ``appliance/.gitignore``.
appliance-stamp-dev:
	@f=$(APPLIANCE_DIR)/mkosi.extra/etc/spatiumddi/appliance-release; \
	mkdir -p $$(dirname $$f); \
	{ \
	  echo "# Generated by ``make appliance-stamp-dev`` for local-build ISOs."; \
	  echo "# CI release builds overwrite this with the real CalVer tag."; \
	  echo "APPLIANCE_VERSION=\"$(SPATIUMDDI_VERSION)\""; \
	} > $$f; \
	echo "→ Stamped appliance-release: $$(cat $$f | grep APPLIANCE_VERSION)"
