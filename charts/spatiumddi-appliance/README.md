# spatiumddi-appliance

Helm chart that reconciles the SpatiumDDI **appliance's local k3s
cluster** to the operator's role assignment.

Issued in [issue #183](https://github.com/spatiumddi/spatiumddi/issues/183)
Phase 2. Phase 1 baked k3s into the slot image; Phase 2 ships this
chart; Phase 3 wires the supervisor as the CRD reconciler that
turns operator role assignments on the control plane into Helm
releases.

## Status

**Phase 2 scaffold — not yet wired to the supervisor.**

The chart structure is stable + lint-clean. Templates render but
the supervisor still runs via docker-compose on the appliance host;
flipping `<role>.enabled: true` does nothing useful until Phase 3
lands.

## Architecture

This chart is **not** a control-plane install — that's
[`charts/spatiumddi/`](../spatiumddi/). This chart targets the
appliance's *local* k3s, deploying the per-role service workloads:

- `dnsBind9` — Deployment + Service. BIND9 authoritative DNS.
- `dnsPowerdns` — Deployment + Service. PowerDNS alternative.
- `dhcpKea` — DaemonSet with `hostNetwork: true`. Kea DHCPv4/v6.
- `supervisor` — DaemonSet, privileged. The reconciler itself.

Each role is gated on a `<role>.enabled` flag. Mutual exclusion
between DNS engines (bind9 vs powerdns) is enforced upstream by the
supervisor's CRD reconciler — at the chart level both blocks can
render side-by-side if values.yaml says so.

## Air-gap defaults

- `global.imagePullPolicy: Never` everywhere. All container images
  preload from `/usr/lib/spatiumddi/images/*.tar.zst` into
  containerd at firstboot. No registry lookups at runtime.
- `global.imageTag` defaults to the slot's `SPATIUMDDI_VERSION`
  stamp; the supervisor rewrites it on apply.

## Local linting

```bash
helm lint charts/spatiumddi-appliance/
helm template demo charts/spatiumddi-appliance/ \
     --set dnsBind9.enabled=true \
     --set dhcpKea.enabled=true
```

CI lint runs on every PR (Phase 2 follow-up).
