# SpatiumDDI performance test suite (`perf/`)

A 24-hour load + soak harness that drives the **whole DDI stack** (Kea DHCP +
BIND9/PowerDNS + the FastAPI/Celery control plane + PostgreSQL/Redis) at
university scale — ~50k students, 200–300k unique devices/day on a realistic
diurnal curve — to prove the **database never becomes the bottleneck** and to find
the ceiling before a customer does.

**Read the design first:** [`docs/PERFORMANCE_TESTING.md`](../docs/PERFORMANCE_TESTING.md).
This README is the operator quick-start; the doc is the spec (section numbers below
refer to it). To prepare a clean appliance to test against (mint a token, bring up the
DNS/DHCP data plane, expose Postgres/Redis), follow
[`docs/PERF_APPLIANCE_SETUP.md`](../docs/PERF_APPLIANCE_SETUP.md).

> ⚠️ This suite runs **OFF the appliance** (dedicated load-gen + monitoring VMs).
> Never run the generators on the system under test — co-locating steals the exact
> CPU/IO/connection budget being measured (§2.3).

---

## What's here

```
perf/
  harness/spddi_perf/   the shared core + controller (the run "control plane")
    canonical.py        §0.A single-source-of-truth numbers + the §1.3 diurnal curve
    manifest.py         run-manifest schema (load + validate)
    setpoints.py        the file-based setpoint bus (controller ↔ workers)
    runpaths.py         the on-disk run-directory layout (§7.4)
    fleet.py            deterministic device identity (shared by DHCP + DNS gens)
    controller.py phases.py watchdog.py checkpoint.py workers.py cli.py collect.py
  manifests/            run manifests (university-24h.yaml = headline; smoke.yaml; variants)
  seeder/               provision + seed a clean appliance (scaffold, fleet-enable, pgss, prune, Phase-0)
  warroom/              live monitoring: native poller + direct psql probe + terminal fallback (§6)
  generators/
    dhcp/               perfdhcp ceiling probe + relay templates (§3.1)
    dns/                dnsperf/resperf/flamethrower + the in-zone-validated query set (§4)
    orchestrator/       the realistic 24h device-fleet FSM + operator + synthetic-UI streams (§3.2)
  dashboards/           off-box Prometheus + Grafana war-room board + on-node exporters (§6)
  reports/              the run-report template (collect.py renders into runs' report/)
  run/                  live run state + artifacts (gitignored)
```

## Box layout (§2.3)

| Box | Runs |
|---|---|
| **lg-0** (controller + monitoring) | `spddi-perf` controller, the war-room poller, off-box Prometheus + Grafana |
| **lg-1** (protocol floors) | perfdhcp shards + dnsperf/resperf/flamethrower (the C blasters) |
| **lg-2** (orchestrator) | the asyncio device-fleet orchestrator (the DB-pressure driver) |

`≥2 boxes` is the floor; the morning-surge peak may need a third. All boxes
NTP-synced to an **in-lab** source (§4.9) inside the egress-blocked test VLAN.

## Prerequisites (on the load-gen boxes)

```bash
# Python deps (controller + orchestrator + war-room + report):
python3 -m venv .venv && . .venv/bin/activate
make perf-deps                      # = pip install -r perf/requirements.txt

# System tools (apt — NOT pip):
sudo apt-get install -y kea-admin dnsperf flamethrower tcpdump chrony
#   kea     -> /usr/sbin/perfdhcp (raw DHCPv4 ceiling, §3.1)
#   dnsperf -> dnsperf + resperf  (raw DNS qps ceiling, §4.3)
```

## Required environment (secrets — never in the manifest, §9.1 / non-negotiable #6)

```bash
export SPDDI_PERF_ADMIN_TOKEN=...      # superadmin API token (lifetime ≥ run length, §7.6.6)
export SPDDI_PERF_PSQL_DSN=...         # direct psql DSN for pg_locks/deadlocks (read-only pg_monitor role)
export SPDDI_PERF_REDIS_URL=...        # for Celery queue-depth LLEN (war-room)
export SPDDI_PERF_CA_BUNDLE=...        # (optional) pin the appliance's self-signed cert; else verify=False

# Point at your appliance without editing committed YAML placeholders:
export SPDDI_PERF_NODE_IP=192.168.0.x  # → api_base derived as https://<node_ip>/api

# Required when using a pre-prepared appliance (PERF_APPLIANCE_SETUP.md §2):
export SPDDI_PERF_DNS_GROUP_ID=<uuid>  # existing DNS group bind9 is already bound to
export SPDDI_PERF_DHCP_GROUP_ID=<uuid> # existing DHCP group kea is already bound to
```

See `docs/PERF_APPLIANCE_SETUP.md` for how to retrieve these IDs after manual setup.
Without them the seeder creates new empty groups that no agent serves.

## Typical workflow

```bash
make perf-validate MANIFEST=perf/manifests/smoke.yaml   # print the resolved plan
make perf-compile                                        # py_compile every file
make perf-dry      MANIFEST=perf/manifests/smoke.yaml    # engine-only dry run (no appliance needed)

# Against a real clean appliance — the easy interactive path:
make perf-tui                                            # pick a manifest, Start/Stop/Resume/Abort, live vitals (§9.3)

# ...or the same thing headless:
make perf-seed     MANIFEST=perf/manifests/university-24h.yaml   # provision + seed (or let the run do it)
make perf-smoke                                          # the GATED ~40min smoke (capacity + baseline tuning, §1.9)
# ...only after the smoke passes its go/no-go:
make perf-run      MANIFEST=perf/manifests/university-24h.yaml

# Watch it:
make perf-tui      RUN_ID=<id>                           # attach the interactive console to a running run
make perf-warroom                                        # terminal live-status (§6.6); or open the Grafana board
make perf-status                                         # the latest run's state.json

# Control / recover:
make perf-stop     RUN_ID=<id>                           # kill-switch (graceful ramp-to-zero)
make perf-resume   RUN_ID=<id>                           # continue from the last checkpointed tick

# Result:
make perf-report   RUN_ID=<id> [BASELINE=<id>]           # SLO pass/fail report (+ regression compare)
```

`make perf-help` lists every target.

## Safety (non-negotiable)

- **DNS is closed-loop (§4.9).** The seeder sets the test BIND to `recursion no;`,
  the query-set generator validates **every** qname is in-zone (out-of-zone → abort),
  and the run VLAN must drop outbound :53. The pre-flight gate aborts on any leak.
  This keeps a 50k-student query flood off the public internet.
- **Kill-switch + watchdog (§7.6).** `make perf-stop` (or `run/<id>/STOP`) ramps every
  worker to zero; the watchdog throttles-then-aborts on an unhealthy appliance and
  preserves the breach snapshot. Workers fail safe to *off* if the controller dies.

## Notes

- All artifacts land under `perf/run/<run_id>/` (gitignored). Rendered reports can be
  published to `perf/reports/<run_id>/` for committing.
- The setpoint bus is a plain JSON file (`run/<id>/setpoints/current.json`) — workers
  are loosely coupled to the controller through it, so each is independently runnable.
- An **interactive TUI console** drives runs from one screen — `make perf-tui`
  (pick a manifest → Start/Stop/Resume/Abort with confirm modals + live vitals).
  It's a face over the same controller + setpoint bus; see §9.3.
