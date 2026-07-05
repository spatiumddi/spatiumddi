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

> **Documentation is in progress.** The receive-only invariant, the collector
> architecture, and the router/firewall peering examples below are current for the
> Phase 1+2 cut; the full data-model / API / UI reference lands as later phases
> ship (see `CLAUDE.md`'s roadmap and issue #566 for status).

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

## Peering a router with the collector

You configure **your** router to peer with the collector and advertise your routing
table to it. The collector is receive-only — it accepts your routes and never sends
one back, so no inbound filtering/route-map is needed on your side to protect the
router. The collector normally **initiates** the TCP session outbound to the router,
so the router only has to (a) have the collector configured as a BGP neighbor and
(b) permit inbound TCP/179 from the collector's IP (see [Firewall](#firewall-tcp179)).

**Fill in these three values** (from the peer you create under *Network → Looking
Glass → Add peer*):

| Placeholder | Where it comes from | Example |
|---|---|---|
| Collector IP | the appliance node's routable IP (the collector uses `hostNetwork`) | `192.0.2.10` |
| Collector ASN | the peer's **Local ASN** field | `65000` |
| Your router ASN | the peer's **Peer ASN** field | `64500` |
| MD5 secret | the peer's optional **MD5 password** field | *(omit if unset)* |

> **eBGP vs iBGP.** The simplest setup is **eBGP** — give the collector a private
> ASN (e.g. `65000`) different from your router's, and the router advertises its
> best paths automatically. If you want the collector to see **every** path (not
> just the best) or you run it inside your own AS, use **iBGP** and mark the
> collector a **route-reflector-client** so the router reflects its full RIB; add
> **add-path / additional-paths send** to expose non-best paths. Either way, enable
> **send-community** so the Routes grid can render your communities.

### Cisco IOS / IOS-XE

```
router bgp 64500
 neighbor 192.0.2.10 remote-as 65000
 neighbor 192.0.2.10 description SpatiumDDI-LookingGlass
 neighbor 192.0.2.10 password <md5-secret>          ! optional; must match the peer
 address-family ipv4 unicast
  neighbor 192.0.2.10 activate
  neighbor 192.0.2.10 send-community both            ! so communities show in the LG
  ! full-table visibility (optional): neighbor 192.0.2.10 additional-paths send
 exit-address-family
```

### Cisco IOS-XR

IOS-XR denies eBGP advertisement by default, so attach a pass route-policy outbound:

```
route-policy PASS
  pass
end-policy
!
router bgp 64500
 neighbor 192.0.2.10
  remote-as 65000
  description SpatiumDDI-LookingGlass
  password encrypted <md5-secret>                    ! optional
  address-family ipv4 unicast
   route-policy PASS out
   send-community-ebgp
```

### Juniper Junos

```
protocols {
    bgp {
        group spatiumddi-lg {
            type external;                            ## iBGP: set `type internal` + cluster
            local-as 64500;
            peer-as 65000;
            export send-table;                        ## advertise your routes to the collector
            neighbor 192.0.2.10 {
                description "SpatiumDDI Looking Glass (receive-only)";
                authentication-key "<md5-secret>";    ## optional
            }
        }
    }
}
policy-options {
    policy-statement send-table {
        term all { then accept; }                     ## scope to taste
    }
}
```

### Arista EOS

```
router bgp 64500
   neighbor 192.0.2.10 remote-as 65000
   neighbor 192.0.2.10 description SpatiumDDI-LookingGlass
   neighbor 192.0.2.10 password <md5-secret>          ! optional
   neighbor 192.0.2.10 send-community
   address-family ipv4
      neighbor 192.0.2.10 activate
```

### FRRouting (FRR / vtysh)

```
router bgp 64500
 neighbor 192.0.2.10 remote-as 65000
 neighbor 192.0.2.10 description SpatiumDDI-LookingGlass
 neighbor 192.0.2.10 password <md5-secret>            ! optional
 address-family ipv4 unicast
  neighbor 192.0.2.10 activate
  neighbor 192.0.2.10 send-community all
  ! iBGP full-table: neighbor 192.0.2.10 route-reflector-client
 exit-address-family
```

### BIRD 2.x

```
protocol bgp spatiumddi_lg {
    local as 64500;
    neighbor 192.0.2.10 as 65000;
    password "<md5-secret>";                          # optional
    ipv4 {
        import none;                                  # the collector never sends us routes
        export all;                                   # advertise our table to the collector
    };
}
```

Other platforms (Nokia SR OS, MikroTik RouterOS 7, VyOS, …) follow the same shape:
a normal BGP neighbor pointing at the collector IP/ASN, an outbound policy that
advertises your table, `send-community`, and an inbound firewall rule for TCP/179.

### Firewall (TCP/179)

The collector dials the router, so the **router side** must accept inbound TCP/179
from the collector's IP (allow both directions if you'd rather the router initiate).

```
! Cisco IOS extended ACL
ip access-list extended BGP-LG
 permit tcp host 192.0.2.10 any eq bgp
 permit tcp host 192.0.2.10 eq bgp any

! Cisco ASA
access-list OUTSIDE_IN extended permit tcp host 192.0.2.10 host <router-ip> eq bgp
```

```bash
# Linux firewall in the path — nftables
nft add rule inet filter input ip saddr 192.0.2.10 tcp dport 179 accept
# …or iptables
iptables -A INPUT -p tcp -s 192.0.2.10 --dport 179 -j ACCEPT
```

On the **SpatiumDDI appliance** side there is nothing to do: assigning the
`looking-glass` role opens TCP/179 automatically through the per-role nftables
drop-in (`spatium.io/role-looking-glass`). On a standalone Docker/K8s collector the
container uses host networking, so TCP/179 is governed by the host's own firewall.

### Verify

Once the router config + firewall are in place, *Network → Looking Glass → Sessions*
shows the peer transition to **Established** (green) within a few seconds, its
prefix counters climb, and the *Routes* tab fills with the prefixes the router
advertised — each origin ASN and community clickable back into SpatiumDDI.

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
