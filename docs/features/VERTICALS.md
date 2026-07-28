---
layout: default
title: Vertical network awareness
---

# Vertical network awareness — AV · BACnet/IP · Industrial-OT

Umbrella issue [#543](https://github.com/spatiumddi/spatiumddi/issues/543), with
three children: [#540](https://github.com/spatiumddi/spatiumddi/issues/540) AV /
Audio-Video-over-IP, [#541](https://github.com/spatiumddi/spatiumddi/issues/541)
BACnet/IP building automation, and
[#542](https://github.com/spatiumddi/spatiumddi/issues/542) Industrial / OT.

Three IP-native domains that a generic IPAM does not speak. They look unrelated
but they are the **same DDI primitives, specialized**: a uniqueness registry, a
segmentation-documentation layer, and conformity rules over both. Each is a
togglable feature module in the Network group, default-enabled so operators
discover them, and switched off by the plants that don't run that vertical.

> **Documentation is in progress.** This covers the shipped Phase 1 of each
> module — the registry, conformity, and UI surfaces. The discovery phases have
> **not** shipped; see [What is deliberately not here](#what-is-deliberately-not-here)
> for why, which is not simply "not yet".

---

## The three at a glance

| | AV over IP (#540) | BACnet/IP (#541) | Industrial / OT (#542) |
|---|---|---|---|
| Feature module | `network.av` | `network.bacnet` | `network.ot` |
| Sidebar | Network → AV flows | Network → BACnet devices | Network → OT devices |
| The differentiating hook | AV descriptor on a multicast group + operator-declared per-protocol ranges | Device instance numbers — a second uniqueness namespace, internetwork-wide | Purdue-level segmentation documented against real subnets |
| Protocols | Dante · AES67 · SMPTE ST 2110 (video/audio/anc) · NDI · RAVENNA | BACnet/IP (`UDP 47808`) | PROFINET · EtherNet/IP · Modbus TCP · OPC UA · S7comm · DNP3 · IEC 61850 |
| Tables | `av_flow_profile`, `av_reserved_range` | `bacnet_device` | `ot_device`, `ot_zone` |
| Anchored to | `multicast_group` (1:1) | `ip_address` (+ denormalized `subnet_id`) | `ip_address` (1:1), `subnet` (1:1 for zones) |
| Conformity checks | `av_flow_outside_reserved_range`, `av_flow_no_ptp_domain` | `bbmd_one_per_subnet`, `bacnet_duplicate_device_instance`, `bacnet_vendor_id_unknown` | `ot_device_crosses_purdue_boundary`, `ot_zone_missing_purdue_level` |
| API prefix | `/api/v1/av/…` | `/api/v1/bacnet/…` | `/api/v1/ot/…` |
| RBAC resource type | `av_flow` | `bacnet_device` | `ot_device` |
| Copilot tools | `find_av_flows`, `count_av_flows`, `find_av_reserved_ranges` | `find_bacnet_devices`, `count_bacnet_devices`, `find_bbmds` | `find_ot_devices`, `count_ot_devices`, `find_ot_zones` |

All rows land via the REST API or the UI. **Nothing in this feature family emits
a packet.**

---

## AV / Audio-Video-over-IP (#540)

### It extends the multicast registry, it does not duplicate it

A Dante flow **is** a `MulticastGroup`. AV adds a 1:1 sidecar
(`av_flow_profile`) carrying what the multicast registry has no business
knowing: the AV protocol, the human flow label an engineer reads off a Dante
Controller or 2110 routing panel, and the PTP clock domain.

It is a sidecar rather than columns on `multicast_group` so that a market-data
multicast shop that never enables `network.av` gets no extra columns on a hot
table, and disabling the module is a clean table drop.

> **Module relationship.** `network.av` is only meaningful alongside
> `network.multicast` — an AV flow is a multicast group. There is no
> module-dependency mechanism in the feature-module system, so this is
> documentation rather than an enforced constraint: turning off
> `network.multicast` while leaving `network.av` on leaves the AV surface
> pointing at groups the operator can no longer browse.

### Reserved ranges are what make the checks real

`av_reserved_range` records, per IPSpace, that a CIDR belongs to a given AV
protocol. Best-practice AoIP is *static* multicast assignment, so an operator's
declared plan is the thing worth checking against.

Well-known starting points are offered as presets, **not** auto-inserted — a
vendor default is a default the operator may deliberately have moved off:

| Protocol | Common default |
|---|---|
| Dante | `239.69.0.0/16` |
| AES67 / RAVENNA | a site-chosen block inside `239.0.0.0/8` |
| SMPTE ST 2110 | no standard range — plants carve their own |

`exclusive` distinguishes "this range is *for* Dante and nothing else belongs
here" from "Dante lives here among other things". Most plants want exclusive;
shared exists because small shops run one flat `239.x` range for everything and
would otherwise get a permanent wall of warnings.

`POST /api/v1/av/allocation-preview` answers "would this allocation collide with
another protocol's declared range?" before the operator commits.

### Conformity

- **`av_flow_outside_reserved_range`** — fails when an AV flow's address sits
  outside every range declared for its protocol. **Not applicable** when no
  range is declared for that protocol: absence of policy is not a violation,
  and failing every flow in that state would train operators to ignore the check.
- **`av_flow_no_ptp_domain`** — advisory warn when the clock domain is
  unrecorded. PTP misconfiguration is the most common AoIP failure mode and the
  domain is the first thing asked for when audio drops, but a missing value is
  undocumented state, not a broken network. Applied uniformly across protocols;
  NDI is arguably not PTP-locked the way AES67/2110 are, but the field records
  documentation rather than asserting the flow is clocked, and special-casing it
  would encode a timing claim this registry cannot verify.

---

## BACnet/IP building automation (#541)

### Device instance numbers are the point

Every BACnet device carries a device instance number (0–4,194,302, 22-bit) that
must be unique across the **entire** internetwork — not per-subnet, not
per-site. Duplicate instances are a classic BAS failure that takes days to
trace, and integrators track them in spreadsheets that drift.

That is the same uniqueness-registry problem IPAM already solves for addresses,
applied to a parallel number space — which is what makes it belong in a DDI
rather than a BAS head-end. `uq_bacnet_device_instance` enforces it **in the
database**, not merely in the API, because the failure it prevents is precisely
the one that makes an internetwork misbehave silently.

`4194303` (`0x3FFFFF`) is reserved by the standard as the "unconfigured /
wildcard" instance used in `Who-Is`, so the maximum assignable value is one
below it. The API returns a **409 naming the conflicting device** rather than
surfacing a raw integrity error.

### BBMD topology is a checkable rule

BACnet broadcasts don't cross IP routers, so each IP subnet in a multi-subnet
BACnet/IP network needs **exactly one** BACnet Broadcast Management Device:

- **zero** → that subnet's devices are invisible to the rest of the internetwork
- **two or more** → duplicated broadcast traffic and duplicate `I-Am` responses

`bbmd_one_per_subnet` fails in **both** directions and names the offending
device instances when there are too many. A subnet with no BACnet devices at all
is *not applicable* rather than failing — most subnets in a mixed estate aren't
BACnet subnets, and flagging them would bury the real findings.

`bdt` / `fdt` (Broadcast Distribution Table / Foreign Device Table) are stored
as JSONB snapshots: read-mostly copies of someone else's state that get rendered
and diffed wholesale rather than queried into. Phase 1 accepts them via the API
so a plant can document topology it already knows.

### Vendor labels

`backend/app/services/bacnet/vendors.py` maps well-known ASHRAE vendor ids to
display names. It is deliberately **small**: a wrong label is worse than no
label, because an integrator chasing a duplicate instance who sees the wrong
manufacturer is being actively misled toward the wrong panel, on the wrong
floor, with the wrong vendor's tool. Unresolved ids fall through to the
operator-supplied `vendor_name`. ASHRAE has assigned well over a thousand ids;
full coverage belongs in an importer with provenance, the way OUI data is
handled.

`bacnet_vendor_id_unknown` advises on devices reporting vendor id 0 or none —
0 is legitimately ASHRAE itself, but in the field it almost always means a
misconfigured, cloned, or counterfeit controller.

---

## Industrial / OT (#542)

### Safety posture — read-only identification, permanently

This module identifies and inventories industrial devices. It **never** reads
tags, writes coils or registers, subscribes to OPC UA, or otherwise touches a
control protocol. That is a safety and liability boundary, not a roadmap gap.
Any hint of write access to a control protocol is out of scope, full stop.

It is also explicitly **not** an OT-security product. Deep passive DPI and
anomaly detection are Nozomi / Claroty / Dragos territory; the angle here is DDI
system-of-record plus segmentation documentation.

### PROFINET names are identities

In PROFINET the device *name*, not the IP, is the device's identity: DCP assigns
the address once by name, and a replacement unit is commissioned by giving it
the old name. `profinet_device_name` gets its own indexed column rather than a
custom field because it is a string OT engineers search by.

### Purdue zoning

`ot_zone` records the Purdue level and cell/area for a subnet; `ot_device`
records the level for the device. `purdue_level` is `Numeric(2,1)` rather than an
integer because **level 3.5 — the DMZ between the manufacturing and enterprise
zones — is real and universally used**, and it is exactly the boundary the
conformity check cares most about.

- **`ot_device_crosses_purdue_boundary`** — fails when a device's level differs
  from its subnet's zoned level. A Level-1 PLC in a Level-4 enterprise subnet is
  the finding an auditor asks for. **Not applicable** when either side is unset:
  an unrecorded level is missing documentation, not a violation, and treating
  unknown as guilty would make the check unusable during rollout.
- **`ot_zone_missing_purdue_level`** — advisory; a subnet carrying OT devices but
  no zone row. This is the check that explains an empty segmentation report.

`cell_area` is free text rather than an FK: plants name cells in wildly local
ways and a registry here would be friction with no payoff at this phase.

### CSV import

`POST /api/v1/ot/devices/import/{preview,commit}` ingests engineering-tool
exports (TIA Portal, Studio 5000). Rows key on IP address, and an address that
does not already exist in IPAM is reported as a **row error rather than
created** — this importer enriches inventory, it does not invent it.

---

## What is deliberately not here

Every discovery phase across all three modules is unshipped, and the reasons are
structural rather than scheduling.

**The umbrella's shared dependency was cancelled.** #543 named
[#40](https://github.com/spatiumddi/spatiumddi/issues/40) (mDNS / Bonjour / WSD
passive discovery) as "the common enabling primitive; worth landing early". #40
was **closed as not planned** after a feasibility review, which found that
mDNS/WSD are link-local multicast — only a host-networked, on-segment agent
hears anything, and the agent↔subnet binding that would require is not modelled
today.

| Deferred | Why |
|---|---|
| #540 Phase 2 — Dante mDNS discovery | Blocked at the source. Dante discovery *is* mDNS, and #40 is closed. |
| #540 Phase 3 — NMOS IS-04 mirror | A full read-only pull integration with its own reconciler and both dashboard surfaces (non-negotiable #15). Cleanly separable; deserves its own change. |
| #541 Phase 2 — `Who-Is` sweep | Needs a UDP broadcast carrying a real BACnet payload. The control plane's only generic UDP prober sends an **empty** datagram, so this needs a new datagram helper plus the same agent↔subnet binding #40 died on. |
| #542 Phase 2 — EtherNet/IP · Modbus · OPC UA probes | The issue expected this to be nearly free via nmap NSE scripts. It is not: nmap **runs** `--script enip-info,modbus-discover` today, but `_parse_host()` never reads `<script>` / `<hostscript>` elements, so the output dies in `raw_xml`. Real discovery needs an NSE-output parser plus a preset threaded through five separate hardcoded lists. |
| #542 Phase 3 — PROFINET DCP | Ethertype `0x8892`, raw L2, not routable. No `SOCK_RAW` code exists in the backend and `CAP_NET_RAW` is a file capability on the `nmap`/`tcpdump` binaries only — the Python process does not have it. Needs a container capability grant, i.e. a deployment change. |

Each phase remains worth doing; each is its own piece of work with its own
prerequisites, and none of them is a metadata phase.

**Also permanently out of scope:** RTP jitter / packet-loss / SDP essence
analysis and NMOS IS-05 connection control (SpatiumDDI is a registry, not a
media monitor); running PIM/IGMP or acting as a BBMD (we document the topology,
we don't participate in it); BACnet object/point-level management (we track the
device, not its 600 objects); and BACnet MS/TP or other serial datalinks, which
aren't IP-addressable.

---

## Operational notes

- **Backup / factory reset.** The five tables — plus the four multicast tables
  they depend on — are covered by the `verticals` backup section and the
  `DESTROY-VERTICALS` factory-reset section. Resetting the verticals section
  destroys only the vertical metadata; the underlying IPAM addresses and subnets
  are untouched.
- **RBAC.** The builtin **Network Editor** role grants `admin` on `av_flow`,
  `bacnet_device`, and `ot_device`. Builtin roles are re-seeded on every boot, so
  no migration is involved.
- **Enum vocabularies** (`av_protocol`, `ot_protocol`, `ot_role`, `seen_via`) are
  module-level `frozenset[str]` validated at the API layer rather than Postgres
  enums, matching the multicast registry's convention — adding a protocol later
  must not require a migration. Values with a *specification-defined* range
  (BACnet's 22-bit instance space, PTP's single octet, Purdue 0–5) do carry
  database `CHECK` constraints, because those bounds will never move.
- **`seen_via`** already accepts the discovery-phase values (`mdns`, `nmos`,
  `whois`, `enip`, `dcp`, …) so those phases need no data migration.
