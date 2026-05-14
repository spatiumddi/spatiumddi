.PHONY: help up down dev build migrate lint test lint-backend lint-frontend test-backend \
        ci ci-backend-lint ci-frontend-lint ci-frontend-build screenshots \
        appliance appliance-builder appliance-iso appliance-clean \
        appliance-bake-images appliance-clean-baked-images appliance-dev-iso \
        appliance-baked-iso appliance-stamp-dev appliance-slot-image

# ── Configuration ──────────────────────────────────────────────────────────────
COMPOSE        = docker compose
COMPOSE_DEV    = docker compose -f docker-compose.yml -f docker-compose.dev.yml
BACKEND_DIR    = backend
FRONTEND_DIR   = frontend

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

build:
	$(COMPOSE) build

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
test: test-backend

test-backend:
	cd $(BACKEND_DIR) && python -m pytest -n auto

test-one:
	@test -n "$(T)" || (echo "Usage: make test-one T=tests/test_health.py::test_liveness"; exit 1)
	cd $(BACKEND_DIR) && python -m pytest $(T) -v

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
	  qcow2=$${raw%.raw}.qcow2; \
	  echo "→ Converting raw → qcow2…"; \
	  docker run --rm --entrypoint qemu-img \
	      -v $(PWD)/$(APPLIANCE_OUT):/build \
	      $(APPLIANCE_BUILDER) \
	      convert -O qcow2 "/build/$$(basename $$raw)" "/build/$$(basename $$qcow2)"; \
	  ls -lh "$$qcow2"; \
	  echo ""; \
	  echo "✓ Built: $$qcow2"; \
	  echo "  Boot it with: qemu-system-x86_64 -enable-kvm -m 4G -smp 2 \\"; \
	  echo "                -drive file=$$qcow2,if=virtio \\"; \
	  echo "                -nic user,hostfwd=tcp::8080-:80,hostfwd=tcp::2222-:22"; \
	else \
	  echo "✗ mkosi did not produce a .raw file in $(APPLIANCE_OUT) — check the log above."; \
	  exit 1; \
	fi

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
appliance-bake-images:
	@bash $(APPLIANCE_DIR)/scripts/bake-images.sh

# Convenience for fast laptop iteration on appliance-level changes
# (installer, firstboot, console dashboard, partition layout,
# networking stack) where you don't want to wait on the docker-image
# rebuild + bake cycle. Skips the bake — firstboot falls back to
# ``docker compose pull`` from ghcr.io on first boot. NOT how releases
# are cut (the release pipeline always bakes — #170 Phase A4).
appliance-dev-iso: appliance-clean-baked-images appliance-stamp-dev appliance appliance-iso
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
appliance-baked-iso: appliance-bake-images appliance appliance-iso appliance-slot-image
	@echo ""
	@echo "✓ Baked appliance ISO ready at $(APPLIANCE_OUT)/"
	@echo "  All container images embedded. Air-gap-ready. First boot does no docker pull."

# Wipe any tarballs that a previous ``appliance-bake-images`` left
# under the mkosi.extra overlay. mkosi copies the overlay verbatim
# into the rootfs, so leftover tarballs would still be baked even
# after we dropped the ``appliance-bake-images`` dep from
# ``appliance-dev-iso``. The directory itself is gitignored.
appliance-clean-baked-images:
	@d=$(APPLIANCE_DIR)/mkosi.extra/usr/local/share/spatiumddi/images; \
	if ls $$d/*.tar.zst >/dev/null 2>&1 || [ -f $$d/BAKED_AT ]; then \
	  echo "→ Cleaning previously-baked image tarballs from $$d …"; \
	  rm -f $$d/*.tar.zst $$d/BAKED_AT; \
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
	sha=$$(git rev-parse --short HEAD 2>/dev/null || echo unknown); \
	mkdir -p $$(dirname $$f); \
	{ \
	  echo "# Generated by ``make appliance-stamp-dev`` for local-build ISOs."; \
	  echo "# CI release builds overwrite this with the real CalVer tag."; \
	  echo "APPLIANCE_VERSION=\"dev-$$sha\""; \
	} > $$f; \
	echo "→ Stamped appliance-release: $$(cat $$f | grep APPLIANCE_VERSION)"
