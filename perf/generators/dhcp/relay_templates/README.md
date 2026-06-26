# perfdhcp relay templates — giaddr → subnet mapping (§3.1.2)

These `.hex` files are **perfdhcp `-T` packet templates**. Each one is a raw
DHCPDISCOVER with a fixed `giaddr` baked into the BOOTP header (offset 24, 4
bytes), so every packet a shard sends carries that relay address. Kea selects
the matching scope by comparing the packet's `giaddr` against each scope's
`relay.ip-addresses` — grounded on
`backend/app/drivers/dhcp/kea.py:236-241`
(`out["relay"] = {"ip-addresses": list(scope.relay_addresses)}`).

This is the relay arm of the **Phase-0 topology decision** (§3.1.2). If relay
proves too fiddly for v1, fall back to **broadcast** (single VLAN/scope, no
giaddr, `dhcp_socket_mode=direct → raw`) — in which case `perfdhcp_shard.py`
targets the node directly with **no** template and these files are unused.

## The fixed 8-giaddr → 8-subnet mapping

The headline manifests carve `10.0.0.0/8` into **8 × /16** (one per second
octet 0–7), each with a `.1` relay address. The manifest's 8-entry `giaddr`
list and each scope's `relay.ip-addresses` line up by index:

| Subnet idx | CIDR            | giaddr (relay IP) | template file              | reverse zone           |
|------------|-----------------|-------------------|----------------------------|------------------------|
| 0          | `10.0.0.0/16`   | `10.0.0.1`        | `giaddr-10.0.0.1.hex`      | `0.10.in-addr.arpa`    |
| 1          | `10.1.0.0/16`   | `10.1.0.1`        | `giaddr-10.1.0.1.hex`      | `1.10.in-addr.arpa`    |
| 2          | `10.2.0.0/16`   | `10.2.0.1`        | `giaddr-10.2.0.1.hex`      | `2.10.in-addr.arpa`    |
| 3          | `10.3.0.0/16`   | `10.3.0.1`        | `giaddr-10.3.0.1.hex`      | `3.10.in-addr.arpa`    |
| 4          | `10.4.0.0/16`   | `10.4.0.1`        | `giaddr-10.4.0.1.hex`      | `4.10.in-addr.arpa`    |
| 5          | `10.5.0.0/16`   | `10.5.0.1`        | `giaddr-10.5.0.1.hex`      | `5.10.in-addr.arpa`    |
| 6          | `10.6.0.0/16`   | `10.6.0.1`        | `giaddr-10.6.0.1.hex`      | `6.10.in-addr.arpa`    |
| 7          | `10.7.0.0/16`   | `10.7.0.1`        | `giaddr-10.7.0.1.hex`      | `7.10.in-addr.arpa`    |

The seeder must set `relay_addresses=[10.i.0.1]` on scope `i`
(`seed.relay_addresses_per_scope: true` in the manifest), and the DHCP group's
`dhcp_socket_mode=relay` so Kea renders `dhcp-socket-type: udp` and answers
unicast back to the relay IP
(`backend/app/drivers/dhcp/kea.py:319-324`, `bundle.dhcp_socket_type`).

## How shards map to giaddrs

`perfdhcp_shard.py` partitions the 8 giaddrs across the K shards
round-robin (`fleet.shard_indices` over the giaddr count). Each shard runs
**one perfdhcp child per giaddr it owns**, and sums their rates. With
`shards <= 8` a shard owns several giaddrs; with `shards > 8` each shard owns
exactly one. Every shard offers only its `new_dora_per_s / K` slice.

## Generating the templates

```bash
cd /root/github/spatiumddi
PYTHONPATH=perf/harness python3 perf/generators/dhcp/build_template.py \
    --manifest perf/manifests/university-24h.yaml
# wrote .../giaddr-10.0.0.1.hex  (giaddr 10.0.0.1 → subnet 10.0.0.0/16)
# ... (8 files)
```

Or inspect a single one without writing:

```bash
PYTHONPATH=perf/harness python3 perf/generators/dhcp/build_template.py \
    --giaddr 10.0.0.1 --print
```

The committed `.hex` files in this directory are pre-generated for the standard
`10.0.0.1 .. 10.7.0.1` set so the shard worker runs without a build step. If a
manifest uses a different giaddr set, regenerate.

## Template internals (what perfdhcp mutates)

The template is a minimal valid `BOOTREQUEST / DHCPDISCOVER`. perfdhcp copies
it per simulated client and mutates only:

| Field      | Offset | perfdhcp flag           | Why                                   |
|------------|--------|-------------------------|---------------------------------------|
| `xid`      | 4      | `-X 4` (auto)           | unique transaction id per exchange    |
| `chaddr`   | 28     | `-b mac=<base>` (auto)  | unique client MAC per simulated client|
| `giaddr`   | 24     | **baked in, NOT mutated** | fixes the subnet Kea selects        |

`perfdhcp_shard.py` pins each shard's `-b mac=<base>` to a **disjoint** block
of the locally-administered OUI space (high window, `PERFDHCP_INDEX_BASE`) via
`spddi_perf.fleet.device_mac`, so perfdhcp's synthetic clients never collide
with the orchestrator's per-device MACs (§3.2 / §4.5) or with sibling shards.

> Note: if a giaddr template is missing on disk, the shard falls back to
> perfdhcp's relayed-mode flag (`-A 1`, encapsulate as relay-agent) so the
> giaddr still routes; the baked template is the precise path.
