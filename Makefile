.PHONY: help up down dev build migrate lint test lint-backend lint-frontend test-backend \
        ci ci-backend-lint ci-frontend-lint ci-frontend-build screenshots \
        appliance appliance-builder appliance-iso appliance-clean

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
	cd $(BACKEND_DIR) && python -m pytest

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

appliance-clean:
	@if [ -d $(APPLIANCE_OUT) ]; then \
	  echo "Removing $(APPLIANCE_OUT) (may need sudo — mkosi outputs are root-owned)"; \
	  rm -rf $(APPLIANCE_OUT) 2>/dev/null || sudo rm -rf $(APPLIANCE_OUT); \
	fi
