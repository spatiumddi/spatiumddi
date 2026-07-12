"""FortiGate DHCP driver (agentless, FortiOS REST API + API token).

FortiGate firewalls run a per-interface DHCP server. SpatiumDDI manages
them agentlessly: the control plane calls the FortiOS REST API directly
with an API-admin **Bearer token**, VDOM-scoped, rather than driving a
co-located agent (see :mod:`app.drivers.dhcp._cloud_base` for the
agentless contract).

**Model mapping** (one SpatiumDDI ``DHCPServer(driver="fortigate")`` per
FortiGate device + VDOM):

* A SpatiumDDI ``DHCPScope`` (one subnet) maps to the FortiGate
  ``system.dhcp.server`` object on the **interface whose primary IP+netmask
  CIDR equals the scope CIDR**. We match on CIDR and configure that
  interface's DHCP server — we never create interfaces or change interface
  IPs. No matching interface → the apply fails with a clear error;
  multiple matches → an ambiguity error.
* Dynamic pools → ``ip-range`` entries (FortiGate supports several).
* ``excluded`` + ``reserved`` pools → ``exclude-range`` entries.
* Static assignments (MAC → IP) → ``reserved-address`` entries.
* Scope options → first-class fields where FortiGate has them
  (``default-gateway`` / ``dns-server1..4`` / ``domain`` / ``ntp-server1..3``
  / ``filename`` / ``lease-time``), the generic ``options`` subtable
  otherwise. ``netmask`` is always derived from the subnet CIDR.

The write unit is the **whole DHCP-server object per scope**: any
scope/pool/static/option edit rebuilds the full desired object and PUTs it
(create-if-absent). This makes "replace-all per interface" atomic. The
interface name is the natural key — one DHCP server per interface — so the
driver stays stateless (no FortiGate mkey persisted).

Credential dict shape (Fernet-encrypted on ``DHCPServer.credentials_encrypted``)::

    {"api_token": "<api-admin-token>", "vdom": "root", "verify_tls": false}

``verify_tls`` defaults to ``False`` (lab firewalls ship self-signed
certs) and a WARNING is logged when TLS verification is disabled, matching
the proxmox / unifi clients.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.drivers.dhcp._cloud_base import (
    AgentlessDHCPDriverBase,
    CloudDHCPError,
    CloudDHCPProbe,
)
from app.drivers.dhcp.base import ScopeDef

logger = structlog.get_logger(__name__)


# Canonical SpatiumDDI option name → FortiGate DHCP option code, for the
# options SpatiumDDI models that FortiGate has no dedicated field for.
# ``routers`` / ``dns-servers`` / ``domain-name`` / ``ntp-servers`` /
# ``bootfile-name`` are handled as first-class fields, so they're absent
# here.
_OPTION_NAME_TO_CODE: dict[str, int] = {
    "time-offset": 2,
    "mtu": 26,
    "tftp-server-name": 66,
    "domain-search": 119,
    "tftp-server-address": 150,
}

# Option names FortiGate derives itself / that map to a first-class field,
# so they never go into the generic ``options`` subtable.
_OPTIONS_HANDLED_ELSEWHERE: frozenset[str] = frozenset(
    {
        "routers",
        "dns-servers",
        "domain-name",
        "ntp-servers",
        "bootfile-name",
        "broadcast-address",
    }
)


def _as_list(value: Any) -> list[str]:
    """Normalise an option value (str / list / comma|space-separated) to a
    list of trimmed non-empty strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [p.strip() for p in str(value).replace(",", " ").split()]
    return [i for i in items if i]


def _ip_int(value: Any) -> int | None:
    """Parse an IPv4 string to its integer form, or None if invalid."""
    try:
        return int(ipaddress.IPv4Address(str(value)))
    except (ipaddress.AddressValueError, ValueError):
        return None


def _int_ip(value: int) -> str:
    """Render an integer back to dotted-quad IPv4."""
    return str(ipaddress.IPv4Address(value))


def _all_ips(values: list[str]) -> bool:
    """True when every value parses as an IP address."""
    if not values:
        return False
    for v in values:
        try:
            ipaddress.ip_address(v)
        except ValueError:
            return False
    return True


class FortiGateDHCPDriver(AgentlessDHCPDriverBase):
    """Agentless driver for FortiGate per-interface DHCP servers."""

    name = "fortigate"
    # The create modal renders + the probe requires these. ``verify_tls``
    # is a bool checkbox; ``vdom`` defaults to ``root`` in the UI.
    credential_fields: tuple[str, ...] = ("api_token", "vdom", "verify_tls")

    # ── HTTP plumbing ───────────────────────────────────────────────────
    def _client(self, server: Any, creds: dict[str, Any]) -> httpx.AsyncClient:
        """Return an httpx client bound to the FortiGate REST API base.

        Single seam tests patch to inject a fake transport — keep every
        request flowing through the returned client. The API token rides
        in the Authorization header (never the URL) and the VDOM is a
        default query param on every call.
        """
        token = self._token(creds)
        host = getattr(server, "host", "") or ""
        port = int(getattr(server, "port", 443) or 443)
        vdom = str(creds.get("vdom") or "root")
        verify = bool(creds.get("verify_tls", False))
        if not verify:
            logger.warning(
                "fortigate.tls_verification_disabled",
                server=str(getattr(server, "id", "")),
                host=host,
            )
        return httpx.AsyncClient(
            base_url=f"https://{host}:{port}/api/v2",
            headers={"Authorization": f"Bearer {token}"},
            params={"vdom": vdom},
            verify=verify,
            timeout=30.0,
        )

    @staticmethod
    def _token(creds: dict[str, Any]) -> str:
        token = (creds or {}).get("api_token")
        if not token:
            raise CloudDHCPError("FortiGate credentials missing 'api_token'.")
        return str(token)

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict[str, Any]:
        """Validate a FortiOS response and return its parsed body.

        FortiOS returns ``{"http_status": 200, "status": "success",
        "results": [...], "mkey": <n>}`` on success and a non-2xx status
        (or ``status: "error"``) on failure. Raise :class:`CloudDHCPError`
        with the FortiOS error code / http status so the write-through
        surfaces a clean 502.
        """
        try:
            body = response.json()
        except (ValueError, TypeError):
            body = {}
        status = response.status_code
        api_status = str(body.get("status", "")) if isinstance(body, dict) else ""
        if 200 <= status < 300 and api_status in ("", "success"):
            return body if isinstance(body, dict) else {"results": body}
        err = ""
        if isinstance(body, dict):
            err = str(body.get("error") or body.get("cli_error") or body.get("message") or "")
        detail = f"HTTP {status}" + (f" (error {err})" if err else "")
        raise CloudDHCPError(f"FortiGate API error: {detail}")

    # ── Interface matching ──────────────────────────────────────────────
    async def _list_interface_cidrs(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Return ``[{name, cidr, ip, netmask, status}]`` for L3 interfaces.

        Only the interface's PRIMARY IP is considered (per plan) — secondary
        IPs are ignored. Interfaces with no IP (``0.0.0.0``) are dropped.
        """
        resp = await client.get("/cmdb/system/interface")
        body = self._unwrap(resp)
        out: list[dict[str, Any]] = []
        for iface in body.get("results") or []:
            name = iface.get("name")
            addr, mask = self._parse_iface_ip(iface.get("ip"))
            if not name or not addr or addr == "0.0.0.0" or not mask:
                continue
            try:
                net = ipaddress.ip_network(f"{addr}/{mask}", strict=False)
            except (ValueError, TypeError):
                continue
            out.append(
                {
                    "name": name,
                    "cidr": str(net),
                    "ip": addr,
                    "netmask": mask,
                    "status": iface.get("status") or "",
                    "alias": iface.get("alias") or "",
                }
            )
        return out

    @staticmethod
    def _parse_iface_ip(raw: Any) -> tuple[str, str]:
        """Parse a FortiOS interface ``ip`` field into ``(addr, netmask)``.

        FortiOS returns this either as a 2-element list ``["1.2.3.4",
        "255.255.255.0"]`` or a space-separated string ``"1.2.3.4
        255.255.255.0"`` depending on version. Handle both.
        """
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            return str(raw[0]).strip(), str(raw[1]).strip()
        if isinstance(raw, str):
            parts = raw.split()
            if len(parts) >= 2:
                return parts[0].strip(), parts[1].strip()
        return "", ""

    async def _match_interface(self, client: httpx.AsyncClient, subnet_cidr: str) -> str:
        """Return the name of the interface whose primary CIDR == ``subnet_cidr``.

        Raises :class:`CloudDHCPError` on no match / ambiguous match so the
        write-through rolls back with a clear 502.
        """
        try:
            target = ipaddress.ip_network(subnet_cidr, strict=False)
        except (ValueError, TypeError) as exc:
            raise CloudDHCPError(f"Scope CIDR {subnet_cidr!r} is not a valid network") from exc
        matches = [
            iface["name"]
            for iface in await self._list_interface_cidrs(client)
            if ipaddress.ip_network(iface["cidr"], strict=False) == target
        ]
        if not matches:
            raise CloudDHCPError(
                f"No FortiGate interface has an IP in {subnet_cidr}. Configure the "
                "interface IP/netmask on the FortiGate first, then retry."
            )
        if len(matches) > 1:
            raise CloudDHCPError(
                f"Multiple FortiGate interfaces match {subnet_cidr} "
                f"({', '.join(matches)}); cannot decide which to configure."
            )
        return matches[0]

    # ── DHCP-server object body ─────────────────────────────────────────
    def _build_server_body(self, interface: str, scope: ScopeDef) -> dict[str, Any]:
        """Render the full ``system.dhcp.server`` object body for ``scope``."""
        net = ipaddress.ip_network(scope.subnet_cidr, strict=False)
        opts = scope.options or {}

        body: dict[str, Any] = {
            "status": "enable" if scope.is_active else "disable",
            "interface": interface,
            "netmask": str(net.netmask),
            "lease-time": int(scope.lease_time or 0),
        }

        # First-class scalar fields are ALWAYS emitted (clearing to their
        # "unset" value when the option is absent) so a whole-object PUT
        # achieves replace-all for scalars too — otherwise an option removed
        # in SpatiumDDI would linger on the FortiGate.

        # default-gateway ← routers (first entry only; FortiGate has one).
        routers = _as_list(opts.get("routers"))
        body["default-gateway"] = routers[0] if routers else "0.0.0.0"

        # DNS ← dns-servers (4 dedicated slots). Absent → dns-service default
        # and all slots cleared.
        dns = _as_list(opts.get("dns-servers"))
        body["dns-service"] = "specify" if dns else "default"
        for i in range(1, 5):
            body[f"dns-server{i}"] = dns[i - 1] if i <= len(dns) else "0.0.0.0"

        # domain ← domain-name (scalar). FortiGate rejects an EMPTY domain
        # ("not a valid domain name", error -651), so — unlike the other
        # scalars — it can't be cleared via the API. We only set it when
        # present; removing the domain-name option in SpatiumDDI leaves the
        # last-set domain on the FortiGate (documented caveat).
        domain = _as_list(opts.get("domain-name"))
        if domain:
            body["domain"] = domain[0]

        # NTP ← ntp-servers (3 dedicated slots).
        ntp = _as_list(opts.get("ntp-servers"))
        body["ntp-service"] = "specify" if ntp else "default"
        for i in range(1, 4):
            body[f"ntp-server{i}"] = ntp[i - 1] if i <= len(ntp) else "0.0.0.0"

        # bootfile-name ← filename (scalar).
        bootfile = _as_list(opts.get("bootfile-name"))
        body["filename"] = bootfile[0] if bootfile else ""

        # ip-range ← dynamic pools; exclude-range ← excluded/reserved pools.
        #
        # FortiGate requires every ``exclude-range`` to fall WITHIN a
        # ``ip-range`` (an out-of-range exclude is rejected with FortiOS
        # error -40, verified live on 7.4.12). Excluding addresses the pool
        # never hands out is a no-op anyway, so we clip each excluded/reserved
        # pool to its intersection with the dynamic range(s) and drop any
        # portion that falls outside.
        dyn: list[tuple[int, int]] = []
        ip_ranges: list[dict[str, Any]] = []
        for pool in scope.pools:
            if pool.pool_type != "dynamic":
                continue
            s, e = _ip_int(pool.start_ip), _ip_int(pool.end_ip)
            if s is None or e is None:
                continue
            dyn.append((s, e))
            ip_ranges.append(
                {
                    "id": len(ip_ranges) + 1,
                    "start-ip": str(pool.start_ip),
                    "end-ip": str(pool.end_ip),
                }
            )
        exclude_ranges: list[dict[str, Any]] = []
        for pool in scope.pools:
            if pool.pool_type not in ("excluded", "reserved"):
                continue
            ps, pe = _ip_int(pool.start_ip), _ip_int(pool.end_ip)
            if ps is None or pe is None:
                continue
            for ds, de in dyn:
                lo, hi = max(ps, ds), min(pe, de)
                if lo <= hi:
                    exclude_ranges.append(
                        {
                            "id": len(exclude_ranges) + 1,
                            "start-ip": _int_ip(lo),
                            "end-ip": _int_ip(hi),
                        }
                    )
            if not any(max(ps, ds) <= min(pe, de) for ds, de in dyn):
                logger.warning(
                    "fortigate.exclude_range_outside_pool_dropped",
                    subnet=scope.subnet_cidr,
                    start=str(pool.start_ip),
                    end=str(pool.end_ip),
                )
        body["ip-range"] = ip_ranges
        body["exclude-range"] = exclude_ranges

        # reserved-address ← static assignments (MAC → IP).
        #
        # NOTE: do NOT send ``action: "assign"`` — on FortiOS 7.4.x it makes
        # the API silently store the reserved ``ip`` as ``0.0.0.0`` (verified
        # live on 7.4.12; a ``{type, ip, mac}`` body stores the IP correctly,
        # adding ``action`` zeroes it). ``assign`` is the default action
        # anyway, so omitting it yields the intended reservation. A
        # whole-object PUT with this list fully replaces the reserved-address
        # table (stale rows are dropped), so replace-all holds.
        reserved: list[dict[str, Any]] = []
        for i, st in enumerate(scope.statics, start=1):
            reserved.append(
                {
                    "id": i,
                    "type": "mac",
                    "ip": str(st.ip_address),
                    "mac": str(st.mac_address),
                    "description": st.hostname or "",
                }
            )
        body["reserved-address"] = reserved

        # Generic options subtable ← everything not handled as a field.
        body["options"] = self._build_options(opts)
        return body

    def _build_options(self, opts: dict[str, Any]) -> list[dict[str, Any]]:
        """Render the generic ``options`` subtable for non-first-class options.

        Handles canonical names via :data:`_OPTION_NAME_TO_CODE` and
        ``code:NN`` custom keys. FortiGate ``options`` items carry ``code``,
        ``type`` (ip | string), and either ``ip`` (space-joined) or
        ``value``.
        """
        out: list[dict[str, Any]] = []
        for name, raw in (opts or {}).items():
            if name in _OPTIONS_HANDLED_ELSEWHERE:
                continue
            code: int | None
            if name in _OPTION_NAME_TO_CODE:
                code = _OPTION_NAME_TO_CODE[name]
            elif name.startswith("code:"):
                try:
                    code = int(name.split(":", 1)[1])
                except (ValueError, IndexError):
                    continue
            else:
                # Unknown / unmappable option name — skip rather than guess.
                continue
            values = _as_list(raw)
            if not values:
                continue
            item: dict[str, Any] = {"id": len(out) + 1, "code": code}
            if _all_ips(values):
                item["type"] = "ip"
                item["ip"] = " ".join(values)
            else:
                item["type"] = "string"
                item["value"] = " ".join(values)
            out.append(item)
        return out

    # ── Provider hooks (whole-scope write, lease read, probe) ───────────
    async def _find_server_id(self, client: httpx.AsyncClient, interface: str) -> int | None:
        """Return the mkey of the DHCP-server object on ``interface``, or None."""
        resp = await client.get("/cmdb/system.dhcp/server")
        body = self._unwrap(resp)
        for srv in body.get("results") or []:
            if srv.get("interface") == interface:
                try:
                    return int(srv.get("id"))
                except (TypeError, ValueError):
                    return None
        return None

    async def _apply_scope(self, server: Any, creds: dict[str, Any], scope: ScopeDef) -> None:
        async with self._client(server, creds) as client:
            interface = await self._match_interface(client, scope.subnet_cidr)
            body = self._build_server_body(interface, scope)
            existing_id = await self._find_server_id(client, interface)
            if existing_id is None:
                resp = await client.post("/cmdb/system.dhcp/server", json=body)
                self._unwrap(resp)
            else:
                # Adopt + replace-all: overwrite whatever was on this
                # interface's DHCP server with SpatiumDDI's desired state.
                logger.info(
                    "fortigate.adopt_existing_dhcp_server",
                    server=str(getattr(server, "id", "")),
                    interface=interface,
                    mkey=existing_id,
                )
                resp = await client.put(f"/cmdb/system.dhcp/server/{existing_id}", json=body)
                self._unwrap(resp)

    async def _remove_scope(self, server: Any, creds: dict[str, Any], subnet_cidr: str) -> None:
        async with self._client(server, creds) as client:
            try:
                interface = await self._match_interface(client, subnet_cidr)
            except CloudDHCPError:
                # No matching interface → nothing we own to delete. Idempotent.
                return
            existing_id = await self._find_server_id(client, interface)
            if existing_id is None:
                return
            resp = await client.delete(f"/cmdb/system.dhcp/server/{existing_id}")
            self._unwrap(resp)

    async def _get_leases(self, server: Any, creds: dict[str, Any]) -> list[dict[str, Any]]:
        async with self._client(server, creds) as client:
            resp = await client.get("/monitor/system/dhcp")
            body = self._unwrap(resp)
        out: list[dict[str, Any]] = []
        for lease in body.get("results") or []:
            if str(lease.get("type") or "ipv4") != "ipv4":
                continue
            ip = lease.get("ip")
            mac = lease.get("mac")
            if not ip or not mac:
                continue
            # FortiGate reports "leased" / "reserved" (and sometimes others).
            status = str(lease.get("status") or "").lower()
            if status and status not in ("leased", "reserved"):
                continue
            out.append(
                {
                    "ip_address": str(ip),
                    "mac_address": str(mac),
                    "hostname": lease.get("hostname") or None,
                    "client_id": None,
                    "state": "active",
                    "expires_at": _epoch_to_dt(lease.get("expire_time")),
                }
            )
        return out

    async def _probe(self, server: Any, creds: dict[str, Any]) -> CloudDHCPProbe:
        async with self._client(server, creds) as client:
            interfaces = await self._list_interface_cidrs(client)
        return CloudDHCPProbe(
            ok=True,
            message=f"Authenticated; {len(interfaces)} L3 interface(s) with an IP visible.",
            interface_count=len(interfaces),
        )

    async def list_interfaces(self, server: Any) -> list[dict[str, Any]]:
        """Preflight helper — list L3 interfaces + their CIDRs for the UI."""
        creds = self._load_credentials(server)
        async with self._client(server, creds) as client:
            return await self._list_interface_cidrs(client)

    # ── Capabilities ────────────────────────────────────────────────────
    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "fortigate",
            "read_only": False,
            "bundle_config_push": False,
            "lease_monitoring": True,
            "scope_management": True,
            "reservation_management": True,
            "exclusion_management": True,
            "address_families": ["ipv4"],
            "transport": "https",
            "notes": (
                "Agentless FortiGate DHCP driver over the FortiOS REST API "
                "(Bearer API token, VDOM-scoped). Each subnet maps to the "
                "system.dhcp.server object on the interface whose CIDR "
                "matches; SpatiumDDI is the source of truth (push-only)."
            ),
        }


def _epoch_to_dt(value: Any) -> datetime | None:
    """Convert a FortiOS ``expire_time`` unix epoch (seconds) to a UTC dt."""
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (ValueError, OSError, OverflowError, TypeError):
        return None


async def test_fortigate_credentials(
    host: str, port: int, credentials: dict[str, Any]
) -> tuple[bool, str]:
    """Dry-run probe for the create/edit modal's Test-connection button.

    Reaches ``host:port`` over HTTPS with the API token + VDOM and lists
    interfaces. Returns ``(ok, message)`` — never raises for an expected
    failure.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    driver = FortiGateDHCPDriver()
    fake = SimpleNamespace(id="<test>", name="<test>", host=host, port=port)
    try:
        result = await driver._probe(fake, credentials)
        return result.ok, result.message
    except CloudDHCPError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — surface any transport error verbatim
        return False, f"fortigate API error: {exc}"


__all__ = ["FortiGateDHCPDriver", "test_fortigate_credentials"]
