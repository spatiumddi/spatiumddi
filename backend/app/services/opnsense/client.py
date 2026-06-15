"""Minimal async OPNsense REST client.

Endpoints consumed (all read-only):

* ``GET  /api/core/firmware/status`` — sanity + firmware version.
* ``POST /api/dhcpv4/leases/searchLease`` — DHCPv4 leases. OPNsense's
  search endpoints return a ``{rows: [...], total, ...}`` envelope.
* ``GET  /api/dhcpv4/settings/getReservation`` — static mappings (the
  ISC ``dhcpd`` ``getReservation`` shape returns
  ``{dhcpd: {<iface>: {staticmap: {...}}}}`` or a ``rows[]`` wrapper
  on newer firmwares; we handle both defensively).
* ``GET  /api/diagnostics/interface/getInterfaceConfig`` — per-interface
  config keyed by interface name, each carrying addresses / CIDRs.
* ``GET  /api/interfaces/vlan_settings/get`` — VLAN definitions
  (``vlan.vlan.<uuid>`` rows with parent / tag / description).
* ``GET  /api/diagnostics/interface/getArp`` — ARP table (secondary
  population, opt-in).

Auth is HTTP Basic: the API key is the username and the API secret is
the password. OPNsense API keys are minted per-user under System →
Access → Users → API keys.

Defensive parsing throughout — OPNsense response shapes vary across
firmware versions and we can't hit a real box in tests, so every
accessor guards missing keys / unexpected types and the
``rows[]``-wrapper convention is handled in one place
(``_unwrap_rows``).
"""

from __future__ import annotations

import ipaddress
import ssl
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class OPNsenseClientError(Exception):
    """Raised when the OPNsense API returns an error we can't recover from.

    The message carries the HTTP status + path + first 200 bytes of the
    response body so operator-facing error messages are useful.

    ``status_code`` is the HTTP status when the failure was a response
    (``401`` / ``403`` / ``5xx`` / …) and ``None`` for transport-level
    failures (timeout / connection refused / non-JSON body). Callers use
    it to tell a genuine "endpoint absent" 404 (safe to treat as an empty
    table) apart from a transient 5xx / timeout (must abort the reconcile
    rather than diff against an empty desired set — see #5).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class _OPNFirmwareInfo:
    # OPNsense reports the running product version under several keys
    # depending on firmware; we normalise to a single string.
    version: str


@dataclass
class _OPNInterface:
    """A configured interface with at least one IPv4/IPv6 address.

    ``cidr`` is the *network* CIDR (e.g. ``10.0.0.0/24``) derived from
    the interface address + prefix. ``device`` is the OS device name
    (``igb0`` / ``vlan0.10``); ``name`` is the OPNsense logical name
    (``lan`` / ``opt1``); ``description`` is the operator-set label.
    """

    name: str  # logical name (lan / wan / opt1 / ...)
    device: str  # OS device (igb0, vlan0.10, ...)
    description: str
    cidr: str  # network CIDR (10.0.0.0/24)
    address: str  # the firewall's own interface IP (10.0.0.1)


@dataclass
class _OPNVlan:
    """A VLAN definition from ``interfaces/vlan_settings/get``."""

    device: str  # vlan device name (vlan0.10)
    parent: str  # physical parent (igb0)
    tag: int | None
    description: str


@dataclass
class _OPNLease:
    """A DHCPv4 lease row from ``dhcpv4/leases/searchLease``."""

    address: str
    mac: str | None
    hostname: str
    state: str  # "active" / "expired" / ... (best-effort)


@dataclass
class _OPNReservation:
    """A static DHCP mapping from ``dhcpv4/settings/getReservation``."""

    address: str
    mac: str | None
    hostname: str
    description: str


@dataclass
class _OPNArpEntry:
    """An ARP-table row from ``diagnostics/interface/getArp``."""

    address: str
    mac: str | None
    hostname: str
    interface: str


def _normalise_mac(mac: str | None) -> str | None:
    """Canonical form: colon-separated, lowercase, no whitespace.

    Returns ``None`` for blanks / OPNsense placeholders (``(incomplete)``
    shows up in ARP tables for unresolved entries).
    """
    if not mac:
        return None
    m = mac.strip().lower().replace("-", ":")
    if not m or m in {"(incomplete)", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"}:
        return None
    return m


def _unwrap_rows(body: Any) -> list[dict[str, Any]]:
    """Pull the row list out of an OPNsense search-style response.

    Handles the common ``{"rows": [...]}`` envelope, a bare list, and
    the occasional ``{"<n>": {...}}`` keyed-dict shape. Always returns
    a list of dicts (dropping any non-dict members defensively).
    """
    rows: Any
    if isinstance(body, dict) and "rows" in body:
        rows = body.get("rows")
    elif isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        # Keyed dict: values are the rows.
        rows = list(body.values())
    else:
        rows = []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _network_cidr(address: str, prefix: Any) -> str | None:
    """Build the network CIDR (``10.0.0.0/24``) from an interface IP +
    prefix length. Returns ``None`` on parse failure.
    """
    try:
        plen = int(prefix)
    except (TypeError, ValueError):
        return None
    try:
        iface = ipaddress.ip_interface(f"{address}/{plen}")
        return str(iface.network)
    except (ValueError, TypeError):
        return None


class OPNsenseClient:
    """Per-firewall async client. One instance per reconcile pass.

    Caller is responsible for ``async with`` lifecycle. Holds a single
    ``httpx.AsyncClient`` so every call shares a TLS session.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        api_key: str,
        api_secret: str,
        verify_tls: bool,
        ca_bundle_pem: str = "",
    ) -> None:
        self._base = f"https://{host}:{port}"
        self._auth = (api_key, api_secret)
        self._verify_tls = verify_tls
        self._ca_bundle_pem = ca_bundle_pem.strip()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> OPNsenseClient:
        verify: Any
        if not self._verify_tls:
            verify = False
            # Operator opted out of TLS verification for this firewall.
            # Surface it on every connect so the insecure posture is
            # visible in the centralized logs (#5); the who/when of
            # enabling it lives in the integration-target audit row.
            logger.warning("opnsense_tls_verification_disabled", endpoint=self._base)
        elif self._ca_bundle_pem:
            verify = ssl.create_default_context(cadata=self._ca_bundle_pem)
        else:
            verify = True
        self._client = httpx.AsyncClient(
            base_url=self._base,
            auth=self._auth,
            headers={"Accept": "application/json"},
            verify=verify,
            timeout=20.0,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, *, json: Any | None = None) -> Any:
        assert self._client is not None, "use within 'async with'"
        try:
            resp = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            # Transport-level failure (timeout / connection refused) — no
            # HTTP status, so status_code stays None and the caller treats
            # it as transient (abort, don't diff against empty).
            raise OPNsenseClientError(f"{path}: {exc}") from exc
        if resp.status_code == 401:
            raise OPNsenseClientError(f"{path}: HTTP 401 — API key/secret invalid", status_code=401)
        if resp.status_code == 403:
            raise OPNsenseClientError(
                f"{path}: HTTP 403 — API user lacks privilege for this path", status_code=403
            )
        if resp.status_code >= 400:
            raise OPNsenseClientError(
                f"{path}: HTTP {resp.status_code} {resp.text[:200]}",
                status_code=resp.status_code,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise OPNsenseClientError(f"{path}: response was not JSON ({exc})") from exc

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, json: Any | None = None) -> Any:
        return await self._request("POST", path, json=json)

    # ── Public surface ───────────────────────────────────────────────

    async def get_firmware(self) -> _OPNFirmwareInfo:
        """Best-effort firmware version. Used as the auth/sanity probe."""
        data = await self._get("/api/core/firmware/status")
        version = ""
        if isinstance(data, dict):
            # OPNsense reports the running version under one of these.
            for key in ("product_version", "os_version", "product_id"):
                v = data.get(key)
                if isinstance(v, str) and v:
                    version = v
                    break
            if not version:
                prod = data.get("product")
                if isinstance(prod, dict):
                    pv = prod.get("product_version") or prod.get("product_series")
                    if isinstance(pv, str) and pv:
                        version = pv
        return _OPNFirmwareInfo(version=version or "unknown")

    async def list_interfaces(self) -> list[_OPNInterface]:
        """Configured interfaces that carry at least one IPv4/IPv6 CIDR.

        ``getInterfaceConfig`` returns a dict keyed by logical interface
        name. Each entry carries ``ipaddr`` / ``subnet`` (IPv4) and/or
        ``ipaddr6`` / ``subnet6`` (IPv6), plus ``device`` + ``descr``.
        Interfaces without an address (or set to ``dhcp`` / ``none``)
        contribute nothing.
        """
        data = await self._get("/api/diagnostics/interface/getInterfaceConfig")
        # #430 — getInterfaceConfig always returns a non-empty dict keyed by
        # logical interface name; a non-dict or empty-dict 200 (proxy error
        # body, envelope change) is a degraded read. Returning [] here would
        # absence-delete every router-owned interface subnet. Matches the
        # #426 reasoning already applied to list_leases/list_arp.
        if not isinstance(data, dict) or not data:
            raise OPNsenseClientError(
                "getInterfaceConfig returned no interfaces — treating as a "
                "degraded read, not zero interfaces"
            )
        out: list[_OPNInterface] = []
        for logical, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            device = str(cfg.get("device") or cfg.get("if") or logical)
            descr = str(cfg.get("descr") or cfg.get("description") or "")
            for addr_key, prefix_key in (("ipaddr", "subnet"), ("ipaddr6", "subnet6")):
                addr = cfg.get(addr_key)
                if not isinstance(addr, str) or not addr:
                    continue
                # Skip dynamic / unconfigured markers.
                if addr.lower() in {"dhcp", "none", "dhcp6", "track6", "slaac"}:
                    continue
                cidr = _network_cidr(addr, cfg.get(prefix_key))
                if cidr is None:
                    continue
                out.append(
                    _OPNInterface(
                        name=str(logical),
                        device=device,
                        description=descr,
                        cidr=cidr,
                        address=addr,
                    )
                )
        return out

    async def list_vlans(self) -> list[_OPNVlan]:
        """VLAN definitions. Empty list on 404 / 403 / no VLANs.

        The ``vlan_settings/get`` shape is
        ``{"vlan": {"vlan": {"<uuid>": {if, tag, descr, ...}}}}``.
        """
        try:
            data = await self._get("/api/interfaces/vlan_settings/get")
        except OPNsenseClientError as exc:
            logger.debug("opnsense_vlans_unavailable", error=str(exc))
            return []
        # #430 — distinguish a legitimately-empty VLAN set (envelope present,
        # inner {}) from a degraded read (envelope absent). Returning [] on a
        # degraded read would absence-delete VLAN-derived subnets. The 404 /
        # plugin-absent case is already handled by the except above.
        outer = data.get("vlan") if isinstance(data, dict) else None
        inner = outer.get("vlan") if isinstance(outer, dict) else None
        if not isinstance(inner, dict):
            raise OPNsenseClientError(
                "vlan_settings/get missing the vlan.vlan envelope — treating "
                "as a degraded read, not zero VLANs"
            )
        rows: dict[str, Any] = inner
        out: list[_OPNVlan] = []
        for _uuid, row in rows.items():
            if not isinstance(row, dict):
                continue
            device = str(row.get("vlanif") or row.get("device") or "")
            parent = str(row.get("if") or row.get("parent") or "")
            tag_raw = row.get("tag")
            tag: int | None
            try:
                tag = int(str(tag_raw)) if tag_raw not in (None, "") else None
            except (TypeError, ValueError):
                tag = None
            out.append(
                _OPNVlan(
                    device=device,
                    parent=parent,
                    tag=tag,
                    description=str(row.get("descr") or row.get("description") or ""),
                )
            )
        return out

    async def list_leases(self) -> list[_OPNLease]:
        """DHCPv4 leases via the ``searchLease`` POST endpoint.

        The POST body asks for a large page so we get the full table in
        one call. Returns an empty list only on 404 (DHCP service not
        enabled / endpoint absent). A 5xx / timeout / other failure is
        re-raised so the reconciler aborts instead of mistaking a
        transient error for an empty lease table and mass-deleting every
        mirrored row (#5).
        """
        try:
            body = await self._post(
                "/api/dhcpv4/leases/searchLease",
                json={"current": 1, "rowCount": 5000, "searchPhrase": ""},
            )
        except OPNsenseClientError as exc:
            if exc.status_code == 404:
                logger.debug("opnsense_leases_unavailable", error=str(exc))
                return []
            raise
        out: list[_OPNLease] = []
        for r in _unwrap_rows(body):
            addr = str(r.get("address") or r.get("ip") or "").strip()
            if not addr:
                continue
            state = str(r.get("status") or r.get("state") or "").strip().lower() or "unknown"
            out.append(
                _OPNLease(
                    address=addr,
                    mac=_normalise_mac(r.get("mac") or r.get("hwaddr")),
                    hostname=str(r.get("hostname") or r.get("client-hostname") or "").strip(),
                    state=state,
                )
            )
        return out

    async def list_reservations(self) -> list[_OPNReservation]:
        """Static DHCP mappings.

        ``getReservation`` returns the ISC ``dhcpd`` config tree on
        older firmware (``{"dhcpd": {"<iface>": {"staticmap": {...}}}}``)
        and a ``rows[]`` wrapper on newer ones. We flatten both shapes.

        Returns an empty list only on 404 (endpoint absent); a transient
        5xx / timeout is re-raised so the reconciler doesn't diff against
        an empty desired set and delete every mirrored reservation (#5).
        """
        try:
            body = await self._get("/api/dhcpv4/settings/getReservation")
        except OPNsenseClientError as exc:
            if exc.status_code == 404:
                logger.debug("opnsense_reservations_unavailable", error=str(exc))
                return []
            raise
        rows: list[dict[str, Any]] = []
        if isinstance(body, dict) and "dhcpd" in body and isinstance(body["dhcpd"], dict):
            # ISC tree shape: dhcpd → <iface> → staticmap → {uuid|idx: {...}}
            for iface_cfg in body["dhcpd"].values():
                if not isinstance(iface_cfg, dict):
                    continue
                staticmap = iface_cfg.get("staticmap")
                if isinstance(staticmap, dict):
                    rows.extend(r for r in staticmap.values() if isinstance(r, dict))
                elif isinstance(staticmap, list):
                    rows.extend(r for r in staticmap if isinstance(r, dict))
        else:
            rows = _unwrap_rows(body)

        out: list[_OPNReservation] = []
        for r in rows:
            addr = str(r.get("ipaddr") or r.get("address") or r.get("ip") or "").strip()
            if not addr:
                continue
            out.append(
                _OPNReservation(
                    address=addr,
                    mac=_normalise_mac(r.get("mac") or r.get("hwaddr")),
                    hostname=str(r.get("hostname") or "").strip(),
                    description=str(r.get("descr") or r.get("description") or "").strip(),
                )
            )
        return out

    async def list_arp(self) -> list[_OPNArpEntry]:
        """ARP table — secondary population, only fetched when the
        operator opts in.

        Returns an empty list only on 404 (endpoint absent); a transient
        5xx / timeout is re-raised so the reconciler aborts rather than
        mistaking a transient error for an empty ARP table and deleting
        every mirrored ARP row (#5).
        """
        try:
            body = await self._get("/api/diagnostics/interface/getArp")
        except OPNsenseClientError as exc:
            if exc.status_code == 404:
                logger.debug("opnsense_arp_unavailable", error=str(exc))
                return []
            raise
        out: list[_OPNArpEntry] = []
        for r in _unwrap_rows(body):
            addr = str(r.get("ip") or r.get("address") or "").strip()
            if not addr:
                continue
            out.append(
                _OPNArpEntry(
                    address=addr,
                    mac=_normalise_mac(r.get("mac") or r.get("hwaddr")),
                    hostname=str(r.get("hostname") or "").strip(),
                    interface=str(r.get("intf") or r.get("interface") or "").strip(),
                )
            )
        return out


__all__ = [
    "OPNsenseClient",
    "OPNsenseClientError",
    "_OPNArpEntry",
    "_OPNFirmwareInfo",
    "_OPNInterface",
    "_OPNLease",
    "_OPNReservation",
    "_OPNVlan",
    "_network_cidr",
    "_normalise_mac",
    "_unwrap_rows",
]
