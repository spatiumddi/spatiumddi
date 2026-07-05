---
layout: default
title: BGP Looking Glass
---

# BGP Looking Glass

Issue [#566](https://github.com/spatiumddi/spatiumddi/issues/566). A per-appliance-node
**receive-only** BGP collector that peers with the operator's edge/core routers,
accepts their routing table, and turns the live Adj-RIB-In into an operator surface
where every prefix, origin ASN, and BGP community is a clickable link back into
IPAM / the ASN catalog / the community catalog.

> **This document is a stub.** Full data-model, API, and UI documentation lands
> alongside the feature's Phase 1+2 implementation (see `CLAUDE.md`'s roadmap for
> current status). This page exists now so the receive-only invariant and the
> collector's relationship to the two adjacent BGP surfaces is written down before
> the feature ships.

---

## Hard safety invariant: receive-only

**The collector is a pure sink. It never advertises a route back to the operator's
network.** No export policy, no next-hop-self, no redistribution — import-only,
every session. A per-peer `max-prefixes` cap protects the collector daemon from a
full-table blow-up, and that cap is rendered directly into the daemon's peer
config (not merely stored in the database).

> SpatiumDDI never advertises routes to your network from this session.

This is a first-class review criterion on every change that touches the rendered
collector config — a rendering bug that adds an export policy would turn a passive
observability tool into a transit path on the operator's network.

## Architecture

The collector (`agent/looking-glass/`, image `ghcr.io/spatiumddi/looking-glass`)
reuses the DNS/DHCP agent architecture wholesale: PSK→JWT bootstrap, ConfigBundle
ETag long-poll + Redis wake, absence-reconcile telemetry push, ready-marker
readiness gate, and the same supervisor role model (`can_run_looking_glass`
capability, `spatium.io/role-looking-glass` per-node label, `LG_AGENT_KEY`
bootstrap secret). See `docs/deployment/DNS_AGENT.md` for the shared agent
architecture this collector clones.

The collector engine is [GoBGP](https://github.com/osrg/gobgp) (Apache 2.0) — a
single Go binary with a gRPC RIB-read API, chosen for its clean receive-only
posture and easy programmatic peer management (no daemon restart needed to
add/remove a peer).

It runs as a DaemonSet with `hostNetwork: true` (see
`charts/spatiumddi-appliance/templates/looking-glass.yaml`) so the BGP TCP/179
session originates from the node's real routable IP — a router has no route to a
pod-CNI address. Per-node hostPath state (`/var/lib/spatiumddi/agents/looking-glass/`)
caches the last-known-good peer config so sessions stay up when the control plane
is briefly unreachable (non-negotiable #5).

## Relationship to two adjacent BGP surfaces (don't conflate)

- **[#527 BGP prefix-hijack monitoring](../OBSERVABILITY.md#91a-bgp-prefix-hijack-monitoring-527)**
  is the *public*-table companion: it polls RIPEstat / RIPE RIS Live for signals
  about SpatiumDDI-tracked prefixes as seen from the *global* Internet routing
  table. The Looking Glass is the *internal*-table view: it peers directly with
  the operator's own routers and shows what that specific router's Adj-RIB-In
  actually contains. They share no code — the hijack monitor never opens a BGP
  session; the Looking Glass never talks to RIPEstat.
- **The MetalLB VIP advertiser (BGP mode)** is a *separate, deferred* piece of
  work: lighting up `frrk8s.enabled=true` on `charts/spatiumddi-metallb/` so
  MetalLB can *advertise* the control-plane VIP to upstream routers via FRRouting
  (GPL v2). That is an **export** path (SpatiumDDI → router); the Looking Glass is
  an **import** path (router → SpatiumDDI) and requires none of the FRR/GPL-v2
  surface. The two features share only the UI concept of "a peer address + ASN" —
  zero code. See `charts/spatiumddi-metallb/values.yaml`'s `bgp:` block for the
  current (gated-off) state of that work.

## Scope (Phase 1 + 2, this cut)

- Collector registration + peer session management (Sessions tab).
- RIB ingest with absence-reconcile (a route disappearing from a live snapshot is
  marked withdrawn, not deleted) and a zero-wire floor guard (an empty snapshot
  from a peer that was previously receiving routes is treated as a collector
  hiccup, not a genuine full withdrawal).
- Routes grid: searchable/filterable RIB browser with origin-ASN links, AS-path,
  community-catalog rendering, and RPKI status computed at ingest.

Deferred to later phases: IPAM prefix-match linkage (subnet/block/space chips,
IP reverse-lookup), the Query tab (`show route` / AS-path regex / community
filter), ping/traceroute from the collector's vantage point, `bgp_lg_*` alert
rules + a Dashboard health card, and VPNv4/VPNv6 + multicast address-family
support. See the issue and its linked implementation plan for the full phasing.
