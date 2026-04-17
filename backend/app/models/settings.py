from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
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

    # Utilization alert thresholds
    utilization_warn_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=80)
    utilization_critical_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=95)

    # Subnet tree UI preference
    subnet_tree_default_expanded_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2
    )

    # Discovery
    discovery_scan_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discovery_scan_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )

    # Release checking
    github_release_check_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

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

    # DHCP defaults — applied as the initial values when creating a new DHCP scope
    dhcp_default_dns_servers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    dhcp_default_domain_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    dhcp_default_domain_search: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    dhcp_default_ntp_servers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    dhcp_default_lease_time: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
