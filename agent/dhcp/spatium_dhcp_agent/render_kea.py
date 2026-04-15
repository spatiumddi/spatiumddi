"""Neutral ConfigBundle → Kea ``Dhcp4`` JSON renderer.

The control plane emits a backend-neutral ConfigBundle describing scopes,
pools, statics, client classes, reservations and global/scope-level options.
This module converts that into a Kea-specific ``Dhcp4`` JSON document.

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

from typing import Any

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

    _put("domain-name-servers", options.get("dns_servers") or options.get("domain-name-servers"))
    _put("ntp-servers", options.get("ntp_servers") or options.get("ntp-servers"))
    _put("routers", options.get("routers") or options.get("gateway"))
    _put("domain-name", options.get("domain_name") or options.get("domain-name"))
    _put("domain-search", options.get("domain_search") or options.get("domain-search"))
    _put("tftp-server-name", options.get("tftp_server") or options.get("tftp-server-name"))
    _put("boot-file-name", options.get("boot_file") or options.get("boot-file-name"))

    # Pass through any raw Kea-style list already shaped correctly.
    raw = options.get("option_data")
    if isinstance(raw, list):
        out.extend(raw)
    return out


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
            {"pool": p["pool"]} if isinstance(p, dict) else {"pool": str(p)} for p in pools
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


def render(
    bundle: dict[str, Any],
    *,
    control_socket: str = "/run/kea/kea4-ctrl-socket",
    lease_file: str = "/var/lib/kea/kea-leases4.csv",
) -> dict[str, Any]:
    """Render a ConfigBundle to a Kea ``{"Dhcp4": {...}}`` document."""
    server = bundle.get("server", {}) or {}
    interfaces = server.get("interfaces") or ["*"]

    dhcp4: dict[str, Any] = {
        "interfaces-config": {"interfaces": list(interfaces)},
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
        "valid-lifetime": int(bundle.get("global_options", {}).get("lease_time") or 3600),
        "renew-timer": 900,
        "rebind-timer": 1800,
        "hooks-libraries": [
            {"library": "/usr/lib/kea/hooks/libdhcp_lease_cmds.so"},
        ],
        "loggers": [
            {
                "name": "kea-dhcp4",
                "output_options": [{"output": "stdout"}],
                "severity": "INFO",
            }
        ],
    }

    opts = _options_from_mapping(bundle.get("global_options"))
    if opts:
        dhcp4["option-data"] = opts

    dhcp4["subnet4"] = [_subnet(s) for s in (bundle.get("subnets") or [])]

    classes = bundle.get("client_classes") or []
    if classes:
        dhcp4["client-classes"] = [
            {
                "name": c["name"],
                **({"test": c["test"]} if c.get("test") else {}),
                **(
                    {"option-data": _options_from_mapping(c.get("options"))}
                    if c.get("options")
                    else {}
                ),
            }
            for c in classes
        ]

    if bundle.get("reservation_mode"):
        dhcp4["reservation-mode"] = bundle["reservation_mode"]

    return {"Dhcp4": dhcp4}
