# Development Guide

> Coding standards, the lint/test stack, the CI gate, the migration
> workflow, and the conventions every change must follow. Read
> [`CONTRIBUTING.md`](../CONTRIBUTING.md) first for the high-level
> contribution flow; this doc is the detailed reference behind it.

The canonical spec for *what* the project is and *why* decisions were
made lives in [`CLAUDE.md`](../CLAUDE.md). This guide covers *how* to
build, test, and ship changes.

---

## 1. Prerequisites

- **Docker Engine 25+** and **Docker Compose v2.20+** — the backend, its
  tests, and the linters all run inside containers, so you don't need a
  local Python toolchain to contribute backend code.
- **Node 20+** (CI builds the frontend on **Node 22**) — only needed for
  the frontend dev loop and the frontend lint/build jobs, which run on
  the host.
- **Python 3.12** is the pinned interpreter version (`backend/pyproject.toml`
  → `target-version = "py312"`). You only need it on the host if you want
  to run the migration-shape linter or backend tooling outside Docker.

---

## 2. First-Time Setup

```bash
git clone https://github.com/spatiumddi/spatiumddi.git
cd spatiumddi

# Create your environment file — at minimum change POSTGRES_PASSWORD and SECRET_KEY
cp .env.example .env
#   SECRET_KEY: openssl rand -hex 32
#   POSTGRES_PASSWORD: any non-default value

make build      # build all Docker images
make migrate    # apply Alembic migrations inside the api container
make up         # start the full stack (production images)
#   — or —
make dev        # start the dev stack with hot-reload (docker-compose.dev.yml)
```

The frontend is served on `http://localhost:8077` by default
(`HTTP_PORT` in `.env`; the container listens on port 80). See
[`deployment/DOCKER.md`](deployment/DOCKER.md) for the full port
reference and TLS setup.

**Default login:** `admin` / `admin`. On a non-demo install the seeded
admin carries `force_password_change=True`, so the first login redirects
to a password-change form (`backend/app/main.py` → `_seed_default_admin`).

To run the DNS and/or DHCP service containers alongside the control
plane, enable the matching Compose profiles:

```bash
COMPOSE_PROFILES=dns,dhcp make up
```

### Frontend-only dev loop

The fastest UI iteration loop runs Vite directly on the host (React 18 +
TypeScript + Vite) against the containerized API:

```bash
cd frontend
npm install
npm run dev
```

If you get locked out of the admin account, see the password-reset
recipe in [`CLAUDE.md`](../CLAUDE.md#development-commands) and
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

---

## 3. Coding Standards

### Backend (Python)

| Tool | Config | Notes |
|---|---|---|
| `ruff` | `[tool.ruff]` in `backend/pyproject.toml` | `line-length = 100`, `target-version = "py312"`. E501 is delegated to black. |
| `black` | `[tool.black]` | `line-length = 100`, `target-version = ["py312"]`. Black owns formatting; ruff owns lint rules. |
| `mypy` | `[tool.mypy]` | `python_version = "3.12"`, `strict = false` (default mode), `plugins = ["pydantic.mypy"]`. |

Run them via Docker so you get the exact pinned versions:

```bash
make lint          # backend (ruff + black + mypy) AND frontend (eslint + prettier)
make lint-backend  # backend only
```

> **Note:** the dev container ships whatever ruff/black/mypy versions
> were installed when the image was built, which can drift from the
> versions CI installs from `pyproject.toml`'s `[dev]` extra. If you have
> a host Python 3.12 with the `[dev]` extras installed, running ruff,
> black, and mypy on the host gives the closest match to the CI gate.

### Frontend (TypeScript)

| Tool | npm script | Notes |
|---|---|---|
| `eslint` | `npm run lint` | Runs with `--max-warnings 0` — a warning fails the job. |
| `prettier` | `npm run format:check` | Use `npm run format` to auto-fix. |
| `tsc` | `npm run typecheck` | `tsc --noEmit`, no emit. |

```bash
make lint-frontend   # eslint + prettier check
```

---

## 4. The Absolute Non-Negotiables

These rules from [`CLAUDE.md`](../CLAUDE.md#absolute-non-negotiables)
apply to every change. They are reproduced here as a quick checklist;
the canonical wording lives in `CLAUDE.md`.

1. **API-first** — every UI action must also work via the REST API.
2. **Async throughout** — no synchronous DB or network calls in request
   handlers.
3. **Permissions enforced server-side** — the API validates authorization
   independently of the UI (see [`PERMISSIONS.md`](PERMISSIONS.md)).
4. **Audit everything** — every mutation is written to the append-only
   `audit_log` *before* the response is returned.
5. **Config caching on agents** — DHCP/DNS containers cache their
   last-known-good config locally and keep running if the control plane
   is unreachable.
6. **No hardcoded secrets** — credentials come from env vars or mounted
   secrets; secrets at rest are Fernet-encrypted.
7. **Structured logs always** — every log line is valid JSON with
   `timestamp`, `level`, `service`, `request_id`.
8. **Incremental DNS updates** — record changes use RFC 2136 DDNS or the
   driver API, never a full server restart.
9. **Idempotent tasks** — every Celery task must be safe to retry.
10. **Driver abstraction** — DHCP/DNS backend logic never leaks into the
    service layer (`backend/app/drivers/{dns,dhcp}/`).
11. **Multi-arch builds** — all Docker images support `linux/amd64` and
    `linux/arm64` (see §9).
12. **K8s manifests stay current** — when you add or change a service,
    update `k8s/base/` and `k8s/README.md`.
13. **MCP coverage for new features** — a new REST resource also gets
    matching Operator-Copilot MCP tools, with the default-enabled state
    an explicit decision per tool.
14. **Feature-module gating** — a new top-level resource family is
    evaluated for a togglable feature module (`app.services.feature_modules`).
15. **New integrations show up on the Dashboard** — wire a new
    read-only integration mirror into both the IPAM `IntegrationsPanel`
    and the dedicated Integrations dashboard tab.
16. **Per-role node-label gating** — a new top-level Helm workload gates
    scheduling on a per-role node label, not on a chart-render toggle.

When in doubt, read the full text and the surrounding "Cross-cutting
Patterns" section in `CLAUDE.md` before you start.

---

## 5. Tests

The backend test suite runs against a **real PostgreSQL instance** (not
mocks) so ORM and query issues surface early
(`backend/tests/conftest.py`).

```bash
make test                                  # full suite — pytest -n auto inside the api container
make test-one T=tests/test_health.py::test_liveness   # a single test, run serially
```

The **frontend** has a test suite too, added with the enrolment QR
(#906) and run by the `frontend-lint` CI job:

```bash
cd frontend && npm test          # vitest, jsdom, run once
cd frontend && npm test -- --watch
```

It exists for the class of bug review and `tsc` cannot catch — the QR
tests decode the image the component actually renders, because a
transposed row or an inverted polarity produces a code that looks
entirely normal and scans as nothing.

### Parallelism + per-worker databases

`make test` runs `python -m pytest -n auto` (pytest-xdist), one worker
per CPU. Each worker **carves its own throwaway database** off the same
Postgres instance — `spatiumddi_test_gw0`, `spatiumddi_test_gw1`, … — so
the session-scoped schema reset and per-test `TRUNCATE` can't step on
another worker's data. The non-xdist case (`pytest` with no `-n`) falls
back to the unsuffixed base database name.

`conftest.py` reads `TEST_DATABASE_URL` (default
`postgresql+asyncpg://spatiumddi:changeme@localhost:5432/spatiumddi_test`)
and rewrites `DATABASE_URL` to the per-worker URL **before** any `app.*`
import, so module-level engines and Celery `task_session` engines land in
the right database. `make test` runs inside the dev-compose `api`
container, which has `pytest` baked into its `build.target: dev` image and
`TEST_DATABASE_URL` pre-set against the dev Postgres service.

Because a single test doesn't benefit from xdist overhead, `make test-one`
runs serially (`-v`, no `-n`).

### What a new endpoint needs

Per [`CONTRIBUTING.md`](../CONTRIBUTING.md), every new API endpoint needs
tests covering the **success**, **unauthorized**, and **validation-error**
cases.

---

## 6. CI Parity — `make ci`

`make ci` runs the same lint/typecheck/build jobs GitHub Actions runs on
every push and pull request. Run it locally before pushing.

```bash
make ci
```

It chains five targets:

| `make` target | What it runs |
|---|---|
| `ci-backend-lint` | `ruff check app tests`, `black --check app tests`, `mypy app` (inside the api container; installs ruff/black/mypy on first run if missing) |
| `ci-frontend-lint` | `npm run lint && npm run format:check && npm run typecheck` (CI's `frontend-lint` job also runs `npm test`) |
| `ci-frontend-build` | `npm run build` |
| `charts-lint` | The `Charts — Lint & Template` gate, in a helm container (see the job table below) |
| `perf-test` | `python -m pytest perf` in a python container |

`make ci` requires the dev stack to be running (the backend checks
execute inside the api container) and Node 20+ on the host. It does **not**
run the backend tests — use `make test` separately for those.

### The actual CI jobs

The CI workflow is [`.github/workflows/ci.yml`](../.github/workflows/ci.yml),
triggered on push to `main` and on every pull request:

| Job | What it does |
|---|---|
| **Backend — Lint & Type Check** (`backend-lint`) | `pip install -e ".[dev]"` on Python 3.12, then `ruff check`, `black --check`, `mypy app`, **plus the migration-shape linter** (`python3 scripts/lint_migrations.py` — see §8). |
| **Backend — Tests** (`backend-test`) | A required-check aggregator over **eight** parallel `backend-test-shard` jobs. Each shard spins up `postgres:16-alpine` + `redis:8.8-alpine` services, runs `alembic upgrade head`, then `pytest -n auto --splits 8 --group N` (pytest-split selects the shard's slice; `-n auto` parallelizes it across the runner's vCPUs, each xdist worker on its own `spatiumddi_test_gw<N>` DB). The aggregator passes only if all eight shards pass. |
| **Frontend — Lint & Type Check** (`frontend-lint`) | Node 22, `npm install`, then `npm run lint`, `npm run format:check`, `npm run typecheck`, `npm test`. |
| **Frontend — Build** (`frontend-build`) | Node 22, `npm install`, `npm run build`. |
| **Charts — Lint & Template** (`charts-lint`) | Helm 3.20 + a checksum-pinned kubeconform. `helm lint --strict` both charts at defaults and with every role / feature toggle on, `helm template` six value sets (umbrella: defaults, all-on, external DB + Redis, the CNPG + Sentinel HA shape; appliance: defaults, all-on, the single-node full-stack shape), `kubeconform -strict` against the Kubernetes 1.31 + CRD-catalog schemas, and `.github/scripts/chart-no-besteffort.py` on every render (#965 — no serving container may lack CPU + memory requests or limits; init containers are exempt, they finish before the pod serves) plus `chart-toggle-coverage.py`, which fails if a template is gated on a values key none of the value sets flips. Rendered manifests are uploaded as the `rendered-charts` artifact. Until #966 nothing on a PR parsed the appliance chart at all; it was first read by helm during the release. `make charts-lint` runs the same script in a helm container. |
| **Perf — Tests** (`perf-test`) | Python 3.12 + pytest, `python -m pytest perf`. Hermetic (fake sockets, no network, no appliance). Tests only — `perf/` is outside the Backend Lint scope and carries pre-existing ruff/black drift (#968). `make perf-test` reproduces it. |

Branch protection on `main` gates on these checks. `make ci` reproduces
the two lint jobs, the frontend build, the chart gate and the perf tests
locally (the last two via Docker, so neither helm nor kubeconform needs to
be on the host); `make test` reproduces the backend test job
(single-runner rather than sharded).

### What runs when — the trigger policy

Every workflow trigger should answer "what would we miss without it?".
Three of the four events have distinct jobs:

| Event | Runs | Why |
|---|---|---|
| **Pull request** | `ci.yml` (backend shards gated on change detection — see below), the per-image builds (single-arch + **Trivy**), `agent-e2e.yml` | Where correctness is gated. Nothing merges without it. |
| **Push to `main`** | `ci.yml`, `docs-publish.yml`, `build-appliance-builder.yml` | Post-merge safety net + the two things that must be *published* from `main`. |
| **Release tag** | `release.yml` only | Builds and publishes every image multi-arch, the chart, and the appliance ISO. |
| **Schedule** | `nightly.yml`, `trivy-scheduled.yml`, `prune-release-assets.yml` | Work that is about *elapsed time*, not about a change. |

Two rules follow, and both were violations once:

- **Per-image build workflows do not trigger on push.** On a tag they raced
  `release.yml`, which builds the same image and pushes the same
  `:<VERSION>` tag — two concurrent multi-arch builds of identical source,
  with the winner decided by whichever finished last, on a release
  artifact. On `main` they published `:main` and `:sha-<short>` that
  nothing consumes (no chart, compose file or manifest pins them, and
  `agent-e2e.yml` deliberately builds from PR source instead) *and* skipped
  Trivy on that path. Both jobs are covered better elsewhere: releases by
  `release.yml`, "build `main` periodically" by `nightly.yml` — which
  scans before it publishes.
- **`build-appliance-builder.yml` is the exception and must stay on
  `main`.** `release.yml`'s ISO job and the `APPLIANCE_BUILDER` default in
  the Makefile both pull `appliance-builder:latest`; that tag is how the
  builder image gets published at all.

`ci.yml` stays on push to `main` on purpose. It is redundant with the PR
run in the common case — GitHub tests the merge result — but it validates
the actual squashed commit, and it is the only check that covers a direct
push or a merge made with `--admin` while shards were still pending.

### Skipping the backend shards on a PR that can't affect them

The workflow has no `paths-ignore` and never will: a skipped workflow run
posts none of the check-runs `protect-main` waits for, so a docs-only PR
would sit blocked forever. What [#813](https://github.com/spatiumddi/spatiumddi/issues/813)
added instead is **per-job** change detection, which keeps the workflow —
and every check name — unconditional:

1. A `changes` job diffs the PR against its merge base and pipes the file
   list through [`.github/scripts/ci-backend-relevant.sh`](../.github/scripts/ci-backend-relevant.sh).
2. The 8 `backend-test-shard` jobs are gated on its output.
3. The `Backend — Tests` aggregator treats a skip as a pass — but **only a
   skip that detection asked for**. An empty output (the `changes` job
   itself failed, taking the shards with it) fails the aggregator.

Push to `main` always runs the full suite; the filter applies to
`pull_request` only.

The path list is a **deny-list**, and that shape is the point. An
allow-list (`run when backend/** changes`) fails silently in the worst
direction — a new top-level directory nobody added stops running the
tests, and the tell is a green PR that was never tested. A deny-list makes
the cost of forgetting a few runner-minutes instead.

Two directories look unrelated to the backend and are deliberately *not*
deny-listed, because backend tests load files out of them by path:

| Test | Reads |
|---|---|
| `test_spatium_console.py` (52 tests) | `appliance/mkosi.extra/usr/local/bin/spatium-console` |
| `test_appliance_firewall_render.py` | `agent/supervisor/spatium_supervisor/firewall_renderer.py` |

Check for new cross-boundary reads before widening the list.
`backend/tests/test_ci_backend_relevant.py` pins the whole table, so
edits to the script are exercised without pushing a branch.

Not yet filtered, and each for a reason: the frontend jobs and
`Backend — Lint` are `protect-main`'s **required** checks, and a skipped
required check is exactly the blocked-PR failure mode above; `agent-test`
and `appliance-test` are seconds each, so the detection would cost more
than it saves.

### Nightly builds

[`.github/workflows/nightly.yml`](../.github/workflows/nightly.yml) builds
and publishes every image from `main` at **02:23 America/New_York** (the
team works Eastern evenings, so the nightly runs after the late pushes —
two UTC crons plus a wall-clock gate keep that true across DST). It exists
because
nothing else builds the *release* image except a release — which is how
[#732](https://github.com/spatiumddi/spatiumddi/issues/732) shipped an api
image carrying pytest as root, undetected until the next release cut it.
The nightly also runs the same Trivy gate CI uses on PRs, so a base-image
CVE surfaces the night the Dockerfile changes rather than at the next tag.

To run current `main` without cutting a release:

```bash
docker pull ghcr.io/spatiumddi/spatiumddi-api:nightly
```

| Tag | Meaning |
|---|---|
| `:nightly` | The most recent successful nightly. Mutable — it moves. |
| `:nightly-YYYYMMDD` | Immutable snapshot of that build. The newest **7** are kept, the rest pruned in the same run. |

The dated ring exists so "nightly broke X" is answerable: without it the
pointer has already rolled and there is nothing to compare against, and a
broken nightly leaves no fallback. Seven is deliberately small — anyone
pulling `:nightly` never sees the rest.

A run is **skipped entirely when `main` has not moved** since the last
nightly, so the seven tags cover seven *changed* days rather than seven
calendar days.

Each nightly also publishes a **pre-release tagged `nightly-YYYY.MM.DD`**
(CalVer date, prefixed so it can never match `release.yml`'s bare-CalVer
tag trigger), carrying that build's appliance ISO + slot-upgrade image and
a `built-from:` line recording the exact commit. Pre-releases older than
**7 days** are deleted automatically, git tag included, so the Releases
page shows at most a week of dated nightlies alongside the real releases
(`prune-release-assets.sh` skips `nightly-*` — the nightly owns its own
retention). A failing nightly opens or comments on a single reused
tracking issue rather than one per night.

`workflow_dispatch` takes `force` (build anyway) and `dry_run` (build and
scan, push and prune nothing).

**Nightly is never tagged `:latest`.** That tag means "latest release".

---

## 7. Repo Layout

A condensed map (full version in [`CLAUDE.md`](../CLAUDE.md#repo-layout)):

```
backend/app/
  api/v1/        HTTP route handlers (ipam/, dns/, dhcp/, auth/, …)
  models/        SQLAlchemy 2.x async models
  services/      Business logic (dns/, dhcp/, dns_io/, ipam_io/, ai/)
  drivers/       DNS + DHCP backend abstraction + concrete impls
  tasks/         Celery tasks
  core/          permissions.py, crypto.py, auth/, audit, …
  config.py, db.py, main.py, celery_app.py
backend/alembic/ Migrations (tracked in git)
backend/tests/   pytest suite (conftest.py carves per-worker DBs)
frontend/src/
  pages/         Top-level routes
  components/     Shared UI; shadcn-style primitives under components/ui/
  lib/api.ts     API clients
agent/           Standalone DNS / DHCP agents + supervisor
charts/          Helm charts
k8s/             Kubernetes manifests
scripts/         lint_migrations.py, seed_demo.py, screenshots/, …
```

---

## 8. Migration Workflow

Models are SQLAlchemy 2.x async (`backend/app/models/`); schema changes
are versioned with Alembic in `backend/alembic/`.

```bash
# Generate a migration by autogenerating against the models
make migration MSG="add foo column"

# Apply pending migrations
make migrate
```

`make migration` runs `alembic revision --autogenerate` inside the api
container; review the generated file in `backend/alembic/versions/` —
autogenerate is a starting point, not a final answer.

### Conventions that bite if you miss them

- **Hand-written `create_table` for a `TimestampMixin` model** must set
  `server_default=text("now()")` on `created_at`/`modified_at`. Tests pass
  via `create_all`, but a fresh-install migration NULL-violates without it.
- **Verify the real Alembic head before setting `down_revision`.** A
  recently-dated file is not necessarily the head — run Alembic's
  `get_heads()` (or `alembic heads`); picking the wrong parent creates two
  heads and breaks CI.

### Expand/contract — the migration-shape linter

SpatiumDDI runs rolling N→N+1 upgrades across multiple control-plane
nodes, so during the mixed-version window the database is shared between
old (N-1) and new (N) application code. A destructive migration that
drops a column N-1 still reads will crash the old pods mid-upgrade.

The contract: **every migration must be safe against both N-1 and N
code** — expand in release N (add the new column/table/dual-write), drop
the old one in release N+1.

`scripts/lint_migrations.py` (stdlib-only, runs in the `backend-lint` CI
job) flags destructive ops (`drop_column` / `drop_table` in downgrades,
etc.) so they get the two-release treatment. Historical violations are
captured in a baseline file at
`backend/alembic/migrations_lint_baseline.txt`. If you intentionally add
a finding the linter should accept, regenerate the baseline and commit it:

```bash
python3 scripts/lint_migrations.py --baseline   # rewrite the baseline
python3 scripts/lint_migrations.py              # what CI runs (exit 1 on non-baselined findings)
python3 scripts/lint_migrations.py --show       # every finding, baselined or not
```

---

## 9. Multi-Arch Image Builds

Every Docker image must support **`linux/amd64` and `linux/arm64`**
(non-negotiable #11). The release pipeline
([`.github/workflows/release.yml`](../.github/workflows/release.yml))
builds each image with `docker/setup-qemu-action` +
`docker/setup-buildx-action` and `platforms: linux/amd64,linux/arm64`.
Local `make build` produces single-arch images for your host — the
multi-arch fan-out happens in CI on a tagged release.

---

## 10. Branch & PR Conventions

- **Branch from `main`.** The project uses one branch per issue, named
  `issue-NNN` (keep every phase of a multi-phase change on the same
  branch). Squash-merge back to `main`.
- **Conventional-commit PR titles:** `<type>(<scope>): <short summary>`
  where `type` ∈ `feat, fix, docs, refactor, perf, test, build, ci,
  chore` and `scope` ∈ `ipam, dns, dhcp, auth, rbac, audit, ui, api, k8s,
  compose, agent-dns, agent-dhcp` (see
  [`.github/pull_request_template.md`](../.github/pull_request_template.md)).
- **Fill in the PR template** — Summary, Area, Test plan, and
  Migration/deployment notes. Don't leave them blank.
- **Link issues with the keyword per issue:**
  `Closes #123, Closes #456` — a comma-list like `Closes #123, #456` only
  closes the first.
- **Run `make ci` before pushing**, and `make test` for any
  behavioral change.

### Pre-PR checklist

- [ ] `make ci` passes (backend lint, frontend lint + typecheck, frontend build)
- [ ] `make test` passes (new/changed behavior is covered)
- [ ] Mutations write to `audit_log` before returning
- [ ] New endpoints have success / unauthorized / validation-error tests
- [ ] Alembic migration included if models changed (and it's expand/contract-safe)
- [ ] `k8s/base/` + `k8s/README.md` updated if a service or its env changed
- [ ] Matching MCP tools added for any new REST surface
- [ ] Third-party components added to the root `NOTICE` if you bundled any

---

## 11. Security Disclosure

Do **not** file security vulnerabilities as public issues. Use
[GitHub Security Advisories](https://github.com/spatiumddi/spatiumddi/security/advisories/new)
for private disclosure, as described in
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

---

## See Also

- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — contribution flow, code
  standards summary, PR process
- [`CLAUDE.md`](../CLAUDE.md) — the canonical project spec, full
  non-negotiables, and cross-cutting patterns
- [`PERMISSIONS.md`](PERMISSIONS.md) — the RBAC permission grammar enforced
  server-side
- [`deployment/DOCKER.md`](deployment/DOCKER.md) — Compose setup, ports, TLS
- [`OBSERVABILITY.md`](OBSERVABILITY.md) — structured logging, metrics,
  health dashboard
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — recovery recipes
