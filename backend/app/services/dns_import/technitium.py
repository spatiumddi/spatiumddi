"""Technitium DNS Server live-pull importer (issue #744).

Walks the REST API of a Technitium server the operator is migrating
*from* and emits the canonical :class:`ImportPreview` shape, same as the
BIND9 / Windows DNS / PowerDNS importers — the shared commit pipeline
writes the rows.

Two calls per pull:

* ``GET /api/zones/list`` → every zone::

      {"status": "ok", "response": {"zones": [
        {"name": "example.com", "type": "Primary", "disabled": false,
         "isExpired": false, "catalog": null}, ...]}}

* ``GET /api/zones/records/get?domain=<z>&zone=<z>&listZone=true`` →
  its records, each carrying a **structured** ``rData`` object rather
  than a wire-format string.

That last point is the whole difference from the PowerDNS importer.
PowerDNS hands back ``content`` strings that parse straight into a
record value; Technitium hands back per-type fields whose names do NOT
match the ones its own *write* API takes, and which translate numeric
rdata into enum names (``tlsaSelector=1`` reads back as
``selector="SPKI"``). ``_rdata_to_value`` is the inverse: it rebuilds
the presentation-format string SpatiumDDI stores. The agent driver
solves the same problem in the other direction — see
``_normalize_rdata`` in ``agent/dns/spatium_dns_agent/drivers/
technitium.py``; the two share the enum tables' intent, and a
round-trip test asserts they agree.

Auth: a bearer token, passed as a ``token`` query param (Technitium's
own convention — it accepts ``Authorization: Bearer`` too, but the
token param is what its docs and console use). The operator pastes the
API URL and a token into the import form; neither is persisted.
"""

from __future__ import annotations

from typing import Any

import httpx

from .canonical import (
    ImportedRecord,
    ImportedSOA,
    ImportedZone,
    ImportPreview,
)


class TechnitiumImportError(ValueError):
    """Raised when the Technitium API can't be reached / parsed.

    Per-zone parse errors don't raise — they land in
    ``ImportedZone.parse_warnings`` so partial success stays visible.
    """


_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

_MAX_ZONES_PER_PULL = 5000

# Record types the importer models. Technitium-proprietary types
# (ANAME / APP / FWD) and DNSSEC artefacts drop out with a per-zone
# warning rather than silently.
_SUPPORTED_RECORD_TYPES = {
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "TXT",
    "NS",
    "PTR",
    "SRV",
    "CAA",
    "TLSA",
    "SSHFP",
    "NAPTR",
    "DNAME",
    "URI",
    "SVCB",
    "HTTPS",
}

_DNSSEC_RECORDS = {"DNSKEY", "RRSIG", "NSEC", "NSEC3", "NSEC3PARAM", "DS"}

# Only these can be imported as authoritative data. A Secondary holds a
# copy of someone else's zone and a Forwarder holds no records at all,
# so importing either would mint rows SpatiumDDI would then try to serve
# as its own.
_IMPORTABLE_ZONE_TYPES = {"Primary"}

# Inverse of the agent driver's enum translation. Technitium returns
# these fields by NAME on read while its write API takes the number.
_TLSA_USAGE = {"PKIX-TA": 0, "PKIX-EE": 1, "DANE-TA": 2, "DANE-EE": 3}
_TLSA_SELECTOR = {"Cert": 0, "SPKI": 1}
_TLSA_MATCHING = {"Full": 0, "SHA2-256": 1, "SHA2-512": 2}
_SSHFP_ALGO = {"RSA": 1, "DSA": 2, "ECDSA": 3, "Ed25519": 4, "Ed448": 6}
_SSHFP_FP_TYPE = {"SHA1": 1, "SHA256": 2}


def _enum_num(table: dict[str, int], value: Any) -> str:
    """Map an enum name back to its number, passing an unrecognised
    value through unchanged so a future Technitium enum degrades to an
    odd-looking record rather than an exception mid-import."""
    return str(table.get(str(value), value))


def _int_or(value: Any, default: int) -> int:
    """``int(x or default)`` silently rewrites a legitimate ZERO to the
    default, which matters here: SVCB/HTTPS priority 0 means AliasMode
    (not ServiceMode), MX preference 0 is the highest priority, and URI
    priority/weight 0 are valid. Only a genuinely absent or unparseable
    value falls back.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_fqdn(name: str) -> str:
    return name if name.endswith(".") else name + "."


def _classify_zone(name: str) -> str:
    bare = name.rstrip(".").lower()
    return "reverse" if bare.endswith((".in-addr.arpa", ".ip6.arpa")) else "forward"


def _rel_name(record_name: str, zone_fqdn: str) -> str:
    """Technitium reports each record's full domain; SpatiumDDI stores a
    label relative to the zone, with ``@`` for the apex."""
    rec = record_name.rstrip(".").lower()
    zone = zone_fqdn.rstrip(".").lower()
    if rec == zone or not rec:
        return "@"
    if rec.endswith("." + zone):
        return rec[: -(len(zone) + 1)]
    return rec


def _rdata_to_value(rtype: str, rdata: dict[str, Any]) -> tuple[str, dict[str, int]]:
    """Rebuild the presentation-format value from Technitium's structured
    rData, plus any structured fields SpatiumDDI stores separately
    (MX/SRV priority, weight, port).
    """
    extra: dict[str, int] = {}

    if rtype in ("A", "AAAA"):
        return str(rdata.get("ipAddress") or ""), extra
    if rtype == "CNAME":
        return str(rdata.get("cname") or ""), extra
    if rtype == "DNAME":
        return str(rdata.get("dname") or ""), extra
    if rtype == "NS":
        return str(rdata.get("nameServer") or ""), extra
    if rtype == "PTR":
        return str(rdata.get("ptrName") or ""), extra
    if rtype == "TXT":
        return str(rdata.get("text") or ""), extra
    if rtype == "MX":
        extra["priority"] = _int_or(rdata.get("preference"), 10)
        return str(rdata.get("exchange") or ""), extra
    if rtype == "SRV":
        extra["priority"] = _int_or(rdata.get("priority"), 0)
        extra["weight"] = _int_or(rdata.get("weight"), 0)
        extra["port"] = _int_or(rdata.get("port"), 0)
        return str(rdata.get("target") or ""), extra
    if rtype == "CAA":
        return (
            f"{_int_or(rdata.get('flags'), 0)} {rdata.get('tag') or 'issue'} "
            f"\"{rdata.get('value') or ''}\"",
            extra,
        )
    if rtype == "TLSA":
        return (
            " ".join(
                [
                    _enum_num(_TLSA_USAGE, rdata.get("certificateUsage")),
                    _enum_num(_TLSA_SELECTOR, rdata.get("selector")),
                    _enum_num(_TLSA_MATCHING, rdata.get("matchingType")),
                    str(rdata.get("certificateAssociationData") or "").lower(),
                ]
            ),
            extra,
        )
    if rtype == "SSHFP":
        return (
            " ".join(
                [
                    _enum_num(_SSHFP_ALGO, rdata.get("algorithm")),
                    _enum_num(_SSHFP_FP_TYPE, rdata.get("fingerprintType")),
                    str(rdata.get("fingerprint") or "").lower(),
                ]
            ),
            extra,
        )
    if rtype == "NAPTR":
        return (
            f"{rdata.get('order') or 0} {rdata.get('preference') or 0} "
            f"\"{rdata.get('flags') or ''}\" \"{rdata.get('services') or ''}\" "
            f"\"{rdata.get('regexp') or ''}\" {rdata.get('replacement') or '.'}",
            extra,
        )
    if rtype == "URI":
        return (
            f"{_int_or(rdata.get('priority'), 1)} "
            f"{_int_or(rdata.get('weight'), 1)} {rdata.get('uri') or ''}",
            extra,
        )
    if rtype in ("SVCB", "HTTPS"):
        params = rdata.get("svcParams") or {}
        rendered = " ".join(f'{k}="{v}"' for k, v in sorted(params.items()))
        target = rdata.get("svcTargetName") or "."
        return (
            f"{_int_or(rdata.get('svcPriority'), 1)} {target}"
            + (f" {rendered}" if rendered else ""),
            extra,
        )
    # Unreachable for the supported set, but keeps the function total.
    return str(rdata), extra


def _soa_from_rdata(rdata: dict[str, Any], ttl: int) -> ImportedSOA:
    return ImportedSOA(
        primary_ns=_normalize_fqdn(str(rdata.get("primaryNameServer") or "")),
        admin_email=_normalize_fqdn(str(rdata.get("responsiblePerson") or "")),
        serial=_int_or(rdata.get("serial"), 0),
        refresh=_int_or(rdata.get("refresh"), 86400),
        retry=_int_or(rdata.get("retry"), 7200),
        expire=_int_or(rdata.get("expire"), 3600000),
        minimum=_int_or(rdata.get("minimum"), 3600),
        ttl=ttl,
    )


def _build_imported_zone(zone_name: str, records_payload: list[dict[str, Any]]) -> ImportedZone:
    fqdn = _normalize_fqdn(zone_name)
    soa: ImportedSOA | None = None
    records: list[ImportedRecord] = []
    skipped: dict[str, int] = {}
    warnings: list[str] = []

    for rec in records_payload:
        rtype = str(rec.get("type") or "").upper()
        rdata = rec.get("rData") or {}
        ttl = _int_or(rec.get("ttl"), 3600)

        if rtype == "SOA":
            soa = _soa_from_rdata(rdata, ttl)
            continue
        if rtype in _DNSSEC_RECORDS or rtype not in _SUPPORTED_RECORD_TYPES:
            skipped[rtype] = skipped.get(rtype, 0) + 1
            continue
        # Technitium marks a disabled record rather than omitting it.
        if rec.get("disabled"):
            skipped[rtype] = skipped.get(rtype, 0) + 1
            continue

        try:
            value, extra = _rdata_to_value(rtype, rdata)
        except (TypeError, ValueError) as exc:
            warnings.append(f"Unparseable {rtype} at {rec.get('name')!r}: {exc}")
            continue
        if not value:
            warnings.append(f"Empty {rtype} value at {rec.get('name')!r} — skipped")
            continue

        records.append(
            ImportedRecord(
                name=_rel_name(str(rec.get("name") or ""), fqdn),
                record_type=rtype,
                value=value,
                ttl=ttl,
                priority=extra.get("priority"),
                weight=extra.get("weight"),
                port=extra.get("port"),
            )
        )

    dnssec_skipped = {k: v for k, v in skipped.items() if k in _DNSSEC_RECORDS}
    if dnssec_skipped:
        warnings.append(
            "Skipped DNSSEC records ("
            + ", ".join(f"{k}×{v}" for k, v in sorted(dnssec_skipped.items()))
            + ") — re-sign the zone after import rather than carrying "
            "signatures across."
        )
    other_skipped = {k: v for k, v in skipped.items() if k not in _DNSSEC_RECORDS}
    if other_skipped:
        warnings.append(
            "Skipped unsupported or disabled records ("
            + ", ".join(f"{k}×{v}" for k, v in sorted(other_skipped.items()))
            + ")."
        )

    return ImportedZone(
        name=fqdn,
        zone_type="primary",
        kind=_classify_zone(fqdn),
        soa=soa,
        records=records,
        parse_warnings=warnings,
    )


def _api_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/api/{path}"


def _unwrap(payload: dict[str, Any], what: str) -> dict[str, Any]:
    """Technitium answers HTTP 200 even on failure — the real status is
    in the body. Same trap the agent driver documents for its own calls."""
    status = payload.get("status")
    if status != "ok":
        raise TechnitiumImportError(
            f"Technitium rejected the {what} request "
            f"(status={status!r}): {payload.get('errorMessage') or 'no detail'}"
        )
    return payload.get("response") or {}


async def parse_technitium_server(
    *,
    api_url: str,
    api_token: str,
    timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
) -> ImportPreview:
    """Live-pull every primary zone + its records from a Technitium server.

    ``api_url`` is the console base (``http://tdns.internal:5380``); the
    ``/api`` suffix is appended here so the operator-facing form stays
    simple.

    Only ``Primary`` zones are imported. Secondary / Stub / Forwarder /
    Catalog zones are reported as warnings instead — a secondary is a
    copy of someone else's data, and importing it would mint rows
    SpatiumDDI then tries to serve authoritatively.

    Per-zone failures become ``parse_warnings`` on that zone rather than
    aborting the pull, matching the other importers' partial-success
    semantics.
    """
    zones: list[ImportedZone] = []
    warnings: list[str] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(
                _api_url(api_url, "zones/list"),
                params={
                    "token": api_token,
                    # 15.4.0 returns every zone and ignores these, but a
                    # version that paginated would hand back page 1 and we
                    # would silently import a fraction of the estate. Ask
                    # for one big page, then assert nothing was held back.
                    "pageNumber": 1,
                    "zonesPerPage": _MAX_ZONES_PER_PULL,
                },
            )
        except httpx.HTTPError as exc:
            raise TechnitiumImportError(
                f"Could not reach the Technitium API at {api_url!r}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise TechnitiumImportError(
                f"Technitium API returned HTTP {resp.status_code}: {resp.text[:200]!r}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise TechnitiumImportError(f"Technitium API returned invalid JSON: {exc}") from exc
        # An expired or wrong token comes back as HTTP 200 with
        # status="invalid-token", so this is the only place it surfaces.
        listing = _unwrap(body, "zone list")

        summaries = listing.get("zones") or []
        # Refuse a truncated view. ``totalPages`` is absent on 15.4.0 (that
        # endpoint is unpaginated there), so this guards against a future
        # version quietly changing the contract, not today's behaviour.
        total_pages = listing.get("totalPages")
        if isinstance(total_pages, int) and total_pages > 1:
            raise TechnitiumImportError(
                f"The Technitium API paginated the zone list ({total_pages} pages) "
                "and this importer reads one page. Importing now would silently "
                "migrate only part of the estate — please report this, it means "
                "the server's API contract has changed."
            )
        if len(summaries) > _MAX_ZONES_PER_PULL:
            raise TechnitiumImportError(
                f"Source has {len(summaries)} zones — over the "
                f"{_MAX_ZONES_PER_PULL}-zone per-pull cap. Split the import "
                "across multiple runs."
            )

        for summary in summaries:
            name = str(summary.get("name") or "")
            ztype = str(summary.get("type") or "")
            if not name:
                continue
            if ztype not in _IMPORTABLE_ZONE_TYPES:
                warnings.append(
                    f"Skipped {name!r}: {ztype} zones aren't imported — only a "
                    "Primary holds authoritative data of its own."
                )
                continue
            # A disabled zone is one the operator turned OFF on the source
            # server. Importing it would bring it back live here — the same
            # outcome the per-record disabled guard prevents, one level up.
            if summary.get("disabled"):
                warnings.append(
                    f"Skipped {name!r}: disabled on the source server. Re-enable "
                    "it there first if it should be imported."
                )
                continue

            try:
                rec_resp = await client.get(
                    _api_url(api_url, "zones/records/get"),
                    params={
                        "token": api_token,
                        "domain": name,
                        "zone": name,
                        "listZone": "true",
                    },
                )
                rec_body = _unwrap(rec_resp.json(), f"records for {name!r}")
            except (httpx.HTTPError, ValueError, TechnitiumImportError) as exc:
                zones.append(
                    ImportedZone(
                        name=_normalize_fqdn(name),
                        zone_type="primary",
                        kind=_classify_zone(name),
                        soa=None,
                        records=[],
                        parse_warnings=[f"Could not pull records: {exc}"],
                    )
                )
                continue

            zones.append(_build_imported_zone(name, rec_body.get("records") or []))

    total_records = sum(len(z.records) for z in zones)
    histogram: dict[str, int] = {}
    for zone in zones:
        for rec in zone.records:
            histogram[rec.record_type] = histogram.get(rec.record_type, 0) + 1

    return ImportPreview(
        source="technitium",
        zones=zones,
        conflicts=[],
        warnings=warnings,
        total_records=total_records,
        record_type_histogram=histogram,
    )


async def test_technitium_connection(
    *, api_url: str, api_token: str, timeout: httpx.Timeout = _DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Cheap reachability + auth probe for the import form."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(_api_url(api_url, "zones/list"), params={"token": api_token})
        except httpx.HTTPError as exc:
            raise TechnitiumImportError(
                f"Could not reach the Technitium API at {api_url!r}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise TechnitiumImportError(
                f"Technitium API returned HTTP {resp.status_code}: {resp.text[:200]!r}"
            )
        listing = _unwrap(resp.json(), "zone list")
    summaries = listing.get("zones") or []
    importable = [z for z in summaries if str(z.get("type")) in _IMPORTABLE_ZONE_TYPES]
    return {
        "ok": True,
        "zone_count": len(summaries),
        "importable_zone_count": len(importable),
    }
