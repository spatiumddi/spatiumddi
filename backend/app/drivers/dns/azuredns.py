"""Azure DNS driver (issue #37, Part B — agentless cloud provider).

Drives Azure DNS public zones through the ``azure-mgmt-dns`` management
SDK, authenticating with a service-principal client secret. Like the
other cloud drivers (Cloudflare / Route 53 / Google Cloud DNS) it
subclasses :class:`CloudDNSDriverBase` and implements only the five
provider hooks — everything DNSDriver-shaped (no-op renders, credential
decrypt, the import / drift / probe wrappers) lives in the base.

Credential dict shape (decrypted from ``DNSServer.credentials_encrypted``)::

    {"tenant_id": ..., "client_id": ..., "client_secret": ...,
     "subscription_id": ..., "resource_group": ...}

The Azure resource model splits a zone's records into *record sets*, one
per ``(relative_name, record_type)`` pair, each carrying a typed list of
records (``a_records``, ``mx_records``, …). We expand each record set into
one neutral :class:`RecordData` per contained record so the import + drift
machinery sees a flat record list like every other driver.

The ``azure.*`` SDKs are lazy-imported inside :meth:`_client` so importing
this module never fails when the optional dependency is absent, and tests
can monkeypatch the client factory. Blocking SDK calls run in
``asyncio.to_thread`` per the async-throughout non-negotiable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.drivers.dns._cloud_base import (
    CloudDNSDriverBase,
    CloudDNSError,
    CloudDNSZone,
    normalize_fqdn,
)
from app.drivers.dns.base import RecordChange, RecordData

logger = structlog.get_logger(__name__)


# Azure record-set ``type`` is the full ARM resource type. Map the suffix
# back to the neutral record-type label we expose everywhere else. SOA is
# intentionally absent — Azure manages the apex SOA itself and we skip it.
_AZURE_TYPE_TO_RECORD_TYPE = {
    "Microsoft.Network/dnszones/A": "A",
    "Microsoft.Network/dnszones/AAAA": "AAAA",
    "Microsoft.Network/dnszones/CNAME": "CNAME",
    "Microsoft.Network/dnszones/MX": "MX",
    "Microsoft.Network/dnszones/TXT": "TXT",
    "Microsoft.Network/dnszones/NS": "NS",
    "Microsoft.Network/dnszones/SRV": "SRV",
    "Microsoft.Network/dnszones/PTR": "PTR",
    "Microsoft.Network/dnszones/CAA": "CAA",
}


def _zone_label(zone_name: str) -> str:
    """Azure zone resource names carry no trailing dot — strip it."""
    return normalize_fqdn(zone_name).rstrip(".")


def _relative_name(name: str) -> str:
    """Azure uses ``"@"`` for the apex, same as us — pass through."""
    n = (name or "").strip()
    return n or "@"


class AzureDNSDriver(CloudDNSDriverBase):
    """Agentless Azure DNS driver via the ``azure-mgmt-dns`` SDK."""

    name = "azure_dns"
    credential_fields = (
        "tenant_id",
        "client_id",
        "client_secret",
        "subscription_id",
        "resource_group",
    )

    # ── SDK client factory (lazy import; patched in tests) ───────────────
    def _client(self, creds: dict[str, Any]) -> Any:
        """Build a ``DnsManagementClient`` from a service-principal secret.

        Lazy-imports the Azure SDKs so this module imports cleanly without
        ``azure-identity`` / ``azure-mgmt-dns`` installed. Raises
        :class:`CloudDNSError` on a missing dependency so the operator gets
        an actionable message rather than a bare ``ImportError``.
        """
        try:
            from azure.identity import ClientSecretCredential
            from azure.mgmt.dns import DnsManagementClient
        except ImportError as exc:  # pragma: no cover — env-dependent
            raise CloudDNSError(
                "azure_dns driver requires the 'azure-identity' and "
                "'azure-mgmt-dns' packages to be installed."
            ) from exc

        credential = ClientSecretCredential(
            tenant_id=creds["tenant_id"],
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
        )
        return DnsManagementClient(credential, creds["subscription_id"])

    def _wrap_errors(self, exc: Exception) -> CloudDNSError:
        """Translate an Azure SDK error into a clean :class:`CloudDNSError`.

        Lazy-imports the SDK exception types so we recognise auth / HTTP
        failures even though the import lives inside a method. Any other
        exception is re-wrapped verbatim by the caller.
        """
        try:
            from azure.core.exceptions import (
                ClientAuthenticationError,
                HttpResponseError,
            )
        except ImportError:  # pragma: no cover — env-dependent
            return CloudDNSError(f"azure_dns API error: {exc}")
        if isinstance(exc, ClientAuthenticationError):
            return CloudDNSError(f"azure_dns authentication failed: {exc}")
        if isinstance(exc, HttpResponseError):
            return CloudDNSError(f"azure_dns API error: {exc}")
        return CloudDNSError(f"azure_dns error: {exc}")

    # ── Zone listing ─────────────────────────────────────────────────────
    async def _list_zones(self, server: Any, creds: dict[str, Any]) -> list[CloudDNSZone]:
        client = self._client(creds)
        rg = (creds.get("resource_group") or "").strip()

        def _list() -> list[Any]:
            if rg:
                return list(client.zones.list_by_resource_group(rg))
            return list(client.zones.list())

        try:
            zones = await asyncio.to_thread(_list)
        except Exception as exc:  # noqa: BLE001 — wrapped into CloudDNSError
            raise self._wrap_errors(exc) from exc

        out: list[CloudDNSZone] = []
        for z in zones:
            name = normalize_fqdn(z.name)
            out.append(
                CloudDNSZone(
                    name=name,
                    zone_id=z.name,
                    is_reverse=name.rstrip(".").endswith("arpa"),
                    record_count=getattr(z, "number_of_record_sets", None),
                )
            )
        return out

    # ── Record listing ───────────────────────────────────────────────────
    async def _list_zone_records(
        self, server: Any, creds: dict[str, Any], zone_name: str
    ) -> list[RecordData]:
        client = self._client(creds)
        rg = (creds.get("resource_group") or "").strip()
        zone_label = _zone_label(zone_name)

        def _list() -> list[Any]:
            return list(client.record_sets.list_by_dns_zone(rg, zone_label))

        try:
            record_sets = await asyncio.to_thread(_list)
        except Exception as exc:  # noqa: BLE001 — wrapped into CloudDNSError
            raise self._wrap_errors(exc) from exc

        records: list[RecordData] = []
        for rs in record_sets:
            records.extend(self._expand_record_set(rs))
        return records

    def _expand_record_set(self, rs: Any) -> list[RecordData]:
        """Expand one Azure record set into per-record neutral rows.

        Skips SOA (apex, Azure-managed) and any record-set ``type`` we
        don't map. ``name`` is already relative to the apex (``"@"``).
        """
        rtype = _AZURE_TYPE_TO_RECORD_TYPE.get(getattr(rs, "type", "") or "")
        if rtype is None:
            return []  # SOA or an unmapped type — drop silently

        name = _relative_name(getattr(rs, "name", "@"))
        ttl = getattr(rs, "ttl", None)
        out: list[RecordData] = []

        def add(value: str) -> None:
            out.append(RecordData(name=name, record_type=rtype, value=value, ttl=ttl))

        if rtype == "A":
            for r in getattr(rs, "a_records", None) or []:
                add(r.ipv4_address)
        elif rtype == "AAAA":
            for r in getattr(rs, "aaaa_records", None) or []:
                add(r.ipv6_address)
        elif rtype == "CNAME":
            cname = getattr(rs, "cname_record", None)
            if cname is not None:
                add(cname.cname)
        elif rtype == "MX":
            for r in getattr(rs, "mx_records", None) or []:
                add(f"{r.preference} {r.exchange}")
        elif rtype == "TXT":
            for r in getattr(rs, "txt_records", None) or []:
                add("".join(r.value))
        elif rtype == "NS":
            for r in getattr(rs, "ns_records", None) or []:
                add(r.nsdname)
        elif rtype == "SRV":
            for r in getattr(rs, "srv_records", None) or []:
                add(f"{r.priority} {r.weight} {r.port} {r.target}")
        elif rtype == "PTR":
            for r in getattr(rs, "ptr_records", None) or []:
                add(r.ptrdname)
        elif rtype == "CAA":
            for r in getattr(rs, "caa_records", None) or []:
                add(f"{r.flags} {r.tag} {r.value}")
        return out

    # ── Record write ─────────────────────────────────────────────────────
    async def _apply_record(self, server: Any, creds: dict[str, Any], change: RecordChange) -> None:
        client = self._client(creds)
        rg = (creds.get("resource_group") or "").strip()
        zone_label = _zone_label(change.zone_name)
        relative = _relative_name(change.record.name)
        rtype = change.record.record_type.upper()

        if change.op == "delete":

            def _delete() -> None:
                client.record_sets.delete(rg, zone_label, relative, rtype)

            try:
                await asyncio.to_thread(_delete)
            except Exception as exc:  # noqa: BLE001 — wrapped into CloudDNSError
                raise self._wrap_errors(exc) from exc
            return

        # create / update — Azure has no distinct create vs update, both
        # are a PUT (create_or_update) of the full record set.
        params = self._build_record_set_params(change.record)

        def _put() -> None:
            client.record_sets.create_or_update(rg, zone_label, relative, rtype, params)

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:  # noqa: BLE001 — wrapped into CloudDNSError
            raise self._wrap_errors(exc) from exc

    def _build_record_set_params(self, record: RecordData) -> dict[str, Any]:
        """Build the create_or_update body for one neutral record.

        Returns the loosely-typed dict form the SDK accepts (``ttl`` plus
        the type-specific record list); MX / SRV string values are split
        back into their structured components.
        """
        rtype = record.record_type.upper()
        ttl = record.ttl if record.ttl is not None else 3600
        params: dict[str, Any] = {"ttl": ttl}
        value = (record.value or "").strip()

        if rtype == "A":
            params["a_records"] = [{"ipv4_address": value}]
        elif rtype == "AAAA":
            params["aaaa_records"] = [{"ipv6_address": value}]
        elif rtype == "CNAME":
            params["cname_record"] = {"cname": value}
        elif rtype == "MX":
            pref, _, exch = value.partition(" ")
            params["mx_records"] = [{"preference": int(pref), "exchange": exch.strip()}]
        elif rtype == "TXT":
            params["txt_records"] = [{"value": [value]}]
        elif rtype == "NS":
            params["ns_records"] = [{"nsdname": value}]
        elif rtype == "SRV":
            prio, weight, port, target = (value.split(None, 3) + ["", "", "", ""])[:4]
            params["srv_records"] = [
                {
                    "priority": int(prio),
                    "weight": int(weight),
                    "port": int(port),
                    "target": target.strip(),
                }
            ]
        elif rtype == "PTR":
            params["ptr_records"] = [{"ptrdname": value}]
        elif rtype == "CAA":
            flags, tag, caa_value = (value.split(None, 2) + ["", "", ""])[:3]
            params["caa_records"] = [{"flags": int(flags), "tag": tag, "value": caa_value.strip()}]
        else:
            raise CloudDNSError(f"azure_dns: unsupported record type {rtype!r}")
        return params

    # ── Zone write ───────────────────────────────────────────────────────
    async def _apply_zone(self, server: Any, creds: dict[str, Any], zone: Any, op: str) -> None:
        client = self._client(creds)
        rg = (creds.get("resource_group") or "").strip()
        zone_label = _zone_label(getattr(zone, "name", ""))

        if op == "create":

            def _create() -> None:
                client.zones.create_or_update(rg, zone_label, {"location": "global"})

            try:
                await asyncio.to_thread(_create)
            except Exception as exc:  # noqa: BLE001 — wrapped into CloudDNSError
                raise self._wrap_errors(exc) from exc
            return

        # delete — zone delete is a long-running operation; block on the
        # poller. Some SDK versions expose a synchronous ``delete`` instead.
        def _delete() -> None:
            if hasattr(client.zones, "begin_delete"):
                client.zones.begin_delete(rg, zone_label).result()
            else:
                client.zones.delete(rg, zone_label)

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:  # noqa: BLE001 — wrapped into CloudDNSError
            raise self._wrap_errors(exc) from exc

    # ── Capabilities ─────────────────────────────────────────────────────
    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "azure_dns",
            "agentless": True,
            "manages_zones": True,
            "views": False,
            "rpz": False,
            "dnssec_online": False,
            "alias_records": True,
            "record_types": [
                "A",
                "AAAA",
                "CNAME",
                "MX",
                "TXT",
                "NS",
                "SRV",
                "CAA",
                "PTR",
                "SOA",
            ],
            "notes": "Azure DNS — online DNSSEC not supported via this driver.",
        }


__all__ = ["AzureDNSDriver"]
