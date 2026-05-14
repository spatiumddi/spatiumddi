"""Platform settings — singleton read/write (superadmin only for writes)."""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_network
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.core.demo_mode import forbid_in_demo_mode
from app.core.permissions import user_has_permission
from app.models.audit_forward import AuditForwardTarget
from app.models.oui import OUIVendor
from app.models.settings import PlatformSettings
from app.services import audit_forward as audit_forward_svc

# SNMP v3 protocol allow-lists. Sticking to the protocols net-snmp's
# Debian build ships out of the box — DES/AES for priv, MD5/SHA for
# auth, plus a sentinel "none" for noAuth/noPriv. Stronger AES-256 +
# SHA-256/384/512 land in net-snmp 5.9+ which Debian trixie carries,
# but we leave that to a follow-up so the first cut works against
# every snmpd build operators are likely to see.
_SNMP_VERSIONS: set[str] = {"v2c", "v3"}
_SNMP_AUTH_PROTOCOLS: set[str] = {"none", "MD5", "SHA"}
_SNMP_PRIV_PROTOCOLS: set[str] = {"none", "DES", "AES"}

# Issue #154 — NTP source modes.
#   ``pool``    — use ``ntp_pool_servers`` only (default for the
#                 stock cloud-init pool.ntp.org configuration).
#   ``servers`` — use ``ntp_custom_servers`` only (air-gapped + shops
#                 that mandate internal NTP).
#   ``mixed``   — use both (operator wants internal primary +
#                 public pool as a fallback).
_NTP_SOURCE_MODES: set[str] = {"pool", "servers", "mixed"}

logger = structlog.get_logger(__name__)
router = APIRouter()

_SINGLETON_ID = 1


# ── Schema ─────────────────────────────────────────────────────────────────────


class SettingsResponse(BaseModel):
    app_title: str
    app_base_url: str
    ip_allocation_strategy: str
    session_timeout_minutes: int
    auto_logout_minutes: int
    utilization_warn_threshold: int
    utilization_critical_threshold: int
    utilization_max_prefix_ipv4: int
    utilization_max_prefix_ipv6: int
    subnet_tree_default_expanded_depth: int
    github_release_check_enabled: bool
    dns_default_ttl: int
    dns_default_zone_type: str
    dns_default_dnssec_validation: str
    dns_recursive_by_default: bool
    dns_auto_sync_enabled: bool
    dns_auto_sync_interval_minutes: int
    dns_auto_sync_delete_stale: bool
    dns_auto_sync_last_run_at: datetime | None
    dns_pull_from_server_enabled: bool
    dns_pull_from_server_interval_minutes: int
    dns_pull_from_server_last_run_at: datetime | None
    dhcp_pull_leases_enabled: bool
    dhcp_pull_leases_interval_seconds: int
    dhcp_pull_leases_last_run_at: datetime | None
    audit_forward_syslog_enabled: bool
    audit_forward_syslog_host: str
    audit_forward_syslog_port: int
    audit_forward_syslog_protocol: str
    audit_forward_syslog_facility: int
    audit_forward_webhook_enabled: bool
    audit_forward_webhook_url: str
    audit_forward_webhook_auth_header: str
    dhcp_default_dns_servers: list[str]
    dhcp_default_domain_name: str
    dhcp_default_domain_search: list[str]
    dhcp_default_ntp_servers: list[str]
    dhcp_default_lease_time: int
    oui_lookup_enabled: bool
    oui_update_interval_hours: int
    oui_last_updated_at: datetime | None
    integration_kubernetes_enabled: bool
    integration_docker_enabled: bool
    integration_proxmox_enabled: bool
    integration_tailscale_enabled: bool
    domain_whois_interval_hours: int
    # Device profiling — fingerbank API key. Boolean reflects whether
    # an encrypted key is stored; the plaintext is never returned.
    fingerbank_api_key_set: bool = False
    # ASN RDAP refresh + RPKI ROA pull (Phase 2 of issue #85).
    asn_whois_interval_hours: int = 24
    rpki_roa_source: str = "cloudflare"
    rpki_roa_refresh_interval_hours: int = 4
    # VRF strict-RD validation toggle (issue #86 phase 2). When False
    # (default), ASN-portion mismatches between the VRF's RD/RT and
    # its linked ASN row produce warnings; when True they are 422.
    vrf_strict_rd_validation: bool = False
    # Operator Copilot daily digest (issue #90 Phase 2). When True,
    # a Celery cron at 08:00 UTC rolls up the prior 24 h, sends to
    # the highest-priority enabled AIProvider for an executive
    # summary, and pushes through audit-forward targets.
    ai_daily_digest_enabled: bool = False
    # Password policy (issue #70). 0 disables history / max-age; the
    # complexity flags are independently toggleable.
    password_min_length: int = 12
    password_require_uppercase: bool = True
    password_require_lowercase: bool = True
    password_require_digit: bool = True
    password_require_symbol: bool = False
    password_history_count: int = 5
    password_max_age_days: int = 0
    # Account lockout (issue #71). 0 disables.
    lockout_threshold: int = 0
    lockout_duration_minutes: int = 15
    lockout_reset_minutes: int = 15
    # ── Appliance SNMP (issue #153) ───────────────────────────────
    # Ciphertext columns are folded into ``*_set`` booleans by the
    # model validator below; v3 user pass fields likewise.
    snmp_enabled: bool = False
    snmp_version: str = "v2c"
    snmp_community_set: bool = False
    snmp_v3_users: list[dict[str, Any]] = []
    snmp_allowed_sources: list[str] = []
    snmp_sys_contact: str = ""
    snmp_sys_location: str = ""
    # ── Appliance NTP (issue #154) ────────────────────────────────
    # No secrets in NTP — server hostnames are not sensitive, so
    # the read shape mirrors the stored shape directly.
    ntp_source_mode: str = "pool"
    ntp_pool_servers: list[str] = ["pool.ntp.org"]
    ntp_custom_servers: list[dict[str, Any]] = []
    ntp_allow_clients: bool = False
    ntp_allow_client_networks: list[str] = []

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _redact_secrets(cls, data: Any) -> Any:
        # When serializing a PlatformSettings ORM instance, fold every
        # ciphertext-bearing column into a boolean ``*_set`` marker so
        # the payload never leaks Fernet bytes — the UI only needs to
        # know whether a value is configured. v3 users get the same
        # treatment per-entry (auth_pass_enc / priv_pass_enc → bool
        # ``*_set`` flags).
        if isinstance(data, PlatformSettings):
            cols = {c.name: getattr(data, c.name) for c in data.__table__.columns}
            cols["fingerbank_api_key_set"] = bool(cols.pop("fingerbank_api_key_encrypted", None))
            cols["snmp_community_set"] = bool(cols.pop("snmp_community_encrypted", None))
            raw_users = cols.get("snmp_v3_users") or []
            cols["snmp_v3_users"] = [_redact_v3_user(u) for u in raw_users]
            return cols
        return data


def _redact_v3_user(u: dict[str, Any]) -> dict[str, Any]:
    """Strip ciphertext from a stored v3-user dict for response shape."""
    return {
        "username": u.get("username", ""),
        "auth_protocol": u.get("auth_protocol") or "none",
        "auth_pass_set": bool(u.get("auth_pass_enc")),
        "priv_protocol": u.get("priv_protocol") or "none",
        "priv_pass_set": bool(u.get("priv_pass_enc")),
    }


def _merge_snmp_v3_users(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Atomic replace + per-user pass merge keyed on username.

    ``incoming`` is the full new list — usernames not present are
    dropped. For each entry, ``auth_pass`` / ``priv_pass`` of
    ``None`` preserves the existing ciphertext for the same
    username; ``""`` clears; anything else is encrypted fresh.

    Encrypted bytes are stored as the URL-safe-base64 string Fernet
    emits so the column stays JSON-friendly.
    """
    from app.core.crypto import encrypt_str

    by_name = {u.get("username"): u for u in existing}
    out: list[dict[str, Any]] = []
    for entry in incoming:
        username = entry["username"]
        prior = by_name.get(username, {})

        def _resolve(submitted: str | None, prior_enc: Any) -> str | None:
            if submitted is None:
                return prior_enc if isinstance(prior_enc, str) and prior_enc else None
            if submitted == "":
                return None
            return encrypt_str(submitted).decode("ascii")

        auth_proto = entry.get("auth_protocol", "none")
        priv_proto = entry.get("priv_protocol", "none")
        out.append(
            {
                "username": username,
                "auth_protocol": auth_proto,
                "auth_pass_enc": (
                    _resolve(entry.get("auth_pass"), prior.get("auth_pass_enc"))
                    if auth_proto != "none"
                    else None
                ),
                "priv_protocol": priv_proto,
                "priv_pass_enc": (
                    _resolve(entry.get("priv_pass"), prior.get("priv_pass_enc"))
                    if priv_proto != "none"
                    else None
                ),
            }
        )
    return out


class SNMPV3UserUpdate(BaseModel):
    """One row in the v3 user list on PUT.

    ``auth_pass`` / ``priv_pass`` semantics mirror the audit-forward
    ``smtp_password`` pattern:

    * ``None`` — leave the existing ciphertext alone (matched by
      ``username`` against the stored list). Used when an operator
      edits sysContact and shouldn't have to retype passwords.
    * ``""`` — clear any existing ciphertext (downgrades the user to
      noAuth or noPriv).
    * non-empty string — encrypt and replace.
    """

    username: str
    auth_protocol: str = "none"
    auth_pass: str | None = None
    priv_protocol: str = "none"
    priv_pass: str | None = None

    @field_validator("username")
    @classmethod
    def _username_nonempty(cls, v: str) -> str:
        # snmpd's USM treats whitespace-only and empty usernames as
        # parse errors — reject early so the operator's PUT is the
        # error site, not a downstream snmpd reload.
        s = v.strip()
        if not s:
            raise ValueError("username may not be empty")
        return s

    @field_validator("auth_protocol")
    @classmethod
    def _valid_auth_proto(cls, v: str) -> str:
        if v not in _SNMP_AUTH_PROTOCOLS:
            raise ValueError(f"auth_protocol must be one of {sorted(_SNMP_AUTH_PROTOCOLS)}")
        return v

    @field_validator("priv_protocol")
    @classmethod
    def _valid_priv_proto(cls, v: str) -> str:
        if v not in _SNMP_PRIV_PROTOCOLS:
            raise ValueError(f"priv_protocol must be one of {sorted(_SNMP_PRIV_PROTOCOLS)}")
        return v


class NTPCustomServerUpdate(BaseModel):
    """One entry in ``ntp_custom_servers`` on PUT.

    ``host`` is the NTP server hostname or IP. ``iburst`` accelerates
    initial sync (chrony sends a burst of 8 packets at startup);
    ``prefer`` tags this server as the canonical source — chrony
    biases toward it for selection when ``prefer`` matches. Both
    flags default off because the safe choice is a neutral
    operator-equal-weight pool; operators flip them as needed.
    """

    host: str
    iburst: bool = False
    prefer: bool = False

    @field_validator("host")
    @classmethod
    def _host_nonempty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("host may not be empty")
        # chrony's parser treats whitespace as a separator inside the
        # server directive, so reject whitespace in the hostname here.
        if any(c.isspace() for c in s):
            raise ValueError("host may not contain whitespace")
        return s


class SettingsUpdate(BaseModel):
    app_title: str | None = None
    app_base_url: str | None = None
    ip_allocation_strategy: str | None = None
    session_timeout_minutes: int | None = None
    auto_logout_minutes: int | None = None
    utilization_warn_threshold: int | None = None
    utilization_critical_threshold: int | None = None
    utilization_max_prefix_ipv4: int | None = None
    utilization_max_prefix_ipv6: int | None = None
    subnet_tree_default_expanded_depth: int | None = None
    github_release_check_enabled: bool | None = None
    dns_default_ttl: int | None = None
    dns_default_zone_type: str | None = None
    dns_default_dnssec_validation: str | None = None
    dns_recursive_by_default: bool | None = None
    dns_auto_sync_enabled: bool | None = None
    dns_auto_sync_interval_minutes: int | None = None
    dns_auto_sync_delete_stale: bool | None = None
    dns_pull_from_server_enabled: bool | None = None
    dns_pull_from_server_interval_minutes: int | None = None
    dhcp_pull_leases_enabled: bool | None = None
    dhcp_pull_leases_interval_seconds: int | None = None
    audit_forward_syslog_enabled: bool | None = None
    audit_forward_syslog_host: str | None = None
    audit_forward_syslog_port: int | None = None
    audit_forward_syslog_protocol: str | None = None
    audit_forward_syslog_facility: int | None = None
    audit_forward_webhook_enabled: bool | None = None
    audit_forward_webhook_url: str | None = None
    audit_forward_webhook_auth_header: str | None = None
    dhcp_default_dns_servers: list[str] | None = None
    dhcp_default_domain_name: str | None = None
    dhcp_default_domain_search: list[str] | None = None
    dhcp_default_ntp_servers: list[str] | None = None
    dhcp_default_lease_time: int | None = None
    oui_lookup_enabled: bool | None = None
    oui_update_interval_hours: int | None = None
    integration_kubernetes_enabled: bool | None = None
    integration_docker_enabled: bool | None = None
    integration_proxmox_enabled: bool | None = None
    integration_tailscale_enabled: bool | None = None
    domain_whois_interval_hours: int | None = None
    # Device profiling — fingerbank API key. Plaintext on the wire
    # (TLS-protected); empty string clears the stored value. Omit the
    # field entirely to leave the existing value alone (pydantic
    # ``model_dump(exclude_none=True)`` semantics in the write path).
    fingerbank_api_key: str | None = None
    # ASN / RPKI Phase 2 settings (issue #85).
    asn_whois_interval_hours: int | None = None
    rpki_roa_source: str | None = None
    rpki_roa_refresh_interval_hours: int | None = None
    # VRF strict-RD validation toggle (issue #86 phase 2).
    vrf_strict_rd_validation: bool | None = None
    # Operator Copilot daily digest (issue #90 Phase 2).
    ai_daily_digest_enabled: bool | None = None
    # Password policy (issue #70).
    password_min_length: int | None = None
    password_require_uppercase: bool | None = None
    password_require_lowercase: bool | None = None
    password_require_digit: bool | None = None
    password_require_symbol: bool | None = None
    password_history_count: int | None = None
    password_max_age_days: int | None = None
    # Account lockout (issue #71).
    lockout_threshold: int | None = None
    lockout_duration_minutes: int | None = None
    lockout_reset_minutes: int | None = None
    # ── Appliance SNMP (issue #153) ───────────────────────────────
    snmp_enabled: bool | None = None
    snmp_version: str | None = None
    # ``None`` = leave alone, ``""`` = clear stored community,
    # non-empty = encrypt + replace. Plaintext on the wire (TLS).
    snmp_community: str | None = None
    # When provided, this list is the new full set — usernames not in
    # the incoming list are removed. Per-user pass semantics see
    # ``SNMPV3UserUpdate`` docstring.
    snmp_v3_users: list[SNMPV3UserUpdate] | None = None
    snmp_allowed_sources: list[str] | None = None
    snmp_sys_contact: str | None = None
    snmp_sys_location: str | None = None
    # ── Appliance NTP (issue #154) ────────────────────────────────
    ntp_source_mode: str | None = None
    ntp_pool_servers: list[str] | None = None
    ntp_custom_servers: list[NTPCustomServerUpdate] | None = None
    ntp_allow_clients: bool | None = None
    ntp_allow_client_networks: list[str] | None = None

    @field_validator("lockout_threshold")
    @classmethod
    def validate_lockout_threshold(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("Lockout threshold must be 0–100 (0 disables)")
        return v

    @field_validator("lockout_duration_minutes", "lockout_reset_minutes")
    @classmethod
    def validate_lockout_window(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 1440):
            raise ValueError("Lockout window must be 1–1440 minutes")
        return v

    @field_validator("password_min_length")
    @classmethod
    def validate_password_min_length(cls, v: int | None) -> int | None:
        # Floor at 6 even when operators relax the policy — anything
        # shorter is a configuration mistake. Cap at 128 because bcrypt
        # truncates inputs above 72 bytes; values past that are dead
        # length the operator might assume is being checked.
        if v is not None and not (6 <= v <= 128):
            raise ValueError("Min password length must be between 6 and 128")
        return v

    @field_validator("password_history_count")
    @classmethod
    def validate_password_history_count(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 24):
            raise ValueError("Password history must be 0–24 (0 disables)")
        return v

    @field_validator("password_max_age_days")
    @classmethod
    def validate_password_max_age_days(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 3650):
            raise ValueError("Password max age must be 0–3650 days (0 disables)")
        return v

    @field_validator("ip_allocation_strategy")
    @classmethod
    def validate_strategy(cls, v: str | None) -> str | None:
        if v is not None and v not in ("sequential", "random"):
            raise ValueError("ip_allocation_strategy must be 'sequential' or 'random'")
        return v

    @field_validator("session_timeout_minutes")
    @classmethod
    def validate_session_timeout(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("Must be >= 0 (0 = no timeout)")
        return v

    @field_validator(
        "dns_auto_sync_interval_minutes",
        "dns_pull_from_server_interval_minutes",
        "oui_update_interval_hours",
    )
    @classmethod
    def validate_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("Must be >= 1")
        return v

    @field_validator("asn_whois_interval_hours", "rpki_roa_refresh_interval_hours")
    @classmethod
    def validate_asn_rpki_interval(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if not (1 <= v <= 168):
            raise ValueError("Must be between 1 and 168 hours")
        return v

    @field_validator("rpki_roa_source")
    @classmethod
    def validate_rpki_source(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in ("cloudflare", "ripe"):
            raise ValueError("rpki_roa_source must be 'cloudflare' or 'ripe'")
        return v

    @field_validator("dhcp_pull_leases_interval_seconds")
    @classmethod
    def validate_dhcp_pull_seconds(cls, v: int | None) -> int | None:
        # Beat ticks every 10 s — anything below that can't be honoured.
        if v is not None and v < 10:
            raise ValueError("Must be >= 10 (Celery beat ticks every 10 seconds)")
        return v

    @field_validator("domain_whois_interval_hours")
    @classmethod
    def validate_domain_whois_interval(cls, v: int | None) -> int | None:
        # Beat ticks every hour for the domain refresh; floor at 1 h
        # (any faster than that just self-skips). Cap at 168 h (one
        # week) — slower than that and operators may as well not have
        # the feature on.
        if v is not None and not (1 <= v <= 168):
            raise ValueError("Must be between 1 and 168 hours")
        return v

    @field_validator("utilization_warn_threshold", "utilization_critical_threshold")
    @classmethod
    def validate_threshold(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 100):
            raise ValueError("Threshold must be between 0 and 100")
        return v

    @field_validator("utilization_max_prefix_ipv4")
    @classmethod
    def validate_max_prefix_v4(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 32):
            raise ValueError("Max IPv4 prefix must be 0–32")
        return v

    @field_validator("utilization_max_prefix_ipv6")
    @classmethod
    def validate_max_prefix_v6(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 128):
            raise ValueError("Max IPv6 prefix must be 0–128")
        return v

    @field_validator("audit_forward_syslog_protocol")
    @classmethod
    def validate_syslog_protocol(cls, v: str | None) -> str | None:
        if v is not None and v not in ("udp", "tcp"):
            raise ValueError("syslog_protocol must be 'udp' or 'tcp'")
        return v

    @field_validator("audit_forward_syslog_port")
    @classmethod
    def validate_syslog_port(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 65535):
            raise ValueError("Port must be 1–65535")
        return v

    @field_validator("audit_forward_syslog_facility")
    @classmethod
    def validate_syslog_facility(cls, v: int | None) -> int | None:
        # RFC 5424 §6.2.1 — facility is 0–23.
        if v is not None and not (0 <= v <= 23):
            raise ValueError("Syslog facility must be 0–23 (RFC 5424)")
        return v

    @field_validator("snmp_version")
    @classmethod
    def _valid_snmp_version(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in _SNMP_VERSIONS:
            raise ValueError(f"snmp_version must be one of {sorted(_SNMP_VERSIONS)}")
        return v

    @field_validator("snmp_allowed_sources")
    @classmethod
    def _valid_snmp_sources(cls, v: list[str] | None) -> list[str] | None:
        # Each entry must parse as a CIDR (or single host). Normalise
        # to the canonical network string so duplicates produced by
        # different host-bit inputs collapse on the way in.
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            s = raw.strip()
            if not s:
                continue
            try:
                net = ip_network(s, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid CIDR or host: {raw!r} ({exc})") from exc
            out.append(str(net))
        return out

    @field_validator("ntp_source_mode")
    @classmethod
    def _valid_ntp_source_mode(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if v not in _NTP_SOURCE_MODES:
            raise ValueError(f"ntp_source_mode must be one of {sorted(_NTP_SOURCE_MODES)}")
        return v

    @field_validator("ntp_pool_servers")
    @classmethod
    def _valid_ntp_pool_servers(cls, v: list[str] | None) -> list[str] | None:
        # Pool hostnames are free-form per chrony's parser. Strip
        # whitespace + drop empties; reject embedded whitespace
        # (chrony reads the directive as space-separated tokens).
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            s = raw.strip()
            if not s:
                continue
            if any(c.isspace() for c in s):
                raise ValueError(f"pool hostname may not contain whitespace: {raw!r}")
            out.append(s)
        return out

    @field_validator("ntp_allow_client_networks")
    @classmethod
    def _valid_ntp_allow_networks(cls, v: list[str] | None) -> list[str] | None:
        # Same CIDR canonicalisation as snmp_allowed_sources — chrony's
        # ``allow`` directive accepts both v4 + v6 networks.
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            s = raw.strip()
            if not s:
                continue
            try:
                net = ip_network(s, strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid CIDR: {raw!r} ({exc})") from exc
            out.append(str(net))
        return out


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _get_or_create(db: DB) -> PlatformSettings:
    settings = await db.get(PlatformSettings, _SINGLETON_ID)
    if settings is None:
        settings = PlatformSettings(id=_SINGLETON_ID)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


_USER_SETTABLE_FIELDS = set(SettingsUpdate.model_fields.keys())


def _column_defaults() -> dict[str, Any]:
    """Introspect the model's `default=` kwargs so the UI has a single source
    of truth for "reset to defaults" — the same values Postgres would insert
    for a fresh row. Only user-settable fields (those present on
    `SettingsUpdate`) are returned; server-managed columns like
    `*_last_run_at` are omitted."""
    out: dict[str, Any] = {}
    for col in PlatformSettings.__table__.columns:
        if col.name not in _USER_SETTABLE_FIELDS:
            continue
        d = col.default
        if d is None:
            continue
        arg = d.arg
        if callable(arg):
            try:
                out[col.name] = arg({})
            except TypeError:
                out[col.name] = arg()
        else:
            out[col.name] = arg
    return out


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("", response_model=SettingsResponse)
async def get_settings(current_user: CurrentUser, db: DB) -> PlatformSettings:
    return await _get_or_create(db)


@router.get("/defaults")
async def get_settings_defaults(current_user: CurrentUser) -> dict[str, Any]:
    return _column_defaults()


@router.put("", response_model=SettingsResponse)
async def update_settings(
    body: SettingsUpdate, current_user: CurrentUser, db: DB
) -> PlatformSettings:
    forbid_in_demo_mode("Platform settings updates are disabled")
    # Superadmin passes via user_has_permission shortcut; users with an
    # explicit `write`/`admin` grant on `settings` also pass.
    if not user_has_permission(current_user, "write", "settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: need 'write' on 'settings'",
        )

    settings = await _get_or_create(db)
    changes = body.model_dump(exclude_none=True)

    # fingerbank_api_key needs Fernet encryption + maps to a different
    # column name. Empty string = clear; non-empty = encrypt + store.
    # Pop it out of ``changes`` so the audit log doesn't capture the
    # plaintext, then set the encrypted column directly.
    if "fingerbank_api_key" in changes:
        from app.core.crypto import encrypt_str

        raw = changes.pop("fingerbank_api_key")
        if raw == "":
            settings.fingerbank_api_key_encrypted = None
            changes["fingerbank_api_key_cleared"] = True
        else:
            settings.fingerbank_api_key_encrypted = encrypt_str(raw)
            # Don't echo the plaintext in the audit log — record only
            # that a value was set.
            changes["fingerbank_api_key_set"] = True

    # snmp_community: same encrypt-and-redact shape as fingerbank.
    if "snmp_community" in changes:
        from app.core.crypto import encrypt_str

        raw = changes.pop("snmp_community")
        if raw == "":
            settings.snmp_community_encrypted = None
            changes["snmp_community_cleared"] = True
        else:
            settings.snmp_community_encrypted = encrypt_str(raw)
            changes["snmp_community_set"] = True

    # snmp_v3_users: atomic replace with per-user pass merge. Match
    # by username so an operator editing one user's protocol doesn't
    # have to retype every other user's passwords. Drop from
    # ``changes`` so the generic setattr loop below doesn't clobber
    # the merged form with the audit-redacted shape we re-stash for
    # logging.
    snmp_users_audit: list[dict[str, Any]] | None = None
    if "snmp_v3_users" in changes:
        incoming = changes.pop("snmp_v3_users")
        existing = list(settings.snmp_v3_users or [])
        merged = _merge_snmp_v3_users(existing, incoming)
        settings.snmp_v3_users = merged
        # Stash the redacted shape for the audit-log call only — never
        # put it back on ``changes`` because the setattr loop would
        # then write the redacted dict back over ``settings.snmp_v3_users``,
        # losing every encrypted pass.
        snmp_users_audit = [_redact_v3_user(u) for u in merged]

    for field, value in changes.items():
        # Skip the synthetic audit-only flags we just inserted.
        if field in (
            "fingerbank_api_key_cleared",
            "fingerbank_api_key_set",
            "snmp_community_cleared",
            "snmp_community_set",
        ):
            continue
        setattr(settings, field, value)

    if snmp_users_audit is not None:
        changes["snmp_v3_users"] = snmp_users_audit

    await db.commit()
    await db.refresh(settings)
    logger.info("platform_settings_updated", user=current_user.username, changes=changes)
    return settings


# ── OUI vendor database ───────────────────────────────────────────────────────
#
# Opt-in feature controlled by ``oui_lookup_enabled``. These endpoints let
# the Settings UI show the vendor-count + last-updated timestamp and kick
# off a manual refresh without waiting for the hourly beat tick.


class OUIStatusResponse(BaseModel):
    enabled: bool
    interval_hours: int
    last_updated_at: datetime | None
    vendor_count: int


class OUIRefreshResponse(BaseModel):
    status: str  # "queued" | "disabled"
    task_id: str | None = None


class OUITaskStatusResponse(BaseModel):
    """Shape returned by the polling endpoint the refresh modal hits.

    ``state`` mirrors Celery's task states (``PENDING``, ``STARTED``,
    ``SUCCESS``, ``FAILURE``, ``RETRY``). When ``state == "SUCCESS"`` the
    ``result`` field carries the diff counters emitted by the task's
    return value. When ``state == "FAILURE"`` the ``error`` field holds
    the exception repr — enough context for the modal to display
    without leaking internal traces to non-admin users (the endpoint is
    already admin-scoped).
    """

    task_id: str
    state: str
    ready: bool
    result: dict[str, Any] | None = None
    error: str | None = None


@router.get("/oui/status", response_model=OUIStatusResponse)
async def get_oui_status(current_user: CurrentUser, db: DB) -> OUIStatusResponse:
    ps = await _get_or_create(db)
    count = (await db.execute(select(func.count(OUIVendor.prefix)))).scalar_one()
    return OUIStatusResponse(
        enabled=ps.oui_lookup_enabled,
        interval_hours=ps.oui_update_interval_hours,
        last_updated_at=ps.oui_last_updated_at,
        vendor_count=int(count),
    )


# ── SNMP community reveal ──────────────────────────────────────────
#
# Operators legitimately need to look up the configured v2c community
# string — it's the credential they paste into their NMS / snmpwalk
# command. The flat ``GET /settings/`` response never returns the
# plaintext (only ``snmp_community_set: bool``), so this dedicated
# endpoint behind a password-confirm + superadmin gate is the path.
# Mirrors the agent-bootstrap-keys reveal pattern (see
# ``backend/app/api/v1/admin/agent_keys.py``).


class RevealCommunityRequest(BaseModel):
    password: str


class RevealCommunityResponse(BaseModel):
    configured: bool
    community: str | None


@router.post(
    "/snmp/reveal-community",
    response_model=RevealCommunityResponse,
    summary="Reveal the configured SNMP v2c community (superadmin + password)",
)
async def reveal_snmp_community(
    body: RevealCommunityRequest,
    current_user: CurrentUser,
    db: DB,
) -> RevealCommunityResponse:
    """Return the v2c community string after password re-verification.

    Same shape as ``POST /api/v1/admin/agent-keys/reveal`` — both the
    success path and every denial path emit an audit row so abuse is
    at least visible. Local-auth users only; external-auth users have
    no local password to re-confirm.
    """
    from app.core.crypto import decrypt_str
    from app.core.security import verify_password
    from app.models.audit import AuditLog

    def _audit_denied(reason: str) -> None:
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="snmp_community_reveal_denied",
                resource_type="platform_settings",
                resource_id="snmp",
                resource_display="SNMP community",
                result="forbidden",
                new_value={"reason": reason},
            )
        )

    if not current_user.is_superadmin:
        _audit_denied("non_superadmin")
        await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only superadmins can reveal the SNMP community",
        )

    if current_user.auth_source != "local":
        _audit_denied("external_auth")
        await db.commit()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "SNMP community reveal requires a local-auth superadmin "
            f"(your account authenticates via {current_user.auth_source}). "
            "Log in as a local admin to reveal the community.",
        )

    if not current_user.hashed_password or not verify_password(
        body.password, current_user.hashed_password
    ):
        _audit_denied("password_mismatch")
        await db.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password is incorrect")

    settings = await _get_or_create(db)
    if not settings.snmp_community_encrypted:
        # No reveal-denied audit row — there's nothing to reveal, the
        # password-confirm path completed cleanly, and an empty row
        # is genuinely informational.
        return RevealCommunityResponse(configured=False, community=None)

    try:
        plaintext = decrypt_str(settings.snmp_community_encrypted)
    except Exception:
        _audit_denied("decrypt_failed")
        await db.commit()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Stored community could not be decrypted (key mismatch?). "
            "Re-set the community in Settings → Appliance → SNMP.",
        ) from None

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="snmp_community_revealed",
            resource_type="platform_settings",
            resource_id="snmp",
            resource_display="SNMP community",
            result="success",
        )
    )
    await db.commit()
    logger.info("snmp_community_revealed", user=current_user.username)
    return RevealCommunityResponse(configured=True, community=plaintext)


@router.post(
    "/oui/refresh",
    response_model=OUIRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_oui_refresh(current_user: CurrentUser, db: DB) -> OUIRefreshResponse:
    if not user_has_permission(current_user, "write", "settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: need 'write' on 'settings'",
        )

    ps = await _get_or_create(db)
    if not ps.oui_lookup_enabled:
        return OUIRefreshResponse(status="disabled")

    # Deferred import so the web process doesn't pull the celery task graph
    # into its startup path.
    from app.tasks.oui_update import update_oui_database_now  # noqa: PLC0415

    result = update_oui_database_now.delay()
    logger.info("oui_refresh_triggered", user=current_user.username, task_id=result.id)
    return OUIRefreshResponse(status="queued", task_id=result.id)


@router.get("/oui/refresh/{task_id}", response_model=OUITaskStatusResponse)
async def get_oui_refresh_status(task_id: str, current_user: CurrentUser) -> OUITaskStatusResponse:
    """Poll an in-flight OUI refresh task.

    Celery's ``AsyncResult`` is backed by Redis (the configured
    ``CELERY_RESULT_BACKEND``) and returns ``PENDING`` for unknown task
    IDs, which is indistinguishable from "queued but not picked up
    yet" — the UI treats both the same. A ``task_id`` from a previous
    restart will stay ``PENDING`` forever; the modal caps its poll at
    a timeout to cover that case.
    """
    # Deferred import keeps the router lightweight.
    from celery.result import AsyncResult  # noqa: PLC0415

    from app.celery_app import celery_app  # noqa: PLC0415

    async_result = AsyncResult(task_id, app=celery_app)
    state = async_result.state
    payload = OUITaskStatusResponse(
        task_id=task_id,
        state=state,
        ready=async_result.ready(),
    )
    if state == "SUCCESS":
        raw = async_result.result
        payload.result = raw if isinstance(raw, dict) else {"value": str(raw)}
    elif state == "FAILURE":
        payload.error = repr(async_result.result) if async_result.result else "task failed"
    return payload


# ── Audit forward targets (multi-target + multi-format) ───────────────────

_VALID_KINDS = {"syslog", "webhook", "smtp"}
_VALID_WEBHOOK_FLAVORS = {"generic", "slack", "teams", "discord"}
_VALID_SMTP_SECURITY = {"none", "starttls", "ssl"}
_VALID_FORMATS = {
    "rfc5424_json",
    "rfc5424_cef",
    "rfc5424_leef",
    "rfc3164",
    "json_lines",
}
_VALID_PROTOCOLS = {"udp", "tcp", "tls"}
_VALID_SEVERITIES = {"info", "warn", "error", "denied"}


class AuditTargetBody(BaseModel):
    """Create / update body. kind-specific fields are ignored for
    other kinds, so a single shape fits all three (syslog/webhook/smtp)."""

    name: str
    enabled: bool = True
    kind: str
    format: str = "rfc5424_json"
    # syslog
    host: str = ""
    port: int = 514
    protocol: str = "udp"
    facility: int = 16
    ca_cert_pem: str | None = None
    # webhook
    url: str = ""
    auth_header: str = ""
    webhook_flavor: str = "generic"
    # smtp
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str = ""
    # When ``None``, leave the existing encrypted password alone (so
    # the operator can edit the From-address without retyping the
    # password). Empty string ``""`` clears it. Anything else is
    # encrypted at rest.
    smtp_password: str | None = None
    smtp_from_address: str = ""
    smtp_to_addresses: list[str] | None = None
    smtp_reply_to: str = ""
    # filter
    min_severity: str | None = None
    resource_types: list[str] | None = None

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        if v not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}")
        return v

    @field_validator("format")
    @classmethod
    def _valid_format(cls, v: str) -> str:
        if v not in _VALID_FORMATS:
            raise ValueError(f"format must be one of {sorted(_VALID_FORMATS)}")
        return v

    @field_validator("protocol")
    @classmethod
    def _valid_protocol(cls, v: str) -> str:
        if v not in _VALID_PROTOCOLS:
            raise ValueError(f"protocol must be one of {sorted(_VALID_PROTOCOLS)}")
        return v

    @field_validator("webhook_flavor")
    @classmethod
    def _valid_flavor(cls, v: str) -> str:
        if v not in _VALID_WEBHOOK_FLAVORS:
            raise ValueError(f"webhook_flavor must be one of {sorted(_VALID_WEBHOOK_FLAVORS)}")
        return v

    @field_validator("smtp_security")
    @classmethod
    def _valid_smtp_security(cls, v: str) -> str:
        if v not in _VALID_SMTP_SECURITY:
            raise ValueError(f"smtp_security must be one of {sorted(_VALID_SMTP_SECURITY)}")
        return v

    @field_validator("min_severity")
    @classmethod
    def _valid_severity(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if v not in _VALID_SEVERITIES:
            raise ValueError(f"min_severity must be one of {sorted(_VALID_SEVERITIES)}")
        return v


class AuditTargetResponse(BaseModel):
    id: str
    name: str
    enabled: bool
    kind: str
    format: str
    host: str
    port: int
    protocol: str
    facility: int
    ca_cert_pem: str | None
    url: str
    # Redact auth_header — we return whether it's set, never the value.
    auth_header_set: bool
    webhook_flavor: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str
    # Redact the SMTP password the same way Fingerbank does — bool only.
    smtp_password_set: bool
    smtp_from_address: str
    smtp_to_addresses: list[str] | None
    smtp_reply_to: str
    min_severity: str | None
    resource_types: list[str] | None
    created_at: datetime
    modified_at: datetime


def _target_to_response(t: AuditForwardTarget) -> AuditTargetResponse:
    return AuditTargetResponse(
        id=str(t.id),
        name=t.name,
        enabled=t.enabled,
        kind=t.kind,
        format=t.format,
        host=t.host,
        port=t.port,
        protocol=t.protocol,
        facility=t.facility,
        ca_cert_pem=t.ca_cert_pem,
        url=t.url,
        auth_header_set=bool(t.auth_header),
        webhook_flavor=t.webhook_flavor or "generic",
        smtp_host=t.smtp_host or "",
        smtp_port=int(t.smtp_port or 587),
        smtp_security=t.smtp_security or "starttls",
        smtp_username=t.smtp_username or "",
        smtp_password_set=bool(t.smtp_password_encrypted),
        smtp_from_address=t.smtp_from_address or "",
        smtp_to_addresses=list(t.smtp_to_addresses) if t.smtp_to_addresses else None,
        smtp_reply_to=t.smtp_reply_to or "",
        min_severity=t.min_severity,
        resource_types=t.resource_types,
        created_at=t.created_at,
        modified_at=t.modified_at,
    )


def _apply_body(t: AuditForwardTarget, body: AuditTargetBody) -> None:
    from app.core.crypto import encrypt_str

    t.name = body.name
    t.enabled = body.enabled
    t.kind = body.kind
    t.format = body.format
    t.host = body.host
    t.port = body.port
    t.protocol = body.protocol
    t.facility = body.facility
    t.ca_cert_pem = body.ca_cert_pem
    t.url = body.url
    t.auth_header = body.auth_header
    t.webhook_flavor = body.webhook_flavor
    t.smtp_host = body.smtp_host
    t.smtp_port = body.smtp_port
    t.smtp_security = body.smtp_security
    t.smtp_username = body.smtp_username
    t.smtp_from_address = body.smtp_from_address
    t.smtp_to_addresses = body.smtp_to_addresses
    t.smtp_reply_to = body.smtp_reply_to
    # ``smtp_password`` semantics: ``None`` = leave existing encrypted
    # value alone (operator editing other fields), ``""`` = clear,
    # anything else = encrypt + replace.
    if body.smtp_password is not None:
        t.smtp_password_encrypted = encrypt_str(body.smtp_password) if body.smtp_password else None
    t.min_severity = body.min_severity
    t.resource_types = body.resource_types


@router.get("/audit-forward-targets", response_model=list[AuditTargetResponse])
async def list_audit_targets(current_user: CurrentUser, db: DB) -> list[AuditTargetResponse]:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    res = await db.execute(select(AuditForwardTarget).order_by(AuditForwardTarget.name))
    return [_target_to_response(t) for t in res.scalars().all()]


@router.post(
    "/audit-forward-targets",
    response_model=AuditTargetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_audit_target(
    body: AuditTargetBody, current_user: CurrentUser, db: DB
) -> AuditTargetResponse:
    forbid_in_demo_mode("Audit-forward target creation is disabled")
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = AuditForwardTarget()
    _apply_body(row, body)
    db.add(row)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 — name collisions land here
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"create failed: {exc}") from exc
    await db.refresh(row)
    return _target_to_response(row)


@router.put("/audit-forward-targets/{target_id}", response_model=AuditTargetResponse)
async def update_audit_target(
    target_id: str, body: AuditTargetBody, current_user: CurrentUser, db: DB
) -> AuditTargetResponse:
    forbid_in_demo_mode("Audit-forward target updates are disabled")
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = await db.get(AuditForwardTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")
    _apply_body(row, body)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"update failed: {exc}") from exc
    await db.refresh(row)
    return _target_to_response(row)


@router.delete("/audit-forward-targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_audit_target(target_id: str, current_user: CurrentUser, db: DB) -> None:
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = await db.get(AuditForwardTarget, target_id)
    if row is None:
        return
    await db.delete(row)
    await db.commit()


@router.post("/audit-forward-targets/{target_id}/test")
async def test_audit_target(target_id: str, current_user: CurrentUser, db: DB) -> dict[str, Any]:
    """Send a synthetic event to one target and report success / error.

    The event is flagged ``action="test_forward"`` so the operator can
    filter it out in the collector if they want. Doesn't land in
    ``audit_log`` — this is explicit probe traffic, not an audit.
    """
    if not current_user.is_superadmin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = await db.get(AuditForwardTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")

    now = datetime.utcnow()
    payload: dict[str, Any] = {
        "id": "test-" + str(row.id),
        "timestamp": now.isoformat() + "Z",
        "action": "test_forward",
        "resource_type": "audit_forward_target",
        "resource_id": str(row.id),
        "resource_display": row.name,
        "result": "success",
        "user_id": str(current_user.id),
        "user_display_name": current_user.display_name,
        "auth_source": "local",
        "changed_fields": [],
        "old_value": None,
        "new_value": None,
    }
    # Decrypt the SMTP password lazily — only the test path needs the
    # cleartext, and only inside this request scope.
    smtp_password = ""
    if row.kind == "smtp" and row.smtp_password_encrypted:
        try:
            from app.core.crypto import decrypt_str

            smtp_password = decrypt_str(row.smtp_password_encrypted)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"failed to decrypt SMTP password: {exc}",
            ) from exc
    target_dict = {
        "name": row.name,
        "kind": row.kind,
        "format": row.format,
        "host": row.host,
        "port": row.port,
        "protocol": row.protocol,
        "facility": row.facility,
        "ca_cert_pem": row.ca_cert_pem,
        "url": row.url,
        "auth_header": row.auth_header or "",
        "webhook_flavor": row.webhook_flavor or "generic",
        "smtp_host": row.smtp_host or "",
        "smtp_port": int(row.smtp_port or 587),
        "smtp_security": row.smtp_security or "starttls",
        "smtp_username": row.smtp_username or "",
        "smtp_password": smtp_password,
        "smtp_from_address": row.smtp_from_address or "",
        "smtp_to_addresses": list(row.smtp_to_addresses or []),
        "smtp_reply_to": row.smtp_reply_to or "",
        "min_severity": None,  # ignore filter on a probe
        "resource_types": None,
    }
    try:
        await audit_forward_svc._deliver_to_target(target_dict, payload)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"delivery failed: {exc}") from exc
    return {"status": "ok", "target": row.name}
