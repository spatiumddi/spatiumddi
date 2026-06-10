"""Platform settings — singleton read/write (superadmin only for writes)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from ipaddress import ip_network
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.core.agent_wake import HOSTCONFIG_ALL, publish_wake
from app.core.demo_mode import forbid_in_demo_mode
from app.core.permissions import is_effective_superadmin, user_has_permission
from app.models.audit import AuditLog
from app.models.audit_forward import AuditForwardTarget
from app.models.oui import OUIVendor
from app.models.settings import PlatformSettings
from app.services import audit_forward as audit_forward_svc
from app.services.appliance.ssh import is_valid_public_key, validate_lockout_safe

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

# Issue #156 — rsyslog forward-target validation. ``udp`` / ``tcp`` are
# plaintext; ``tls`` uses rsyslog's gtls stream driver + requires a CA
# PEM. Wire formats map to rsyslog templates in services/appliance/syslog.py.
_SYSLOG_PROTOCOLS: set[str] = {"udp", "tcp", "tls"}
_SYSLOG_FORMATS: set[str] = {"rfc5424", "rfc3164", "json"}

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
    reverse_dns_enabled: bool
    reverse_dns_interval_minutes: int
    reverse_dns_resolvers: list[str] | None
    reverse_dns_last_run_at: datetime | None
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
    integration_cloud_enabled: bool
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
    # ── Appliance timezone (issue #165) ───────────────────────────
    # Empty string means "follow install-time default" (no override).
    timezone: str = ""
    # ── Appliance verbose boot console ────────────────────────────
    # True = standard Linux console on boot (kernel messages + systemd
    # status + getty, no dashboard); False = quiet boot + dashboard.
    verbose_boot: bool = False
    # ── Maintenance mode (issue #57) ──────────────────────────────
    # System-wide read-only switch. ``maintenance_started_at`` is
    # server-managed (stamped on enable / cleared on disable) and so is
    # read-only here — it has no companion field on SettingsUpdate.
    maintenance_mode_enabled: bool = False
    maintenance_message: str = ""
    maintenance_started_at: datetime | None = None
    # ── Appliance LLDP (issue #343) ───────────────────────────────
    # No secrets — LLDP advertises public identity, so the read shape
    # mirrors the stored shape directly (like NTP).
    lldp_enabled: bool = False
    lldp_tx_interval: int = 30
    lldp_tx_hold: int = 4
    lldp_protocols: list[str] = []
    lldp_interface_pattern: str = ""
    lldp_management_pattern: str = ""
    lldp_sys_name: str = ""
    lldp_sys_description: str = ""
    lldp_med_location: dict[str, Any] = {}
    lldp_snmp_agentx: bool = False
    # ── Appliance syslog forwarding (issue #156) ──────────────────
    # Per-target ``ca_cert_pem`` ciphertext is folded into a per-entry
    # ``ca_cert_set`` boolean by the model validator below — the wire
    # never carries the CA PEM bytes (mirrors the SNMP community).
    syslog_enabled: bool = False
    syslog_targets: list[dict[str, Any]] = []
    syslog_filter: str = ""
    syslog_buffer_disk: bool = False
    # ── Appliance SSH (issue #157) ─────────────────────────────────
    # Public keys are NOT secrets — the authorized-keys list is returned
    # verbatim (no redaction, unlike the SNMP community / syslog CA PEM).
    # Each entry is ``{name, public_key, comment}``.
    ssh_authorized_keys: list[dict[str, Any]] = []
    ssh_password_auth_enabled: bool = True
    ssh_allow_root_login: bool = False
    ssh_port: int = 22
    ssh_allowed_source_networks: list[str] = []

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
            # Issue #156 — fold each syslog target's CA PEM ciphertext into
            # a ``ca_cert_set`` boolean so the wire never carries the PEM.
            raw_targets = cols.get("syslog_targets") or []
            cols["syslog_targets"] = [_redact_syslog_target(t) for t in raw_targets]
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


def _redact_syslog_target(t: dict[str, Any]) -> dict[str, Any]:
    """Strip the CA PEM ciphertext from a stored syslog-target dict for
    the response shape — the operator only needs to know whether a CA
    is configured (``ca_cert_set``), never the PEM bytes (#156)."""
    return {
        "host": t.get("host", ""),
        "port": int(t.get("port") or 514),
        "protocol": t.get("protocol") or "udp",
        "format": t.get("format") or "rfc5424",
        "ca_cert_set": bool(t.get("ca_cert_pem")),
    }


def _merge_syslog_targets(
    existing: list[dict[str, Any]], incoming: list[SyslogTargetUpdate]
) -> list[dict[str, Any]]:
    """Atomic replace + per-target CA-PEM merge keyed on ``(host, port)``.

    ``incoming`` is the full new list — targets not present are dropped.
    For each entry, ``ca_cert_pem`` of ``None`` preserves the existing
    ciphertext for the same ``(host, port)`` key; ``""`` clears; anything
    else is encrypted fresh. Mirrors ``_merge_snmp_v3_users`` so an
    operator editing a target's format doesn't have to re-paste its CA
    PEM. Encrypted bytes are stored as the URL-safe-base64 string Fernet
    emits so the JSONB column stays JSON-friendly.
    """
    from app.core.crypto import encrypt_str

    by_key = {(t.get("host"), int(t.get("port") or 514)): t for t in existing}
    out: list[dict[str, Any]] = []
    for entry in incoming:
        prior = by_key.get((entry.host, entry.port), {})
        if entry.protocol == "tls":
            submitted = entry.ca_cert_pem
            prior_enc = prior.get("ca_cert_pem")
            if submitted is None:
                ca_enc = prior_enc if isinstance(prior_enc, str) and prior_enc else None
            elif submitted == "":
                ca_enc = None
            else:
                ca_enc = encrypt_str(submitted).decode("ascii")
        else:
            # Non-TLS targets carry no CA — drop any prior ciphertext.
            ca_enc = None
        out.append(
            {
                "host": entry.host,
                "port": entry.port,
                "protocol": entry.protocol,
                "format": entry.format,
                "ca_cert_pem": ca_enc,
            }
        )
    return out


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


class SyslogTargetUpdate(BaseModel):
    """One entry in ``syslog_targets`` on PUT (#156).

    ``host`` is the collector hostname / IP; ``port`` 1–65535;
    ``protocol`` one of ``udp`` / ``tcp`` / ``tls``; ``format`` one of
    ``rfc5424`` / ``rfc3164`` / ``json``. ``ca_cert_pem`` is REQUIRED
    when ``protocol == 'tls'`` and ignored otherwise; its merge
    semantics mirror the SNMP v3 pass / SMTP password shape:

    * ``None`` — leave the existing ciphertext alone (matched by
      ``(host, port)`` against the stored list).
    * ``""`` — clear any existing ciphertext.
    * non-empty string — encrypt and replace.
    """

    host: str
    port: int = 514
    protocol: str = "udp"
    format: str = "rfc5424"
    ca_cert_pem: str | None = None

    @field_validator("host")
    @classmethod
    def _syslog_host_nonempty(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("host may not be empty")
        if any(c.isspace() for c in s):
            raise ValueError("host may not contain whitespace")
        return s

    @field_validator("port")
    @classmethod
    def _syslog_port_range(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("port must be 1–65535")
        return v

    @field_validator("protocol")
    @classmethod
    def _syslog_protocol(cls, v: str) -> str:
        if v not in _SYSLOG_PROTOCOLS:
            raise ValueError(f"protocol must be one of {sorted(_SYSLOG_PROTOCOLS)}")
        return v

    @field_validator("format")
    @classmethod
    def _syslog_format(cls, v: str) -> str:
        if v not in _SYSLOG_FORMATS:
            raise ValueError(f"format must be one of {sorted(_SYSLOG_FORMATS)}")
        return v

    @model_validator(mode="after")
    def _tls_requires_ca(self) -> SyslogTargetUpdate:
        # A TLS target needs a CA to validate the collector's cert. The
        # PEM may be supplied now (non-empty string) OR already stored —
        # but ``""`` (explicit clear) on a TLS target leaves it with no
        # CA, which can't validate, so reject that here. ``None`` is OK:
        # it means "keep the existing CA", which the merge resolves.
        if self.protocol == "tls" and self.ca_cert_pem == "":
            raise ValueError("ca_cert_pem is required when protocol is 'tls'")
        return self


class SshAuthorizedKeyUpdate(BaseModel):
    """One entry in ``ssh_authorized_keys`` on PUT (#157).

    ``public_key`` is a single OpenSSH public-key line (``<type>
    <base64-blob> [comment]``); ``name`` is an operator label, ``comment``
    an optional note. Public keys are NOT secrets — they are stored
    verbatim (no Fernet). The key is validated strictly so a malformed /
    multi-line / control-char value can't slip into authorized_keys.
    """

    name: str = ""
    public_key: str
    comment: str = ""

    @field_validator("public_key")
    @classmethod
    def _valid_public_key(cls, v: str) -> str:
        s = v.strip()
        if not is_valid_public_key(s):
            raise ValueError(
                "public_key must be a valid OpenSSH public key "
                "(e.g. 'ssh-ed25519 AAAA… comment') — type + base64 blob, "
                "no embedded newlines / control characters"
            )
        return s

    @field_validator("name", "comment")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        # Names / comments land near the rendered authorized_keys line —
        # reject control chars (incl. newlines) so they can't break the
        # file format.
        if any(ord(c) < 0x20 for c in v):
            raise ValueError("must not contain control characters")
        return v.strip()


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
    reverse_dns_enabled: bool | None = None
    reverse_dns_interval_minutes: int | None = None
    reverse_dns_resolvers: list[str] | None = None
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
    integration_cloud_enabled: bool | None = None
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
    # ── Appliance timezone (issue #165) ───────────────────────────
    timezone: str | None = None
    # ── Appliance verbose boot console ────────────────────────────
    verbose_boot: bool | None = None
    # ── Maintenance mode (issue #57) ──────────────────────────────
    # ``maintenance_started_at`` is intentionally NOT settable here —
    # it's server-stamped in ``update_settings`` when the enable flag
    # flips, never operator-supplied.
    maintenance_mode_enabled: bool | None = None
    maintenance_message: str | None = None
    # ── Appliance LLDP (issue #343) ───────────────────────────────
    lldp_enabled: bool | None = None
    lldp_tx_interval: int | None = None
    lldp_tx_hold: int | None = None
    lldp_protocols: list[str] | None = None
    lldp_interface_pattern: str | None = None
    lldp_management_pattern: str | None = None
    lldp_sys_name: str | None = None
    lldp_sys_description: str | None = None
    lldp_med_location: dict[str, Any] | None = None
    lldp_snmp_agentx: bool | None = None
    # ── Appliance syslog forwarding (issue #156) ──────────────────
    syslog_enabled: bool | None = None
    # When provided, this list is the new full set — targets not in the
    # incoming list are removed. Per-target CA-PEM merge semantics see
    # ``SyslogTargetUpdate`` docstring (None = leave, "" = clear,
    # non-empty = encrypt + replace).
    syslog_targets: list[SyslogTargetUpdate] | None = None
    syslog_filter: str | None = None
    syslog_buffer_disk: bool | None = None
    # ── Appliance SSH (issue #157) ─────────────────────────────────
    ssh_authorized_keys: list[SshAuthorizedKeyUpdate] | None = None
    ssh_password_auth_enabled: bool | None = None
    ssh_allow_root_login: bool | None = None
    ssh_port: int | None = None
    ssh_allowed_source_networks: list[str] | None = None

    @field_validator("ssh_port")
    @classmethod
    def _valid_ssh_port(cls, v: int | None) -> int | None:
        if v is None:
            return None
        if not (1 <= v <= 65535):
            raise ValueError("ssh_port must be 1–65535")
        # Privileged-port floor — reject < 1024 except 22 so an operator
        # can't park sshd somewhere that needs root-only bind privileges
        # the runner can't reliably reach. 22 stays the un-removable
        # default; the host runner does the real bind / in-use check.
        if v < 1024 and v != 22:
            raise ValueError(
                "ssh_port below 1024 is not allowed (except 22) — pick a "
                "non-privileged port or keep 22"
            )
        return v

    @field_validator("ssh_allowed_source_networks")
    @classmethod
    def _valid_ssh_sources(cls, v: list[str] | None) -> list[str] | None:
        # Same CIDR canonicalisation as snmp_allowed_sources — the host
        # nftables drop-in source-scopes the ssh port to these.
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

    @field_validator("syslog_filter")
    @classmethod
    def _valid_syslog_filter(cls, v: str | None) -> str | None:
        # rsyslog selector — ``facility.severity`` tokens, comma-joined,
        # plus ``*`` wildcards and the ``!`` / ``=`` / ``;`` modifiers.
        # Rendered straight into the conf body, so reject control chars /
        # newlines / quotes to keep config injection out. Empty = the
        # renderer defaults to ``*.*``.
        if v is None:
            return None
        v = v.strip()
        if v and not re.fullmatch(r"[A-Za-z0-9_*.,;:=!\- ]+", v):
            raise ValueError(
                "syslog_filter may only contain letters, digits, and * . , ; : = ! - _ space"
            )
        return v

    @field_validator("lldp_tx_interval")
    @classmethod
    def _valid_lldp_tx_interval(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 3600):
            raise ValueError("lldp_tx_interval must be 1..3600 seconds")
        return v

    @field_validator("lldp_tx_hold")
    @classmethod
    def _valid_lldp_tx_hold(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 100):
            raise ValueError("lldp_tx_hold must be 1..100")
        return v

    @field_validator("lldp_protocols")
    @classmethod
    def _valid_lldp_protocols(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        allowed = {"cdp", "edp", "fdp", "sonmp"}
        out: list[str] = []
        for p in v:
            pl = str(p).strip().lower()
            if pl not in allowed:
                raise ValueError(f"lldp_protocols entries must be one of {sorted(allowed)}")
            if pl not in out:
                out.append(pl)
        return out

    @field_validator("lldp_interface_pattern", "lldp_management_pattern")
    @classmethod
    def _valid_lldp_pattern(cls, v: str | None) -> str | None:
        # Rendered straight into an lldpcli ``configure … pattern`` directive,
        # so reject anything outside the glob/CIDR charset to keep config
        # injection out (newlines, quotes, shell metachars).
        if v is None:
            return None
        v = v.strip()
        if v and not re.fullmatch(r"[A-Za-z0-9_*!,.\- /:]+", v):
            raise ValueError("pattern may only contain letters, digits, and * ! , . - _ / : space")
        return v

    @field_validator("lldp_sys_name", "lldp_sys_description")
    @classmethod
    def _valid_lldp_sys_text(cls, v: str | None) -> str | None:
        # Quoted by the renderer, but reject control chars / newlines outright
        # so a value can't break out of the lldpcli line.
        if v is None:
            return None
        if any(ord(c) < 32 for c in v):
            raise ValueError("must not contain control characters or newlines")
        return v

    @field_validator("lldp_med_location")
    @classmethod
    def _valid_lldp_med_location(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        # LLDP-MED location (issue #348). The only rendered form today is ELIN
        # (E911 number) — must be digits, since it's rendered bare into
        # ``configure med location elin <n>``. Other keys are accepted + stored
        # for future coordinate/civic support but not yet rendered.
        if v is None:
            return None
        elin = v.get("elin")
        if elin is not None and not str(elin).strip().isdigit():
            raise ValueError("lldp_med_location.elin must be a numeric E911 ELIN")
        return v

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str | None) -> str | None:
        """Validate IANA tz name on PUT. Empty string is allowed
        (clears the override). Non-empty must parse via
        ``zoneinfo.ZoneInfo`` — anything else 422s with a clean
        message instead of waiting for the host runner to bounce."""
        if v is None or v == "":
            return v
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001 — surface the parse failure
            raise ValueError(f"timezone {v!r} is not a valid IANA tz name: {exc}") from exc
        return v

    @field_validator("maintenance_message")
    @classmethod
    def _validate_maintenance_message(cls, v: str | None) -> str | None:
        # Mirrors the column width (VARCHAR(500)) — reject overlong banners
        # at the API boundary with a clean 422 instead of a DB truncation
        # error. ``None`` (field omitted) is left alone.
        if v is not None and len(v) > 500:
            raise ValueError("maintenance_message must be 500 characters or fewer")
        return v

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
        "reverse_dns_interval_minutes",
        "oui_update_interval_hours",
    )
    @classmethod
    def validate_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("Must be >= 1")
        return v

    @field_validator("reverse_dns_resolvers")
    @classmethod
    def validate_resolvers(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        import ipaddress

        cleaned: list[str] = []
        for entry in v:
            host = (entry or "").strip()
            if not host:
                continue
            try:
                ipaddress.ip_address(host)
            except ValueError as exc:
                raise ValueError(f"Invalid resolver IP: {entry!r}") from exc
            cleaned.append(host)
        return cleaned

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

    # Maintenance mode (issue #57). Capture the prior enabled state before
    # the setattr loop mutates it so we can detect an actual flip (and
    # server-stamp / clear ``maintenance_started_at`` accordingly + write
    # a dedicated audit row + invalidate the middleware cache). Computed
    # here so it sees the value as it was BEFORE this request.
    _maintenance_prev = bool(settings.maintenance_mode_enabled)
    _maintenance_requested = changes.get("maintenance_mode_enabled")

    # Whether this update touches any host-config field a HOSTCONFIG_ALL
    # subscriber acts on. The DHCP-agent /config long-poll folds SNMP /
    # NTP / LLDP into its ETag; the supervisor heartbeat (#358 Phase 1)
    # ALSO acts on timezone / verbose-boot / firewall-master / MetalLB /
    # VIP / web-UI-CIDR, so a change to any of those must wake a parked
    # supervisor too. (A DHCP agent woken by a field it doesn't fold just
    # re-checks its ETag and 304s — cheap, and these are rare cluster-
    # config edits.) The set below only matches keys actually present in
    # this request. Computed from ``changes`` before the snmp_* fields get
    # popped / rewritten so detection is order-independent.
    _supervisor_host_config_fields = {
        "timezone",
        "verbose_boot",
        "firewall_enabled",
        "metallb_enabled",
        "metallb_pool_addresses",
        "control_plane_vip",
        "web_ui_allowed_cidrs",
    }
    _host_config_touched = any(
        field.startswith(("snmp_", "ntp_", "lldp_", "syslog_", "ssh_"))
        or field in _supervisor_host_config_fields
        for field in changes
    )

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

    # syslog_targets: same atomic-replace + per-target CA-PEM merge as
    # SNMP v3 users (#156). Match by ``(host, port)`` so editing one
    # target's format doesn't drop another's CA PEM. A TLS target with
    # no CA (neither submitted now nor stored prior) is rejected so the
    # operator's PUT is the error site, not a downstream rsyslog reload.
    syslog_targets_audit: list[dict[str, Any]] | None = None
    if "syslog_targets" in changes:
        incoming_targets: list[SyslogTargetUpdate] = body.syslog_targets or []
        existing_targets = list(settings.syslog_targets or [])
        merged_targets = _merge_syslog_targets(existing_targets, incoming_targets)
        for tgt in merged_targets:
            if tgt["protocol"] == "tls" and not tgt.get("ca_cert_pem"):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"syslog target {tgt['host']}:{tgt['port']} uses TLS but has "
                        "no CA certificate configured — paste a CA PEM for it."
                    ),
                )
        # Drop the raw incoming list out of ``changes`` so the generic
        # setattr loop doesn't clobber the merged form; the model
        # attribute is set directly here.
        changes.pop("syslog_targets")
        settings.syslog_targets = merged_targets
        syslog_targets_audit = [_redact_syslog_target(t) for t in merged_targets]

    # ssh_authorized_keys: atomic replace (no per-entry secret merge — public
    # keys are not secrets). Normalise the incoming pydantic models into
    # plain ``{name, public_key, comment}`` dicts so the JSONB column stays
    # JSON-friendly. CROSS-FIELD lockout-safety guard (#157): compute the
    # MERGED post-update state (resulting password-auth flag + resulting key
    # list) and refuse to disable password auth when zero valid keys would
    # survive — otherwise the operator locks themselves out of every
    # appliance host. The host runner mirrors this guard defensively.
    ssh_keys_normalised: list[dict[str, Any]] | None = None
    if "ssh_authorized_keys" in changes:
        incoming_keys: list[SshAuthorizedKeyUpdate] = body.ssh_authorized_keys or []
        ssh_keys_normalised = [
            {"name": k.name, "public_key": k.public_key, "comment": k.comment}
            for k in incoming_keys
        ]
        changes.pop("ssh_authorized_keys")
        settings.ssh_authorized_keys = ssh_keys_normalised

    # Resulting (merged) state for the lockout check — read the change if
    # present, else the value already on the row. Compute regardless of
    # whether keys / the toggle were in this request, since either one
    # changing can produce an unsafe combination.
    _resulting_password_auth = bool(
        changes.get("ssh_password_auth_enabled", settings.ssh_password_auth_enabled)
    )
    _resulting_keys = (
        ssh_keys_normalised
        if ssh_keys_normalised is not None
        else list(settings.ssh_authorized_keys or [])
    )
    _ssh_field_in_request = any(f.startswith("ssh_") for f in changes) or (
        ssh_keys_normalised is not None
    )
    if _ssh_field_in_request and not validate_lockout_safe(
        _resulting_keys, _resulting_password_auth
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Refusing to disable SSH password authentication with no "
                "authorized keys configured — you would lock yourself out of "
                "every appliance host. Add at least one valid public key first, "
                "or keep password authentication enabled."
            ),
        )

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
    if syslog_targets_audit is not None:
        changes["syslog_targets"] = syslog_targets_audit
    if ssh_keys_normalised is not None:
        changes["ssh_authorized_keys"] = ssh_keys_normalised

    # Issue #157 — dedicated audit row for any SSH config change
    # (non-negotiable #4). SSH access is high-blast-radius (it gates who
    # can log into every appliance host), so a durable audit entry is
    # required. Public keys are NOT secrets, so the full key list is
    # recorded (no redaction). Fires whenever this request touched any
    # ``ssh_*`` field.
    if _ssh_field_in_request:
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="update",
                resource_type="platform_settings",
                resource_id="ssh",
                resource_display="SSH access",
                result="success",
                new_value={
                    "password_auth_enabled": bool(settings.ssh_password_auth_enabled),
                    "allow_root_login": bool(settings.ssh_allow_root_login),
                    "port": int(settings.ssh_port or 22),
                    "allowed_source_networks": list(settings.ssh_allowed_source_networks or []),
                    "authorized_keys": [
                        {
                            "name": (k.get("name") or "") if isinstance(k, dict) else "",
                            "comment": (k.get("comment") or "") if isinstance(k, dict) else "",
                            "public_key": (
                                (k.get("public_key") or "") if isinstance(k, dict) else ""
                            ),
                        }
                        for k in (settings.ssh_authorized_keys or [])
                    ],
                },
            )
        )

    # Issue #156 — dedicated audit row for any syslog-forwarding change
    # (non-negotiable #4). The generic ``logger.info`` below is not an
    # ``audit_log`` row; syslog forwarding ships logs off-box + carries a
    # secret CA PEM, so a durable, redacted audit entry is required. Fires
    # whenever this request touched any ``syslog_*`` field (the targets
    # list shows only the redacted shape — no CA PEM bytes).
    _syslog_touched = any(f.startswith("syslog_") for f in changes)
    if _syslog_touched:
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action="update",
                resource_type="platform_settings",
                resource_id="syslog",
                resource_display="Syslog forwarding",
                result="success",
                new_value={
                    "enabled": bool(settings.syslog_enabled),
                    "targets": [_redact_syslog_target(t) for t in (settings.syslog_targets or [])],
                    "filter": settings.syslog_filter or "",
                    "buffer_disk": bool(settings.syslog_buffer_disk),
                },
            )
        )

    # Maintenance mode flip handling (issue #57). When the enable flag
    # actually changes state, server-stamp / clear ``maintenance_started_at``
    # (never operator-supplied) and write a dedicated audit row so the
    # window is unambiguously bracketed in the audit log.
    _maintenance_flipped = (
        _maintenance_requested is not None and bool(_maintenance_requested) != _maintenance_prev
    )
    if _maintenance_flipped:
        now_enabled = bool(_maintenance_requested)
        settings.maintenance_started_at = datetime.now(UTC) if now_enabled else None
        db.add(
            AuditLog(
                user_id=current_user.id,
                user_display_name=current_user.display_name,
                auth_source=current_user.auth_source,
                action=("maintenance_mode.enabled" if now_enabled else "maintenance_mode.disabled"),
                resource_type="platform_settings",
                resource_id="maintenance",
                resource_display="Maintenance mode",
                result="success",
                new_value={
                    "enabled": now_enabled,
                    "message": settings.maintenance_message or "",
                },
            )
        )

    await db.commit()
    await db.refresh(settings)

    # Drop the middleware's process-local cache so the flipping worker
    # enforces (or lifts) the read-only block on its very next request.
    if _maintenance_flipped:
        from app.core import maintenance_mode as _maintenance_mode

        _maintenance_mode.invalidate_cache()

    # The settings router has no request-scoped wake collector, so
    # publish directly after the commit. Only fire when a host-config
    # field actually changed so unrelated settings writes don't wake
    # every DHCP-agent long-poll (which subscribes to HOSTCONFIG_ALL).
    if _host_config_touched:
        await publish_wake(HOSTCONFIG_ALL)

    logger.info("platform_settings_updated", user=current_user.username, changes=changes)
    return settings


class ReverseDnsRunResponse(BaseModel):
    status: str  # "queued" | "enqueue_failed"
    task_id: str | None = None


@router.post("/reverse-dns/run", response_model=ReverseDnsRunResponse)
async def trigger_reverse_dns_run(current_user: CurrentUser, db: DB) -> ReverseDnsRunResponse:
    """Queue an on-demand reverse-DNS sweep (issue #41).

    Runs the same ``sweep_reverse_dns`` task the beat dispatcher uses, with
    ``force=True`` so it bypasses the enabled-gate + per-run interval — an
    operator can sweep on demand even with the scheduled sweep off.
    """
    forbid_in_demo_mode("Reverse-DNS sweep is disabled")
    if not user_has_permission(current_user, "write", "settings"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: need 'write' on 'settings'",
        )

    from app.models.audit import AuditLog

    task_id: str | None = None
    try:
        from app.tasks.reverse_dns import sweep_reverse_dns

        result = sweep_reverse_dns.delay(force=True)
        task_id = result.id
    except Exception as exc:  # noqa: BLE001 — broker unreachable; report, don't 500
        logger.warning("reverse_dns_trigger_enqueue_failed", error=str(exc))

    db.add(
        AuditLog(
            user_id=current_user.id,
            user_display_name=current_user.display_name,
            auth_source=current_user.auth_source,
            action="reverse-dns",
            resource_type="platform",
            resource_id="1",
            resource_display="reverse-dns-run",
            result="success" if task_id else "error",
            error_detail=None if task_id else "task broker unreachable",
        )
    )
    await db.commit()
    return ReverseDnsRunResponse(status="queued" if task_id else "enqueue_failed", task_id=task_id)


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

    if not is_effective_superadmin(current_user):
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
    if not is_effective_superadmin(current_user):
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
    if not is_effective_superadmin(current_user):
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
    if not is_effective_superadmin(current_user):
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
    if not is_effective_superadmin(current_user):
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
    if not is_effective_superadmin(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    row = await db.get(AuditForwardTarget, target_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Target not found")

    now = datetime.now(UTC)
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
