# `perf/reports/` — committed run reports + the report template

This directory holds (1) the Jinja2 report **template** and (2) the **published**,
committable copies of run reports. It is the durable home for the deliverable the
performance suite produces: the SLO verdict and the evidence behind it
(docs/PERFORMANCE_TESTING.md §8).

## What lives here

```
perf/reports/
├── template.md.j2          # the report template (rendered by spddi_perf.collect)
├── README.md               # this file
└── <RUN_ID>/               # published report for a run (created by --publish)
      slo_results.json       # machine-readable gate (§8.3/§8.4) — small, diffable
      comparison.json        # §8.5 regression diff (only when --baseline given)
      report.md              # human report (executive verdict + SLO table + …)
      report.html            # standalone HTML (dependency-free md→html)
```

The **live** run directory (raw NDJSON, generator stats, snapshots) lives under
`perf/run/<RUN_ID>/`; the report generator writes into `perf/run/<RUN_ID>/report/`
first, and `--publish` copies the small report artifacts here for committing.

## The report generator

`collect.py` is invoked as a module (the `cli.py` / controller contract — note it
takes **no** `--manifest`; it reads the resolved manifest the controller pinned into
the run dir):

```bash
cd /path/to/spatiumddi
PYTHONPATH=perf/harness python3 -m spddi_perf.collect \
    --run-id <RUN_ID> --run-root perf/run [--baseline <RUN_ID>] [--out <dir>] [--publish]
```

or via the harness CLI:

```bash
PYTHONPATH=perf/harness python3 -m spddi_perf.cli report --run-id <RUN_ID> [--baseline <RUN_ID>]
```

### What it does

1. **Ingests** the run dir — every war-room NDJSON surface
   (`warroom/{health_platform,pg_overview,pg_connections,pg_tables,redis_overview,
   redis_wakebus,celery_queues,metrics_*,pg_locks,pg_activity,pg_user_tables,
   domain_counts,operator_mutation}.ndjson`), the generator stats
   (`generators/perfdhcp.shard*.stat`, `dnsperf.stat`, `orchestrator.shard*.*`),
   the setpoint history, and the point-in-time `snapshots/*.json`. **All ingestion
   is graceful** — a missing surface makes the corresponding SLO row `NO_DATA`,
   never a crash. It runs cleanly against a partial / dry run dir.
2. **Computes** the §8.3 consolidated SLO table mechanically into
   `report/slo_results.json` — criterion (a) a1–a12, (b) b1–b13, (c) c1–c7
   (discovery / `CEILING`, not pass/fail), (d) d1–d13. Each row is
   `{id, slo, source, measured, threshold, verdict}` where verdict ∈
   `PASS | FAIL | NO_DATA | N_A | CEILING`. The structural invariants
   (conns < 70%, deadlocks = 0, REFUSED = 0, evictions = 0, restarts = 0) are
   **not** relaxed (§8.3.1).
3. **Renders** `report/report.md` (+ `.html`) — the §8.4 executive verdict block
   (per-criterion PASS/FAIL + OVERALL incl. `CONDITIONAL PASS`), the SLO table with
   measured-vs-threshold, the t0↔tEnd delta tables (the §8.2.4 row-count ledger,
   per-table dead-tup/autovacuum, db-size), the bottleneck finding (first-to-give +
   the ready §5 mitigation A–I), and profile/provenance.
4. **`--baseline`** runs the §8.5 regression comparison: refuses to compare profiles
   that differ on the load-bearing axes (`d_total`, `lease_seconds`, `t1_seconds`,
   `ddns`, `query_log_enabled`, `subnets`, `reverse_zone_shape`, `powerdns`,
   `dnssec`); diffs every SLO row with a ±20% band; applies the gate policy
   (**BLOCK** on any PASS→FAIL / new deadlock / restart / eviction / ceiling drop >
   band, else **WARN**); writes `report/comparison.json`. A `BLOCK` gate makes the
   process exit `3` so CI/release gating can read the exit code.
5. **`--publish`** copies the small report artifacts to `perf/reports/<RUN_ID>/` for
   committing — `slo_results.json` + `comparison.json` are the only files CI needs,
   and they are stable-schema + diffable in git.

### Why external (not the product's own surfaces)

The authoritative numbers are the off-box client-side stats + the direct-psql probe
(§8.6): don't perturb the SUT (the product dashboards run on the api pod + CNPG,
the two components under test), `metric_sample` is 60s-coarse and carries no
connection/lock/bloat/RSS coverage, and native dashboards go dark exactly when the
api pod saturates — which is the failure we're hunting. The product surfaces are
collected for corroboration; the deep-DB series (deadlocks/locks/per-table tuples)
comes only from the psql poller.

## CI / release gating

`slo_results.json` + `comparison.json` are the contract for the release workflow: a
job reads them and posts WARN/BLOCK on the release PR, making the 24h soak a
gateable artifact like `make ci`. Promote a clean run to a baseline by committing its
`perf/reports/<RUN_ID>/` and pointing the next comparison at it (`--baseline`);
re-baseline deliberately, never silently (the manifest's `git_sha` + `mitigations_
applied` make a baseline's pedigree explicit, §8.5).
