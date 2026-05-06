from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PlatformSettings(Base):
    """Singleton settings table — always exactly one row with id=1."""

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Branding
    app_title: Mapped[str] = mapped_column(String(255), nullable=False, default="SpatiumDDI")

    # External-facing URL (used for OIDC / SAML redirect + callback URLs). Empty
    # means "derive from the incoming request" at runtime.
    app_base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    # IP allocation
    ip_allocation_strategy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="sequential"
    )

    # Session / security
    session_timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    auto_logout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Password policy (issue #70). Defaults are deliberately permissive so
    # an upgrade doesn't suddenly invalidate working passwords; operators
    # tighten in Settings → Security. ``password_history_count = 0``
    # disables history checking; ``password_max_age_days = 0`` disables
    # forced rotation. Validator + history live in
    # ``app.services.password_policy``.
    password_min_length: Mapped[int] = mapped_column(
        Integer, nullable=False, default=12, server_default=sa_text("12")
    )
    password_require_uppercase: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    password_require_lowercase: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    password_require_digit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    password_require_symbol: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    password_history_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default=sa_text("5")
    )
    password_max_age_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )

    # Account lockout (issue #71). ``lockout_threshold = 0`` disables
    # the feature; that's the default so an upgrade never locks an
    # admin out. Threshold counts failed logins inside a rolling
    # ``lockout_reset_minutes`` window — anything older falls out.
    # When the threshold is hit, the account is locked for
    # ``lockout_duration_minutes``; superadmin unlock via /users/<id>/
    # unlock clears both columns.
    lockout_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    lockout_duration_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default=sa_text("15")
    )
    lockout_reset_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default=sa_text("15")
    )

    # Utilization alert thresholds
    utilization_warn_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=80)
    utilization_critical_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=95)

    # Exclude small PTP / loopback-style subnets from utilization reporting
    # (dashboard + alerts). A subnet is excluded when its prefix length is
    # strictly larger than this value — i.e. at the default 29 for v4 we
    # exclude /30, /31, /32 (PTP links, single-host); at 126 for v6 we
    # exclude /127 (RFC 6164 PTP) and /128. Set to 32 / 128 to disable.
    utilization_max_prefix_ipv4: Mapped[int] = mapped_column(Integer, nullable=False, default=29)
    utilization_max_prefix_ipv6: Mapped[int] = mapped_column(Integer, nullable=False, default=126)

    # Subnet tree UI preference
    subnet_tree_default_expanded_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2
    )

    # Release checking. When enabled, a daily Celery beat task queries
    # ``api.github.com/repos/{github_repo}/releases/latest`` and stores
    # the result on the columns below. Operators can turn this off in
    # air-gapped deployments or for forks that don't want the check.
    github_release_check_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # Result columns written by the task. Null until the first successful
    # check; retained through disabled periods so the UI can still show
    # "last seen X" when the toggle is off.
    latest_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    update_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latest_release_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latest_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Populated when the most recent check hit an error (rate limit, DNS,
    # parse issue). Cleared on a successful check. Surfaced in the admin
    # release-check panel so operators don't chase a stale banner.
    latest_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # DNS defaults
    dns_default_ttl: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    dns_default_zone_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="primary"
    )
    dns_default_dnssec_validation: Mapped[str] = mapped_column(
        String(20), nullable=False, default="auto"
    )
    dns_recursive_by_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # IPAM ↔ DNS auto-sync (Celery beat fires every 60s, task gates on these).
    dns_auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dns_auto_sync_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    # When False (default), auto-sync only creates/updates records; stale records
    # (auto-generated rows pointing at deleted IPs) are left for manual cleanup.
    dns_auto_sync_delete_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dns_auto_sync_last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Scheduled "pull from authoritative server" (AXFR → additive import).
    # Complements the IPAM→DNS push direction above; together they keep
    # SpatiumDDI's DB in sync with both its own intent (IPAM) and the live
    # state on the authoritative DNS server (e.g. a Windows DC).
    # Beat fires every 60s; task gates on these so the UI can change cadence
    # without restarting celery-beat.
    dns_pull_from_server_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    dns_pull_from_server_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    dns_pull_from_server_last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Scheduled "pull leases from DHCP server" — counterpart to the DNS
    # pull-from-server setting above, but for lease reads (Windows DHCP
    # WinRM today). Beat fires every 60s; task gates on these so the UI can
    # change cadence without restarting celery-beat. Additive-only: lease
    # rows are upserted by (server_id, ip_address); mirrored IPAM rows are
    # removed by the existing lease-cleanup sweep when expires_at passes.
    dhcp_pull_leases_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Interval in *seconds* (was minutes pre-b2f7e91d3c48). Beat ticks every
    # 10 s now, so values down to ~10 s take effect as configured; anything
    # smaller is floored. Operators who want true sub-minute cadence should
    # be mindful of the load it puts on the Windows DC — a 15 s poll is one
    # WinRM round trip every 15 s per server.
    dhcp_pull_leases_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15
    )
    dhcp_pull_leases_last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # DHCP defaults — applied as the initial values when creating a new DHCP scope
    dhcp_default_dns_servers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    dhcp_default_domain_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    dhcp_default_domain_search: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    dhcp_default_ntp_servers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    dhcp_default_lease_time: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)

    # Audit event forwarding — ship each committed AuditLog row out to an
    # external syslog collector and/or a generic HTTP webhook. Both targets
    # are independent: enable either, both, or neither. Failure to deliver
    # never blocks the audit commit; errors are logged via structlog and
    # eventually surface in the scheduled-task / system logs view.
    audit_forward_syslog_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    audit_forward_syslog_host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    audit_forward_syslog_port: Mapped[int] = mapped_column(Integer, nullable=False, default=514)
    # protocol: udp | tcp (TLS deferred — needs cert management)
    audit_forward_syslog_protocol: Mapped[str] = mapped_column(
        String(10), nullable=False, default="udp"
    )
    # facility: 0–23, mapped to LOG_* per RFC 5424 §6.2.1. Default is
    # "local0" (16) since audit events are clearly app-level, not kernel
    # or auth-daemon scope.
    audit_forward_syslog_facility: Mapped[int] = mapped_column(Integer, nullable=False, default=16)

    audit_forward_webhook_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    audit_forward_webhook_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    # Optional Authorization header (e.g. "Bearer …" or "Basic …"); stored
    # in plaintext today because the rest of this row is plaintext too —
    # move to Fernet alongside the provider creds when we tighten secrets
    # at rest across the board.
    audit_forward_webhook_auth_header: Mapped[str] = mapped_column(
        String(1024), nullable=False, default=""
    )

    # IEEE OUI vendor lookup. Opt-in because the daily fetch pulls a ~5 MB
    # CSV from standards-oui.ieee.org and a lot of deployments genuinely
    # don't care about vendor names. When disabled the Celery task is a
    # no-op, list endpoints skip the join, and the UI hides the vendor
    # suffix. Interval is stored in hours — the IEEE CSV changes slowly,
    # once a day is the right default; lab installs can crank it down if
    # they're debugging OUI loader problems.
    oui_lookup_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    oui_update_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    oui_last_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Integrations — one toggle per integration type. Granular by design:
    # enabling Kubernetes should not implicitly enable a future Terraform
    # Cloud / ServiceNow integration. When a toggle is on, the
    # corresponding top-level sidebar nav item appears.
    integration_kubernetes_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    integration_docker_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    integration_proxmox_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    integration_tailscale_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Trash retention — high-blast-radius IPAM/DNS/DHCP rows are soft-
    # deleted (``deleted_at`` set) and the nightly purge sweep hard-
    # deletes anything older than this many days. Set to 0 to disable
    # purge entirely (rows accumulate forever; manual permanent-delete
    # is still available). Default 30 d matches the user-facing spec.
    soft_delete_purge_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # Gate for the reservation expiry sweep task. When False, manually-
    # reserved IPs with a reserved_until timestamp are never auto-released.
    reservation_sweep_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    # How many days to retain DHCPLeaseHistory rows. Set to 0 to keep forever.
    dhcp_lease_history_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default=sa_text("90")
    )

    # Fingerbank API key for passive DHCP device fingerprinting (Phase 2 of
    # the device profiling feature). Fernet-encrypted at rest; null when the
    # operator hasn't configured a key. When unset, the agent still ships
    # raw option-55 / option-60 fingerprints into ``dhcp_fingerprint`` —
    # operators just don't get the enriched device-type / class /
    # manufacturer triple. Read via ``services.profiling.fingerbank``
    # which decrypts on demand.
    fingerbank_api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # ASN RDAP refresh cadence (Phase 2 of issue #85). Beat ticks
    # hourly; per-row ``asn.next_check_at`` gates against this knob.
    asn_whois_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24, server_default=sa_text("24")
    )

    # RPKI ROA pull source — ``cloudflare`` (default) or ``ripe``.
    rpki_roa_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cloudflare", server_default=sa_text("'cloudflare'")
    )

    # Cadence for the RPKI ROA refresh task; ROA dump cached in-memory
    # for 5 min inside the source service.
    rpki_roa_refresh_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default=sa_text("4")
    )

    # Domain WHOIS refresh cadence (Phase 2 of issue #87). Same per-row
    # gating shape as the ASN cadence above.
    domain_whois_interval_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24, server_default=sa_text("24")
    )

    # VRF cross-cutting validation gate (Phase 2 of issue #86). When
    # False (default), an ``ASN:N`` route-distinguisher whose ASN
    # portion does not match the VRF's linked ``asn.number`` produces
    # a non-blocking warning in the create / update response. When
    # True, the same mismatch is a 422. Same logic applies to each
    # ``ASN:N`` entry in the VRF's import / export route-target lists.
    vrf_strict_rd_validation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    # ── Operator Copilot caps + pricing (issue #90 Wave 4) ────────────
    # Per-user daily cap on token consumption against the chat
    # endpoint. None = unlimited. Default unlimited because most
    # operators self-host with local LLMs where this is moot; cloud-
    # API-using deployments will set it.
    ai_per_user_daily_token_cap: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Per-user daily cost cap in USD. None = unlimited. Same default
    # rationale as the token cap.
    ai_per_user_daily_cost_cap_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    # Operator-supplied per-model rate overrides. Shape:
    #   {"<model_id>": {"input": <usd_per_million>, "output": <...>}}
    # The pricing service consults this *first* — operators can pin
    # rates for their custom-hosted or non-canonical model names that
    # the in-code rate sheet wouldn't recognise (e.g. "qwen3:8b" → $0).
    ai_pricing_overrides: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )

    # ── Daily digest (issue #90 Phase 2) ────────────────────────────
    # When True, a Celery beat job once per day rolls up the previous
    # 24 h of audit / alert / lease activity, sends it to the highest-
    # priority enabled AIProvider for an executive summary, and pushes
    # the result through the existing audit-forward targets (filter on
    # ``resource_types: ["ai.digest"]`` to route it separately from
    # alerts). Default off — operators turn it on once they've put an
    # SMTP / webhook target in place.
    ai_daily_digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
