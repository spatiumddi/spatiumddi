"""IP-prefix RDAP lookup ("who owns this IP?").

Uses the IANA RDAP bootstrap (``data.iana.org/rdap/ipv4.json`` /
``ipv6.json``) to find the responsible RIR's RDAP server, then
queries it directly for the prefix the IP lives in. Returns a small
normalised dict — operators want to see "ARIN says this is Cloudflare,
abuse@cloudflare.com, prefix 1.1.1.0/24" not the raw RDAP soup.

Side-effect-free + idempotent. Cached in-process for 6 h (matches
the domain RDAP bootstrap pattern) so a chat that asks about ten IPs
doesn't fetch the bootstrap ten times.
"""

from __future__ import annotations

import ipaddress
import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

_BOOTSTRAP_V4_URL = "https://data.iana.org/rdap/ipv4.json"
_BOOTSTRAP_V6_URL = "https://data.iana.org/rdap/ipv6.json"
_BOOTSTRAP_TTL_SECONDS = 6 * 3600
_PER_REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)


# In-process cache. ``_cache[scope]`` holds a tuple of (expires_at,
# parsed bootstrap services list). The list is the raw RDAP shape:
# each entry is ``[[prefixes...], [rdap_base_urls...]]``.
_cache: dict[str, tuple[float, list[list[list[str]]]]] = {}


async def _fetch_bootstrap(scope: str) -> list[list[list[str]]] | None:
    cached = _cache.get(scope)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    url = _BOOTSTRAP_V4_URL if scope == "v4" else _BOOTSTRAP_V6_URL
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_PER_REQUEST_TIMEOUT) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info("ip_rdap_bootstrap_unreachable", scope=scope, error=str(exc))
        return None
    if resp.status_code != 200:
        logger.info("ip_rdap_bootstrap_unreachable", scope=scope, status=resp.status_code)
        return None
    try:
        payload = resp.json()
    except ValueError as exc:
        logger.info("ip_rdap_bootstrap_invalid", scope=scope, error=str(exc))
        return None
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, list):
        return None
    _cache[scope] = (now + _BOOTSTRAP_TTL_SECONDS, services)
    return services


def _resolve_rdap_base(
    services: list[list[list[str]]], target: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> str | None:
    """Walk the bootstrap services list for the most-specific prefix
    containing ``target`` and return the first RDAP base URL."""
    best_prefixlen = -1
    best_base: str | None = None
    for entry in services:
        if len(entry) < 2:
            continue
        prefixes, urls = entry[0], entry[1]
        if not prefixes or not urls:
            continue
        for cidr in prefixes:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                continue
            if target.version != net.version:
                continue
            if target in net and net.prefixlen > best_prefixlen:
                best_prefixlen = net.prefixlen
                # Pick HTTPS over HTTP if both are listed.
                https_url = next(
                    (u for u in urls if isinstance(u, str) and u.startswith("https://")), None
                )
                best_base = https_url or (urls[0] if isinstance(urls[0], str) else None)
    return best_base


def _extract_holder_org(payload: dict[str, Any]) -> str | None:
    entities = payload.get("entities")
    if not isinstance(entities, list):
        return None
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles")
        if not isinstance(roles, list):
            continue
        if not any(r in {"registrant", "administrative"} for r in roles):
            continue
        vcard = ent.get("vcardArray")
        if not isinstance(vcard, list) or len(vcard) < 2:
            continue
        for item in vcard[1]:
            if not isinstance(item, list) or len(item) < 4:
                continue
            kind = item[0]
            if kind in {"fn", "org"} and isinstance(item[3], str) and item[3].strip():
                return item[3].strip()
    return None


def _extract_abuse(payload: dict[str, Any]) -> str | None:
    """First abuse contact email surfaced via the abuse-role entity
    or any nested vCard ``email``."""
    entities = payload.get("entities")
    if not isinstance(entities, list):
        return None

    def _email_from_vcard(vcard: Any) -> str | None:
        if not isinstance(vcard, list) or len(vcard) < 2:
            return None
        for item in vcard[1]:
            if isinstance(item, list) and len(item) >= 4 and item[0] == "email":
                if isinstance(item[3], str) and "@" in item[3]:
                    return item[3].strip()
        return None

    # Pass 1: explicit abuse role.
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        roles = ent.get("roles")
        if isinstance(roles, list) and "abuse" in roles:
            email = _email_from_vcard(ent.get("vcardArray"))
            if email:
                return email
            # Some RIRs nest abuse contacts under a sub-entity.
            for sub in ent.get("entities") or []:
                if isinstance(sub, dict):
                    email = _email_from_vcard(sub.get("vcardArray"))
                    if email:
                        return email
    return None


async def lookup_ip(addr: str) -> dict[str, Any] | None:
    """RDAP lookup for an IPv4 / IPv6 address.

    Returns a normalised dict::

        {
            "address": "1.1.1.1",
            "prefix": "1.1.1.0/24",
            "handle": "NET-1-1-1-0-1",
            "name": "APNIC-LABS",
            "country": "AU",
            "holder_org": "Cloudflare, Inc.",
            "abuse_email": "abuse@cloudflare.com",
            "rir": "apnic",
            "rdap_url": "https://rdap.apnic.net/ip/1.1.1.0/24",
        }

    Returns ``None`` on any failure. Bogons / private / multicast /
    reserved ranges return a stub dict with ``rir="reserved"`` so the
    caller can render "RFC 1918 — not in public registries" instead
    of treating it as an unknown failure.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None

    if ip.is_private:
        return {
            "address": str(ip),
            "rir": "private",
            "note": "RFC 1918 / RFC 4193 — not in public RDAP",
        }
    if ip.is_loopback:
        return {"address": str(ip), "rir": "reserved", "note": "Loopback (RFC 1122 / 4291)"}
    if ip.is_link_local:
        return {
            "address": str(ip),
            "rir": "reserved",
            "note": "Link-local (RFC 3927 / 4291)",
        }
    if ip.is_multicast:
        return {
            "address": str(ip),
            "rir": "reserved",
            "note": "Multicast (RFC 5771 / 4291)",
        }
    if ip.is_reserved or ip.is_unspecified:
        return {"address": str(ip), "rir": "reserved", "note": "Reserved by IANA"}

    scope = "v4" if ip.version == 4 else "v6"
    services = await _fetch_bootstrap(scope)
    if services is None:
        return None
    base = _resolve_rdap_base(services, ip)
    if base is None:
        return None
    base = base.rstrip("/")

    url = f"{base}/ip/{ip}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_PER_REQUEST_TIMEOUT) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info("ip_rdap_unreachable", addr=str(ip), error=f"transport: {exc}")
        return None
    if resp.status_code != 200:
        logger.info("ip_rdap_unreachable", addr=str(ip), status=resp.status_code)
        return None
    try:
        payload = resp.json()
    except ValueError as exc:
        logger.info("ip_rdap_invalid_payload", addr=str(ip), error=str(exc))
        return None
    if not isinstance(payload, dict):
        return None

    # ``startAddress`` / ``endAddress`` describe the matched prefix;
    # collapse them to a CIDR for human display.
    start = payload.get("startAddress")
    end = payload.get("endAddress")
    cidr: str | None = None
    if isinstance(start, str) and isinstance(end, str):
        try:
            networks = list(
                ipaddress.summarize_address_range(
                    ipaddress.ip_address(start), ipaddress.ip_address(end)
                )
            )
            if networks:
                cidr = str(networks[0])
        except (ValueError, TypeError):
            cidr = None

    handle = payload.get("handle") if isinstance(payload.get("handle"), str) else None
    name = payload.get("name") if isinstance(payload.get("name"), str) else None
    country = payload.get("country") if isinstance(payload.get("country"), str) else None

    return {
        "address": str(ip),
        "prefix": cidr,
        "handle": handle,
        "name": name,
        "country": country,
        "holder_org": _extract_holder_org(payload),
        "abuse_email": _extract_abuse(payload),
        "rir": _rir_from_url(base),
        "rdap_url": url,
    }


_RIR_BY_HOST_FRAGMENT = {
    "rdap.arin.net": "arin",
    "rdap.db.ripe.net": "ripe",
    "rdap.apnic.net": "apnic",
    "rdap.lacnic.net": "lacnic",
    "rdap.afrinic.net": "afrinic",
}


def _rir_from_url(base: str) -> str:
    for host, rir in _RIR_BY_HOST_FRAGMENT.items():
        if host in base:
            return rir
    return "unknown"


__all__ = ["lookup_ip"]
