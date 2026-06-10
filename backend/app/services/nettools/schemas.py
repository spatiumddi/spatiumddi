"""Pydantic request/response models for the built-in network tools (#58).

Lives in the service layer (not the api layer) so the runner +
socket_tools modules can import the result models + ``validate_host``
without creating a circular import back through the
``app.api.v1.tools`` package ``__init__`` (which imports the router,
which imports these services). The api layer re-exports everything from
``app.api.v1.tools.schemas`` for handler convenience.

Every request model validates its target / port / record-type input
tightly enough that the argv builders in
:mod:`app.services.nettools.runner` can pass values straight through to
``create_subprocess_exec`` without risk of shell injection. The
validators mirror the nmap runner's ``_validate_target`` /
``_validate_port_spec`` model — a literal IPv4/IPv6 address, a CIDR, or
an RFC 1123 hostname, nothing else.

Responses are deliberately thin and uniform: each tool returns the
resolved ``argv`` (so operators see exactly what ran), an ``exit_code``,
timing, and the captured stdout/stderr. The socket-based tools
(port-test, TLS-cert) return structured fields instead.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final

from pydantic import BaseModel, Field, field_validator

# ── shared validators ──────────────────────────────────────────────

# RFC 1123 / 952 hostname — same shape the nmap runner accepts. Labels
# of [A-Za-z0-9-] up to 63 chars, joined by dots, optional trailing dot.
_HOSTNAME_RE: Final = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)

# dig record types we expose — same curated list as the DNS propagation
# tool plus a couple query-only extras (ANY, NAPTR, DS, DNSKEY).
_VALID_RECORD_TYPES: Final[frozenset[str]] = frozenset(
    {
        "A",
        "AAAA",
        "CNAME",
        "MX",
        "TXT",
        "NS",
        "SOA",
        "PTR",
        "SRV",
        "CAA",
        "TLSA",
        "DS",
        "DNSKEY",
        "NAPTR",
        "ANY",
    }
)


def validate_host(value: str) -> str:
    """Accept a literal IPv4/IPv6 address or an RFC 1123 hostname.

    Rejects CIDR, shell metacharacters, spaces, and anything that isn't
    a valid DNS label / IP literal. Raised as a Pydantic ``ValueError``
    so it surfaces as a 422.
    """
    value = value.strip()
    if not value:
        raise ValueError("host is required")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if _HOSTNAME_RE.match(value):
        return value
    raise ValueError(f"host must be a valid IPv4/IPv6 address or hostname (got {value!r})")


def validate_host_or_cidr(value: str) -> str:
    """Like :func:`validate_host` but also accepts a CIDR (for traceroute /
    ping where the operator may target a network address). Falls back to
    the host validator otherwise."""
    value = value.strip()
    if "/" in value:
        try:
            net = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError(f"not a valid CIDR: {value!r} ({exc})") from exc
        return str(net.network_address)
    return validate_host(value)


# ── ping / traceroute / mtr ─────────────────────────────────────────


class HostRequest(BaseModel):
    """Shared single-host request shape for ping / traceroute / mtr."""

    host: str = Field(min_length=1, max_length=253)

    @field_validator("host")
    @classmethod
    def _v_host(cls, v: str) -> str:
        return validate_host(v)


class CommandResult(BaseModel):
    """Uniform subprocess result. ``available`` is False when the binary
    is missing — handlers surface this as a clean 200 with the reason in
    ``error`` rather than a 500."""

    tool: str
    argv: list[str]
    available: bool
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: float | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


# ── dig ─────────────────────────────────────────────────────────────


class DigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=253)
    record_type: str = Field(default="A")
    # Optional resolver to query (``@server``). When null dig uses the
    # server's /etc/resolv.conf.
    server: str | None = Field(default=None, max_length=253)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        # A DNS name is hostname-shaped; reuse the host validator but
        # allow a leading underscore label (e.g. _dmarc, _acme-challenge)
        # which is valid in DNS but not in RFC 1123 host syntax.
        candidate = v.lstrip("_")
        if candidate and _HOSTNAME_RE.match(candidate.replace("_", "a")):
            return v
        if _HOSTNAME_RE.match(v):
            return v
        raise ValueError(f"name is not a valid DNS name: {v!r}")

    @field_validator("record_type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        u = v.strip().upper()
        if u not in _VALID_RECORD_TYPES:
            raise ValueError(
                f"Unsupported record type. Allowed: {', '.join(sorted(_VALID_RECORD_TYPES))}"
            )
        return u

    @field_validator("server")
    @classmethod
    def _v_server(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_host(v)


# ── whois ───────────────────────────────────────────────────────────


class WhoisRequest(BaseModel):
    query: str = Field(min_length=1, max_length=253)

    @field_validator("query")
    @classmethod
    def _v_query(cls, v: str) -> str:
        # whois accepts an IP, a domain, or an ASN ("AS13335"). Validate
        # against the host shape (covers IP + domain); allow a leading
        # "AS"/"as" + digits for ASN queries.
        v = v.strip()
        if not v:
            raise ValueError("query is required")
        if re.fullmatch(r"(?i:as)?\d{1,10}", v):
            return v
        return validate_host(v)


# ── port test ───────────────────────────────────────────────────────


class PortTestRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    protocol: str = Field(default="tcp")
    timeout_seconds: float = Field(default=5.0, ge=0.5, le=15.0)

    @field_validator("host")
    @classmethod
    def _v_host(cls, v: str) -> str:
        return validate_host(v)

    @field_validator("protocol")
    @classmethod
    def _v_proto(cls, v: str) -> str:
        u = v.strip().lower()
        if u not in {"tcp", "udp"}:
            raise ValueError("protocol must be 'tcp' or 'udp'")
        return u


class PortTestResult(BaseModel):
    host: str
    port: int
    protocol: str
    # tcp: "open" | "closed" | "filtered" | "error"
    # udp: "open|filtered" | "closed" | "error" (UDP can't be definitive)
    state: str
    rtt_ms: float | None = None
    error: str | None = None


# ── TLS certificate inspection ──────────────────────────────────────


class TlsCertRequest(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=443, ge=1, le=65535)
    # Optional SNI override; defaults to ``host`` when null.
    server_name: str | None = Field(default=None, max_length=253)
    timeout_seconds: float = Field(default=8.0, ge=0.5, le=15.0)

    @field_validator("host")
    @classmethod
    def _v_host(cls, v: str) -> str:
        return validate_host(v)

    @field_validator("server_name")
    @classmethod
    def _v_sni(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_host(v)


class TlsCertResult(BaseModel):
    host: str
    port: int
    server_name: str | None
    ok: bool
    subject: str | None = None
    issuer: str | None = None
    san: list[str] = []
    not_before: str | None = None
    not_after: str | None = None
    days_remaining: int | None = None
    expired: bool = False
    self_signed: bool = False
    hostname_matches: bool | None = None
    serial: str | None = None
    signature_algorithm: str | None = None
    error: str | None = None


# ── DNS propagation (reuses the dns_tools helper) ───────────────────


class PropagationRequest(BaseModel):
    name: str = Field(min_length=1, max_length=253)
    record_type: str = Field(default="A")
    resolvers: list[str] | None = Field(default=None, max_length=12)
    timeout_seconds: float = Field(default=3.0, ge=0.5, le=10.0)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        candidate = v.lstrip("_").replace("_", "a")
        if candidate and _HOSTNAME_RE.match(candidate):
            return v.rstrip(".")
        raise ValueError(f"name is not a valid DNS name: {v!r}")

    @field_validator("record_type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        u = v.strip().upper()
        if u not in _VALID_RECORD_TYPES:
            raise ValueError(
                f"Unsupported record type. Allowed: {', '.join(sorted(_VALID_RECORD_TYPES))}"
            )
        return u

    @field_validator("resolvers")
    @classmethod
    def _v_resolvers(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [validate_host(ip) for ip in v]


# ── MAC vendor lookup (reuses services/oui) ─────────────────────────


class MacVendorRequest(BaseModel):
    macs: list[str] = Field(min_length=1, max_length=256)


class MacVendorEntry(BaseModel):
    mac: str
    vendor: str | None = None
    is_voip_phone: bool = False


class MacVendorResult(BaseModel):
    # ``oui_enabled`` surfaces the platform-settings short-circuit so the
    # UI can explain "OUI lookup is disabled" instead of rendering empty
    # vendor cells.
    oui_enabled: bool
    entries: list[MacVendorEntry]


__all__ = [
    "CommandResult",
    "DigRequest",
    "HostRequest",
    "MacVendorEntry",
    "MacVendorRequest",
    "MacVendorResult",
    "PortTestRequest",
    "PortTestResult",
    "PropagationRequest",
    "TlsCertRequest",
    "TlsCertResult",
    "WhoisRequest",
    "validate_host",
    "validate_host_or_cidr",
]
