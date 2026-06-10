"""Tests for the MCP catch-up write operations (issues #280 / #304).

Exercises the Operation ``preview`` + ``apply`` contract for the
conformity / webhook / multicast-domain / SNMP / NTP / DNSSEC /
import operations registered in
:mod:`app.services.ai.operations_writes`. External-pull operations
(DNS/DHCP import, webhook test) are covered via their hermetic
validation/rejection paths; the happy paths that need a live WinRM /
REST / HTTP endpoint are left to the importer/webhook integration
tests.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import User
from app.services.ai.operations import get_operation


async def _user(db: AsyncSession, *, superadmin: bool = True, name: str = "ai-writes") -> User:
    u = User(
        username=f"{name}-{uuid.uuid4().hex[:8]}",
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.test",
        display_name="AI Writer",
        hashed_password=hash_password("x"),
        is_superadmin=superadmin,
    )
    u.groups = []  # mark loaded — is_effective_superadmin walks .groups (#351)
    db.add(u)
    await db.commit()
    return u


# ── Conformity ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_conformity_policy_preview_apply(db_session: AsyncSession) -> None:
    from app.models.conformity import ConformityPolicy
    from app.services.ai.operations_writes import CreateConformityPolicyArgs

    user = await _user(db_session)
    op = get_operation("create_conformity_policy")
    assert op is not None
    args = CreateConformityPolicyArgs(
        name="PCI-test",
        target_kind="platform",
        check_kind="audit_log_immutable",
        framework="PCI-DSS",
        severity="critical",
    )
    preview = await op.preview(db_session, user, args)
    assert preview.ok, preview.detail
    assert "PCI-test" in preview.preview_text

    result = await op.apply(db_session, user, args)
    row = await db_session.get(ConformityPolicy, uuid.UUID(result["id"]))
    assert row is not None
    assert row.is_builtin is False
    assert row.framework == "PCI-DSS"
    # audit row landed
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.resource_type == "conformity_policy",
                AuditLog.resource_id == result["id"],
            )
        )
    ).scalar_one()
    assert audit.action == "create"


@pytest.mark.asyncio
async def test_create_conformity_policy_rejects_bad_check_kind(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import CreateConformityPolicyArgs

    user = await _user(db_session)
    op = get_operation("create_conformity_policy")
    assert op is not None
    args = CreateConformityPolicyArgs(
        name="bad", target_kind="platform", check_kind="does_not_exist"
    )
    preview = await op.preview(db_session, user, args)
    assert preview.ok is False
    assert "check_kind" in preview.detail


@pytest.mark.asyncio
async def test_update_and_evaluate_conformity_policy(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import (
        CreateConformityPolicyArgs,
        EvaluateConformityPolicyArgs,
        UpdateConformityPolicyArgs,
    )

    user = await _user(db_session)
    created = await get_operation("create_conformity_policy").apply(  # type: ignore[union-attr]
        db_session,
        user,
        CreateConformityPolicyArgs(
            name="immutability", target_kind="platform", check_kind="audit_log_immutable"
        ),
    )
    pid = uuid.UUID(created["id"])

    upd = get_operation("update_conformity_policy")
    assert upd is not None
    res = await upd.apply(
        db_session, user, UpdateConformityPolicyArgs(policy_id=pid, severity="info", enabled=False)
    )
    assert "severity" in res["updated_fields"]

    ev = get_operation("evaluate_conformity_policy")
    assert ev is not None
    summary = await ev.apply(db_session, user, EvaluateConformityPolicyArgs(policy_id=pid))
    assert {"passed", "failed", "warned", "not_applicable", "total"} <= set(summary)


# ── Webhooks ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_webhook_apply_and_superadmin_gate(db_session: AsyncSession) -> None:
    from app.models.event_subscription import EventSubscription
    from app.services.ai.operations_writes import CreateWebhookArgs

    op = get_operation("create_webhook")
    assert op is not None
    args = CreateWebhookArgs(name="siem", url="https://siem.example/hook")

    # non-superadmin is rejected at preview
    viewer = await _user(db_session, superadmin=False, name="viewer")
    preview = await op.preview(db_session, viewer, args)
    assert preview.ok is False
    assert "superadmin" in preview.detail.lower()

    # superadmin creates + gets a one-time secret
    admin = await _user(db_session)
    preview = await op.preview(db_session, admin, args)
    assert preview.ok, preview.detail
    result = await op.apply(db_session, admin, args)
    assert result["secret_plaintext"]
    row = await db_session.get(EventSubscription, uuid.UUID(result["id"]))
    assert row is not None and row.url == "https://siem.example/hook"


@pytest.mark.asyncio
async def test_create_webhook_rejects_bad_url(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import CreateWebhookArgs

    admin = await _user(db_session)
    op = get_operation("create_webhook")
    assert op is not None
    preview = await op.preview(db_session, admin, CreateWebhookArgs(name="x", url="ftp://nope"))
    assert preview.ok is False


# ── Multicast domain ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multicast_domain_create_update_delete(db_session: AsyncSession) -> None:
    from app.models.multicast import MulticastDomain
    from app.services.ai.operations_writes import (
        CreateMulticastDomainArgs,
        DeleteMulticastDomainArgs,
        UpdateMulticastDomainArgs,
    )

    user = await _user(db_session)
    created = await get_operation("create_multicast_domain").apply(  # type: ignore[union-attr]
        db_session, user, CreateMulticastDomainArgs(name="studio-pim", pim_mode="dense")
    )
    did = uuid.UUID(created["id"])
    assert (await db_session.get(MulticastDomain, did)) is not None

    upd = await get_operation("update_multicast_domain").apply(  # type: ignore[union-attr]
        db_session, user, UpdateMulticastDomainArgs(domain_id=did, description="edge feeds")
    )
    assert "description" in upd["updated_fields"]

    deleted = await get_operation("delete_multicast_domain").apply(  # type: ignore[union-attr]
        db_session, user, DeleteMulticastDomainArgs(domain_id=did)
    )
    assert deleted["deleted"] is True
    assert (await db_session.get(MulticastDomain, did)) is None


@pytest.mark.asyncio
async def test_multicast_domain_sparse_requires_rp(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import CreateMulticastDomainArgs

    user = await _user(db_session)
    op = get_operation("create_multicast_domain")
    assert op is not None
    # sparse with no RP → apply raises ValueError (mapped from the 422)
    with pytest.raises(ValueError):
        await op.apply(
            db_session, user, CreateMulticastDomainArgs(name="needs-rp", pim_mode="sparse")
        )


# ── SNMP / NTP ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_snmp_settings_apply(db_session: AsyncSession) -> None:
    from app.api.v1.settings.router import _get_or_create
    from app.services.ai.operations_writes import UpdateSNMPSettingsArgs

    user = await _user(db_session)
    op = get_operation("update_snmp_settings")
    assert op is not None
    args = UpdateSNMPSettingsArgs(enabled=True, version="v2c", community="s3cret-comm")
    preview = await op.preview(db_session, user, args)
    assert preview.ok, preview.detail
    await op.apply(db_session, user, args)
    settings = await _get_or_create(db_session)
    assert settings.snmp_enabled is True
    assert settings.snmp_community_encrypted is not None  # stored encrypted, not plaintext


@pytest.mark.asyncio
async def test_update_ntp_settings_validation_and_apply(db_session: AsyncSession) -> None:
    from app.api.v1.settings.router import _get_or_create
    from app.services.ai.operations_writes import UpdateNTPSettingsArgs

    user = await _user(db_session)
    op = get_operation("update_ntp_settings")
    assert op is not None
    bad = await op.preview(db_session, user, UpdateNTPSettingsArgs(source_mode="bogus"))
    assert bad.ok is False
    await op.apply(
        db_session, user, UpdateNTPSettingsArgs(source_mode="servers", allow_clients=True)
    )
    settings = await _get_or_create(db_session)
    assert settings.ntp_source_mode == "servers"
    assert settings.ntp_allow_clients is True


@pytest.mark.asyncio
async def test_update_syslog_settings_preview_apply(db_session: AsyncSession) -> None:
    from app.api.v1.settings.router import _get_or_create
    from app.core.crypto import decrypt_str
    from app.services.ai.operations_writes import (
        SyslogTargetArg,
        UpdateSyslogSettingsArgs,
    )

    user = await _user(db_session)
    op = get_operation("update_syslog_settings")
    assert op is not None

    # A TLS target with no CA fails preview.
    bad = await op.preview(
        db_session,
        user,
        UpdateSyslogSettingsArgs(
            enabled=True,
            targets=[SyslogTargetArg(host="x", port=6514, protocol="tls")],
        ),
    )
    assert bad.ok is False

    # Happy path — UDP target + a TLS target carrying a CA PEM.
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    args = UpdateSyslogSettingsArgs(
        enabled=True,
        targets=[
            SyslogTargetArg(host="a.example", port=514, protocol="udp", format="rfc5424"),
            SyslogTargetArg(
                host="b.example", port=6514, protocol="tls", format="json", ca_cert_pem=pem
            ),
        ],
        filter="*.*",
    )
    preview = await op.preview(db_session, user, args)
    assert preview.ok, preview.detail
    await op.apply(db_session, user, args)

    settings = await _get_or_create(db_session)
    assert settings.syslog_enabled is True
    assert len(settings.syslog_targets) == 2
    # The TLS target's CA PEM is stored encrypted, never plaintext.
    tls_target = next(t for t in settings.syslog_targets if t["protocol"] == "tls")
    assert tls_target["ca_cert_pem"] is not None
    assert pem not in tls_target["ca_cert_pem"]
    assert decrypt_str(tls_target["ca_cert_pem"].encode("ascii")) == pem

    # Audit row written with resource_id='syslog'.
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.resource_type == "platform_settings",
                    AuditLog.resource_id == "syslog",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].new_value["via"] == "ai_proposal"


@pytest.mark.asyncio
async def test_update_syslog_settings_requires_superadmin(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import UpdateSyslogSettingsArgs

    user = await _user(db_session, superadmin=False)
    op = get_operation("update_syslog_settings")
    assert op is not None
    block = await op.preview(db_session, user, UpdateSyslogSettingsArgs(enabled=True))
    assert block.ok is False


# ── DNSSEC + import (hermetic rejection paths) ────────────────────────


@pytest.mark.asyncio
async def test_sign_zone_dnssec_preview_rejects_missing_zone(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import SignZoneDNSSECArgs

    user = await _user(db_session)
    op = get_operation("sign_zone_dnssec")
    assert op is not None
    preview = await op.preview(
        db_session, user, SignZoneDNSSECArgs(group_id=uuid.uuid4(), zone_id=uuid.uuid4())
    )
    assert preview.ok is False


@pytest.mark.asyncio
async def test_commit_dns_import_rejects_missing_args(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import CommitDNSImportArgs

    user = await _user(db_session)
    op = get_operation("commit_dns_import")
    assert op is not None
    # powerdns source without api_url/api_key
    preview = await op.preview(
        db_session, user, CommitDNSImportArgs(source="powerdns", target_group_id=uuid.uuid4())
    )
    assert preview.ok is False
    assert "api_url" in preview.detail


@pytest.mark.asyncio
async def test_commit_dhcp_import_rejects_missing_server(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import CommitDHCPImportArgs

    user = await _user(db_session)
    op = get_operation("commit_dhcp_import")
    assert op is not None
    preview = await op.preview(
        db_session,
        user,
        CommitDHCPImportArgs(
            source="windows_dhcp", server_id=uuid.uuid4(), target_group_id=uuid.uuid4()
        ),
    )
    assert preview.ok is False
    assert "not found" in preview.detail.lower()


# ── Review-fix coverage (PR #323): superadmin gates + semantics ───────


@pytest.mark.asyncio
async def test_dnssec_sign_requires_superadmin(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import SignZoneDNSSECArgs

    viewer = await _user(db_session, superadmin=False, name="viewer")
    op = get_operation("sign_zone_dnssec")
    assert op is not None
    preview = await op.preview(
        db_session, viewer, SignZoneDNSSECArgs(group_id=uuid.uuid4(), zone_id=uuid.uuid4())
    )
    assert preview.ok is False
    assert "superadmin" in preview.detail.lower()


@pytest.mark.asyncio
async def test_commit_dns_import_requires_superadmin(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import CommitDNSImportArgs

    viewer = await _user(db_session, superadmin=False, name="viewer")
    op = get_operation("commit_dns_import")
    assert op is not None
    # superadmin gate fires BEFORE the off-prem pull
    preview = await op.preview(
        db_session,
        viewer,
        CommitDNSImportArgs(
            source="powerdns",
            api_url="http://pdns:8081",
            api_key="x",
            target_group_id=uuid.uuid4(),
        ),
    )
    assert preview.ok is False
    assert "superadmin" in preview.detail.lower()


@pytest.mark.asyncio
async def test_update_multicast_domain_to_sparse_requires_rp(db_session: AsyncSession) -> None:
    from app.services.ai.operations_writes import (
        CreateMulticastDomainArgs,
        UpdateMulticastDomainArgs,
    )

    user = await _user(db_session)
    created = await get_operation("create_multicast_domain").apply(  # type: ignore[union-attr]
        db_session, user, CreateMulticastDomainArgs(name="flip-pim", pim_mode="dense")
    )
    did = uuid.UUID(created["id"])
    op = get_operation("update_multicast_domain")
    assert op is not None
    # flipping to sparse with no RP must be rejected at preview + apply
    preview = await op.preview(
        db_session, user, UpdateMulticastDomainArgs(domain_id=did, pim_mode="sparse")
    )
    assert preview.ok is False
    with pytest.raises(ValueError):
        await op.apply(
            db_session, user, UpdateMulticastDomainArgs(domain_id=did, pim_mode="sparse")
        )


@pytest.mark.asyncio
async def test_update_webhook_can_clear_event_types(db_session: AsyncSession) -> None:
    from app.models.event_subscription import EventSubscription
    from app.services.ai.operations_writes import CreateWebhookArgs, UpdateWebhookArgs

    admin = await _user(db_session)
    created = await get_operation("create_webhook").apply(  # type: ignore[union-attr]
        db_session,
        admin,
        CreateWebhookArgs(name="hook", url="https://h.example/x", event_types=["subnet.created"]),
    )
    sid = uuid.UUID(created["id"])
    upd = get_operation("update_webhook")
    assert upd is not None

    # omitting event_types keeps the existing filter
    await upd.apply(db_session, admin, UpdateWebhookArgs(subscription_id=sid, description="kept"))
    row = await db_session.get(EventSubscription, sid)
    assert row is not None and row.event_types == ["subnet.created"]

    # explicitly passing event_types=None clears it back to all-events
    await upd.apply(db_session, admin, UpdateWebhookArgs(subscription_id=sid, event_types=None))
    await db_session.refresh(row)
    assert row.event_types is None
