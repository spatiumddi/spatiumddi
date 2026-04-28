"""DNS data models: server groups, servers, views, zones, records, ACLs, blocking lists."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class DNSServerZoneState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-server zone-loaded-serial snapshot.

    Each agent posts back the serial it *actually rendered* after a
    successful config apply — the "ground truth" of what's live on
    that particular server, as distinct from ``DNSZone.last_serial`` which
    is the value the control plane most-recently pushed.

    Unique on ``(server_id, zone_id)`` so the evaluator can drive a
    single row per pair — upserts replace the previous snapshot
    rather than accumulating history.

    Drift detection: for every zone in a group, compare each server's
    ``current_serial`` to the others. Equal → in sync. Different →
    surface "N of M on serial X, rest on Y" on the zone detail page
    and (optionally, via the alerts framework) as a ``zone_serial_drift``
    alert rule.
    """

    __tablename__ = "dns_server_zone_state"
    __table_args__ = (
        UniqueConstraint("server_id", "zone_id", name="uq_dns_server_zone_state"),
        Index("ix_dns_server_zone_state_zone", "zone_id"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    zone_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_zone.id", ondelete="CASCADE"),
        nullable=False,
    )
    current_serial: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DNSServerGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Logical cluster of DNS servers sharing configuration (e.g. internal-resolvers, external-auth)."""

    __tablename__ = "dns_server_group"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # group_type values: internal | external | dmz | custom
    group_type: Mapped[str] = mapped_column(String(50), nullable=False, default="internal")
    default_view: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # TSIG key shared by all servers in this group, used to authenticate
    # RFC 2136 dynamic updates from the agent over loopback. Auto-generated
    # on first server registration.
    tsig_key_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tsig_key_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tsig_key_algorithm: Mapped[str] = mapped_column(
        String(50), nullable=False, default="hmac-sha256"
    )

    servers: Mapped[list["DNSServer"]] = relationship(
        "DNSServer", back_populates="group", cascade="all, delete-orphan"
    )
    views: Mapped[list["DNSView"]] = relationship(
        "DNSView", back_populates="group", cascade="all, delete-orphan"
    )
    zones: Mapped[list["DNSZone"]] = relationship(
        "DNSZone", back_populates="group", cascade="all, delete-orphan"
    )
    acls: Mapped[list["DNSAcl"]] = relationship(
        "DNSAcl", back_populates="group", cascade="all, delete-orphan"
    )
    options: Mapped["DNSServerOptions | None"] = relationship(
        "DNSServerOptions", back_populates="group", uselist=False, cascade="all, delete-orphan"
    )
    blocklists: Mapped[list["DNSBlockList"]] = relationship(
        "DNSBlockList",
        secondary="dns_blocklist_group_assoc",
        back_populates="server_groups",
    )


class DNSServer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Individual physical/virtual DNS server managed by SpatiumDDI."""

    __tablename__ = "dns_server"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # driver: bind9 (only supported backend)
    driver: Mapped[str] = mapped_column(String(50), nullable=False, default="bind9")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=53)
    # api_port: used for rndc (BIND9)
    api_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # roles: authoritative | recursive | forwarder (JSON array of strings)
    roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # User-controlled "pause" — when False, this server is skipped by the
    # health-check sweep, the bi-directional sync task, and the record-op
    # dispatcher. Separate from ``status`` (which tracks reachability —
    # derived, not user-editable). Default True so existing rows keep
    # their current behaviour post-migration.
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # status: active | unreachable | syncing | error | disabled
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Agent bookkeeping (see docs/deployment/DNS_AGENT.md §2, §6)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, unique=True
    )
    agent_jwt_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_config_etag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Fernet-encrypted JSON blob for driver-specific admin credentials.
    # windows_dns Path B stores a dict:
    #   {"username", "password", "winrm_port", "transport", "use_tls",
    #    "verify_tls"}
    # Agent-based drivers (bind9) leave this NULL — they authenticate via
    # the agent JWT. Path A (RFC 2136 record CRUD) also leaves this NULL
    # and signs updates with the group-level TSIG key instead.
    credentials_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    group: Mapped["DNSServerGroup"] = relationship("DNSServerGroup", back_populates="servers")

    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_dns_server_group_name"),)


class DNSRecordOp(UUIDPrimaryKeyMixin, Base):
    """Per-record mutation queued for an agent to apply via RFC 2136."""

    __tablename__ = "dns_record_op"

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    zone_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    op: Mapped[str] = mapped_column(String(20), nullable=False)  # create | update | delete
    record: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target_serial: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DNSServerOptions(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Server-level options applied globally to all views/zones on the server group."""

    __tablename__ = "dns_server_options"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Forwarders
    forwarders: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # forward_policy: first | only
    forward_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="first")

    # Recursion
    recursion_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_recursion: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["any"])

    # DNSSEC — auto | yes | no
    dnssec_validation: Mapped[str] = mapped_column(String(10), nullable=False, default="auto")

    # GSS-TSIG (Kerberos)
    gss_tsig_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gss_tsig_keytab_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    gss_tsig_realm: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gss_tsig_principal: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Notify — yes | no | explicit | master-only
    notify_enabled: Mapped[str] = mapped_column(String(20), nullable=False, default="yes")
    also_notify: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    allow_notify: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Query / Transfer ACLs
    allow_query: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["any"])
    allow_query_cache: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=lambda: ["localhost", "localnets"]
    )
    allow_transfer: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["none"])
    blackhole: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Query logging
    query_log_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # channel: file | syslog | stderr
    query_log_channel: Mapped[str] = mapped_column(String(20), nullable=False, default="file")
    query_log_file: Mapped[str] = mapped_column(
        String(500), nullable=False, default="/var/log/named/queries.log"
    )
    # severity: info | debug | notice | warning | error
    query_log_severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    # print-category / print-severity / print-time in `channel` block
    query_log_print_category: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    query_log_print_severity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    query_log_print_time: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    group: Mapped["DNSServerGroup"] = relationship("DNSServerGroup", back_populates="options")
    trust_anchors: Mapped[list["DNSTrustAnchor"]] = relationship(
        "DNSTrustAnchor", back_populates="server_options", cascade="all, delete-orphan"
    )


class DNSTrustAnchor(UUIDPrimaryKeyMixin, Base):
    """DNSSEC trust anchors (managed-keys / trust-anchors in BIND9)."""

    __tablename__ = "dns_trust_anchor"

    server_options_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_options.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    zone_name: Mapped[str] = mapped_column(String(255), nullable=False)
    algorithm: Mapped[int] = mapped_column(Integer, nullable=False)
    key_tag: Mapped[int] = mapped_column(Integer, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    is_initial_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )

    server_options: Mapped["DNSServerOptions"] = relationship(
        "DNSServerOptions", back_populates="trust_anchors"
    )


class DNSAcl(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Named address match list reusable across options, views, and zones."""

    __tablename__ = "dns_acl"
    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_dns_acl_group_name"),)

    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    group: Mapped["DNSServerGroup | None"] = relationship("DNSServerGroup", back_populates="acls")
    entries: Mapped[list["DNSAclEntry"]] = relationship(
        "DNSAclEntry",
        back_populates="acl",
        cascade="all, delete-orphan",
        order_by="DNSAclEntry.order",
    )


class DNSAclEntry(UUIDPrimaryKeyMixin, Base):
    """Single entry in a named ACL (CIDR, IP, key reference, or ACL reference)."""

    __tablename__ = "dns_acl_entry"

    acl_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_acl.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # value: CIDR, IP, literal (any/none/localhost/localnets), key name, or ACL reference
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    negate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    acl: Mapped["DNSAcl"] = relationship("DNSAcl", back_populates="entries")


class DNSView(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Split-horizon DNS view — different clients see different zone data."""

    __tablename__ = "dns_view"
    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_dns_view_group_name"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # match_clients / match_destinations: JSON arrays of CIDRs / ACL names
    match_clients: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["any"])
    match_destinations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    recursion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # evaluation order (lower = first match)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # View-level query control overrides — fall back to server options when null
    allow_query: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    allow_query_cache: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    group: Mapped["DNSServerGroup"] = relationship("DNSServerGroup", back_populates="views")
    zones: Mapped[list["DNSZone"]] = relationship("DNSZone", back_populates="view")
    blocklists: Mapped[list["DNSBlockList"]] = relationship(
        "DNSBlockList",
        secondary="dns_blocklist_view_assoc",
        back_populates="views",
    )


class DNSZone(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """DNS zone — authoritative, secondary, stub, or forward."""

    __tablename__ = "dns_zone"
    __table_args__ = (
        UniqueConstraint("group_id", "view_id", "name", name="uq_dns_zone_group_view_name"),
        Index("ix_dns_zone_name", "name"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    view_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_view.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # name: FQDN with trailing dot, e.g. "example.com."
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # zone_type values: primary | secondary | stub | forward
    zone_type: Mapped[str] = mapped_column(String(20), nullable=False, default="primary")
    # kind: forward | reverse
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="forward")

    # SOA fields
    ttl: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    refresh: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    retry: Mapped[int] = mapped_column(Integer, nullable=False, default=7200)
    expire: Mapped[int] = mapped_column(Integer, nullable=False, default=3600000)
    minimum: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    primary_ns: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    admin_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    is_auto_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    linked_subnet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subnet.id", ondelete="SET NULL"), nullable=True
    )
    # Set by the Tailscale reconciler when this zone synthesises
    # ``<tailnet>.ts.net`` from the device list (Phase 2). The FK
    # cascades on tenant delete so the synthetic zone + its records
    # disappear cleanly when the tenant row is removed. While
    # non-null the API blocks edits / deletes — the reconciler is
    # the only writer.
    tailscale_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tailscale_tenant.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Optional per-zone color key (from a curated swatch set) shown as a
    # dot/stripe in zone lists + tree nodes. Free-form hex is not accepted
    # so both light and dark themes remain legible. See API validator for
    # the allowed keys.
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dnssec_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_serial: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Zone-level ACL overrides (inherit from server options if null)
    allow_query: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    allow_transfer: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    also_notify: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    notify_enabled: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Conditional-forwarder config. Only meaningful when ``zone_type == "forward"``.
    # ``forwarders`` is the upstream resolver list (IP or IP@port strings).
    # ``forward_only`` true → ``forward only;`` (don't fall through to recursion);
    # false → ``forward first;`` (fall through if all forwarders fail).
    forwarders: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa_text("'[]'::jsonb")
    )
    forward_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )

    group: Mapped["DNSServerGroup"] = relationship("DNSServerGroup", back_populates="zones")
    view: Mapped["DNSView | None"] = relationship("DNSView", back_populates="zones")
    records: Mapped[list["DNSRecord"]] = relationship(
        "DNSRecord", back_populates="zone", cascade="all, delete-orphan"
    )


class DNSRecord(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Individual DNS resource record within a zone."""

    __tablename__ = "dns_record"
    __table_args__ = (
        Index("ix_dns_record_zone_name", "zone_id", "name"),
        Index("ix_dns_record_fqdn", "fqdn"),
    )

    zone_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_zone.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    view_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dns_view.id", ondelete="SET NULL"), nullable=True
    )
    # name: relative label, e.g. "host1" (not "host1.example.com.")
    # "@" means zone apex
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # fqdn: computed + stored for search
    fqdn: Mapped[str] = mapped_column(String(511), nullable=False, default="")
    # record_type values: A | AAAA | CNAME | MX | TXT | NS | PTR | SRV | CAA | TLSA | SSHFP | NAPTR | LOC
    record_type: Mapped[str] = mapped_column(String(10), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    ttl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight: Mapped[int | None] = mapped_column(Integer, nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ip_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_address.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    # Set by the Kubernetes reconciler when this record mirrors an
    # Ingress (or annotated Service) hostname from a cluster. FK
    # cascades on cluster delete.
    kubernetes_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kubernetes_cluster.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Set by the Tailscale reconciler when the row mirrors a
    # tailnet device (Phase 2). The FK cascades on tenant delete.
    # API blocks edits / deletes while non-null.
    tailscale_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tailscale_tenant.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    zone: Mapped["DNSZone"] = relationship("DNSZone", back_populates="records")


# ── Blocking Lists / RPZ ────────────────────────────────────────────────────

# Association tables: a blocklist can be applied to many server groups and/or
# many views. A view or group can reference many blocklists.
dns_blocklist_group_assoc = Table(
    "dns_blocklist_group_assoc",
    Base.metadata,
    Column(
        "blocklist_id",
        UUID(as_uuid=True),
        ForeignKey("dns_blocklist.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "group_id",
        UUID(as_uuid=True),
        ForeignKey("dns_server_group.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


dns_blocklist_view_assoc = Table(
    "dns_blocklist_view_assoc",
    Base.metadata,
    Column(
        "blocklist_id",
        UUID(as_uuid=True),
        ForeignKey("dns_blocklist.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "view_id",
        UUID(as_uuid=True),
        ForeignKey("dns_view.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class DNSBlockList(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named collection of domains to be blocked via RPZ or equivalent backend mechanism.

    A blocklist is backend-neutral: the DNS driver consumes an effective list
    of entries + exceptions via the service layer and emits the appropriate
    BIND9 RPZ zone or BIND9 RPZ config. No driver specifics live on
    this model.
    """

    __tablename__ = "dns_blocklist"

    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # category: ads | malware | tracking | adult | custom | ...
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="custom")
    # source_type: manual | url | file_upload
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    feed_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # format: hosts | domains | adblock
    feed_format: Mapped[str] = mapped_column(String(20), nullable=False, default="hosts")
    # 0 = manual refresh only
    update_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    # block_mode: nxdomain | sinkhole | refused
    block_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="nxdomain")
    sinkhole_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    entries: Mapped[list["DNSBlockListEntry"]] = relationship(
        "DNSBlockListEntry",
        back_populates="blocklist",
        cascade="all, delete-orphan",
    )
    exceptions: Mapped[list["DNSBlockListException"]] = relationship(
        "DNSBlockListException",
        back_populates="blocklist",
        cascade="all, delete-orphan",
    )

    server_groups: Mapped[list["DNSServerGroup"]] = relationship(
        "DNSServerGroup",
        secondary=dns_blocklist_group_assoc,
        back_populates="blocklists",
    )
    views: Mapped[list["DNSView"]] = relationship(
        "DNSView",
        secondary=dns_blocklist_view_assoc,
        back_populates="blocklists",
    )


class DNSBlockListEntry(UUIDPrimaryKeyMixin, Base):
    """A single domain entry within a blocklist."""

    __tablename__ = "dns_blocklist_entry"
    __table_args__ = (
        UniqueConstraint("list_id", "domain", name="uq_dns_blocklist_entry_list_domain"),
        Index("ix_dns_blocklist_entry_list_domain", "list_id", "domain"),
        Index("ix_dns_blocklist_entry_domain", "domain"),
    )

    list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_blocklist.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(512), nullable=False)
    # block_mode values: block | redirect | nxdomain
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False, default="block")
    # target: for redirect entries, the IP/hostname to return instead
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # source: manual | feed
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    is_wildcard: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Operator note — parallels DNSBlockListException.reason. Only meaningful
    # for manual entries; feed-sourced entries would overwrite it on refresh.
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    blocklist: Mapped["DNSBlockList"] = relationship("DNSBlockList", back_populates="entries")


class DNSBlockListException(UUIDPrimaryKeyMixin, Base):
    """Allow-list exception — domain is never blocked by the parent list."""

    __tablename__ = "dns_blocklist_exception"
    __table_args__ = (
        UniqueConstraint("list_id", "domain", name="uq_dns_blocklist_exception_list_domain"),
        Index("ix_dns_blocklist_exception_list_domain", "list_id", "domain"),
    )

    list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dns_blocklist.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    blocklist: Mapped["DNSBlockList"] = relationship("DNSBlockList", back_populates="exceptions")
