"""Kea DHCP driver.

Renders the SpatiumDDI ``ConfigBundle`` into a Kea Dhcp4 JSON config
structure (see https://kea.readthedocs.io/). The agent container pushes
that config via the Kea control-agent HTTP API (config-set + config-reload)
or by writing ``/etc/kea/kea-dhcp4.conf`` + restarting the daemon.

Only a minimal control-channel implementation is included here — heavy
lifting happens in the agent runtime. The control plane is responsible
for *shape* (valid JSON) and *auditing*, not daemon transport.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.drivers.dhcp.base import (
    ClientClassDef,
    ConfigBundle,
    DHCPDriver,
    PoolDef,
    ScopeDef,
    StaticAssignmentDef,
)

logger = structlog.get_logger(__name__)


# Map of SpatiumDDI option-name → Kea option-data ``name`` in the default
# ``dhcp4`` option space. Kea accepts standard DHCPv4 options by their
# IANA names here.
_KEA_OPTION_NAMES: dict[str, str] = {
    "routers": "routers",
    "dns-servers": "domain-name-servers",
    "domain-name": "domain-name",
    "broadcast-address": "broadcast-address",
    "ntp-servers": "ntp-servers",
    "tftp-server-name": "tftp-server-name",
    "bootfile-name": "boot-file-name",
    "tftp-server-address": "tftp-server-address",
    "domain-search": "domain-search",
    "mtu": "interface-mtu",
    "time-offset": "time-offset",
}

# Map of SpatiumDDI option-name → Kea Dhcp6 ``option-data`` name. DHCPv6
# uses a different option-code space + cmdlet names; only options that
# have a true v6 equivalent are forwarded. Options with no v6 analogue
# (``routers`` — v6 uses Router Advertisements; ``broadcast-address`` —
# no broadcast in v6; ``mtu`` / ``time-offset`` — no native option) are
# dropped from v6 scopes with a warning log so the operator can spot
# misconfigured inheritance rather than Kea silently rejecting the
# config on reload.
_KEA_OPTION_NAMES_V6: dict[str, str] = {
    "dns-servers": "dns-servers",         # DHCPv6 option 23
    "domain-search": "domain-search",     # DHCPv6 option 24
    "ntp-servers": "sntp-servers",        # DHCPv6 option 31 (SNTP)
    "bootfile-name": "bootfile-url",      # DHCPv6 option 59 (URL form)
}

# Options that have no DHCPv6 equivalent — dropped from v6 scopes.
_DHCP4_ONLY_OPTION_NAMES: frozenset[str] = frozenset(
    {"routers", "broadcast-address", "mtu", "time-offset",
     "domain-name", "tftp-server-name", "tftp-server-address"}
)


def _render_option_data(
    options: dict[str, Any], *, address_family: str = "ipv4"
) -> list[dict[str, Any]]:
    """Translate a {name: value} options map into Kea's ``option-data`` list.

    ``address_family="ipv6"`` routes through the Dhcp6 name map and
    drops options that don't exist in DHCPv6 — emitting a v4 option
    under the Dhcp6 block would make Kea reject the config on reload.
    """
    is_v6 = address_family == "ipv6"
    name_map = _KEA_OPTION_NAMES_V6 if is_v6 else _KEA_OPTION_NAMES
    out: list[dict[str, Any]] = []
    for key, val in options.items():
        if is_v6 and key in _DHCP4_ONLY_OPTION_NAMES:
            logger.warning(
                "kea_option_skipped_v6_no_equivalent",
                option=key,
                reason="no DHCPv6 equivalent",
            )
            continue
        kea_name = name_map.get(key, key)
        if isinstance(val, list):
            data = ", ".join(str(x) for x in val)
        else:
            data = str(val)
        out.append({"name": kea_name, "data": data})
    return out


def _render_pool(pool: PoolDef, *, address_family: str = "ipv4") -> dict[str, Any]:
    # Kea expresses "excluded" / "reserved" pools indirectly via
    # reservations + pool boundaries. We emit dynamic pools only; excluded
    # ranges are conveyed to the agent as metadata for boundary splitting.
    d: dict[str, Any] = {"pool": f"{pool.start_ip} - {pool.end_ip}"}
    if pool.class_restriction:
        d["client-class"] = pool.class_restriction
    if pool.options_override:
        d["option-data"] = _render_option_data(
            pool.options_override, address_family=address_family
        )
    return d


def _render_reservation(
    s: StaticAssignmentDef, *, address_family: str = "ipv4"
) -> dict[str, Any]:
    # Dhcp6 reservations use ``ip-addresses`` (plural, list) and don't
    # accept ``hw-address`` as the match key by default — ``duid`` /
    # ``hw-address`` is configured per-subnet in real deployments. Emit
    # the minimum viable v6 shape and let the agent / operator layer on
    # ``host-reservation-identifiers`` via server options.
    if address_family == "ipv6":
        r: dict[str, Any] = {
            "hw-address": s.mac_address,
            "ip-addresses": [s.ip_address],
        }
    else:
        r = {
            "hw-address": s.mac_address,
            "ip-address": s.ip_address,
        }
    if s.hostname:
        r["hostname"] = s.hostname
    if s.client_id:
        r["client-id"] = s.client_id
    if s.options_override:
        r["option-data"] = _render_option_data(
            s.options_override, address_family=address_family
        )
    return r


def _render_scope(scope: ScopeDef) -> dict[str, Any]:
    af = scope.address_family  # "ipv4" | "ipv6"
    dynamic_pools = [p for p in scope.pools if p.pool_type == "dynamic"]
    subnet_key = "subnet"  # Kea names the CIDR field "subnet" for both families
    pools_key = "pools" if af != "ipv6" else "pools"  # same in both
    out: dict[str, Any] = {
        subnet_key: scope.subnet_cidr,
        pools_key: [_render_pool(p, address_family=af) for p in dynamic_pools],
        "reservations": [_render_reservation(s, address_family=af) for s in scope.statics],
        "valid-lifetime": scope.lease_time,
    }
    if scope.min_lease_time is not None:
        out["min-valid-lifetime"] = scope.min_lease_time
    if scope.max_lease_time is not None:
        out["max-valid-lifetime"] = scope.max_lease_time
    if scope.options:
        out["option-data"] = _render_option_data(scope.options, address_family=af)
    return out


def _render_client_class(
    c: ClientClassDef, *, address_family: str = "ipv4"
) -> dict[str, Any]:
    d: dict[str, Any] = {"name": c.name}
    if c.match_expression:
        d["test"] = c.match_expression
    if c.options:
        d["option-data"] = _render_option_data(c.options, address_family=address_family)
    return d


class KeaDriver(DHCPDriver):
    """Kea DHCPv4 driver — emits a ``Dhcp4`` JSON config structure."""

    name = "kea"

    def render_config(self, bundle: ConfigBundle) -> str:
        # Split scopes by address family. The Kea daemons Dhcp4 and Dhcp6
        # are separate processes; the agent runs whichever process(es) it
        # has scopes for. We emit both top-level blocks so the agent can
        # consume a single bundle regardless of family mix.
        v4_scopes = [s for s in bundle.scopes if s.is_active and s.address_family != "ipv6"]
        v6_scopes = [s for s in bundle.scopes if s.is_active and s.address_family == "ipv6"]

        out: dict[str, Any] = {}
        if v4_scopes or not v6_scopes:
            out["Dhcp4"] = {
                "valid-lifetime": bundle.options.lease_time,
                "interfaces-config": {"interfaces": ["*"]},
                "lease-database": {
                    "type": "memfile",
                    "persist": True,
                    "name": "/var/lib/kea/kea-leases4.csv",
                },
                "subnet4": [_render_scope(s) for s in v4_scopes],
                "client-classes": [
                    _render_client_class(c, address_family="ipv4")
                    for c in bundle.client_classes
                ],
                "option-data": _render_option_data(
                    bundle.options.options, address_family="ipv4"
                ),
            }
        if v6_scopes:
            # Kea names the subnet list "subnet6" in Dhcp6 mode. Options /
            # client-class options render through the Dhcp6 name map;
            # v4-only options (routers, mtu, …) are dropped with a
            # warning log rather than emitted under the wrong space.
            out["Dhcp6"] = {
                "valid-lifetime": bundle.options.lease_time,
                "interfaces-config": {"interfaces": ["*"]},
                "lease-database": {
                    "type": "memfile",
                    "persist": True,
                    "name": "/var/lib/kea/kea-leases6.csv",
                },
                "subnet6": [_render_scope(s) for s in v6_scopes],
                "client-classes": [
                    _render_client_class(c, address_family="ipv6")
                    for c in bundle.client_classes
                ],
                "option-data": _render_option_data(
                    bundle.options.options, address_family="ipv6"
                ),
            }
        return json.dumps(out, indent=2, sort_keys=True)

    async def apply_config(self, server: Any, bundle: ConfigBundle) -> None:
        logger.info(
            "kea_apply_config",
            server_id=str(getattr(server, "id", "?")),
            etag=bundle.etag,
        )
        # Agent-side: POST to Kea control-agent. Control plane just logs +
        # enqueues; the real call happens in the agent runtime.

    async def reload(self, server: Any) -> None:
        logger.info("kea_reload", server_id=str(getattr(server, "id", "?")))

    async def restart(self, server: Any) -> None:
        logger.info("kea_restart", server_id=str(getattr(server, "id", "?")))

    async def get_leases(self, server: Any) -> list[dict[str, Any]]:
        # Agent pushes leases via /agents/lease-events; control plane read
        # goes through the lease table, not the driver.
        return []

    async def health_check(self, server: Any) -> tuple[bool, str]:
        return True, "ok"

    def validate_config(self, bundle: ConfigBundle) -> tuple[bool, list[str]]:
        errors: list[str] = []
        seen_subnets: set[str] = set()
        for s in bundle.scopes:
            if s.subnet_cidr in seen_subnets:
                errors.append(f"duplicate subnet: {s.subnet_cidr}")
            seen_subnets.add(s.subnet_cidr)
            for p in s.pools:
                if not p.start_ip or not p.end_ip:
                    errors.append(f"pool in {s.subnet_cidr} missing start/end")
        return (not errors), errors

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "kea",
            "version": "2.x",
            "options": sorted(_KEA_OPTION_NAMES.keys()),
            "features": {
                "client_classes": True,
                "reservations": True,
                "ddns": True,
                "ha": True,
            },
        }


__all__ = ["KeaDriver"]
