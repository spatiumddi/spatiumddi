"""BGP prefix-hijack monitoring (issue #527).

Unit tests over the evaluation core (origin-AS mismatch, RPKI
invalid-vs-unknown severity, more-specific detection, latch/auto-resolve)
plus the alert evaluator integration. External HTTP is mocked — no live
RIPEstat / RIS Live network calls.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.alerts import AlertEvent, AlertRule
from app.models.asn import ASN, ASNRpkiRoa
from app.models.auth import User
from app.models.bgp_monitor import BGPHijackDetection, BGPTrackedPrefix
from app.models.feature_module import FeatureModule
from app.models.settings import PlatformSettings
from app.services import alerts as alerts_svc
from app.services import bgp as bgp_pkg
from app.services import feature_modules
from app.services.bgp import hijack_monitor as hm
from app.tasks import bgp_hijack_poll as poll_mod

pytestmark = pytest.mark.asyncio


async def _asn(db: AsyncSession, number: int = 64500) -> ASN:
    row = ASN(number=number, name=f"AS{number}", kind="public", registry="arin")
    db.add(row)
    await db.flush()
    return row


async def _tracked(
    db: AsyncSession,
    asn: ASN,
    prefix: str,
    *,
    allowed: list[int] | None = None,
) -> BGPTrackedPrefix:
    row = BGPTrackedPrefix(
        asn_id=asn.id,
        prefix=prefix,
        expected_origin_asn=int(asn.number),
        source="manual",
        enabled=True,
        allowed_origins=allowed or [],
    )
    db.add(row)
    await db.flush()
    return row


# ── RPKI status derivation ────────────────────────────────────────────


async def test_rpki_status_unknown_when_no_roa(db_session: AsyncSession) -> None:
    status = await hm.derive_rpki_status(db_session, "192.0.2.0/24", 64510)
    assert status == hm.RPKI_UNKNOWN


async def test_rpki_status_invalid_when_roa_authorises_other_origin(
    db_session: AsyncSession,
) -> None:
    owner = await _asn(db_session, 64500)
    db_session.add(
        ASNRpkiRoa(
            asn_id=owner.id,
            prefix="192.0.2.0/24",
            max_length=24,
            trust_anchor="arin",
            state="valid",
        )
    )
    await db_session.flush()
    # A different origin announcing the covered prefix → invalid.
    status = await hm.derive_rpki_status(db_session, "192.0.2.0/24", 64999)
    assert status == hm.RPKI_INVALID


async def test_rpki_status_valid_when_roa_authorises_observed_origin(
    db_session: AsyncSession,
) -> None:
    owner = await _asn(db_session, 64500)
    db_session.add(
        ASNRpkiRoa(
            asn_id=owner.id,
            prefix="192.0.2.0/23",
            max_length=24,
            trust_anchor="arin",
            state="valid",
        )
    )
    await db_session.flush()
    # Same origin, within max_length → valid (legit, not a hijack).
    status = await hm.derive_rpki_status(db_session, "192.0.2.0/24", 64500)
    assert status == hm.RPKI_VALID


async def test_severity_ladder() -> None:
    assert hm.severity_for_rpki(hm.RPKI_INVALID) == "critical"
    assert hm.severity_for_rpki(hm.RPKI_UNKNOWN) == "warning"


# ── exact-prefix hijack evaluation ────────────────────────────────────


async def test_evaluate_opens_detection_on_unexpected_origin(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    asn = await _asn(db_session, 64500)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")

    async def _overview(resource: str) -> dict:
        return {"available": True, "asns": [{"asn": 64999, "holder": "EVIL"}]}

    async def _related(resource: str) -> dict:
        return {"available": True, "prefixes": []}

    monkeypatch.setattr(bgp_pkg, "fetch_prefix_overview", _overview)
    monkeypatch.setattr(bgp_pkg, "fetch_related_prefixes", _related)

    now = datetime.now(UTC)
    summary = await hm.evaluate_tracked_prefix(db_session, tracked, now=now)
    assert summary["opened"] == 1

    det = (await db_session.execute(select(BGPHijackDetection))).scalars().all()
    assert len(det) == 1
    row = det[0]
    assert row.detection_kind == hm.KIND_PREFIX_HIJACK
    assert row.observed_origin_asn == 64999
    assert row.rpki_status == hm.RPKI_UNKNOWN
    assert row.severity == "warning"
    assert row.resolved_at is None


async def test_evaluate_skips_expected_and_allowlisted_origins(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    asn = await _asn(db_session, 64500)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24", allowed=[64501])

    async def _overview(resource: str) -> dict:
        # Both the expected origin and an allowlisted extra origin.
        return {
            "available": True,
            "asns": [{"asn": 64500}, {"asn": 64501}],
        }

    async def _related(resource: str) -> dict:
        return {"available": True, "prefixes": []}

    monkeypatch.setattr(bgp_pkg, "fetch_prefix_overview", _overview)
    monkeypatch.setattr(bgp_pkg, "fetch_related_prefixes", _related)

    summary = await hm.evaluate_tracked_prefix(db_session, tracked, now=datetime.now(UTC))
    assert summary["opened"] == 0
    count = (await db_session.execute(select(BGPHijackDetection))).scalars().all()
    assert count == []


async def test_evaluate_invalid_gets_critical_severity(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    asn = await _asn(db_session, 64500)
    db_session.add(
        ASNRpkiRoa(
            asn_id=asn.id,
            prefix="192.0.2.0/24",
            max_length=24,
            trust_anchor="arin",
            state="valid",
        )
    )
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")
    await db_session.flush()

    async def _overview(resource: str) -> dict:
        return {"available": True, "asns": [{"asn": 64999}]}

    async def _related(resource: str) -> dict:
        return {"available": True, "prefixes": []}

    monkeypatch.setattr(bgp_pkg, "fetch_prefix_overview", _overview)
    monkeypatch.setattr(bgp_pkg, "fetch_related_prefixes", _related)

    await hm.evaluate_tracked_prefix(db_session, tracked, now=datetime.now(UTC))
    row = (await db_session.execute(select(BGPHijackDetection))).scalars().one()
    assert row.rpki_status == hm.RPKI_INVALID
    assert row.severity == "critical"


# ── more-specific (sub-prefix) hijack ─────────────────────────────────


async def test_evaluate_detects_more_specific(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    asn = await _asn(db_session, 64500)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")

    async def _overview(resource: str) -> dict:
        # Exact prefix announced correctly by us.
        return {"available": True, "asns": [{"asn": 64500}]}

    async def _related(resource: str) -> dict:
        return {
            "available": True,
            "prefixes": [
                {
                    "prefix": "192.0.2.128/25",
                    "origin_asn": 64999,
                    "relationship": "More Specific",
                },
                # A more-specific from us — must NOT fire.
                {
                    "prefix": "192.0.2.0/25",
                    "origin_asn": 64500,
                    "relationship": "More Specific",
                },
            ],
        }

    monkeypatch.setattr(bgp_pkg, "fetch_prefix_overview", _overview)
    monkeypatch.setattr(bgp_pkg, "fetch_related_prefixes", _related)

    summary = await hm.evaluate_tracked_prefix(db_session, tracked, now=datetime.now(UTC))
    assert summary["opened"] == 1
    row = (await db_session.execute(select(BGPHijackDetection))).scalars().one()
    assert row.detection_kind == hm.KIND_MORE_SPECIFIC
    assert str(row.observed_prefix) == "192.0.2.128/25"
    assert row.observed_origin_asn == 64999


# ── latch / dedup / auto-resolve ──────────────────────────────────────


async def test_latch_dedup_and_resolve(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    asn = await _asn(db_session, 64500)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")

    async def _overview(resource: str) -> dict:
        return {"available": True, "asns": [{"asn": 64999}]}

    async def _related(resource: str) -> dict:
        return {"available": True, "prefixes": []}

    monkeypatch.setattr(bgp_pkg, "fetch_prefix_overview", _overview)
    monkeypatch.setattr(bgp_pkg, "fetch_related_prefixes", _related)

    t0 = datetime.now(UTC)
    s1 = await hm.evaluate_tracked_prefix(db_session, tracked, now=t0)
    assert s1["opened"] == 1

    # Second pass while still announcing — bumps last_seen_at, no new row.
    t1 = t0 + timedelta(hours=1)
    s2 = await hm.evaluate_tracked_prefix(db_session, tracked, now=t1)
    assert s2["opened"] == 0
    rows = (await db_session.execute(select(BGPHijackDetection))).scalars().all()
    assert len(rows) == 1
    assert rows[0].last_seen_at == t1
    assert rows[0].resolved_at is None

    # Now the announcement delists — no re-observation, then the sweep
    # runs past the delist window.
    later = t1 + hm.DEFAULT_DELIST_WINDOW + timedelta(hours=1)
    resolved = await hm.resolve_stale_detections(db_session, asn_id=asn.id, now=later)
    assert resolved == 1
    rows = (await db_session.execute(select(BGPHijackDetection))).scalars().all()
    assert rows[0].resolved_at == later


# ── alert evaluator integration ───────────────────────────────────────


async def _rule(db: AsyncSession, rule_type: str) -> AlertRule:
    rule = AlertRule(
        name=rule_type,
        rule_type=rule_type,
        severity="warning",
        enabled=True,
        notify_syslog=False,
        notify_webhook=False,
        notify_smtp=False,
    )
    db.add(rule)
    await db.flush()
    return rule


async def test_alert_opens_and_autoresolves(db_session: AsyncSession) -> None:
    asn = await _asn(db_session, 64500)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")
    rule = await _rule(db_session, alerts_svc.RULE_TYPE_BGP_PREFIX_HIJACK)

    now = datetime.now(UTC)
    det = BGPHijackDetection(
        tracked_prefix_id=tracked.id,
        asn_id=asn.id,
        tracked_prefix="192.0.2.0/24",
        observed_prefix="192.0.2.0/24",
        expected_origin_asn=64500,
        observed_origin_asn=64999,
        detection_kind="prefix_hijack",
        rpki_status="invalid",
        severity="critical",
        source="ripestat_poll",
        first_seen_at=now,
        last_seen_at=now,
    )
    db_session.add(det)
    await db_session.commit()

    # First eval opens an AlertEvent with the per-detection severity.
    summary = await alerts_svc.evaluate_all(db_session)
    assert summary["opened"] >= 1
    ev = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)))
        .scalars()
        .all()
    )
    assert len(ev) == 1
    assert ev[0].severity == "critical"
    assert ev[0].subject_type == "bgp_hijack"
    assert ev[0].resolved_at is None

    # Resolve the detection → the matcher stops returning it → the
    # AlertEvent auto-resolves on the next pass.
    det.resolved_at = datetime.now(UTC)
    await db_session.commit()
    await alerts_svc.evaluate_all(db_session)
    ev2 = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)))
        .scalars()
        .one()
    )
    assert ev2.resolved_at is not None


async def test_alert_skips_acknowledged_detection(db_session: AsyncSession) -> None:
    asn = await _asn(db_session, 64500)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")
    rule = await _rule(db_session, alerts_svc.RULE_TYPE_BGP_MORE_SPECIFIC)

    now = datetime.now(UTC)
    det = BGPHijackDetection(
        tracked_prefix_id=tracked.id,
        asn_id=asn.id,
        tracked_prefix="192.0.2.0/24",
        observed_prefix="192.0.2.128/25",
        expected_origin_asn=64500,
        observed_origin_asn=64999,
        detection_kind="more_specific",
        rpki_status="unknown",
        severity="warning",
        source="ripestat_poll",
        first_seen_at=now,
        last_seen_at=now,
        acknowledged=True,
    )
    db_session.add(det)
    await db_session.commit()

    await alerts_svc.evaluate_all(db_session)
    ev = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)))
        .scalars()
        .all()
    )
    assert ev == []  # acknowledged rows never open an alert


# ── finding 1: pruning a tracked prefix must NOT destroy its detection ─


async def test_pruned_tracked_prefix_keeps_open_detection(db_session: AsyncSession) -> None:
    """The FK is ``ON DELETE SET NULL`` (not CASCADE) so pruning a tracked
    prefix — which the poll does the moment a victim prefix drops out of
    RIPEstat / ROA sources, i.e. mid-hijack — leaves every open detection
    intact with a NULL ``tracked_prefix_id`` and keeps it latched."""
    asn = await _asn(db_session, 64500)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")
    # Capture ids while the ORM objects are still live — they're expired
    # after commit and re-reading a PK attribute would trigger a sync
    # lazy-load in the async session.
    asn_id = asn.id

    now = datetime.now(UTC)
    det = BGPHijackDetection(
        tracked_prefix_id=tracked.id,
        asn_id=asn_id,
        tracked_prefix="192.0.2.0/24",
        observed_prefix="192.0.2.0/24",
        expected_origin_asn=64500,
        observed_origin_asn=64999,
        detection_kind="prefix_hijack",
        rpki_status="invalid",
        severity="critical",
        source="ripestat_poll",
        first_seen_at=now,
        last_seen_at=now,
    )
    db_session.add(det)
    await db_session.commit()
    det_id = det.id

    # Prune the tracked prefix — the DB-level ON DELETE SET NULL fires.
    await db_session.delete(tracked)
    await db_session.commit()

    db_session.expire_all()
    tp_id, resolved_at, tracked_prefix, det_asn_id = (
        await db_session.execute(
            select(
                BGPHijackDetection.tracked_prefix_id,
                BGPHijackDetection.resolved_at,
                BGPHijackDetection.tracked_prefix,
                BGPHijackDetection.asn_id,
            ).where(BGPHijackDetection.id == det_id)
        )
    ).one()
    # The detection survives, FK is NULL, still open, other columns intact.
    assert tp_id is None
    assert resolved_at is None
    assert str(tracked_prefix) == "192.0.2.0/24"
    assert det_asn_id == asn_id

    # resolve_stale_detections still keys off asn_id + last_seen_at, so a
    # freshly-seen (NULL-FK) detection is NOT resolved before the window.
    resolved = await hm.resolve_stale_detections(db_session, asn_id=asn_id, now=now)
    assert resolved == 0

    # The alert matcher renders off tracked_prefix / observed_prefix (never
    # the FK) — the NULL-FK detection still opens an alert.
    rule = await _rule(db_session, alerts_svc.RULE_TYPE_BGP_PREFIX_HIJACK)
    await db_session.commit()
    summary = await alerts_svc.evaluate_all(db_session)
    assert summary["opened"] >= 1
    ev = (
        (await db_session.execute(select(AlertEvent).where(AlertEvent.rule_id == rule.id)))
        .scalars()
        .all()
    )
    assert len(ev) == 1
    assert ev[0].resolved_at is None


# ── finding 2: /bgp is gated by the network.asn feature module ─────────


async def _superadmin(db: AsyncSession) -> str:
    u = User(
        username=f"admin-{uuid.uuid4().hex[:6]}",
        email=f"{uuid.uuid4().hex[:6]}@x.com",
        display_name="Admin",
        hashed_password=hash_password("x"),
        is_superadmin=True,
    )
    db.add(u)
    await db.flush()
    return create_access_token(str(u.id))


async def test_bgp_router_gated_by_network_asn_module(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """With ``network.asn`` disabled every /bgp endpoint 404s at the module
    gate — before any RIPEstat call — exactly like the sibling /asns
    surface (non-negotiable #14)."""
    token = await _superadmin(db_session)
    db_session.add(FeatureModule(id="network.asn", enabled=False))
    await db_session.commit()
    feature_modules.invalidate_cache()

    r = await client.get(
        "/api/v1/bgp/asn/64500/announced-prefixes",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


# ── poll task: refresh cadence + outage-safe resolution ───────────────


async def _enable_bgp(db: AsyncSession, *, interval_hours: int = 6) -> None:
    db.add(
        PlatformSettings(
            id=1,
            bgp_monitoring_enabled=True,
            bgp_monitoring_interval_hours=interval_hours,
        )
    )
    await db.flush()


async def test_poll_refresh_gated_by_cadence(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finding 3: the per-ASN prefix refresh (RIPEstat announced-prefixes)
    runs once for a newly-tracked AS, then is skipped on a second poll
    within the configured interval."""
    await _asn(db_session, 64500)
    await _enable_bgp(db_session)
    await db_session.commit()

    async def _announced(number: int) -> dict:
        return {"available": True, "prefixes": [{"prefix": "192.0.2.0/24"}]}

    async def _overview(resource: str) -> dict:
        return {"available": True, "asns": [{"asn": 64500}]}

    async def _related(resource: str) -> dict:
        return {"available": True, "prefixes": []}

    monkeypatch.setattr(bgp_pkg, "fetch_announced_prefixes", _announced)
    monkeypatch.setattr(bgp_pkg, "fetch_prefix_overview", _overview)
    monkeypatch.setattr(bgp_pkg, "fetch_related_prefixes", _related)

    calls = {"n": 0}
    real_refresh = poll_mod.refresh_tracked_prefixes_for_asn

    async def _spy(db: AsyncSession, asn: ASN, *, now: datetime) -> int:
        calls["n"] += 1
        return await real_refresh(db, asn, now=now)

    monkeypatch.setattr(poll_mod, "refresh_tracked_prefixes_for_asn", _spy)

    r1 = await poll_mod._run_poll()
    assert r1["status"] == "ran"
    # Newly-tracked AS with no prefixes yet → refreshed promptly.
    assert calls["n"] == 1
    assert r1["asns_refreshed"] == 1
    assert r1["prefixes_added"] == 1

    # Second poll immediately after: the prefix's next_check_at is now
    # interval_hours in the future, so the AS is NOT due for a refresh.
    r2 = await poll_mod._run_poll()
    assert r2["status"] == "ran"
    assert calls["n"] == 1  # NOT re-called within the interval
    assert r2["asns_refreshed"] == 0


async def test_poll_does_not_resolve_when_unavailable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finding 4: a RIPEstat soft outage must NOT auto-resolve an ongoing
    hijack's open detection, even once it ages past the delist window."""
    asn = await _asn(db_session, 64500)
    await _enable_bgp(db_session)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")
    # Detection last seen well past the delist window (ongoing hijack that
    # the poll would normally have resolved by now).
    old = datetime.now(UTC) - hm.DEFAULT_DELIST_WINDOW - timedelta(hours=6)
    det = BGPHijackDetection(
        tracked_prefix_id=tracked.id,
        asn_id=asn.id,
        tracked_prefix="192.0.2.0/24",
        observed_prefix="192.0.2.0/24",
        expected_origin_asn=64500,
        observed_origin_asn=64999,
        detection_kind="prefix_hijack",
        rpki_status="invalid",
        severity="critical",
        source="ripestat_poll",
        first_seen_at=old,
        last_seen_at=old,
    )
    db_session.add(det)
    tracked.next_check_at = None  # make it due for evaluation this pass
    await db_session.commit()
    det_id = det.id

    # RIPEstat soft outage — everything comes back unavailable.
    async def _unavail(resource: str) -> dict:
        return {"available": False}

    async def _announced(number: int) -> dict:
        return {"available": False}

    monkeypatch.setattr(bgp_pkg, "fetch_prefix_overview", _unavail)
    monkeypatch.setattr(bgp_pkg, "fetch_related_prefixes", _unavail)
    monkeypatch.setattr(bgp_pkg, "fetch_announced_prefixes", _announced)

    res = await poll_mod._run_poll()
    assert res["status"] == "ran"
    assert res["detections_resolved"] == 0

    db_session.expire_all()
    row = (
        await db_session.execute(select(BGPHijackDetection).where(BGPHijackDetection.id == det_id))
    ).scalar_one()
    # Still OPEN despite being past the delist window — data was unavailable.
    assert row.resolved_at is None


async def test_poll_resolves_when_available_and_delisted(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for finding 4: when data IS available and the announcement
    has delisted past the window, the detection resolves as before."""
    asn = await _asn(db_session, 64500)
    await _enable_bgp(db_session)
    tracked = await _tracked(db_session, asn, "192.0.2.0/24")
    old = datetime.now(UTC) - hm.DEFAULT_DELIST_WINDOW - timedelta(hours=6)
    det = BGPHijackDetection(
        tracked_prefix_id=tracked.id,
        asn_id=asn.id,
        tracked_prefix="192.0.2.0/24",
        observed_prefix="192.0.2.0/24",
        expected_origin_asn=64500,
        observed_origin_asn=64999,
        detection_kind="prefix_hijack",
        rpki_status="invalid",
        severity="critical",
        source="ripestat_poll",
        first_seen_at=old,
        last_seen_at=old,
    )
    db_session.add(det)
    tracked.next_check_at = None
    await db_session.commit()
    det_id = det.id

    # Available, but the hijacker origin is gone (only the legit origin).
    async def _overview(resource: str) -> dict:
        return {"available": True, "asns": [{"asn": 64500}]}

    async def _related(resource: str) -> dict:
        return {"available": True, "prefixes": []}

    async def _announced(number: int) -> dict:
        return {"available": True, "prefixes": []}

    monkeypatch.setattr(bgp_pkg, "fetch_prefix_overview", _overview)
    monkeypatch.setattr(bgp_pkg, "fetch_related_prefixes", _related)
    monkeypatch.setattr(bgp_pkg, "fetch_announced_prefixes", _announced)

    res = await poll_mod._run_poll()
    assert res["status"] == "ran"
    assert res["detections_resolved"] == 1

    db_session.expire_all()
    row = (
        await db_session.execute(select(BGPHijackDetection).where(BGPHijackDetection.id == det_id))
    ).scalar_one()
    assert row.resolved_at is not None


# ── RIS Live overlap match determinism (Copilot review) ───────────────


async def test_ris_match_tracked_prefers_most_specific() -> None:
    """When overlapping tracked prefixes both cover an announcement,
    ``_match_tracked`` picks the most-specific (longest-prefixlen) one
    regardless of iteration order; an exact match still wins."""
    import ipaddress
    from types import SimpleNamespace

    from app.services.bgp import ris_live

    t23 = SimpleNamespace(prefix="192.0.2.0/23")
    t24 = SimpleNamespace(prefix="192.0.2.0/24")
    announced = ipaddress.ip_network("192.0.2.0/25")

    for order in ([t23, t24], [t24, t23]):
        tracked, kind = ris_live._match_tracked(announced, {4: order, 6: []})  # type: ignore[arg-type]
        assert tracked is t24  # most-specific covering prefix
        assert kind == ris_live.KIND_MORE_SPECIFIC

    # Exact announcement of the /24 → exact-hijack kind against the /24.
    tracked, kind = ris_live._match_tracked(
        ipaddress.ip_network("192.0.2.0/24"), {4: [t23, t24], 6: []}  # type: ignore[arg-type]
    )
    assert tracked is t24
    assert kind == ris_live.KIND_PREFIX_HIJACK
