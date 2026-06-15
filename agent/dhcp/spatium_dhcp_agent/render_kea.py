"""Neutral ConfigBundle → Kea ``Dhcp4`` + ``Dhcp6`` JSON renderer.

The control plane emits a backend-neutral ConfigBundle describing scopes,
pools, statics, client classes, reservations and global/scope-level options.
This module converts that into a Kea-specific config document carrying BOTH
a ``Dhcp4`` and a ``Dhcp6`` block (the agent runs both daemons always-on;
the ``Dhcp6`` block is an idle skeleton when no v6 scopes exist). The two
blocks are split into separate files by ``sync.py`` because ``kea-dhcp4 -t``
rejects a stray ``Dhcp6`` key (and vice-versa).

A minimal bundle looks like::

    {
        "etag": "sha256:...",
        "schema_version": 1,
        "server": {"name": "dhcp1", "interfaces": ["eth0"]},
        "global_options": {
            "dns_servers": ["1.1.1.1"],
            "ntp_servers": ["192.0.2.123"],   # rendered as DHCP option 42
            "domain_name": "example.com",
            "domain_search": ["example.com"],
            "lease_time": 3600,
        },
        "subnets": [
            {
                "id": 1,
                "subnet": "192.0.2.0/24",
                "pools": [{"pool": "192.0.2.100 - 192.0.2.200"}],
                "options": {"routers": ["192.0.2.1"], "ntp_servers": ["192.0.2.5"]},
                "reservations": [
                    {"hw_address": "aa:bb:cc:dd:ee:ff",
                     "ip_address": "192.0.2.50",
                     "hostname": "printer1"}
                ],
                "client_class": null,
                "valid_lifetime": 3600,
            }
        ],
        "client_classes": [
            {"name": "voip", "test": "substring(option[60].hex,0,12) == 'Cisco-Phone'"}
        ],
        "reservation_mode": "all",
    }

NTP servers are REQUIRED to be emitted as DHCP option 42 (RFC 2132). Users
rely on clients receiving the NTP server list via DHCP.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse, urlunparse

import structlog

_log = structlog.get_logger(__name__)

# Well-known DHCPv4 option codes we emit by name when present in the bundle.
# Kea accepts ``{"name": "ntp-servers", ...}`` natively; we include explicit
# ``code`` entries for resilience against older Kea versions.
_OPTION_CODES: dict[str, int] = {
    "routers": 3,
    "domain-name-servers": 6,
    "domain-name": 15,
    "ntp-servers": 42,  # RFC 2132 § 8.3 — DO NOT OMIT
    "domain-search": 119,
}

# Map of SpatiumDDI / bundle-neutral option-name → Kea Dhcp6 ``option-data``
# name. DHCPv6 uses a different option-code space + names from v4; only
# options with a true v6 equivalent are forwarded. Mirrors
# ``_KEA_OPTION_NAMES_V6`` in ``backend/app/drivers/dhcp/kea.py``. Both the
# snake_case (``dns_servers``) and hyphenated (``dns-servers``) input keys
# the control plane may emit are accepted via the ``_put`` calls in
# ``_options_from_mapping_v6``.
_KEA_OPTION_NAMES_V6: dict[str, str] = {
    "dns-servers": "dns-servers",  # DHCPv6 option 23
    "domain-search": "domain-search",  # DHCPv6 option 24
    "ntp-servers": "sntp-servers",  # DHCPv6 option 31 (SNTP)
    "bootfile-name": "bootfile-url",  # DHCPv6 option 59 (URL form)
}

# Options that have no DHCPv6 equivalent — dropped from v6 scopes with a
# warning so a misconfigured inherited option surfaces to the operator
# instead of Kea silently rejecting the whole config on reload. Mirrors
# ``_DHCP4_ONLY_OPTION_NAMES`` in the backend driver.
_DHCP4_ONLY_OPTION_NAMES: frozenset[str] = frozenset(
    {
        "routers",
        "broadcast-address",
        "mtu",
        "time-offset",
        "domain-name",
        "tftp-server-name",
        "tftp-server-address",
    }
)


def _opt(name: str, value: Any) -> dict[str, Any]:
    """Build one Kea option-data entry."""
    if isinstance(value, list):
        data = ", ".join(str(v) for v in value)
    else:
        data = str(value)
    entry: dict[str, Any] = {"name": name, "data": data}
    if name in _OPTION_CODES:
        entry["code"] = _OPTION_CODES[name]
        entry["space"] = "dhcp4"
    return entry


def _options_from_mapping(options: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Translate bundle-neutral keys to Kea option names.

    Supported keys (both snake_case and hyphenated are accepted):
        dns_servers / domain-name-servers
        ntp_servers / ntp-servers       (DHCP option 42)
        routers
        domain_name / domain-name
        domain_search / domain-search
        tftp_server / tftp-server-name
        boot_file / boot-file-name
    """
    if not options:
        return []
    out: list[dict[str, Any]] = []

    def _put(name: str, val: Any) -> None:
        if val in (None, "", []):
            return
        out.append(_opt(name, val))

    _put(
        "domain-name-servers",
        options.get("dns_servers") or options.get("domain-name-servers"),
    )
    _put("ntp-servers", options.get("ntp_servers") or options.get("ntp-servers"))
    _put("routers", options.get("routers") or options.get("gateway"))
    _put("domain-name", options.get("domain_name") or options.get("domain-name"))
    _put("domain-search", options.get("domain_search") or options.get("domain-search"))
    _put(
        "tftp-server-name",
        options.get("tftp_server") or options.get("tftp-server-name"),
    )
    _put("boot-file-name", options.get("boot_file") or options.get("boot-file-name"))

    # Pass through any raw Kea-style list already shaped correctly.
    raw = options.get("option_data")
    if isinstance(raw, list):
        out.extend(raw)
    return out


def _options_from_mapping_v6(options: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Translate bundle-neutral option keys into Kea ``Dhcp6`` option-data.

    DHCPv6 has a separate option-code space and a different set of names
    from v4 — see ``_KEA_OPTION_NAMES_V6``. v4-only options (routers,
    domain-name, mtu, …) have no v6 analogue and are dropped with a
    warning rather than emitted under the wrong space (which would make
    Kea reject the config on reload). Mirrors ``_render_option_data(
    address_family="ipv6")`` in the backend driver.

    Both snake_case and hyphenated input keys are accepted, matching the
    v4 ``_options_from_mapping`` behaviour.
    """
    if not options:
        return []
    out: list[dict[str, Any]] = []

    def _put(key: str, val: Any) -> None:
        if val in (None, "", []):
            return
        if isinstance(val, list):
            data = ", ".join(str(v) for v in val)
        else:
            data = str(val)
        out.append({"name": _KEA_OPTION_NAMES_V6[key], "data": data})

    _put("dns-servers", options.get("dns_servers") or options.get("dns-servers"))
    _put("domain-search", options.get("domain_search") or options.get("domain-search"))
    _put("ntp-servers", options.get("ntp_servers") or options.get("ntp-servers"))
    _put("bootfile-name", options.get("boot_file") or options.get("bootfile-name"))

    # Warn on any v4-only option the operator inherited onto a v6 scope so
    # the misconfig is visible — the option is simply not emitted.
    for k in options:
        normalized = k.replace("_", "-")
        if normalized in _DHCP4_ONLY_OPTION_NAMES:
            _log.warning("kea_option_skipped_v6_no_equivalent", option=k)

    # Pass through any raw Kea-style list already shaped correctly.
    raw = options.get("option_data")
    if isinstance(raw, list):
        out.extend(raw)
    return out


def _reservation_v6(res: dict[str, Any]) -> dict[str, Any]:
    """Render a single Dhcp6 host reservation.

    Dhcp6 reservations use ``ip-addresses`` (plural list) rather than the
    v4 ``ip-address`` scalar. Mirrors ``_render_reservation(
    address_family="ipv6")`` in the backend driver.
    """
    out: dict[str, Any] = {}
    # DHCPv6 clients are identified by DUID (issue #368): when a duid is set we
    # key the reservation on it and drop hw-address, mirroring the backend
    # driver. Otherwise fall back to hw-address (matched via the subnet's
    # host-reservation-identifiers).
    if res.get("duid"):
        out["duid"] = res["duid"]
    elif "hw_address" in res or "mac" in res:
        out["hw-address"] = res.get("hw_address") or res.get("mac")
    addr = res.get("ip_address") or res.get("ip")
    if addr:
        out["ip-addresses"] = [addr]
    if res.get("hostname"):
        out["hostname"] = res["hostname"]
    opts = _options_from_mapping_v6(res.get("options"))
    if opts:
        out["option-data"] = opts
    return out


def _pd_pool_v6(p: dict[str, Any]) -> dict[str, Any] | None:
    """Render a DHCPv6 prefix-delegation pool dict (issue #368).

    Mirrors the backend driver's ``_render_pd_pool``. Returns ``None`` on a
    malformed row so one bad pool can't fail the whole render.
    """
    pd_prefix = p.get("pd_prefix")
    delegated = p.get("delegated_length")
    if not pd_prefix or not delegated:
        return None
    try:
        net = ipaddress.ip_network(pd_prefix, strict=False)
    except ValueError:
        return None
    out: dict[str, Any] = {
        "prefix": str(net.network_address),
        "prefix-len": net.prefixlen,
        "delegated-len": int(delegated),
    }
    excluded = p.get("excluded_prefix")
    if excluded:
        try:
            ex = ipaddress.ip_network(excluded, strict=False)
            out["excluded-prefix"] = str(ex.network_address)
            out["excluded-prefix-len"] = ex.prefixlen
        except ValueError:
            # Malformed excluded-prefix: render the pd-pool without it rather
            # than dropping the pool. The control plane validates it on create/
            # update, so this is defensive against an unexpected wire value.
            _log.warning("dhcp_pd_excluded_prefix_invalid", value=excluded)
    if p.get("class_restriction"):
        out["client-class"] = p["class_restriction"]
    return out


def _scope_to_subnet6(scope: dict[str, Any]) -> dict[str, Any]:
    """Translate a wire-shape v6 ScopeDef dict into a Kea ``subnet6`` entry.

    Mirrors the backend driver's ``_render_scope`` for ``address_family
    == "ipv6"``: the ``v6_address_mode`` discriminator gates what Kea
    serves —

      * ``stateful``  → address pools + option-data
      * ``stateless`` → no pools, option-data only (Information-Request)
      * ``slaac``     → no pools, no option-data (the router's RA does it)

    Wire shape (from ``backend/app/api/v1/dhcp/agents.py``):
      {subnet_cidr, lease_time, options, address_family, v6_address_mode,
       pools:[{start_ip,end_ip,pool_type}],
       statics:[{ip_address,mac_address,hostname}], ddns_enabled}
    """
    cidr = scope["subnet_cidr"]
    mode = scope.get("v6_address_mode") or "stateful"
    serve_addresses = mode == "stateful"
    serve_options = mode in ("stateful", "stateless")

    out: dict[str, Any] = {
        "id": _stable_subnet_id(cidr),
        "subnet": cidr,
    }
    # Only dynamic pools become Kea lease pools; excluded/reserved ranges
    # are IPAM-level bookkeeping. SLAAC / stateless subnets serve no pools.
    if serve_addresses:
        dyn = [
            p
            for p in (scope.get("pools") or [])
            if (p.get("pool_type") or "dynamic") == "dynamic"
        ]
        if dyn:
            out["pools"] = [{"pool": f"{p['start_ip']} - {p['end_ip']}"} for p in dyn]
        # Prefix-delegation pools (issue #368) — drop malformed rows.
        pd = [
            p
            for p in (scope.get("pools") or [])
            if (p.get("pool_type") or "dynamic") == "pd"
        ]
        if pd:
            rendered_pd = [r for r in (_pd_pool_v6(p) for p in pd) if r is not None]
            if rendered_pd:
                out["pd-pools"] = rendered_pd
    if serve_options:
        opts = _options_from_mapping_v6(scope.get("options"))
        if opts:
            out["option-data"] = opts
    # A pure-SLAAC subnet has no DHCP role, so host reservations (which
    # assign addresses / host-specific options) are dropped too.
    if serve_addresses or serve_options:
        resv = [
            _reservation_v6(
                {
                    "ip_address": s["ip_address"],
                    "hw_address": s["mac_address"],
                    "duid": s.get("duid"),
                    "hostname": s.get("hostname") or "",
                    "options": s.get("options_override"),
                }
            )
            for s in (scope.get("statics") or [])
        ]
        if resv:
            out["reservations"] = resv
    if scope.get("lease_time"):
        out["valid-lifetime"] = int(scope["lease_time"])
    # #430 — honour the per-scope min/max lease bounds (Kea clamps the
    # client-requested lease into [min, max]). Omitted → Kea defaults.
    if scope.get("min_lease_time"):
        out["min-valid-lifetime"] = int(scope["min_lease_time"])
    if scope.get("max_lease_time"):
        out["max-valid-lifetime"] = int(scope["max_lease_time"])
    relay = scope.get("relay_addresses")
    if relay:
        out["relay"] = {"ip-addresses": list(relay)}
    return out


def _normalize_mac_for_kea(raw: str) -> str | None:
    """Return a normalized colon-separated lowercase MAC, or None if invalid.

    Kea's ``hexstring(pkt4.mac, ':')`` yields a lowercase colon-separated
    form — we must match that exactly. We accept operator input in the
    common variants (``AA-BB-CC-DD-EE-FF``, ``aabbccddeeff``, etc.) and
    coerce to the canonical shape. Anything that doesn't yield exactly
    12 hex chars is dropped with a warning rather than emitted malformed
    — a single bad row shouldn't take the whole Kea config down.
    """
    cleaned = "".join(ch for ch in raw.lower() if ch in "0123456789abcdef")
    if len(cleaned) != 12:
        _log.warning("drop_mac_invalid", raw=raw)
        return None
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


def _build_drop_expression(mac_blocks: list[dict[str, Any]]) -> str:
    """Build a Kea client-class ``test`` expression for the DROP list.

    Returns ``""`` when the list is empty — caller skips DROP rendering
    entirely in that case. We use ``hexstring(pkt4.mac, ':') == '...'``
    per MAC and OR them together. Kea has no upper limit on expression
    length in practice; at ~70 chars per clause a 10k-entry blocklist
    is ~700KB which Kea handles (validated against 2.6).
    """
    norms: list[str] = []
    for entry in mac_blocks:
        mac = entry.get("mac_address") if isinstance(entry, dict) else None
        if not mac:
            continue
        n = _normalize_mac_for_kea(str(mac))
        if n is not None:
            norms.append(n)
    if not norms:
        return ""
    return " or ".join(f"hexstring(pkt4.mac, ':') == '{m}'" for m in norms)


def _reservation(res: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "hw_address" in res or "mac" in res:
        out["hw-address"] = res.get("hw_address") or res.get("mac")
    if "client_id" in res:
        out["client-id"] = res["client_id"]
    if "duid" in res:
        out["duid"] = res["duid"]
    if "ip_address" in res or "ip" in res:
        out["ip-address"] = res.get("ip_address") or res.get("ip")
    if res.get("hostname"):
        out["hostname"] = res["hostname"]
    opts = _options_from_mapping(res.get("options"))
    if opts:
        out["option-data"] = opts
    if res.get("client_classes"):
        out["client-classes"] = list(res["client_classes"])
    return out


def _subnet(subnet: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": int(subnet["id"]),
        "subnet": subnet["subnet"],
    }
    pools = subnet.get("pools") or []
    if pools:
        out["pools"] = [
            {"pool": p["pool"]} if isinstance(p, dict) else {"pool": str(p)}
            for p in pools
        ]
    opts = _options_from_mapping(subnet.get("options"))
    if opts:
        out["option-data"] = opts
    resv = [_reservation(r) for r in (subnet.get("reservations") or [])]
    if resv:
        out["reservations"] = resv
    if subnet.get("valid_lifetime"):
        out["valid-lifetime"] = int(subnet["valid_lifetime"])
    if subnet.get("renew_timer"):
        out["renew-timer"] = int(subnet["renew_timer"])
    if subnet.get("rebind_timer"):
        out["rebind-timer"] = int(subnet["rebind_timer"])
    if subnet.get("client_class"):
        out["client-class"] = subnet["client_class"]
    if subnet.get("require_client_classes"):
        out["require-client-classes"] = list(subnet["require_client_classes"])
    if subnet.get("interface"):
        out["interface"] = subnet["interface"]
    if subnet.get("relay_ips"):
        out["relay"] = {"ip-addresses": list(subnet["relay_ips"])}
    return out


def _stable_subnet_id(cidr: str) -> int:
    """Derive a deterministic Kea subnet-id from the CIDR.

    Kea tracks leases by subnet-id and loses them if the id changes
    between renders. The control-plane wire format carries no numeric
    id, so we hash the CIDR into a stable uint32. Never zero (Kea
    treats 0 as "unassigned").
    """
    digest = hashlib.sha256(cidr.encode("utf-8")).digest()
    n = int.from_bytes(digest[:4], "big")
    return n or 1


def _scope_to_subnet(scope: dict[str, Any]) -> dict[str, Any]:
    """Translate a wire-shape ScopeDef dict into a Kea subnet4 entry.

    Wire shape (from ``backend/app/api/v1/dhcp/agents.py``):
      {subnet_cidr, lease_time, options, pools:[{start_ip,end_ip,pool_type}],
       statics:[{ip_address,mac_address,hostname}], ddns_enabled}
    """
    cidr = scope["subnet_cidr"]
    out: dict[str, Any] = {
        "id": _stable_subnet_id(cidr),
        "subnet": cidr,
    }
    # Only dynamic pools are Kea lease pools; excluded/reserved ranges
    # are IPAM-level bookkeeping and must NOT be offered as pools.
    dyn = [
        p
        for p in (scope.get("pools") or [])
        if (p.get("pool_type") or "dynamic") == "dynamic"
    ]
    if dyn:
        out["pools"] = [{"pool": f"{p['start_ip']} - {p['end_ip']}"} for p in dyn]
    opts = _options_from_mapping(scope.get("options"))
    if opts:
        out["option-data"] = opts
    resv = [
        _reservation(
            {
                "ip_address": s["ip_address"],
                "hw_address": s["mac_address"],
                "hostname": s.get("hostname") or "",
                "client_id": s.get("client_id"),
                "options": s.get("options_override"),
            }
        )
        for s in (scope.get("statics") or [])
    ]
    if resv:
        out["reservations"] = resv
    if scope.get("lease_time"):
        out["valid-lifetime"] = int(scope["lease_time"])
    # #430 — honour the per-scope min/max lease bounds (Kea clamps the
    # client-requested lease into [min, max]). Omitted → Kea defaults.
    if scope.get("min_lease_time"):
        out["min-valid-lifetime"] = int(scope["min_lease_time"])
    if scope.get("max_lease_time"):
        out["max-valid-lifetime"] = int(scope["max_lease_time"])
    # Relay-agent matching (issue #337) — Kea selects this subnet for
    # packets whose giaddr is one of these relay IPs. Required for
    # subnets not directly attached to a centralized server.
    relay = scope.get("relay_addresses")
    if relay:
        out["relay"] = {"ip-addresses": list(relay)}
    return out


def _resolve_peer_url(url: str) -> str:
    """Kea's HA hook parses peer URLs with Boost asio directly, so
    ``url`` must resolve to a literal IP address — hostnames aren't
    looked up by Kea itself. We resolve agent-side via the container's
    resolver (Docker DNS on compose, k8s DNS on Kubernetes) so operator-
    friendly hostnames like ``http://dhcp-kea-2:8000/`` keep working.

    Already-IP hosts are passed through unchanged. Resolution failures
    return the original URL so Kea surfaces a readable error instead of
    us silently swallowing a misconfig.
    """
    if not url:
        return url
    try:
        p = urlparse(url)
        host = p.hostname
        if not host:
            return url
        # Already a valid IPv4/IPv6 literal — nothing to do.
        try:
            ipaddress.ip_address(host)
            return url
        except ValueError:
            pass
        ip = socket.gethostbyname(host)
        port_part = f":{p.port}" if p.port else ""
        netloc = f"{ip}{port_part}"
        resolved = urlunparse(
            (p.scheme, netloc, p.path or "/", p.params, p.query, p.fragment)
        )
        _log.info("ha_peer_url_resolved", hostname=host, ip=ip, url=resolved)
        return resolved
    except (OSError, ValueError) as exc:
        _log.warning("ha_peer_url_resolve_failed", url=url, error=str(exc))
        return url


def _ha_hook(failover: dict[str, Any]) -> dict[str, Any]:
    """Render the ``libdhcp_ha.so`` hook entry from a failover payload.

    Shape mirrors Kea's HA hook reference (ARM §14.3): the hook takes
    a ``parameters`` dict that in turn contains a ``high-availability``
    list with one entry per relationship. Each entry has
    ``this-server-name``, ``mode``, a ``peers`` array, and heartbeat
    tuning.
    """
    peers = [
        {
            "name": p["name"],
            "url": _resolve_peer_url(p["url"]),
            "role": p["role"],
            "auto-failover": bool(p.get("auto-failover", True)),
        }
        for p in failover["peers"]
    ]
    relationship = {
        "this-server-name": failover["this_server_name"],
        "mode": failover["mode"],
        "heartbeat-delay": int(failover.get("heartbeat_delay_ms", 10000)),
        "max-response-delay": int(failover.get("max_response_delay_ms", 60000)),
        "max-ack-delay": int(failover.get("max_ack_delay_ms", 10000)),
        "max-unacked-clients": int(failover.get("max_unacked_clients", 5)),
        "peers": peers,
    }
    return {
        "library": "/usr/lib/kea/hooks/libdhcp_ha.so",
        "parameters": {"high-availability": [relationship]},
    }


def render(
    bundle: dict[str, Any],
    *,
    control_socket: str = "/run/kea/kea4-ctrl-socket",
    lease_file: str = "/var/lib/kea/kea-leases4.csv",
    control_socket_v6: str | None = None,
    lease_file_v6: str | None = None,
) -> dict[str, Any]:
    """Render a ConfigBundle into a Kea config document.

    Always returns BOTH a ``{"Dhcp4": {...}}`` and a ``{"Dhcp6": {...}}``
    block — the agent container runs kea-dhcp4 AND kea-dhcp6 always-on
    (dual-stack). When the bundle carries no ``address_family == "ipv6"``
    scope the Dhcp6 block is an idle skeleton (empty ``subnet6``, no
    option-data / client-classes) that binds nothing; when v6 scopes are
    present they render into ``subnet6``. Each daemon is a separate Kea
    process with its own control socket + lease store. This mirrors the
    v4/v6 split in ``backend/app/drivers/dhcp/kea.py``.

    NOTE: the two blocks MUST be written to SEPARATE config files —
    ``kea-dhcp4 -t`` rejects a document containing a stray ``Dhcp6`` key
    (and vice-versa). ``sync.py`` splits this combined return into
    ``kea-dhcp4.conf`` (Dhcp4 only) and ``kea-dhcp6.conf`` (Dhcp6 only).

    ``control_socket_v6`` / ``lease_file_v6`` default to the v4 paths with
    the ``4`` swapped for ``6`` (``kea4-ctrl-socket`` → ``kea6-ctrl-socket``,
    ``kea-leases4.csv`` → ``kea-leases6.csv``) so the v6 daemon never
    collides with the v4 daemon's socket / lease store.
    """
    server = bundle.get("server", {}) or {}
    interfaces = server.get("interfaces") or ["*"]

    dhcp4: dict[str, Any] = {
        "interfaces-config": {
            "interfaces": list(interfaces),
            # Issue #365 — default to ``raw`` (AF_PACKET) so Kea hears
            # broadcast DISCOVERs from directly-attached clients. The
            # control plane sends the resolved value (``raw`` for group
            # socket_mode "direct", ``udp`` for "relay"); the ``raw``
            # fallback only applies to bundles from an older control plane
            # that predates the ``server`` block. ``raw`` needs CAP_NET_RAW
            # (granted on the appliance DaemonSet + compose Kea services).
            "dhcp-socket-type": server.get("dhcp_socket_type", "raw"),
        },
        "control-socket": {
            "socket-type": "unix",
            "socket-name": control_socket,
        },
        "lease-database": {
            "type": "memfile",
            "persist": True,
            "name": lease_file,
            "lfc-interval": 3600,
        },
        "expired-leases-processing": {
            "reclaim-timer-wait-time": 10,
            "flush-reclaimed-timer-wait-time": 25,
            "hold-reclaimed-time": 3600,
            "max-reclaim-leases": 100,
            "max-reclaim-time": 250,
            "unwarned-reclaim-cycles": 5,
        },
        "valid-lifetime": int(
            bundle.get("global_options", {}).get("lease_time") or 3600
        ),
        "renew-timer": 900,
        "rebind-timer": 1800,
        "hooks-libraries": [
            {"library": "/usr/lib/kea/hooks/libdhcp_lease_cmds.so"},
        ],
        "loggers": [
            {
                "name": "kea-dhcp4",
                # Two outputs by design:
                #   * stdout — picked up by `docker logs` for the
                #     existing operator workflow.
                #   * file — tailed by ``LogShipper`` and shipped to
                #     the control plane for the Logs UI's "DHCP
                #     Activity" tab. Kea rotates the file in-process
                #     via ``maxsize`` / ``maxver`` so we don't need
                #     external logrotate.
                "output_options": [
                    {"output": "stdout"},
                    {
                        "output": "/var/log/kea/kea-dhcp4.log",
                        "maxsize": 50_000_000,
                        "maxver": 5,
                        "flush": True,
                    },
                ],
                "severity": "INFO",
            }
        ],
    }

    # HA hook — only present when the control plane pins this server to
    # a DHCPFailoverChannel. Kea rejects a config that references
    # ``libdhcp_ha.so`` without matching ``libdhcp_lease_cmds.so``, so
    # the lease_cmds hook above is load-bearing here too.
    failover = bundle.get("failover")
    if isinstance(failover, dict) and failover.get("peers"):
        dhcp4["hooks-libraries"].append(_ha_hook(failover))

    opts = _options_from_mapping(bundle.get("global_options"))
    if opts:
        dhcp4["option-data"] = opts

    # Prefer the canonical control-plane wire shape (``scopes``). Fall
    # back to the legacy pre-translated ``subnets`` shape for tests /
    # hand-crafted bundles that still use it.
    #
    # Split the canonical wire scopes by address family: v4 scopes render
    # into ``subnet4`` here, v6 scopes into a ``Dhcp6`` block below. The
    # legacy ``subnets`` shape is v4-only by construction.
    scopes = bundle.get("scopes")
    v6_scopes: list[dict[str, Any]] = []
    if scopes is not None:
        v4_scopes = [s for s in scopes if (s.get("address_family") or "ipv4") != "ipv6"]
        v6_scopes = [s for s in scopes if (s.get("address_family") or "ipv4") == "ipv6"]
        dhcp4["subnet4"] = [_scope_to_subnet(s) for s in v4_scopes]
    else:
        dhcp4["subnet4"] = [_subnet(s) for s in (bundle.get("subnets") or [])]

    # Client classes: wire carries ``match_expression``, legacy/hand-
    # crafted fixtures carry ``test``. Accept either.
    classes = bundle.get("client_classes") or []
    rendered_classes: list[dict[str, Any]] = [
        {
            "name": c["name"],
            **(
                {"test": c.get("test") or c.get("match_expression")}
                if (c.get("test") or c.get("match_expression"))
                else {}
            ),
            **(
                {"option-data": _options_from_mapping(c.get("options"))}
                if c.get("options")
                else {}
            ),
        }
        for c in classes
    ]

    # MAC blocklist — render as Kea's reserved ``DROP`` class. Any packet
    # whose hardware address matches the OR-ed expression is silently
    # dropped before allocation. ``DROP`` is a Kea built-in name, not
    # something the operator can reuse — so if a user-defined class is
    # already named ``DROP`` we skip blocklist rendering to avoid
    # clobbering it (defensive; the API already reserves the name).
    drop_expr = _build_drop_expression(bundle.get("mac_blocks") or [])
    if drop_expr and not any(c.get("name") == "DROP" for c in rendered_classes):
        rendered_classes.append({"name": "DROP", "test": drop_expr})

    if rendered_classes:
        dhcp4["client-classes"] = rendered_classes

    if bundle.get("reservation_mode"):
        dhcp4["reservation-mode"] = bundle["reservation_mode"]

    out: dict[str, Any] = {"Dhcp4": dhcp4}

    # Dhcp6 block — ALWAYS emitted. The agent container runs kea-dhcp6
    # always-on (dual-stack), so a valid Dhcp6 doc must always be
    # rendered. When the bundle carries no v6 scopes the block is an
    # idle skeleton: no host interfaces bound (``interfaces: []``, safe
    # on IPv6-less hosts), empty ``subnet6``, and no global option-data /
    # client-classes. When v6 scopes ARE present the daemon binds the
    # bundle's interfaces and serves them. The Dhcp6 daemon is a separate
    # Kea process with its own control socket + lease store; option-data
    # renders through the v6 name map (v4-only options dropped with a
    # warning), and reservations use the v6 ``ip-addresses`` (plural)
    # shape. Mirrors the ``Dhcp6`` block in
    # ``backend/app/drivers/dhcp/kea.py``.
    ctrl6 = control_socket_v6 or control_socket.replace("kea4", "kea6")
    lease6 = lease_file_v6 or lease_file.replace("leases4", "leases6")
    # Idle skeleton binds nothing; an active v6 config binds the bundle's
    # interfaces just like Dhcp4.
    v6_interfaces = list(interfaces) if v6_scopes else []
    dhcp6: dict[str, Any] = {
        "interfaces-config": {"interfaces": v6_interfaces},
        # Match host reservations on DUID (v6-native, issue #368) then
        # hw-address. Mirrors the backend Dhcp6 block.
        "host-reservation-identifiers": ["duid", "hw-address"],
        "control-socket": {
            "socket-type": "unix",
            "socket-name": ctrl6,
        },
        "lease-database": {
            "type": "memfile",
            "persist": True,
            "name": lease6,
            "lfc-interval": 3600,
        },
        "expired-leases-processing": {
            "reclaim-timer-wait-time": 10,
            "flush-reclaimed-timer-wait-time": 25,
            "hold-reclaimed-time": 3600,
            "max-reclaim-leases": 100,
            "max-reclaim-time": 250,
            "unwarned-reclaim-cycles": 5,
        },
        "valid-lifetime": int(
            bundle.get("global_options", {}).get("lease_time") or 3600
        ),
        "renew-timer": 900,
        "rebind-timer": 1800,
        "hooks-libraries": [
            {"library": "/usr/lib/kea/hooks/libdhcp_lease_cmds.so"},
        ],
        "subnet6": [_scope_to_subnet6(s) for s in v6_scopes],
        "loggers": [
            {
                "name": "kea-dhcp6",
                "output_options": [
                    {"output": "stdout"},
                    {
                        "output": "/var/log/kea/kea-dhcp6.log",
                        "maxsize": 50_000_000,
                        "maxver": 5,
                        "flush": True,
                    },
                ],
                "severity": "INFO",
            }
        ],
    }
    # Global option-data + client classes only apply when v6 scopes are
    # being served — the idle skeleton stays bare so it can't reject on
    # an inherited v4-only global option.
    if v6_scopes:
        opts6 = _options_from_mapping_v6(bundle.get("global_options"))
        if opts6:
            dhcp6["option-data"] = opts6
        # Client classes render through the v6 name map. The MAC blocklist
        # DROP class is v4-only (Kea v6 has no ``pkt4.mac``) so it is not
        # carried into Dhcp6 — matching the backend driver.
        rendered_classes_v6: list[dict[str, Any]] = [
            {
                "name": c["name"],
                **(
                    {"test": c.get("test") or c.get("match_expression")}
                    if (c.get("test") or c.get("match_expression"))
                    else {}
                ),
                **(
                    {"option-data": _options_from_mapping_v6(c.get("options"))}
                    if c.get("options")
                    else {}
                ),
            }
            for c in classes
        ]
        if rendered_classes_v6:
            dhcp6["client-classes"] = rendered_classes_v6
    out["Dhcp6"] = dhcp6

    return out
