.PHONY: help up down dev build migrate lint test lint-backend lint-frontend test-backend \
        ci ci-backend-lint ci-frontend-lint ci-frontend-build screenshots

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
