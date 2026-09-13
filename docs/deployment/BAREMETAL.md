# Bare-Metal Deployment

> **There are no Ansible playbooks and no systemd-native / `.deb` / `.rpm`
> package install path in this repo today.** A "Bare metal / VM (Ansible)"
> option appears as **📋 Planned** in the [README deployment table](https://github.com/spatiumddi/spatiumddi/blob/main/README.md#deployment-options),
> but it is not implemented — do not expect playbooks under `ansible/` or
> `playbooks/` (there are none). The single `GET /api/v1/ansible/inventory`
> endpoint in the codebase is an Ansible **dynamic-inventory** source for
> consuming SpatiumDDI data, not an installer.

If you want SpatiumDDI running on a physical machine or a plain VM, you have
three real, supported paths today. Pick by how much of the OS you want to own:

| Path | What you manage | Start here |
|---|---|---|
| **Docker Compose on a host/VM** | Your own OS + Docker; SpatiumDDI runs in containers | [`DOCKER.md`](DOCKER.md) |
| **HA PostgreSQL (Patroni) under Compose** | Same, plus a 3-node Patroni cluster for the database | [§ HA PostgreSQL](#ha-postgresql-patroni-under-docker-compose) below |
| **OS appliance image (true bare-metal-OS install)** | Nothing — boot the image, configure via web UI | [`APPLIANCE.md`](APPLIANCE.md) |

The **OS appliance is the supported bare-metal-OS path**: a bootable Debian 13
image with k3s and the full SpatiumDDI stack pre-baked, installed straight onto
the disk with no prior OS or container-runtime setup.

---

## 1. Docker Compose on a bare-metal host or VM

This is the canonical way to run SpatiumDDI on a machine you already own. Install
Docker Engine + Compose v2 on your host (any Linux distro Docker supports), then
follow the standard Compose guide:

- **[`DOCKER.md`](DOCKER.md)** — prerequisites, port reference, first-time setup
  (`cp .env.example .env`, `docker compose build`, `docker compose run --rm migrate`,
  `docker compose up -d`), TLS, and password reset.

Everything in that guide applies unchanged on bare metal — the only difference
from a cloud host is that you provide the box. The DNS and DHCP service containers
are opt-in via Compose profiles (`COMPOSE_PROFILES=dns,dhcp docker compose up -d`),
which is useful on bare metal where you may want the host's real NIC bound to a
DHCP server.

---

## 2. HA PostgreSQL (Patroni) under Docker Compose

For a single-host stack the bundled `postgres` service is enough. If you want the
database to survive a node failure on a bare-metal/VM deployment, the repo ships a
**reference** Patroni overlay:

- **[`k8s/ha/postgres-docker-compose.yaml`](https://github.com/spatiumddi/spatiumddi/blob/main/k8s/ha/postgres-docker-compose.yaml)**
  — a 3-node Patroni PostgreSQL cluster (1 leader + 2 replicas) with a 3-node etcd
  cluster for consensus and an HAProxy front-end. The application connects to
  HAProxy on port **5000** (read/write → primary) with **5001** for read-only
  replicas; no application code changes are needed.

It is meant to **replace** the single `postgres` service in the base
`docker-compose.yml`, layered on as a second Compose file:

```bash
docker compose -f docker-compose.yml -f k8s/ha/postgres-docker-compose.yaml up -d
```

Notes before you use it:

- This is a **reference template, not a turnkey HA cluster.** The HAProxy service
  mounts `./haproxy.cfg`, which is **not** included in the repo — you must supply
  your own HAProxy config that fronts the three Patroni REST APIs (`pg1`/`pg2`/`pg3`
  on `:8008`) and routes `:5000` to the current primary.
  **If you are upgrading an existing deployment**, note that the HAProxy image
  moved from `2.9-alpine` to `3.4-alpine` in
  [#975](https://github.com/spatiumddi/spatiumddi/issues/975) — 2.9 was a
  short-lived non-LTS branch with no release since March 2025, and 3.4 is the
  current LTS (`haproxy:lts-alpine` and `haproxy:3.4-alpine` are the same
  image). Your `haproxy.cfg` is yours, so **we cannot validate it for you**:
  HAProxy 3.x removed keywords that were deprecated through the 2.x series, and
  a config that only warned on 2.9 can be a hard startup failure on 3.4. The
  canonical Patroni configuration from the Patroni documentation — `mode tcp`,
  `option httpchk`, `http-check expect status 200`, `default-server ... on-marked-down
  shutdown-sessions` — was verified unchanged on both versions, so a config of
  that shape needs no edit. Check yours before deploying:

  ```bash
  docker run --rm -v "$PWD/haproxy.cfg:/c.cfg:ro" haproxy:3.4-alpine \
      haproxy -c -f /c.cfg
  ```
- The three Patroni services run **`patroni-spatiumddi:latest`, which is built by
  this overlay, not pulled.** The `build.dockerfile_inline` block in the same file
  layers Patroni onto `postgres:16-alpine` via pip, so the image exists only on the
  host that ran `docker compose`; nothing publishes it and the name resolves in no
  registry. Two consequences worth knowing: the `latest` tag is local and
  meaningless as a version (recorded as such in
  [`versions.json`](https://github.com/spatiumddi/spatiumddi/blob/main/versions.json),
  so it stops reading like a pin somebody forgot), and **the Patroni version is
  whatever pip resolves at build time** — pin it in the inline Dockerfile if you
  need two hosts to agree, because `docker compose build` on different days will
  not.
- Point the application at HAProxy by setting `DATABASE_URL` to the HAProxy
  endpoint (e.g. `postgresql+asyncpg://spatiumddi:<password>@haproxy:5000/spatiumddi`)
  instead of the default single-node `postgres` host.
- Override the credentials via `POSTGRES_PASSWORD`, `POSTGRES_SUPERUSER_PASSWORD`,
  and `POSTGRES_REPLICATION_PASSWORD` — do not ship the in-file defaults.
- The `spatiumddi` Docker network is declared `external: true`, so it must already
  exist (the base `docker-compose.yml` creates it).

This is the same Patroni reference called out as **Topology 4 — HA control plane**
in [`TOPOLOGIES.md`](TOPOLOGIES.md), which has the full HA picture (Patroni +
Redis Sentinel + API hosts behind a load balancer). If you are on the OS appliance
instead, HA PostgreSQL is provided automatically via CloudNativePG when you promote
control-plane members — you do not run Patroni there.

---

## 3. OS appliance — the supported bare-metal-OS install

If you want SpatiumDDI to **own the whole machine** (no host OS to maintain, no
Docker to install), use the OS appliance image. It is a bootable Debian 13 image
that installs straight onto bare-metal disk and runs the full stack on embedded
k3s, configured entirely from the web UI and the dedicated `/appliance`
management hub.

- **[`APPLIANCE.md`](APPLIANCE.md)** — image layout, the installer wizard, install
  roles (control plane / appliance agent), atomic A/B slot upgrades, and the
  in-UI management surface (TLS, releases, pods, logs, diagnostics).

Build a local ISO with `make appliance-dev-iso`.

---

## Where to go next

- **[`DOCKER.md`](DOCKER.md)** — the canonical container guide; use it for any
  bare-metal-host-with-Docker deployment.
- **[`APPLIANCE.md`](APPLIANCE.md)** — the supported bare-metal-OS install.
- **[`TOPOLOGIES.md`](TOPOLOGIES.md)** — six reference production topologies,
  including the HA control plane (Topology 4) that the Patroni overlay above
  belongs to.
- **[`KUBERNETES.md`](KUBERNETES.md)** — the umbrella Helm chart walkthrough for
  Kubernetes / Helm deployments, backed by [`k8s/README.md`](https://github.com/spatiumddi/spatiumddi/blob/main/k8s/README.md)
  and the chart's own [`charts/spatiumddi/README.md`](https://github.com/spatiumddi/spatiumddi/blob/main/charts/spatiumddi/README.md).
